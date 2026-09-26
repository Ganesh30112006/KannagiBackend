"""Request/response models. JSON uses camelCase to match the frontend types."""

import re
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

CouponKind = Literal["free60", "three5", "freeSnack100", "halfDelivery", "four10", "premium5"]
OfferId = Literal["tier50", "tier100", "first", "bulk"]
Block = Literal["A", "B", "C"]
Delivery = Literal["Pickup", "Room Delivery"]
Payment = Literal["UPI", "Pay on Delivery"]
StoreOverride = Literal["auto", "online", "offline"]
# Which reward applies when a spin coupon and an automatic offer both could: the bigger saving, or always the coupon.
CouponRule = Literal["best", "coupon"]


def indian_mobile(value: str) -> str:
    """A 10-digit Indian mobile number, written any common way (+91, 0 prefix, spaces), as +91XXXXXXXXXX."""
    digits = re.sub(r"[\s-]", "", value)
    match = re.fullmatch(r"(?:\+?91|0)?([6-9][0-9]{9})", digits)
    if match is None:
        raise ValueError("Enter a 10-digit mobile number.")
    return f"+91{match.group(1)}"


def upi_reference(value: str) -> str:
    """The reference a UPI app shows after paying (12-digit UTR, or a longer transaction ID)."""
    cleaned = re.sub(r"\s", "", value)
    if not re.fullmatch(r"[A-Za-z0-9]{6,35}", cleaned):
        raise ValueError("Enter the UPI reference (UTR) from your payment app: 6 to 35 letters or digits.")
    return cleaned.upper()


# A UPI ID (VPA): name@bank, e.g. 7032767115@ibl.
UPI_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,63}")


def mobile_digits(value: str) -> str:
    """A mobile number as its 10 digits (for tel: and WhatsApp links)."""
    return indian_mobile(value)[3:]


# Order and product numbers are Postgres INTEGERs: a bigger number can't be one, and asking the
# database for it would fail instead of finding nothing.
MAX_ID = 2**31 - 1
# An order or product number in a URL.
IdPath = Annotated[int, Path(ge=1, le=MAX_ID)]

# Product photos may be uploaded as data URLs; keep them to a few MB.
MAX_IMAGE_LENGTH = 3_000_000


# Text Postgres can't store (a NUL character) or that isn't valid unicode (a lone surrogate).
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("*", mode="after")
    @classmethod
    def _storable(cls, value):
        # Refused as bad input here; otherwise saving or searching with it fails later with a 500.
        if isinstance(value, str) and _UNSTORABLE.search(value):
            raise ValueError("This text has characters that aren't allowed.")
        return value


def _price(_cls: type, value: float | None) -> float | None:
    """Prices are rupees with at most 2 decimals (paise)."""
    if value is None:
        return None
    if round(value, 2) != value:
        raise ValueError("Prices can have at most 2 decimal places.")
    return value


class Stripped(CamelModel):
    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


# --- auth ---


class AuthIn(Stripped):
    """Email is only the username: the shop never sends email."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class SignupIn(AuthIn):
    # So the shop can reach her, e.g. with a new password if she forgets hers.
    mobile: str = Field(max_length=20)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, value: str) -> str:
        return indian_mobile(value)


class UserOut(CamelModel):
    id: str
    email: str | None = None
    phone: str | None = None  # an admin's or shopkeeper's sign-in number
    mobile: str | None = None  # a customer's mobile number
    is_shopkeeper: bool  # signed in to the dashboard (as a shopkeeper or an admin)
    is_admin: bool = False  # signed in to /admin
    is_owner: bool = False  # the main admin from the server settings (ADMIN_MOBILE)
    first_order_available: bool


class AuthOut(CamelModel):
    token: str
    user: UserOut


class EmailIn(Stripped):
    email: EmailStr


class MessageOut(CamelModel):
    message: str


class StaffLoginIn(Stripped):
    """Admins and shopkeepers sign in with their mobile number and the password an admin gave them."""

    phone: str = Field(max_length=20)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("phone")
    @classmethod
    def _mobile(cls, value: str) -> str:
        return indian_mobile(value)


# --- profile ---


class ProfileBody(Stripped):
    full_name: str = Field(min_length=1, max_length=100)
    # The shopkeeper calls or WhatsApps this number, so it must be a real mobile number.
    phone: str = Field(max_length=20)
    block: Block
    room_number: str = Field(min_length=1, max_length=20)

    @field_validator("phone")
    @classmethod
    def _mobile(cls, value: str) -> str:
        return indian_mobile(value)


# --- products ---


class ProductOut(CamelModel):
    id: int
    name: str
    emoji: str
    mrp: float
    stock: int
    threshold: int
    category: str
    image: str | None = None


class ProductCreate(Stripped):
    name: str = Field(min_length=1, max_length=80)
    mrp: float = Field(gt=0, le=100_000)
    stock: int = Field(ge=0, le=100_000)
    threshold: int = Field(default=3, ge=0, le=100_000)
    emoji: str = Field(default="🛍️", max_length=16)
    category: str = Field(default="Snacks", max_length=40)
    image: str | None = Field(default=None, max_length=MAX_IMAGE_LENGTH)

    _check_price = field_validator("mrp")(classmethod(_price))


class ProductUpdate(Stripped):
    """Only fields present in the request are changed; send image: null to remove the photo."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    mrp: float | None = Field(default=None, gt=0, le=100_000)
    stock: int | None = Field(default=None, ge=0, le=100_000)
    # Relative change applied atomically, so quick taps or two shopkeepers can't overwrite each other.
    stock_delta: int | None = Field(default=None, ge=-100_000, le=100_000)
    threshold: int | None = Field(default=None, ge=0, le=100_000)
    image: str | None = Field(default=None, max_length=MAX_IMAGE_LENGTH)

    _check_price = field_validator("mrp")(classmethod(_price))


# --- promotions & store ---


class DailyOffer(CamelModel):
    id: OfferId
    title: str = Field(max_length=80)
    note: str = Field(max_length=200)
    icon: str = Field(max_length=16)
    active: bool


class WheelPrize(CamelModel):
    code: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=80)
    short_label: str = Field(max_length=80)
    icon: str = Field(max_length=16)
    kind: CouponKind | None
    active: bool


class Promotions(CamelModel):
    launch_message: str = Field(max_length=200)
    daily_offers: list[DailyOffer] = Field(max_length=4)
    wheel_prizes: list[WheelPrize] = Field(min_length=2, max_length=16)
    coupon_rule: CouponRule = "best"


class SoldItem(CamelModel):
    name: str
    qty: int


class SalesSummary(CamelModel):
    """All-time totals, computed by the database so the dashboard doesn't download every order."""

    order_count: int
    revenue: float
    investment: float  # purchase cost of the items sold
    sold: list[SoldItem]


class StoreStatus(CamelModel):
    override: StoreOverride
    online: bool


class StoreUpdate(CamelModel):
    override: StoreOverride


# --- wishes ---


class WishIn(Stripped):
    name: str = Field(min_length=1, max_length=60)


class WishOut(CamelModel):
    name: str
    count: int


class WishResult(WishOut):
    already_requested: bool


# --- spin wheel ---


class CouponOut(CamelModel):
    code: str
    label: str
    short_label: str
    icon: str
    kind: CouponKind
    expires_at: int  # epoch milliseconds


class SpinStatus(CamelModel):
    spun_today: bool
    coupon: CouponOut | None


class SpinResult(CamelModel):
    index: int
    prize: WheelPrize
    prizes: list[WheelPrize]
    coupon: CouponOut | None


# --- orders ---


class OrderItemIn(CamelModel):
    product_id: int = Field(gt=0, le=MAX_ID)
    quantity: int = Field(gt=0, le=100)


class OrderIn(Stripped):
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    delivery: Delivery
    payment: Payment
    utr: str | None = Field(default=None, max_length=100)
    free_pick: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    block: Block = "A"
    room: str | None = Field(default=None, max_length=20)
    # The total the customer saw (and may already have paid by UPI). If prices or offers changed
    # since, the order is refused with the new total instead of silently charging a different amount.
    expected_total: float | None = Field(default=None, ge=0, le=10_000_000)

    @field_validator("phone")
    @classmethod
    def _mobile(cls, value: str | None) -> str | None:
        return indian_mobile(value) if value else None

    @field_validator("utr")
    @classmethod
    def _utr(cls, value: str | None) -> str | None:
        return upi_reference(value) if value else None


class UtrIn(Stripped):
    utr: str = Field(max_length=100)

    @field_validator("utr")
    @classmethod
    def _utr(cls, value: str) -> str:
        return upi_reference(value)


class PaymentIn(CamelModel):
    received: bool


class OrderItemOut(CamelModel):
    name: str
    qty: int
    purchase_price: float


class OrderOut(CamelModel):
    id: int
    order_number: int
    created_at: int  # epoch milliseconds
    items: list[OrderItemOut]
    total: float
    delivery: Delivery
    details: str
    payment: Payment
    fulfilled: bool
    discount: float
    discount_label: str | None = None
    coupon: str | None = None
    utr: str | None = None
    payment_confirmed: bool = False
    cancelled: bool = False
    on_request: bool
    freebies: list[str]


class OrderCustomer(CamelModel):
    """Who placed an order and where it goes; shown to the shopkeeper only."""

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    block: Block | None = None
    room: str | None = None


class AdminOrderOut(OrderOut):
    customer: OrderCustomer


# --- site admin (/admin) ---


class SitePublic(Stripped):
    """Shop details every page uses: how to pay, whom to call, where to pick up, when the shop opens,
    which options are on, and the price rules. Set by the site admin."""

    upi_id: str = Field(default="7032767115@ibl", max_length=130)
    upi_name: str = Field(default="Mavidi Rajendra Prasad", min_length=1, max_length=60)
    shop_phone: str = Field(default="7032767115", max_length=20)
    help_phone: str = Field(default="9704535908", max_length=20)
    pickup_point: str = Field(default="Block B, Room 618", min_length=1, max_length=80)
    # Automatic store status: online from open_hour until close_hour (may wrap past midnight).
    open_hour: int = Field(default=23, ge=0, le=23)
    close_hour: int = Field(default=1, ge=0, le=23)
    upi_enabled: bool = True
    cash_enabled: bool = True
    pickup_enabled: bool = True
    room_delivery_enabled: bool = True
    delivery_fee: int = Field(default=10, ge=0, le=100)  # rupees, room delivery
    markup: int = Field(default=5, ge=0, le=100)  # rupees added to each item's MRP (eggs: once per order)

    @field_validator("upi_id")
    @classmethod
    def _upi_id(cls, value: str) -> str:
        if not UPI_ID_PATTERN.fullmatch(value):
            raise ValueError("Enter a UPI ID like 7032767115@ibl.")
        return value

    @field_validator("shop_phone", "help_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return mobile_digits(value)

    @model_validator(mode="after")
    def _consistent(self):
        if self.open_hour == self.close_hour:
            raise ValueError("Opening and closing hours must be different.")
        if not (self.upi_enabled or self.cash_enabled):
            raise ValueError("Keep at least one way to pay (UPI or Pay on Delivery).")
        if not (self.pickup_enabled or self.room_delivery_enabled):
            raise ValueError("Keep at least one way to get orders (Pickup or Room Delivery).")
        return self


class SiteValues(SitePublic):
    """Everything the site admin sets, including what only the server uses."""

    signups_open: bool = True


class SiteAdminOut(SiteValues):
    photo_uploads_enabled: bool


class StaffIn(Stripped):
    """A new admin or shopkeeper account, made by an admin."""

    phone: str = Field(max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["shopkeeper", "admin"]

    @field_validator("phone")
    @classmethod
    def _mobile(cls, value: str) -> str:
        return indian_mobile(value)


class AdminPasswordIn(Stripped):
    """An admin changing their own password. Spaces around a password are dropped here as at sign-in,
    so the password saved is the one that signs in."""

    current: str = Field(min_length=1, max_length=128)
    new: str = Field(min_length=8, max_length=128)


class BlockIn(CamelModel):
    blocked: bool


class NewPasswordIn(Stripped):
    """A new password an admin sets for someone else (a customer who forgot hers, a shopkeeper, an admin)."""

    password: str = Field(min_length=8, max_length=128)


class StoredProfile(CamelModel):
    """Hostel details as saved (not re-validated: older ones predate the mobile-number rule)."""

    full_name: str
    phone: str
    block: str
    room_number: str


class AdminUserOut(CamelModel):
    id: str
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    is_shopkeeper: bool
    is_admin: bool
    is_owner: bool  # the main admin from the server settings: can't be removed, blocked or reset here
    blocked: bool
    created_at: int  # epoch milliseconds
    profile: StoredProfile | None = None
    order_count: int
    spent: float  # orders not cancelled
    reset_requested_at: int | None = None  # she asked for a new password (epoch milliseconds)


class AdminOverview(CamelModel):
    customers: int
    shopkeepers: int
    admins: int
    blocked: int
    orders_today: int
    revenue_today: float
    open_orders: int
    awaiting_payment: int
    store_online: bool
    reset_requests: int  # customers waiting for a new password
