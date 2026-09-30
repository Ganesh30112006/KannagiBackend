"""The shop details the site admin sets at /admin: payments, contacts, hours, options and prices.
(Admins and shopkeepers are accounts of their own: see staff.py.)"""

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import sync
from .models import SiteSettings
from .schemas import SitePublic, SiteValues

logger = logging.getLogger("kannagi")

ROW_ID = 1
_CACHE_KEY = "site_settings"  # the row, cached for the rest of the request (Session.info)


def _row(db: Session) -> SiteSettings | None:
    if _CACHE_KEY not in db.info:
        db.info[_CACHE_KEY] = db.get(SiteSettings, ROW_ID)
    return db.info[_CACHE_KEY]


def _ensure_row(db: Session) -> SiteSettings:
    row = _row(db)
    if row is None:
        row = SiteSettings(id=ROW_ID, values=SiteValues().model_dump())
        db.add(row)
        db.info[_CACHE_KEY] = row
    return row


def seed(db: Session) -> None:
    """Create the settings row with the defaults (the shop's original details). Doesn't commit."""
    _ensure_row(db)


# The closing hour last read, remembered by this process (alerts.scheduled_summary checks the time against
# it without waking the database). Refreshed by every read of the settings.
last_close_hour: int | None = None


def values(db: Session) -> SiteValues:
    global last_close_hour
    row = _row(db)
    if row is None or not row.values:
        found = SiteValues()
    else:
        try:
            found = SiteValues.model_validate(row.values)
        except ValidationError:
            logger.exception("Stored site settings are invalid; using the defaults")
            found = SiteValues()
    last_close_hour = found.close_hour
    return found


def public(db: Session) -> SitePublic:
    return SitePublic.model_validate(values(db).model_dump(include=set(SitePublic.model_fields)))


def save(db: Session, new: SiteValues) -> None:
    """Store new settings; open pages pick them up on their next sync. Doesn't commit."""
    _ensure_row(db).values = new.model_dump()
    sync.bump(db, sync.SITE)
