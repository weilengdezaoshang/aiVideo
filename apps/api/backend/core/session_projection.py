"""Project committed Run events into a durable Session journal without reversing locks.

The projector locks only a Session, never a Run. Workers remain independent; a
crash rolls back journal, artifact index, notification and wakeup together.
"""

from sqlalchemy import exists, or_, select

from backend.core.orm import (CoreAssetVersion, CoreEvent, CoreRun, CoreSession,
    CoreSessionArtifact, CoreSessionDecision, CoreSessionEvent, CoreSessionMessage)
from backend.core.execution.orm import CanvasWrite
from backend.infrastructure.orm import Outbox
from backend.core.session_summary import SummaryInput, compact
from backend.core.version_switch_orm import VersionSwitchCommand


async def project_session_once(factory, *, limit: int) -> bool:
    from backend.core.sessions.store import (append_artifact, next_artifact_ordinal,
        notify, persist_summary)
    missing = (select(CoreEvent.id).join(CoreRun, CoreRun.id == CoreEvent.run_id)
        .where(CoreRun.session_id == CoreSession.id,
            ~exists(select(CoreSessionEvent.id).where(CoreSessionEvent.event_id == CoreEvent.id)))
        .correlate(CoreSession))
    switched = select(VersionSwitchCommand.id).where(
        VersionSwitchCommand.workspace_id == CoreSession.workspace_id,
        VersionSwitchCommand.user_id == CoreSession.user_id,
        VersionSwitchCommand.document_id == CoreSession.canvas_id,
        VersionSwitchCommand.status.in_(("applied", "conflict", "rejected")),
        ~exists(select(CoreSessionEvent.id).where(CoreSessionEvent.event_id == VersionSwitchCommand.id))).correlate(CoreSession)
    async with factory.begin() as tx:
        session = await tx.scalar(select(CoreSession).where(or_(exists(missing), exists(switched)))
            .order_by(CoreSession.id).limit(1).with_for_update(skip_locked=True))
        if session is None:
            return False
        events = list(await tx.scalars(select(CoreEvent)
            .join(CoreRun, CoreRun.id == CoreEvent.run_id)
            .where(CoreRun.session_id == session.id,
                ~exists(select(CoreSessionEvent.id).where(CoreSessionEvent.event_id == CoreEvent.id)))
            .order_by(CoreEvent.created_at, CoreEvent.run_sequence, CoreEvent.id).limit(limit)))
        for event in events:
            session.event_cursor += 1
            tx.add(CoreSessionEvent(session_id=session.id, sequence=session.event_cursor,
                event_id=event.id, causation_id=event.causation_id, event_type=event.event_type,
                run_id=event.run_id, task_id=event.task_id, payload={"event_id": str(event.id)}))
            if event.task_id and event.to_state in {"succeeded", "cancelled"}:
                old = await tx.scalar(select(CoreSessionArtifact.id).where(
                    CoreSessionArtifact.task_id == event.task_id))
                version = await tx.scalar(select(CoreAssetVersion).where(
                    CoreAssetVersion.task_id == event.task_id).order_by(CoreAssetVersion.version.desc()).limit(1))
                if old is None and version is not None:
                    write = await tx.get(CanvasWrite, event.task_id)
                    await append_artifact(tx, session,
                        ordinal=await next_artifact_ordinal(tx, session.id),
                        task_id=event.task_id, run_id=event.run_id, completed_at=event.created_at,
                        core_asset_id=version.asset_id, core_asset_version_id=version.id,
                        stored_asset_id=version.stored_asset_id,
                        canvas_node_id=write.node_id if write else None,
                        position=write.rect if write else None)
            if event.aggregate_type == "run" and event.task_id is None and event.to_state in {"completed", "failed", "cancelled"}:
                from backend.core.sessions.contracts import SessionSettings
                run = await tx.get(CoreRun, event.run_id)
                if run and run.parent_run_id is None and run.status in {"completed", "failed", "cancelled"} and session.active_run_id == run.id:
                    session.active_run_id = None
                duration = (event.created_at - (run.started_at or run.created_at)).total_seconds() if run else 0
                if run and run.parent_run_id is None and duration >= SessionSettings().slow_task_notify_seconds:
                    await notify(tx, session, user_id=session.user_id, event_id=event.id,
                        kind="run_completed", body={"run_id": str(event.run_id), "status": event.to_state})
        commands = list(await tx.scalars(select(VersionSwitchCommand).where(
            VersionSwitchCommand.workspace_id == session.workspace_id,
            VersionSwitchCommand.user_id == session.user_id,
            VersionSwitchCommand.document_id == session.canvas_id,
            VersionSwitchCommand.status.in_(("applied", "conflict", "rejected")),
            ~exists(select(CoreSessionEvent.id).where(CoreSessionEvent.event_id == VersionSwitchCommand.id)))
            .order_by(VersionSwitchCommand.created_at, VersionSwitchCommand.id).limit(limit)))
        for command in commands:
            session.event_cursor += 1
            tx.add(CoreSessionEvent(session_id=session.id, sequence=session.event_cursor,
                event_id=command.id, event_type="asset_changed", version_id=command.core_asset_version_id,
                payload={"command_id": str(command.id)}))
        from backend.core.supervisor.contracts import Settings
        await tx.flush()
        recent = list(await tx.scalars(select(CoreSessionArtifact).where(
            CoreSessionArtifact.session_id == session.id).order_by(
                CoreSessionArtifact.ordinal.desc()).limit(Settings().recent_output_limit)))
        session.recent_assets = [{"asset_id": str(row.stored_asset_id),
            "core_asset_id": str(row.core_asset_id), "version_id": str(row.core_asset_version_id),
            "run_id": str(row.run_id), "task_id": str(row.task_id), "ordinal": row.ordinal}
            for row in recent if row.stored_asset_id is not None]
        decisions = list(await tx.scalars(select(CoreSessionDecision).where(
            CoreSessionDecision.session_id == session.id, CoreSessionDecision.superseded_by_id.is_(None))))
        messages = list(await tx.scalars(select(CoreSessionMessage).where(
            CoreSessionMessage.session_id == session.id,
            CoreSessionMessage.sequence > session.summary_cursor).order_by(CoreSessionMessage.sequence)))
        summary = compact([SummaryInput(row.sequence, row.role, str((row.content or {}).get("text") or ""))
            for row in messages], previous_cursor=session.summary_cursor, previous_summary=session.summary,
            confirmed_decisions={str(row.id): str(row.value) for row in decisions})
        if summary is not None:
            await persist_summary(tx, session, from_sequence=session.summary_cursor + 1,
                to_sequence=summary.cursor, body=summary.text, decision_ids=list(summary.decision_ids),
                input_hash=summary.input_hash, writer="extract")
        tx.add(Outbox(aggregate_type="session", aggregate_id=str(session.id), event_type="core.changed",
            payload={"sessionId": str(session.id)}))
        await tx.flush()
        return True
