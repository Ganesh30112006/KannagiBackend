"""Password and PIN hashing (bcrypt). Kept apart from security.py so settings code can use it too."""

import base64
import hashlib

import bcrypt


def _prehash(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes; pre-hashing keeps long/multibyte passwords intact.
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


# Checked against when the email is unknown, so a wrong email takes as long as a wrong password.
DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except ValueError:
        return False
