import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not keep tz info)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Customers sign in with email and password. Admins and shopkeepers ("staff", made at /admin) sign
    # in with a mobile number (`phone`) and a password; they have no email.
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    # A customer's own mobile number (+91XXXXXXXXXX), given at sign-up so the shop can reach her.
    # Separate from `phone`, which is only a staff sign-in number.
    mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    is_shopkeeper: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    first_order_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Login tokens issued before this are refused, so a password reset signs out every device.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by the site admin: a blocked account is signed out and can't sign in again.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Deleted at /admin but kept because orders point at it: no email, number or password any more.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResetRequest(Base):
    """A customer forgot her password and asked the shop for a new one. The site admin sets it at
    /admin and tells her (the shop sends no emails or codes). One open request per account."""

    __tablename__ = "reset_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20))
    block: Mapped[str] = mapped_column(String(1))
    room_number: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    emoji: Mapped[str] = mapped_column(String(16), default="🛍️")
    purchase_price: Mapped[float] = mapped_column(Float)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=3)
    category: Mapped[str] = mapped_column(String(40), default="Snacks")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    __tablename__ = "orders"
    # AUTOINCREMENT so order numbers are never reused.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    total: Mapped[float] = mapped_column(Float)
    delivery: Mapped[str] = mapped_column(String(20))
    details: Mapped[str] = mapped_column(String(500))
    payment: Mapped[str] = mapped_column(String(20))
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    discount: Mapped[float] = mapped_column(Float, default=0)
    discount_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    coupon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    utr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # UPI orders are placed first and paid after; the shopkeeper ticks this once the money shows up.
    payment_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    on_request: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cancelled by the site admin: its stock went back on the shelf and it no longer counts anywhere.
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    freebies: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Who to hand it to, as it was when she ordered (a later profile change doesn't rewrite old orders).
    customer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_block: Mapped[str | None] = mapped_column(String(1), nullable=True)
    customer_room: Mapped[str | None] = mapped_column(String(20), nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_name: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    purchase_price: Mapped[float] = mapped_column(Float)
    sale_price: Mapped[float] = mapped_column(Float)

    order: Mapped[Order] = relationship(back_populates="items")


class MartSettings(Base):
    """Single-row table (id=1) with shop-wide settings editable by the shopkeeper."""

    __tablename__ = "mart_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    launch_message: Mapped[str] = mapped_column(String(200))
    daily_offers: Mapped[list[dict]] = mapped_column(JSON)
    wheel_prizes: Mapped[list[dict]] = mapped_column(JSON)
    store_override: Mapped[str] = mapped_column(String(10), default="auto")
    coupon_rule: Mapped[str] = mapped_column(String(10), default="best")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WishRequest(Base):
    __tablename__ = "wish_requests"
    # One vote per customer per item, so counts reflect how many girls want it.
    __table_args__ = (UniqueConstraint("user_id", "item_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_key: Mapped[str] = mapped_column(String(60), index=True)
    label: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Spin(Base):
    """One wheel spin per customer per shop-local day; a winning spin is the customer's coupon."""

    __tablename__ = "spins"
    __table_args__ = (UniqueConstraint("user_id", "spun_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    spun_on: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    label: Mapped[str] = mapped_column(String(80))
    icon: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SiteSettings(Base):
    """Single row (id=1): what the site admin sets at /admin. `values` holds the shop details
    (schemas.SiteValues). Older databases also have two unused columns from the shared shopkeeper PIN
    and admin password, which accounts with their own passwords replaced."""

    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SyncState(Base):
    """A change counter per kind of shared data (catalog, orders, ...). Every write bumps its counter in
    the same transaction, so open pages can ask "what changed since revision N?" and download only that."""

    __tablename__ = "sync_state"

    topic: Mapped[str] = mapped_column(String(20), primary_key=True)
    rev: Mapped[int] = mapped_column(Integer, default=0)


class LoginFailure(Base):
    """A failed password/PIN attempt. Stored in the database so the count survives restarts and is shared by every API process."""

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
