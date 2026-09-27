"""Back up the shop's database to one SQLite file, or restore a backup into a new, empty database.

    python -m app.backup                         # backups/kannagi-<date>-<time>.db, keeps the newest 14
    python -m app.backup --dir D:/shop-backups --keep 30
    python -m app.backup --restore backups/kannagi-20260925-140000.db

Uses DATABASE_URL (the live shop's database when run by start-production, or from .env).

A backup holds every table, taken from one consistent snapshot, so each order always has its items.
It is an ordinary SQLite database: open it with any SQLite viewer, or run the API on a copy of it
(DATABASE_URL=sqlite:///path/to/copy.db) to look around.

--restore copies a backup into DATABASE_URL, but only while that database has no accounts, orders or sales,
so it can never overwrite a live shop. To recover, make a new database (for example a new Neon branch),
point DATABASE_URL at it, restore into it, then switch the shop over.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, Integer, create_engine, func, insert, inspect, select, text

from . import models
from .config import BACKEND_DIR
from .database import Base, engine, retire_shared_sign_ins, upgrade_schema

DEFAULT_DIR = BACKEND_DIR.parent / "backups"
PREFIX = "kannagi-"
BATCH = 500


def backup(directory: Path = DEFAULT_DIR, keep: int = 14, source: Engine = engine) -> tuple[Path, dict[str, int]]:
    """Write a new backup file; returns its path and the rows copied per table."""
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}.db"
    partial = final.with_name(final.name + ".partial")  # never mistaken for a finished backup
    partial.unlink(missing_ok=True)
    try:
        if source.dialect.name == "sqlite" and source.url.database not in (None, "", ":memory:"):
            _sqlite_snapshot(Path(source.url.database), partial)
        else:
            _copy_tables(source, partial)
        counts = _counts(_sqlite_engine(partial))
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    _prune(directory, keep)
    return final, counts


def restore(backup_file: Path, target: Engine = engine) -> dict[str, int]:
    """Copy a backup into an empty database; returns the rows copied per table."""
    if not backup_file.is_file():
        raise SystemExit(f"No backup at {backup_file}")
    source = _sqlite_engine(backup_file, read_only=True)
    Base.metadata.create_all(target)
    if target is engine:
        upgrade_schema()
    copied: dict[str, int] = {}
    with source.connect() as reader, target.begin() as writer:
        # A backup taken before a column or table existed still restores; those get their defaults.
        saved = inspect(reader)
        saved_columns = {name: {column["name"] for column in saved.get_columns(name)} for name in saved.get_table_names()}
        # A first start makes the main admin (ADMIN_MOBILE), so admin accounts alone still count as empty
        # (but not once an admin has entered manual sales).
        User = models.User.__table__
        in_use = (
            writer.scalar(select(func.count()).select_from(models.Order.__table__))
            or writer.scalar(select(func.count()).select_from(models.ManualSale.__table__))
            or writer.scalar(select(func.count()).select_from(User).where(User.c.is_admin.is_(False)))
        )
        if in_use:
            raise SystemExit(
                "This database already has customers, shopkeepers, orders or sales, so nothing was restored. "
                "Restore into a new, empty database instead."
            )
        # Only default data (settings, the main admin) is here: replace it with the backup's. The next
        # start puts the main admin's password from ADMIN_PASSWORD back.
        for table in reversed(Base.metadata.sorted_tables):
            writer.execute(table.delete())
        for table in Base.metadata.sorted_tables:
            if table.name in saved_columns:
                copied[table.name] = _copy_rows(reader, writer, table, saved_columns[table.name])
        if "is_admin" not in saved_columns.get("users", set()):
            retire_shared_sign_ins(writer)  # a backup from the shared admin password and PIN days
        if target.dialect.name == "postgresql":
            _reset_sequences(writer)
    return copied


def _sqlite_engine(path: Path, read_only: bool = False) -> Engine:
    if read_only:
        return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    return create_engine(f"sqlite:///{path.as_posix()}")


def _sqlite_snapshot(database: Path, destination: Path) -> None:
    # SQLite's online backup: a consistent copy, even while the shop is writing.
    live, copy = sqlite3.connect(database), sqlite3.connect(destination)
    try:
        live.backup(copy)
    finally:
        copy.close()
        live.close()


def _copy_tables(source: Engine, destination: Path) -> None:
    target = _sqlite_engine(destination)
    Base.metadata.create_all(target)
    # One REPEATABLE READ transaction: every table is read as of the same moment.
    with source.connect().execution_options(isolation_level="REPEATABLE READ") as reader, target.begin() as writer:
        for table in Base.metadata.sorted_tables:
            _copy_rows(reader, writer, table)
    target.dispose()


def _copy_rows(reader, writer, table, present: set[str] | None = None) -> int:
    copied = 0
    columns = [column for column in table.columns if present is None or column.name in present]
    rows = reader.execute(select(*columns)).mappings()
    while batch := [dict(row) for row in rows.fetchmany(BATCH)]:
        writer.execute(insert(table), batch)
        copied += len(batch)
    return copied


def _counts(database: Engine) -> dict[str, int]:
    with database.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    database.dispose()
    return counts


def _reset_sequences(writer) -> None:
    # Rows were copied with their ids: move each id counter past them so new rows don't collide.
    for table in Base.metadata.sorted_tables:
        key = list(table.primary_key.columns)
        if len(key) != 1 or not isinstance(key[0].type, Integer):
            continue
        sequence = writer.scalar(text("SELECT pg_get_serial_sequence(:table, :column)"), {"table": table.name, "column": key[0].name})
        if sequence:
            writer.execute(
                text(f"SELECT setval(:sequence, COALESCE((SELECT MAX({key[0].name}) FROM {table.name}), 0) + 1, false)"),
                {"sequence": sequence},
            )


def _prune(directory: Path, keep: int) -> None:
    backups = sorted(directory.glob(f"{PREFIX}*.db"))
    for old in backups[: max(len(backups) - keep, 0)]:
        old.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="where backups go (default: backups/)")
    parser.add_argument("--keep", type=int, default=14, help="how many backups to keep (default: 14)")
    parser.add_argument("--restore", type=Path, metavar="BACKUP", help="restore this backup into an empty database")
    args = parser.parse_args()
    target = engine.url.render_as_string(hide_password=True)
    if args.restore:
        copied = restore(args.restore)
        print(f"Restored {args.restore} into {target}: " + ", ".join(f"{n} {name}" for name, n in copied.items()))
        return
    path, counts = backup(args.dir, max(args.keep, 1))
    print(f"Backed up {target} to {path}: {counts['users']} accounts, {counts['orders']} orders, {counts['products']} products")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
