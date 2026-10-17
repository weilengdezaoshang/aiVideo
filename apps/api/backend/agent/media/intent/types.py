"""分层意图契约；语义步骤与底层生成 DAG 分开建模。"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

L2 = Literal['text_to_image', 'image_edit', 'text_to_video', 'image_to_multiframe', 'image_to_video', 'other', 'unsupported']
Certainty = Literal['strong', 'weak', 'none']


class IntentValue(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Feasibility(IntentValue):
    possibleL2: list[L2]
    has_image: bool
    capabilityOk: dict[str, bool] = Field(default_factory=dict)


class Step(IntentValue):
    id: str = Field(pattern=r'^s[12]$')
    l2: L2
    path: str | None = None
    slots: dict = Field(default_factory=dict)
    needs_image: bool = False
    input_from: Literal['selection', 'upload', 'step:s1'] = 'selection'
    depends_on: list[str] = Field(default_factory=list)


class ClarifyOption(IntentValue):
    id: str
    label: str
    l2: L2 | None = None
    steps: list[Step] | None = None


class ClarifyTicket(IntentValue):
    id: str
    request_id: str
    snapshot: dict
    references: list[str]
    message: str
    expires_at: float
    question: str
    options: list[ClarifyOption]


class IntentDecision(IntentValue):
    certainty: Certainty
    l2: L2 | None = None
    path: str | None = None
    slots: dict = Field(default_factory=dict)
    clarify: ClarifyTicket | None = None
    steps: list[Step] | None = Field(default=None, min_length=1, max_length=2)
    trace: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def bounded_sequence(self):
        if self.steps and len(self.steps) > 1:
            first, second = self.steps
            if (first.id != 's1' or second.id != 's2' or
                    first.l2 not in {'image_edit', 'text_to_image'} or
                    second.l2 != 'image_to_multiframe' or first.depends_on or
                    first.input_from == 'step:s1' or second.depends_on != ['s1'] or
                    second.input_from != 'step:s1'):
                raise ValueError('仅支持先生图或改图，再用上一步产物生成视频')
        return self
