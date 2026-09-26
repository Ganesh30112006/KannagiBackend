"""Limits on failed attempts (wrong password, wrong PIN), shared through the database."""

from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import LoginFailure, utcnow


class FailureLimiter:
    def __init__(self, name: str, max_failures: int, window_seconds: int, message: str):
        self.name = name
        self.max_failures = max_failures
        self.window = timedelta(seconds=window_seconds)
        self.message = message

    def _key(self, key: str) -> str:
        return f"{self.name}:{key}"[:200]

    def check(self, db: Session, *keys: str) -> None:
        """Raise 429 if any key (e.g. the account and the client IP) is locked out."""
        cutoff = utcnow() - self.window
        for key in keys:
            count = db.scalar(
                select(func.count()).where(LoginFailure.key == self._key(key), LoginFailure.created_at > cutoff)
            )
            if (count or 0) >= self.max_failures:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, self.message)

    def fail(self, db: Session, *keys: str) -> None:
        db.add_all(LoginFailure(key=self._key(key)) for key in keys)
        # Old attempts no longer count; drop them as we go so the table stays small.
        db.execute(delete(LoginFailure).where(LoginFailure.created_at < utcnow() - timedelta(days=1)))
        db.commit()

    def reset(self, db: Session, *keys: str) -> None:
        db.execute(delete(LoginFailure).where(LoginFailure.key.in_([self._key(key) for key in keys])))
        db.commit()
