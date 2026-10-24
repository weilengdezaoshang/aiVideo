"""Session resolution with lock order Session before Run and without canvas-before-session locks."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from backend.core.orm import CoreSession
from backend.infrastructure.orm import Document


async def resolve_session(tx, *, workspace_id, user_id, document_id, language="zh-CN"):
    session = await tx.scalar(select(CoreSession).where(
        CoreSession.workspace_id == workspace_id, CoreSession.canvas_id == document_id,
        CoreSession.user_id == user_id, CoreSession.closed_at.is_(None)).limit(1).with_for_update())
    if session is not None:
        doc = await tx.get(Document, document_id)
        if not doc or doc.workspace_id != workspace_id:
            raise LookupError("画布不存在")
        return session, doc
    doc = await tx.get(Document, document_id)
    if not doc or doc.workspace_id != workspace_id:
        raise LookupError("画布不存在")
    bind = getattr(tx, "bind", None) or (tx.get_bind() if hasattr(tx, "get_bind") else None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "") or "postgresql"
    if dialect == "postgresql":
        await tx.execute(pg_insert(CoreSession).values(
            id=uuid.uuid4(), workspace_id=workspace_id, canvas_id=doc.id, source_canvas_id=doc.id,
            user_id=user_id, language=language
        ).on_conflict_do_nothing(index_elements=["workspace_id", "canvas_id", "user_id"],
            index_where=text("closed_at IS NULL AND canvas_id IS NOT NULL")))
    else:
        session = CoreSession(workspace_id=workspace_id, canvas_id=doc.id, source_canvas_id=doc.id,
            user_id=user_id, language=language)
        try:
            async with tx.begin_nested():
                tx.add(session)
                await tx.flush()
        except IntegrityError:
            session = None
    session = await tx.scalar(select(CoreSession).where(
        CoreSession.workspace_id == workspace_id, CoreSession.canvas_id == document_id,
        CoreSession.user_id == user_id, CoreSession.closed_at.is_(None)).limit(1).with_for_update())
    if session is None:
        raise LookupError("session_missing")
    return session, doc
