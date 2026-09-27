"""Change counters that let open pages sync cheaply: they send the revisions they have, and get back
only the data whose revision moved (see routers/session.py: /sync)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import SyncState

CATALOG = "catalog"  # products, prices, stock
ORDERS = "orders"  # any order placed or changed (payment, fulfilment), and manual sales
PROMOTIONS = "promotions"  # offers, spin wheel, launch message
WISHES = "wishes"  # wishlist requests
SITE = "site"  # shop details the site admin sets (UPI, contacts, hours, options, pricing)
TOPICS = (CATALOG, ORDERS, PROMOTIONS, WISHES, SITE)


def bump(db: Session, *topics: str) -> None:
    """Mark topics as changed. Call just before the commit that makes the change, so both become
    visible together (and a rollback undoes both)."""
    # Changes still waiting in the session are written first, so every transaction locks its data rows
    # before the counters; in the opposite order, two edits of one product could deadlock.
    db.flush()
    insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    # One row per topic, always in the same order, so concurrent writers can't deadlock on them.
    for topic in sorted(set(topics)):
        statement = insert(SyncState).values(topic=topic, rev=1)
        db.execute(statement.on_conflict_do_update(index_elements=[SyncState.topic], set_={"rev": SyncState.rev + 1}))


def revisions(db: Session) -> dict[str, int]:
    """Current revision of every topic (0 for one that has never changed)."""
    stored = dict(db.execute(select(SyncState.topic, SyncState.rev)).tuples().all())
    return {topic: stored.get(topic, 0) for topic in TOPICS}
