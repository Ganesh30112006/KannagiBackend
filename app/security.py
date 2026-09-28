from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User
from .passwords import DUMMY_HASH, hash_password, verify_password

__all__ = ["DUMMY_HASH", "hash_password", "verify_password"]  # re-exported for the routers

_bearer = HTTPBearer(auto_error=False)
TOKEN_ISSUER = "kannagi-night-mart"
ADMIN_SESSION = timedelta(hours=12)
BLOCKED = "This account has been blocked. Please contact the shop."
# What a sign-in opens: the shop (customer), the dashboard (shopkeeper) or /admin (admin).
SessionKind = Literal["customer", "shopkeeper", "admin"]


def _password_version(user: User) -> int:
    """Changes whenever the password is reset (millisecond precision), 0 if it never was."""
    changed = user.password_changed_at
    return int(changed.replace(tzinfo=timezone.utc).timestamp() * 1000) if changed else 0


def create_token(user: User, kind: SessionKind = "customer") -> str:
    """A sign-in token. Admin sign-ins last 12 hours, the others JWT_EXPIRE_DAYS."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user.id,
        "iat": now,
        "exp": now + (ADMIN_SESSION if kind == "admin" else timedelta(days=settings.jwt_expire_days)),
        "iss": TOKEN_ISSUER,
        "pwv": _password_version(user),
    }
    if kind != "customer":
        claims["role"] = kind
    user.session_kind = kind  # type: ignore[attr-defined]
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def _kind(user: User) -> SessionKind:
    return getattr(user, "session_kind", "customer")


def has_shop_access(user: User) -> bool:
    """Signed in as a shopkeeper, or as an admin (admins control everything)."""
    return _kind(user) in ("shopkeeper", "admin")


def is_site_admin(user: User) -> bool:
    return _kind(user) == "admin"


def _unauthorized(detail: str = "Please sign in again.") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise _unauthorized()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=TOKEN_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
    except jwt.PyJWTError:
        raise _unauthorized() from None
    user = db.get(User, payload.get("sub"))
    if user is None or user.deleted_at is not None:
        raise _unauthorized()
    if payload.get("pwv", 0) != _password_version(user):
        raise _unauthorized()  # signed in before the password was changed
    role = payload.get("role")
    # Checked on every request, so taking the role away at /admin ends that sign-in at once.
    if role == "admin" and user.is_admin:
        kind: SessionKind = "admin"
    elif role == "shopkeeper" and user.is_shopkeeper:
        kind = "shopkeeper"
    elif role is None and user.is_customer:
        kind = "customer"
    else:
        # The role was taken away, or an admin/shopkeeper account without its own sign-in (for
        # example a session from before these accounts had passwords).
        raise _unauthorized()
    if user.blocked:
        raise _unauthorized(BLOCKED)
    user.session_kind = kind  # type: ignore[attr-defined]
    # When this sign-in runs out (naive UTC, like the database): order alerts on this device stop then.
    user.session_expires_at = datetime.fromtimestamp(payload["exp"], timezone.utc).replace(tzinfo=None)  # type: ignore[attr-defined]
    return user


def shopkeeper(user: User = Depends(current_user)) -> User:
    if not has_shop_access(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Shopkeeper access required.")
    return user


def site_admin(user: User = Depends(current_user)) -> User:
    if not is_site_admin(user):
        # Same answer as an unknown path: /admin's API doesn't advertise itself to anyone else.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return user
