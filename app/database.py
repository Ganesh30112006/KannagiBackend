import functools
import threading
from collections.abc import Iterator

from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    return create_engine(
        url,
        # Neon scales to zero and closes idle connections: test each one before use.
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": 10,
            # Neon's pooled endpoint runs PgBouncer in transaction mode; skip server-side prepared statements.
            "prepare_threshold": None,
        },
    )


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Columns added after the first release. create_all() creates missing tables but never alters
# existing ones, so these are added in place (keeping the data) on start-up and by setup_db.
ADDED_COLUMNS = {
    "users": {
        "email_verified_at": "TIMESTAMP",
        "password_changed_at": "TIMESTAMP",
        "phone": "VARCHAR(20)",
        "blocked": "BOOLEAN NOT NULL DEFAULT FALSE",
        "mobile": "VARCHAR(20)",
        "is_admin": "BOOLEAN NOT NULL DEFAULT FALSE",
        "deleted_at": "TIMESTAMP",
        "is_customer": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "mart_settings": {
        "coupon_rule": "VARCHAR(10) NOT NULL DEFAULT 'best'",
        "wheel_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
        "offline_orders": "BOOLEAN NOT NULL DEFAULT TRUE",
    },
    "spins": {"min_order": "INTEGER", "min_items": "INTEGER", "amount": "INTEGER"},
    # Each item's own markup (filled in on start-up from the shop-wide one it replaces: seed.py).
    "products": {"markup": "INTEGER"},
    "orders": {
        "customer_name": "VARCHAR(100)",
        "customer_phone": "VARCHAR(20)",
        "customer_block": "VARCHAR(1)",
        "customer_room": "VARCHAR(20)",
        "payment_confirmed": "BOOLEAN NOT NULL DEFAULT FALSE",
        "cancelled": "BOOLEAN NOT NULL DEFAULT FALSE",
        "free_items": "JSON",
    },
}


def upgrade_schema(connection: Connection | None = None) -> list[str]:
    """Add any missing ADDED_COLUMNS; returns the ones added (safe to run repeatedly). Runs in the given
    connection's transaction (start-up's, see startup.py), or in one of its own."""
    if connection is None:
        with engine.begin() as own:
            added = _upgrade(own)
    else:
        added = _upgrade(connection)
    if "users.email (now optional)" in added and engine.dialect.name == "sqlite":
        _rebuild_sqlite_users()  # outside any transaction
    return added


def _upgrade(connection: Connection) -> list[str]:
    added = []
    # Only writes when something is missing: every start-up runs this.
    inspector = inspect(connection)  # same connection as the changes, so it sees the current schema
    for table, columns in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                added.append(f"{table}.{name}")
    if "users.email_verified_at" in added:
        # Accounts made before email confirmation existed stay able to order.
        connection.execute(text("UPDATE users SET email_verified_at = created_at"))
    if "users.phone" in added:
        connection.execute(text("CREATE UNIQUE INDEX ix_users_phone ON users (phone)"))
    if "users.mobile" in added:
        # Customers from before sign-up asked for a number: use the one in their delivery details.
        connection.execute(text(
            "UPDATE users SET mobile = (SELECT phone FROM profiles WHERE profiles.user_id = users.id) "
            "WHERE email IS NOT NULL"
        ))
    if "users.is_admin" in added:
        retire_shared_sign_ins(connection)
    if "users.is_customer" in added:
        mark_customers(connection)
    if not any(index["name"] == "ix_users_mobile" for index in inspector.get_indexes("users")):
        # Customers sign in with their mobile number.
        connection.execute(text("CREATE INDEX ix_users_mobile ON users (mobile)"))
        added.append("users.mobile (index)")
    email = next(column for column in inspector.get_columns("users") if column["name"] == "email")
    if not email["nullable"]:
        # Admin and shopkeeper accounts have no email. (SQLite's table is rebuilt after this transaction.)
        if connection.dialect.name != "sqlite":
            connection.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))
        added.append("users.email (now optional)")
    return added


def mark_customers(connection) -> None:
    """Customer accounts from before the is_customer column: the ones with an email (staff have none)."""
    connection.execute(text("UPDATE users SET is_customer = TRUE WHERE email IS NOT NULL"))


def retire_shared_sign_ins(connection) -> None:
    """Admins and shopkeepers now have their own accounts and passwords (made at /admin), replacing the
    shared admin password and shopkeeper PIN. The account behind the shared admin password goes (kept,
    as deleted, if it ever ordered), and customer email accounts are only customers."""
    legacy = "id = 'site-admin'"
    ordered = "EXISTS (SELECT 1 FROM orders WHERE orders.user_id = 'site-admin')"
    connection.execute(text(f"UPDATE users SET deleted_at = CURRENT_TIMESTAMP WHERE {legacy} AND {ordered}"))
    connection.execute(text(f"DELETE FROM users WHERE {legacy} AND NOT {ordered}"))
    connection.execute(text("UPDATE users SET is_shopkeeper = FALSE WHERE email IS NOT NULL"))


def _rebuild_sqlite_users() -> None:
    # SQLite can't change a column in place: rebuild the table with the current definition.
    # Foreign keys must be off (outside any transaction) or dropping the old table would delete every order.
    users = Base.metadata.tables["users"]
    columns = ", ".join(column.name for column in users.columns)
    raw = engine.raw_connection()
    try:
        raw.commit()
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        try:
            cursor.execute("BEGIN")
            create = str(CreateTable(users).compile(engine)).replace("CREATE TABLE users", "CREATE TABLE users_new", 1)
            cursor.execute(create)
            cursor.execute(f"INSERT INTO users_new ({columns}) SELECT {columns} FROM users")
            cursor.execute("DROP TABLE users")
            cursor.execute("ALTER TABLE users_new RENAME TO users")
            for index in users.indexes:
                cursor.execute(str(CreateIndex(index).compile(engine)))
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()


_sqlite_turns = threading.Lock()


def takes_turns(handler):
    """For endpoints that read something and then change it (placing, cancelling or handing over an
    order, deleting an account, a first save of hostel details): the same one twice at once must not
    both act on what they read. Postgres makes them take turns with row locks (SELECT ... FOR UPDATE)
    inside the handlers; SQLite ignores those, and only ever has one API process, so there they take
    turns here. The lock is taken in the handler's own thread, so waiting requests can't starve it."""

    @functools.wraps(handler)
    def in_turn(*args, **kwargs):
        if engine.dialect.name != "sqlite":
            return handler(*args, **kwargs)
        with _sqlite_turns:
            return handler(*args, **kwargs)

    return in_turn


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
