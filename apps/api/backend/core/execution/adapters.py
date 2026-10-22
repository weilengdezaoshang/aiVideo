"""Existing staged provider adapter. Deployment routing and credentials stay pinned."""

import httpx

from backend.config import Config
from backend.models import GenParams, InitImage
from backend.providers.staged import StagedProvider
from backend.providers.download import download_artifact
from backend.providers.policy import ProviderFailure
from backend.core.tools.errors import normalize_provider_failure, normalize_error
from .contracts import ProviderResult


class StagedAdapter:
    def __init__(self, tool, client, media_root, credentials, allowed_origins, artifact_client=None):
        binding = tool.execution.binding["binding"]
        raw = dict(binding["providerSnapshot"])
        secret = credentials.get(raw.get("credentialRef"), {})
        raw.update({key: secret[key] for key in ("imageApiKey", "videoApiKey") if key in secret})
        self.artifact_client = artifact_client or client
        self.config, self.client, self.root, self.origins = Config.model_validate(raw), client, media_root, allowed_origins
        self.provider = StagedProvider(self.config, client)
        self.idempotent = tool.execution.provider_idempotent
        self.query_registration = False  # Current real providers do not implement this protocol.
        self.cancellable = tool.execution.cancellable
        self.kind = next(p.media_type for p in tool.outputs if p.media_type in {"image", "video"})
        self.spec = tool.execution.binding["spec"]
        self.failure_rules = tool.failures

    def arguments(self, params):
        p = GenParams(prompt=params["prompt"], kind=self.kind,
            model=self.config.videoModel if self.kind == "video" else self.config.cloudModel,
            width=params["width"], height=params["height"], durationSec=params.get("duration_seconds", self.spec["durationSec"]),
            fps=params.get("fps", self.spec["fps"]), denoise=self.spec["denoise"])
        if params.get("count", 1) != 1:
            raise ValueError("batch proposals must be expanded and audited into individual tasks before execution")
        def media(value):
            if not value:
                return None
            # Storage identities are host-bound by the worker, never taken from model paths.
            path = (self.root / value["storage_key"]).resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError("reference path outside MediaHost")
            import hashlib
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != value["sha256"]:
                raise ValueError("reference hash changed")
            return InitImage(data, value["ext"])
        image, mask = media(params.get("image")), media(params.get("mask"))
        return p, image, mask

    async def prepare(self, params):
        p, image, mask = self.arguments(params)
        try:
            return (await self.provider.prepare(p, 0, image, mask)).payload
        except httpx.HTTPError as exc:
            raise OSError("provider preparation failed before submission") from exc

    async def submit(self, registration_id, params, idempotency_key):
        p, image, mask = self.arguments(params)
        try:
            result = await self.provider.submit(p, 0, params["_prepared"], registration_id, image, mask)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TimeoutError("submission outcome unknown") from exc
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
                code = body.get("code") or body.get("error", {}).get("code")
            except (ValueError, AttributeError, TypeError):
                code = None
            failure = normalize_error(self.config.cloudVendor, code, status=exc.response.status_code, operation="submit", rules=self.failure_rules)
            if failure.submission_uncertain and failure.kind not in {"blocked_by_model", "param_invalid", "quota"}:
                raise TimeoutError("submission outcome unknown") from None
            return ProviderResult("failed", None, submission_accepted=False, failure=failure.kind)
        except ValueError:
            # A completed but malformed response is not a safe reason to generate again.
            return ProviderResult("failed", None, failure="unknown")
        except ProviderFailure as exc:
            normalized = normalize_provider_failure(exc)
            if normalized.submission_uncertain:
                raise TimeoutError("submission unknown") from None
            return ProviderResult("failed", None, submission_accepted=False, failure=normalized.kind)
        return self.convert(result, None)

    def convert(self, result, ident):
        if result.phase == "failed":
            code = result.payload.get("code")
            failure = normalize_error(self.config.cloudVendor if self.config.provider == "cloud" else self.config.provider, code, rules=self.failure_rules)
            return ProviderResult("failed", ident, failure=failure.kind)
        if result.generated:
            return ProviderResult("succeeded", result.external_id or ident, data=result.generated.data,
                ext=result.generated.ext, actual_credits=self.spec["credits"])
        if result.phase == "download":
            return ProviderResult("succeeded", result.external_id or ident, locator=result.payload,
                actual_credits=self.spec["credits"])
        return ProviderResult("pending", result.external_id or ident)

    async def query(self, task_id, registration_id):
        if not task_id:
            return None
        try:
            return self.convert(await self.provider.poll(task_id, self.kind), task_id)
        except (httpx.HTTPError, ProviderFailure) as exc:
            raise OSError("provider query temporarily unavailable") from exc

    async def cancel(self, task_id):
        await self.provider.cancel(task_id, self.kind)
        # Cancel accepted is not terminal proof. Poll confirms stopped vs already completed.
        result = await self.provider.poll(task_id, self.kind)
        if result.payload.get("provider_status") == "CANCELED":
            return ProviderResult("cancelled", task_id)
        return self.convert(result, task_id)

    async def download(self, locator, kind):
        internal = self.config.provider == "comfyui"
        origins = self.origins
        if internal:
            import httpx
            url = httpx.URL(self.config.comfyUrl)
            origins = {str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/")}
        try:
            artifact = await download_artifact(self.client if internal else self.artifact_client, locator, kind, origins, internal=internal)
        except httpx.HTTPError as exc:
            raise OSError("artifact transfer temporarily unavailable") from exc
        return artifact.data, artifact.ext
