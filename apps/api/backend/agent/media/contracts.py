"""Untrusted planner JSON is validated before admission, leases or paid submissions."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing import Annotated
from backend.agent.config import AgentRoute
from .request_context import AgentRequestContext

Tier = Literal["preview", "final"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)


class InputFrom(Contract):
    stepId: str = Field(min_length=1, max_length=32)
    index: int = Field(default=0, ge=0, le=15)


class MediaStep(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    title: str = Field(min_length=1, max_length=80)
    capabilityId: str = Field(min_length=1, max_length=80)
    dependsOn: list[str] = Field(max_length=32)
    targetNodes: list[str] = Field(min_length=1, max_length=16)
    tier: Tier
    prompt: str = Field(min_length=1, max_length=4000)
    intentStepId: str | None = Field(default=None, pattern=r"^s[12]$")
    parallelGroup: str | None = Field(default=None, max_length=64)
    fanOut: int = Field(default=1, ge=1, le=16)
    variants: list[str] = Field(default_factory=list, max_length=16)
    inputFrom: list[InputFrom] = Field(default_factory=list, max_length=16)
    onFailure: Literal["keep_partial", "fail_downstream", "fail_run"] = "fail_downstream"


class MediaPlan(Contract):
    planVersion: Literal["1"] = "1"
    goal: str = Field(min_length=1, max_length=8000)
    goalType: Literal["image", "video"]
    tier: Tier = "preview"
    steps: list[MediaStep] = Field(min_length=1, max_length=32)
    sourceAssetIds: list[str] = Field(default_factory=list, max_length=64)
    maxConcurrency: int = Field(default=4, ge=1, le=16)
    reviewMode: Literal["off", "soft", "hard"] = "off"

    @model_validator(mode="after")
    def bounded_jobs(self):
        if sum(step.fanOut for step in self.steps) > 64:
            raise ValueError("A media plan is limited to 64 child tasks")
        return self


class MultiPlan(MediaPlan):
    """复用原 DAG 存储与队列契约，仅增加语义步骤分组校验。"""

    @model_validator(mode="after")
    def semantic_groups(self):
        if {step.intentStepId for step in self.steps} != {"s1", "s2"}:
            raise ValueError("顺位计划必须恰好包含两个语义步骤")
        return self


class TierSpec(Contract):
    credits: int = Field(ge=0, le=1_000_000)
    width: int = Field(default=512, ge=64, le=2048, multiple_of=8)
    height: int = Field(default=512, ge=64, le=2048, multiple_of=8)
    durationSec: float = Field(default=4, ge=1, le=12)
    fps: int = Field(default=16, ge=4, le=30)
    denoise: float = Field(default=0.7, ge=0.05, le=1)


class Binding(Contract):
    version: str = Field(min_length=1, max_length=128)
    engine: Literal["provider", "frames"] = "provider"
    providerSnapshot: dict = Field(default_factory=dict)
    maxConcurrency: int = Field(default=4, ge=1, le=16)
    chargePolicy: Literal["on_success", "on_submission"] = "on_submission"
    maxAttempts: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def validate_snapshot(self):
        from backend.config import Config
        raw = self.providerSnapshot
        if any(any(word in key.lower() for word in ("key", "secret", "password")) for key in raw):
            raise ValueError("Bindings contain credential references, never credentials")
        if self.engine == "provider":
            config = Config.model_validate(raw)
            if not raw.get("configVersion"):
                raise ValueError("Provider binding requires configVersion")
            if config.provider == "comfyui" and not raw.get("comfyUrl"):
                raise ValueError("ComfyUI binding requires an explicit endpoint")
            if config.provider == "cloud" and not (config.cloudBaseUrl and raw.get("credentialRef")):
                raise ValueError("Cloud binding requires endpoint and credentialRef")
        elif raw:
            raise ValueError("Local frame composition must not carry a provider snapshot")
        return self


class Capability(Contract):
    id: Literal["text_to_image", "image_edit", "text_to_video", "image_to_video", "frames_to_video"]
    enabled: bool = False
    outputType: Literal["image", "video"]
    moderation: Literal["off", "required"] = "off"
    maxRefImages: int = Field(default=0, ge=0, le=16)
    tiers: dict[Tier, TierSpec]
    binding: Binding | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        if self.outputType != ("image" if self.id in {"text_to_image", "image_edit"} else "video"):
            raise ValueError("Capability output type does not match its ID")
        if self.binding:
            expected_engine = "frames" if self.id == "frames_to_video" else "provider"
            if self.binding.engine != expected_engine:
                raise ValueError("Capability must bind to its implemented execution engine")
            if expected_engine == "provider":
                snapshot = self.binding.providerSnapshot
                model = "cloudModel" if self.outputType == "image" else "videoModel"
                if not snapshot.get(model):
                    raise ValueError("Binding must pin a model")
                if self.id == "image_to_video" and snapshot.get("provider") == "cloud" and not snapshot.get("videoModel", "").startswith("wan2.7-i2v"):
                    raise ValueError("Cloud I2V requires the implemented wan2.7 first-frame adapter")
                expected_refs = 1 if self.id in {"image_edit", "image_to_video"} else 0
                if self.maxRefImages != expected_refs:
                    raise ValueError("Reference limits must match the implemented single-reference adapter")
                if snapshot.get("provider") == "cloud":
                    from backend.config import Config
                    from backend.models import GenParams, InitImage
                    from backend.providers.cloud import CloudProvider
                    provider = CloudProvider(Config.model_validate(snapshot), None)
                    for spec in self.tiers.values():
                        params = GenParams(prompt="binding validation", model=snapshot[model], kind=self.outputType,
                            width=spec.width, height=spec.height, durationSec=spec.durationSec, fps=spec.fps)
                        _, request = (provider.video_request(params, InitImage(b"", "png") if self.id == "image_to_video" else None) if self.outputType == "video" else
                            provider.request(params, 0, InitImage(b"", "png") if self.id == "image_edit" else None, None))
                        if request["model"] != snapshot[model]:
                            raise ValueError("Binding must pin the actual billed model; implicit model replacement is forbidden")
        return self


class ReviewSettings(Contract):
    maxTasks: int = Field(default=32, ge=1, le=32)
    maxExpandedTasks: int = Field(default=64, ge=1, le=64)
    maxCredits: int = Field(default=1_000_000, ge=0)


class CatalogConfig(Contract):
    admission: ReviewSettings = Field(default_factory=ReviewSettings)
    version: str = Field(min_length=1, max_length=128)
    environment: Literal["production", "development"] = "production"
    enabled: bool = False
    autoStartPreview: bool = True
    reviewMode: Literal["off", "soft", "hard"] = "off"
    reviewer: AgentRoute | None = None
    reviewThreshold: float = Field(default=0.7, ge=0, le=1)
    confirmCreditsAbove: int = Field(default=50, ge=0)
    maxConcurrency: int = Field(default=4, ge=1, le=16)
    cancelGraceSeconds: int = Field(default=120, ge=1, le=600)
    capabilities: list[Capability] = Field(default_factory=list, max_length=16)


class HostTurn(Contract):
    clientRequestId: str = Field(min_length=1, max_length=128)
    documentId: str
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    baseRevision: int = Field(ge=1)
    selectedObjectIds: list[str] = Field(default_factory=list, max_length=200)
    referenceAssetIds: list[str] = Field(default_factory=list, max_length=16)
    context: AgentRequestContext | None = None
    supersedeRunId: str | None = None
    clarifyRunId: str | None = None
    clarifyOptionId: str | None = Field(default=None, max_length=64)


class HostConfirm(Contract):
    clientRequestId: str = Field(min_length=1, max_length=128)
    approved: bool


class HostFinal(Contract):
    clientRequestId: str = Field(min_length=1, max_length=128)
    baseRevision: int = Field(ge=1)


class ReviewVerdict(Contract):
    passed: bool
    score: float = Field(ge=0, le=1)
    issues: list[str] = Field(default_factory=list, max_length=8)
    contentBlocked: bool = False


class HostRetry(Contract):
    clientRequestId: str = Field(min_length=1, max_length=128)
    stepId: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    index: int = Field(ge=0, le=15)


class CanvasActionResult(Contract):
    resultObjectId: str = Field(min_length=1, max_length=200)
    baseRevision: int = Field(ge=1)
