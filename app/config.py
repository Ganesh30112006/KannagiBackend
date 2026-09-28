import logging
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEV_JWT_SECRET = "dev-only-insecure-secret"
MIN_SECRET_LENGTH = 32
MIN_STAFF_PASSWORD_LENGTH = 8  # admins and shopkeepers, like customers
# backend/.env holds development settings. A production run (ENVIRONMENT=production in the real
# environment, which start-production loads from the root .env) never reads it, so a
# development-only setting can't leak into the live shop.
DEV_ENV_FILE = None if os.environ.get("ENVIRONMENT", "").lower() == "production" else BACKEND_DIR / ".env"

logger = logging.getLogger("kannagi")


@dataclass(frozen=True)
class CloudinaryAccount:
    cloud_name: str
    api_key: str
    api_secret: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=DEV_ENV_FILE, extra="ignore")

    environment: str = "development"  # "production" turns on the startup safety checks
    # SQLite by default; for Neon use the *pooled* connection string (host contains "-pooler").
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'kannagi.db').as_posix()}"
    jwt_secret: str = DEV_JWT_SECRET
    jwt_expire_days: int = 30
    # The main admin's sign-in at /admin (mobile number and password). Every start makes sure this
    # account exists with this password; it can add more admins and the shopkeepers there.
    admin_mobile: str = ""
    admin_password: str = ""
    # Shared secret the website server sends with every call. Without it the API answers 404,
    # so browsers and anyone else who finds the backend can't use it directly.
    internal_api_key: str = ""
    # cloudinary://<api_key>:<api_secret>@<cloud_name> (Cloudinary dashboard > API Keys).
    # When unset, product photos are stored inline in the database (fine for local development).
    cloudinary_url: str = ""
    cloudinary_folder: str = "kannagi-night-mart"
    enable_docs: bool = False
    shop_timezone: str = "Asia/Kolkata"
    max_request_bytes: int = 4 * 1024 * 1024
    # Order alerts (a notification for each new order on the shopkeepers' phones and laptops): the shop's
    # own private key, made once with `python -m app.webpush`. Unset, the dashboard says alerts aren't set
    # up. Changing it turns alerts off on every device until it's turned on again there.
    vapid_private_key: str = ""
    # How the push services (Google, Apple, Mozilla, Microsoft) can reach the shop about its alerts.
    push_contact: str = "https://kannagimart.tech"

    @field_validator("admin_mobile", "admin_password", "jwt_secret", "internal_api_key", "database_url", "cloudinary_url", "vapid_private_key", "push_contact")
    @classmethod
    def _strip(cls, value: str) -> str:
        # A value pasted into a dashboard with a space or newline around it would otherwise never match
        # what the website sends (the key) or what the sign-in form sends (the password).
        return value.strip()

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Neon hands out postgresql:// (or postgres://) URLs; SQLAlchemy needs the driver named.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def on_render(self) -> bool:
        return os.environ.get("RENDER", "").lower() == "true"  # Render sets RENDER=true on its servers

    @cached_property
    def owner_phone(self) -> str | None:
        """ADMIN_MOBILE as stored (+91XXXXXXXXXX), or None when unset or not a mobile number."""
        from .schemas import indian_mobile

        try:
            return indian_mobile(self.admin_mobile) if self.admin_mobile else None
        except ValueError:
            return None

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.shop_timezone)

    @cached_property
    def cloudinary(self) -> CloudinaryAccount | None:
        if not self.cloudinary_url:
            return None
        parsed = urlparse(self.cloudinary_url)
        if parsed.scheme != "cloudinary" or not (parsed.hostname and parsed.username and parsed.password):
            raise ValueError("CLOUDINARY_URL must look like cloudinary://<api_key>:<api_secret>@<cloud_name>")
        return CloudinaryAccount(parsed.hostname, unquote(parsed.username), unquote(parsed.password))

    @cached_property
    def vapid_key(self):
        """The order alerts' signing key (VAPID_PRIVATE_KEY), or None when alerts aren't set up. Raises
        ValueError for a key that isn't one."""
        if not self.vapid_private_key:
            return None
        from .webpush import private_key

        return private_key(self.vapid_private_key)

    def check(self) -> None:
        """Refuse to run in production with missing or weak secrets."""
        problems = []
        if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < MIN_SECRET_LENGTH:
            problems.append(f"JWT_SECRET must be a random string of at least {MIN_SECRET_LENGTH} characters")
        if len(self.internal_api_key) < MIN_SECRET_LENGTH:
            problems.append(f"INTERNAL_API_KEY must be a random string of at least {MIN_SECRET_LENGTH} characters")
        if not (self.admin_mobile and self.admin_password):
            problems.append("ADMIN_MOBILE and ADMIN_PASSWORD (the main admin's sign-in at /admin) must both be set")
        elif self.owner_phone is None:
            problems.append("ADMIN_MOBILE must be a 10-digit mobile number")
        if self.admin_password and len(self.admin_password) < MIN_STAFF_PASSWORD_LENGTH:
            problems.append(f"ADMIN_PASSWORD must be at least {MIN_STAFF_PASSWORD_LENGTH} characters")
        if self.on_render and self.is_sqlite:
            problems.append("DATABASE_URL must point to Postgres (Neon) on Render: its disk is wiped on every deploy")
        try:
            self.cloudinary  # noqa: B018 - validates the URL format
        except ValueError as error:
            problems.append(str(error))
        try:
            self.vapid_key  # noqa: B018 - validates the key
        except ValueError as error:
            problems.append(str(error))
        if not self.push_contact.startswith(("https://", "mailto:")):
            problems.append("PUSH_CONTACT must be a web address (https://...) or mailto:")
        if not problems:
            return
        if self.is_production:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
        for problem in problems:
            logger.warning("Development mode: %s", problem)


settings = Settings()
