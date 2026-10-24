"""Transaction-scoped contract operations. Never commit the caller's transaction."""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import AssetContent, CoreSettings, RunInput, TaskInput
from .orm import CoreAsset, CoreAssetLineage, CoreAssetVersion, CoreEvent, CoreRun, CoreTask
from .event_context import current_event_id
from .states import EntityNotFound, InvalidTransition, RunState, TaskState, VersionConflict


@dataclass(frozen=True)
class TransitionResult:
    event_id: UUID
    run_sequence: int
    version: int
    from_state: str
    to_state: str


def contract_error(exc: DBAPIError):
    code = getattr(exc.orig, "sqlstate", None)
    if code == "P4001":
        return VersionConflict("version_conflict: read the current record before retrying")
    if code == "P4004":
        return EntityNotFound("entity_not_found")
    if code == "P4002":
        return InvalidTransition("invalid_transition: " + str(exc.orig).splitlines()[0])
    return None


async def transition(tx: AsyncSession, entity_type: str, entity_id: UUID, expected_version: int,
                     target_state: str, *, reason: str | None = None, causation_id: UUID | None = None,
                     correlation_id: UUID | None = None) -> TransitionResult:
    """The single state transition entrypoint; the database enforces the same path.

    The savepoint keeps a rejected transition from poisoning an enclosing transaction.
    State and its outbox event remain uncommitted until that transaction commits.
    No conflict is automatically retried.
    """
    allowed = set(RunState) if entity_type == "run" else set(TaskState) if entity_type == "task" else set()
    if target_state not in allowed:
        raise InvalidTransition("unknown entity or target state")
    if type(expected_version) is not int or expected_version < 0:
        raise VersionConflict("expected_version must be a nonnegative integer")
    try:
        async with tx.begin_nested():
            event_id = await tx.scalar(text("""SELECT core_transition(:entity, :ident, :expected,
                :target, :reason, :cause, :correlation)"""), {
                    "entity": entity_type, "ident": entity_id, "expected": expected_version,
                    "target": target_state, "reason": reason, "cause": causation_id or current_event_id.get(),
                    "correlation": correlation_id})
            event = await tx.get(CoreEvent, event_id)
            model = CoreRun if entity_type == "run" else CoreTask
            await tx.get(model, entity_id, populate_existing=True)
            if entity_type == "task":
                await tx.get(CoreRun, event.run_id, populate_existing=True)
            return TransitionResult(event.id, event.run_sequence, event.aggregate_version,
                                    event.from_state, event.to_state)
    except DBAPIError as exc:
        mapped = contract_error(exc)
        if mapped:
            raise mapped from exc
        raise


async def create_run(tx: AsyncSession, value: RunInput, settings: CoreSettings | None = None) -> CoreRun:
    settings = settings or CoreSettings()
    row = CoreRun(**value.model_dump(), submit_fence=value.input_revision, credits_limit=settings.credits_limit,
        tokens_limit=settings.tokens_limit, max_thought_rounds=settings.max_thought_rounds)
    tx.add(row)
    await tx.flush()
    await tx.refresh(row)  # creation trigger has appended the first event/cursor
    return row


async def create_task(tx: AsyncSession, value: TaskInput, settings: CoreSettings | None = None) -> CoreTask:
    settings = settings or CoreSettings()
    row = CoreTask(**value.model_dump(), tokens_limit=settings.tokens_limit,
        max_retries=settings.max_task_retries)
    tx.add(row)
    await tx.flush()
    await tx.get(CoreRun, row.run_id, populate_existing=True)
    return row


async def append_asset_version(tx: AsyncSession, asset_id: UUID, expected_version: int,
                               value: AssetContent, *, parents: tuple[UUID, ...] = (),
                               relation: str = "derived") -> CoreAssetVersion:
    """Append a complete snapshot and move the head with CAS; never update an old version."""
    try:
        async with tx.begin_nested():
            asset = await tx.scalar(select(CoreAsset).where(CoreAsset.id == asset_id)
                                    .execution_options(populate_existing=True))
            if asset is None:
                raise EntityNotFound("asset_not_found")
            if asset.version != expected_version:
                raise VersionConflict("asset version conflict")
            row = CoreAssetVersion(asset_id=asset.id, workspace_id=asset.workspace_id,
                version=asset.current_version_number + 1, previous_version_id=asset.current_version_id,
                **value.model_dump())
            tx.add(row)
            await tx.flush()
            changed = await tx.execute(update(CoreAsset).where(CoreAsset.id == asset_id,
                CoreAsset.version == expected_version).values(version=expected_version + 1,
                current_version_id=row.id, current_version_number=row.version)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise VersionConflict("asset version conflict")
            for parent in dict.fromkeys(parents):
                tx.add(CoreAssetLineage(input_version_id=parent, output_version_id=row.id, relation=relation))
            await tx.flush()
            await tx.refresh(asset)
            return row
    except DBAPIError as exc:
        mapped = contract_error(exc)
        if mapped:
            raise mapped from exc
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise VersionConflict("concurrent asset version conflict") from exc
        raise


async def create_asset(tx: AsyncSession, workspace_id: UUID, user_id: str, ownership: str,
                       value: AssetContent) -> CoreAsset:
    async with tx.begin_nested():
        asset = CoreAsset(workspace_id=workspace_id, user_id=user_id, ownership=ownership)
        tx.add(asset)
        await tx.flush()
        await append_asset_version(tx, asset.id, 0, value)
        return asset
