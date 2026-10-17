"""TECH AgentRequest v1 adapted to the canonical AgentRun admission transaction.

Only persisted document metadata can resolve targets. Client labels, history,
permissions and model choices are never authoritative. No second Host store.
"""
from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator



class ContextValue(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class Mention(ContextValue):
    id: str = Field(min_length=1, max_length=128)


class MessagePart(ContextValue):
    type: Literal["text", "mention"]
    text: str | None = Field(default=None, min_length=1, max_length=8000)
    id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def content(self):
        if (self.type == "text" and self.text is None) or (self.type == "mention" and self.id is None):
            raise ValueError("Message part requires text or reference ID")
        return self


class Selection(ContextValue):
    ids: list[str] = Field(max_length=200)
    count: int = Field(ge=0)
    truncated: bool
    select_all: bool = False
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    selection_token: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.ids)) != len(self.ids) or any(not ident or len(ident) > 128 for ident in self.ids):
            raise ValueError("SELECTION_MISMATCH")
        if not self.truncated and not self.select_all and self.count != len(self.ids):
            raise ValueError("SELECTION_MISMATCH")
        return self


class Viewport(ContextValue):
    bbox: list[float] = Field(min_length=4, max_length=4)
    zoom: float = Field(gt=0, le=64)


class Attachment(ContextValue):
    type: Literal["image", "file"]
    ref: str = Field(min_length=1, max_length=512)


class AgentRequestContext(ContextValue):
    schema_version: Literal[1]
    request_id: str
    conversation_id: str
    doc_id: str
    doc_version: int = Field(ge=0)
    client_ts: int = Field(gt=0)
    client_id: str = Field(min_length=1, max_length=128)
    tab_id: str = Field(min_length=1, max_length=128)
    message: list[MessagePart] | None = Field(default=None, max_length=200)
    message_text: str | None = Field(default=None, max_length=8000)
    mentions: list[Mention] = Field(default_factory=list, max_length=20)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)
    selection: Selection
    viewport: Viewport
    focus_frame_id: str | None = Field(default=None, min_length=1, max_length=128)
    active_page_id: str | None = Field(default=None, min_length=1, max_length=128)
    current_tool: str | None = Field(default=None, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def no_client_authority(cls, value):
        if isinstance(value, dict) and any(key in value for key in ("user_id", "role", "can_edit", "recent_ops", "history", "canvasContext")):
            raise ValueError("Client identity and history are forbidden")
        return value

    @field_validator("request_id", "conversation_id", "doc_id")
    @classmethod
    def uuid_id(cls, value):
        uuid.UUID(value)
        return value

    @model_validator(mode="after")
    def validate_message(self):
        text = ''.join(part.text or '' for part in self.message or [] if part.type == "text")
        if not self.message and not (self.message_text or '').strip():
            raise ValueError("Message is required")
        if len(text) + len(self.message_text or '') > 8000:
            raise ValueError("PAYLOAD_TOO_LARGE")
        refs = {part.id for part in self.message or [] if part.type == "mention"}
        if any(item.id not in refs for item in self.mentions):
            raise ValueError("Mention missing from message")
        return self

    def request_text(self):
        if self.message is not None:
            from html import escape
            return ''.join(part.text if part.type == "text" else f'<ref id="{escape(part.id, quote=True)}"/>' for part in self.message)
        return self.message_text or ''


def resolve_context(context: AgentRequestContext, doc_data: dict, revision: int, message: str, *, layered=False):
    from .catalog import HostError
    if context.doc_version != revision:
        raise HostError("STALE_VERSION", "画布版本已变化，请保存后重新发送")
    if context.selection.truncated or context.selection.select_all or context.selection.selection_token:
        raise HostError("SELECTION_TOKEN_REQUIRED", "当前入口不支持截断或整页选区", status=400)
    if context.attachments:
        raise HostError("ATTACHMENT_REJECTED", "请先将附件导入画布", status=400)
    if context.active_page_id:
        raise HostError("UNSUPPORTED_PAGE", "当前入口不支持多页上下文", status=400)
    objects = doc_data.get("objects", {})
    selection, mentions = context.selection.ids, [item.id for item in context.mentions]
    if any(ident not in objects for ident in selection):
        raise HostError("STALE_REF", "选中的节点已不存在", status=404)
    if any(ident not in objects for ident in mentions + ([context.focus_frame_id] if context.focus_frame_id else [])):
        raise HostError("MENTION_NOT_FOUND", "引用的节点已不存在", status=404)
    if layered:
        # @ 优先；只重读持久化 ID，结构门负责后续排除无效图片。
        primary = mentions or selection
        source = "explicit_mentions" if mentions else "current_selection" if selection else "none"
        primary = [node for node in primary if objects[node].get("kind") == "image" and objects[node].get("assetId")]
        return primary, {"schema_version": 1, "request_id": context.request_id,
            "doc_id": context.doc_id, "doc_version": revision, "client_id": context.client_id,
            "tab_id": context.tab_id, "selection_ids": selection, "mention_ids": mentions,
            "targets_primary": primary, "targets_source": source,
            "viewport": context.viewport.model_dump(mode="json")}
    video_text = re.sub(r"(?:不要|不需要|无需|不生成)\s*(?:视频|动画|短片)", "", message)
    video = bool(re.search(r"视频|动画|动起来|\bvideo\b|\banimate\b", video_text, re.I))
    from .canvas_actions import is_cutout_request
    edit = not video and bool(is_cutout_request(message) or re.search(r"改成|改为|修改|编辑|换.*风格|\bedit\b", message, re.I) or
        (re.search(r"水彩|风格", message) and not re.search(r"画|绘制|生成|创建|generate|draw|create", message, re.I)))
    transform = video and bool(re.search(r"改成|改为|转换|转为|转成|动起来|animate", message, re.I))
    selected = bool(re.search(r"选中的?|这张|这几个|selected", message, re.I))
    pronoun = bool(re.search(r"改它|把它|它们|\bit\b|\bthem\b", message, re.I))
    pure = bool(re.search(r"画|生成|创建|绘制|generate|draw|create", message, re.I)) and not edit and not transform
    primary, source = [], "none"
    if mentions and (pronoun or not selected):
        primary, source = mentions, "explicit_mentions"
    elif selected and selection:
        primary, source = selection, "current_selection"
    elif pure and not mentions:
        source = "no-ref"
    elif (edit or transform) and selection:
        primary, source = selection, "current_selection"
    elif mentions:
        primary, source = mentions, "explicit_mentions"
    if (edit or transform) and not primary:
        raise HostError("REFERENCE_REQUIRED", "请选中一张可用图片作为参考", status=422)
    if (edit or transform) and any(objects[node].get("kind") != "image" or not objects[node].get("assetId") for node in primary):
        raise HostError("REFERENCE_REQUIRED", "此操作需要已导入的图片参考，不能使用视频替代", status=422)
    # The admission fingerprint includes the full validated request, but only ID
    # metadata is retained in the context snapshot, never text, labels or URLs.
    snapshot = {"schema_version": 1, "request_id": context.request_id,
        "doc_id": context.doc_id, "doc_version": revision,
        "client_id": context.client_id, "tab_id": context.tab_id,
        "selection_ids": selection, "mention_ids": mentions,
        "targets_primary": primary, "targets_source": source,
        "viewport": context.viewport.model_dump(mode="json")}
    return primary, snapshot
