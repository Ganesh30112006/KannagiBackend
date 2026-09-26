"""Admins and shopkeepers ("staff"): accounts with a mobile number and a password, made at /admin.

The main admin comes from the server settings (ADMIN_MOBILE, ADMIN_PASSWORD). Every start makes sure
that account exists, is an admin, isn't blocked and has that password, so those settings always open
/admin. It can't be removed or blocked at /admin, and its password is changed in the settings.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import User, utcnow
from .passwords import hash_password, verify_password

logger = logging.getLogger("kannagi")


def is_owner(user: User) -> bool:
    """The main admin from the server settings."""
    return user.phone is not None and user.phone == settings.owner_phone


def ensure_owner(db: Session) -> None:
    phone, password = settings.owner_phone, settings.admin_password
    if phone is None or not password:
        return  # production refuses to start without them (config.check); development may run without
    user = db.scalar(select(User).where(User.phone == phone))
    if user is None:
        db.add(User(phone=phone, password_hash=hash_password(password), is_admin=True))
        try:
            db.commit()
        except IntegrityError:  # another API process created it at the same moment
            db.rollback()
        return
    changed = False
    if not user.is_admin or user.blocked:
        user.is_admin, user.blocked = True, False
        changed = True
    if not verify_password(password, user.password_hash):
        # ADMIN_PASSWORD changed: its old sign-ins end.
        user.password_hash = hash_password(password)
        user.password_changed_at = utcnow()
        changed = True
        logger.info("Main admin password updated from ADMIN_PASSWORD")
    if changed:
        db.commit()
