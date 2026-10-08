import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not keep tz info)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Customers sign in with their mobile number (`mobile`) and a password. Customer accounts from before
    # have an email too, no longer used. Admins and shopkeepers ("staff", made at /admin) sign in with a
    # mobile number (`phone`) and a password.
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    # A customer's mobile number (+91XXXXXXXXXX): her sign-in, and how the shop reaches her. Separate
    # from `phone`, which is only a staff sign-in number (one person may have both accounts).
    mobile: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    is_customer: Mapped[bool] = mapped_column(Boolean, default=False)  # signed up on the website
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
    # Rupees added to its MRP for customers (eggs: once per order). Set on the item's card; None (items
    # from before, until start-up fills them in) counts as the usual ₹5.
    markup: Mapped[int | None] = mapped_column(Integer, nullable=True, default=5)
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
    # Cancelled by the shop: its stock went back on the shelf and it no longer counts anywhere.
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    freebies: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Beside each freebie, the item taken from stock for it (its product id), or None: a free pick, a
    # loyalty pick or the item the shop gave for a free chocolate or snack. None for the whole list on
    # orders from before this was kept (services.free_stock then finds the item by its name).
    free_items: Mapped[list[int | None] | None] = mapped_column(JSON, nullable=True)
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


class ManualSale(Base):
    """A sale made in person (at the shop's room, outside the website), entered by a shopkeeper or admin on
    the dashboard. Its items come off the stock and it counts in sales and profit, like an order."""

    __tablename__ = "manual_sales"
    # AUTOINCREMENT so sale numbers are never reused.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # What was received: the shop's prices unless the shopkeeper entered another amount.
    total: Mapped[float] = mapped_column(Float)
    payment: Mapped[str] = mapped_column(String(10))  # Cash or UPI
    note: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Who entered it, as their sign-in number (text, so it stays readable after the account is gone).
    recorded_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Undone (entered by mistake): its items went back on the shelf and it no longer counts anywhere.
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    items: Mapped[list["ManualSaleItem"]] = relationship(back_populates="sale", cascade="all, delete-orphan")


class ManualSaleItem(Base):
    __tablename__ = "manual_sale_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("manual_sales.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_name: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    purchase_price: Mapped[float] = mapped_column(Float)  # MRP when sold: the cost, for profit
    sale_price: Mapped[float] = mapped_column(Float)  # the shop's price each when sold

    sale: Mapped[ManualSale] = relationship(back_populates="items")


class StockEntry(Base):
    """Stock added (new stock bought) or taken off by hand on the dashboard: a new item's starting stock,
    − / + or a typed number, or deleting an item. The admin's Investment page adds these up. Sales,
    cancelled orders and undone sales aren't entries: they aren't stock bought or thrown away. Quick
    changes to one item by the same person are one entry (see routers/admin.py: _log_stock)."""

    __tablename__ = "stock_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    product_name: Mapped[str] = mapped_column(String(80))
    change: Mapped[int] = mapped_column(Integer)  # units added (+) or taken off (−)
    purchase_price: Mapped[float] = mapped_column(Float)  # MRP each at the time: what the stock cost
    # Who changed it, as their sign-in number (text, so it stays readable after the account is gone).
    recorded_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MartSettings(Base):
    """Single-row table (id=1) with shop-wide settings editable by the shopkeeper."""

    __tablename__ = "mart_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    launch_message: Mapped[str] = mapped_column(String(200))
    daily_offers: Mapped[list[dict]] = mapped_column(JSON)
    wheel_prizes: Mapped[list[dict]] = mapped_column(JSON)
    store_override: Mapped[str] = mapped_column(String(10), default="auto")
    coupon_rule: Mapped[str] = mapped_column(String(10), default="best")
    wheel_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # customers can spin
    # While the store is offline, customers can still order (on request). Off: they send a request instead.
    offline_orders: Mapped[bool] = mapped_column(Boolean, default=True)
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


class OrderRequest(Base):
    """While the store is offline and not taking orders, a customer can ask the shop for what's in her
    cart. Shopkeepers and admins see it on the dashboard (and get an alert), get in touch, and mark it
    done. No stock is taken. One per customer: sending again replaces it; ordering removes it."""

    __tablename__ = "order_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    items: Mapped[list[dict]] = mapped_column(JSON)  # [{"productId", "name", "qty"}], as sent
    delivery: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # The won slice's terms at the time (services.coupon_terms); empty on coupons from before they existed.
    min_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class AlertDevice(Base):
    """A phone or laptop browser where a shopkeeper or admin turned on order alerts: its push address and
    keys (see webpush.py), and the sign-in it belongs to. Alerts go there only while that sign-in lasts: not
    after it runs out, the password is changed ("sign out everywhere" too), or the account loses its role,
    is blocked or deleted. Signing out on the device removes it."""

    __tablename__ = "alert_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(100))
    auth: Mapped[str] = mapped_column(String(50))
    role: Mapped[str] = mapped_column(String(20))  # the sign-in's: shopkeeper or admin
    # The sign-in's password version (security._password_version) and when it runs out.
    password_version: Mapped[int] = mapped_column(BigInteger)
    session_expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Each time the dashboard opens on the device it's refreshed (with that sign-in's details).
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginFailure(Base):
    """A failed password/PIN attempt. Stored in the database so the count survives restarts and is shared by every API process."""

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
