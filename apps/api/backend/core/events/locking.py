"""Background locks never wait for a busy aggregate or transport record."""
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError


class LockBusy(Exception):
    pass


def lock_error(error):
    return isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) == "55P03"


async def bounded(tx, settings):
    # Covers implicit locks (UPDATE, unique index checks, alert upserts) as well.
    await tx.execute(text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": f"{settings.lock_timeout_ms}ms"})


async def locked(tx, model, ident):
    row = await tx.scalar(select(model).where(model.__table__.primary_key.columns.values()[0] == ident)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if row is None and await tx.get(model, ident, populate_existing=True) is not None:
        raise LockBusy(f"{model.__tablename__} busy")
    return row
