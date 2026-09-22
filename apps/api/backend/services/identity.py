"""OIDC establishes identity; PostgreSQL membership authorizes every resource request."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select

from backend.infrastructure.orm import BrowserSession, WorkspaceMember

SESSION_COOKIE = "__Host-aivideo-session"
CSRF_COOKIE = "__Host-aivideo-csrf"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def principal_id(issuer: str, subject: str) -> str:
    return digest(issuer + "\0" + subject)


@dataclass(frozen=True)
class Identity:
    principal: str
    workspace_id: uuid.UUID
    role: str


async def issue_session(factory, principal: str, lifetime_s: int = 28800):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    async with factory.begin() as session:
        session.add(BrowserSession(token_hash=digest(token), principal_id=principal,
            csrf_hash=digest(csrf), expires_at=datetime.now(timezone.utc) + timedelta(seconds=lifetime_s)))
    return token, csrf


async def authenticated(request: Request) -> Identity:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(401, "请先登录")
    async with request.app.state.factory() as session:
        browser = await session.get(BrowserSession, digest(token))
        if browser is None or browser.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(401, "登录已过期")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != request.app.state.settings.public_origin:
                raise HTTPException(403, "请求来源不可信")
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not hmac.compare_digest(digest(supplied), browser.csrf_hash):
                raise HTTPException(403, "CSRF 校验失败")
        query = select(WorkspaceMember).where(WorkspaceMember.principal_id == browser.principal_id)
        requested = request.headers.get("x-workspace-id") or request.query_params.get("workspaceId")
        if requested:
            try:
                workspace = uuid.UUID(requested)
            except ValueError:
                raise HTTPException(400, "工作区标识无效") from None
            query = query.where(WorkspaceMember.workspace_id == workspace)
        member = (await session.execute(query.order_by(WorkspaceMember.workspace_id).limit(1))).scalar_one_or_none()
        if member is None:
            raise HTTPException(403, "无工作区访问权限")
        return Identity(browser.principal_id, member.workspace_id, member.role)
