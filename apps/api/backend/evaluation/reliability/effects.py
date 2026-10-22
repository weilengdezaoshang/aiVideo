"""Only in the disposable regression database; production effects remain unchanged."""

from sqlalchemy import MetaData, Table, Column, Uuid, BigInteger, Identity

metadata = MetaData()
effects = Table(
    "reliability_effects",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("run_id", Uuid, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("ordinal", BigInteger, Identity(), nullable=False),
)
