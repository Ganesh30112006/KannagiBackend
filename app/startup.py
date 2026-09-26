"""What every start does to the database: create missing tables, add missing columns, insert the default
rows and make sure the main admin exists. All of it is safe to repeat."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, upgrade_schema
from .seed import seed
from .staff import ensure_owner

# Any fixed number: the key of the Postgres lock that start-up holds.
STARTUP_LOCK = 7_345_901


def prepare_database() -> list[str]:
    """Returns the columns added. On Postgres it all happens in one transaction that first takes a lock,
    so API processes starting together (WEB_CONCURRENCY on Render) take turns: the first one upgrades,
    and the others then find nothing left to do instead of failing on the same change. The lock ends
    with the transaction, so it's safe behind Neon's pooled (PgBouncer) connection string."""
    if engine.dialect.name != "postgresql":
        # SQLite: one API process on one computer.
        Base.metadata.create_all(engine)
        added = upgrade_schema()
        with SessionLocal() as db:
            seed(db)
            ensure_owner(db)
        return added
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STARTUP_LOCK})
        Base.metadata.create_all(connection)
        added = upgrade_schema(connection)
        # The session's commits become savepoints: everything is committed together at the end.
        with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
            seed(db)
            ensure_owner(db)
    return added
