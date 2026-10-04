"""Request/response models. JSON uses camelCase to match the frontend types."""

import re
from typing import Annotated, Literal
from urllib.parse import parse_qs

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from . import webpush

CouponKind = Literal["free60", "three5", "freeSnack100", "halfDelivery", "four10", "premium5"]
OfferId = Literal["tier50", "tier100", "first", "bulk", "loyalty"]
Block = Literal["A", "B", "C"]
Delivery = Literal["Pickup", "Room Delivery"]
Payment = Literal["UPI", "Pay on Delivery"]
ManualPayment = Literal["Cash", "UPI"]  # how an in-person sale was paid
StoreOverride = Literal["auto", "online", "offline"]
# The Investment and Profit pages' periods, in the shop's dates: today, the last 7 days, this month, last
# month, all time.
InvestmentPeriod = Literal["today", "week", "month", "last_month", "all"]
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


def upi_qr_payee(text: str) -> str | None:
    """The UPI ID a UPI QR pays (upi://pay?pa=...&pn=...), or None when it isn't one."""
    scheme, _, query = text.partition("?")
    if scheme.lower() != "upi://pay" or any(c.isspace() for c in text):
        return None
    payee = (parse_qs(query).get("pa") or [""])[0]
    return payee if UPI_ID_PATTERN.fullmatch(payee) else None


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


class MobileIn(Stripped):
    """A customer's mobile number: her username, and how the shop reaches her (it never sends messages
    or codes by itself)."""

    mobile: str = Field(max_length=20)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, value: str) -> str:
        return indian_mobile(value)


class LoginIn(MobileIn):
    password: str = Field(min_length=1, max_length=128)


class SignupIn(MobileIn):
    password: str = Field(min_length=8, max_length=128)


class UserOut(CamelModel):
    id: str
    email: str | None = None  # an older customer account's email (no longer used)
    phone: str | None = None  # an admin's or shopkeeper's sign-in number
    mobile: str | None = None  # a customer's mobile number: her sign-in
    is_shopkeeper: bool  # signed in to the dashboard (as a shopkeeper or an admin)
    is_admin: bool = False  # signed in to /admin
    is_owner: bool = False  # the main admin from the server settings (ADMIN_MOBILE)
    first_order_available: bool


class AuthOut(CamelModel):
    token: str
    user: UserOut


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
    # Badges on the customer's shelf (only in the product list; left out when not true).
    popular: bool | None = None  # among the best sellers of the last two weeks
    is_new: bool | None = None  # added in the last week (the newest few)


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
    category: str | None = Field(default=None, min_length=1, max_length=40)
    image: str | None = Field(default=None, max_length=MAX_IMAGE_LENGTH)

    _check_price = field_validator("mrp")(classmethod(_price))


# --- promotions & store ---


class DailyOffer(CamelModel):
    """An offer card: its text (what customers read) and what checkout gives (see services.best_deal).
    The amounts the shop set apply; one that isn't set works as it always has (in brackets)."""

    id: OfferId
    title: str = Field(max_length=80)
    note: str = Field(max_length=200)
    icon: str = Field(max_length=16)
    active: bool
    percent: int | None = Field(default=None, ge=0, le=100)  # % off: first order (10), cart over ₹200 (20)
    free_delivery: bool | None = None  # cart over ₹200: no room delivery fee (no)
    gift: int | None = Field(default=None, ge=0, le=1000)  # a free chocolate worth ₹: ₹50+ (5), cart over ₹200 (none)
    pick_up_to: int | None = Field(default=None, ge=1, le=1000)  # ₹100+ and loyalty: a free item she picks, MRP up to ₹ (₹10 item)
    every: int | None = Field(default=None, ge=2, le=100)  # loyalty: every Nth completed order earns a free pick (10)


class WheelPrize(CamelModel):
    """A wheel slice. kind is what it gives (None: Better Luck; three5 and four10 are both "₹ off"); the
    cart it needs and the amount are the shop's (see services.coupon_terms). The label and short label
    are made from those when saved (services.prize_text), so what a slice says is what it gives."""

    code: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=80)
    short_label: str = Field(max_length=80)
    icon: str = Field(max_length=16)
    kind: CouponKind | None
    active: bool
    min_order: int | None = Field(default=None, ge=0, le=5000)  # ₹, items subtotal
    min_items: int | None = Field(default=None, ge=0, le=50)
    amount: int | None = Field(default=None, ge=1, le=500)  # ₹ off, or the free snack's value (not delivery)


class Promotions(CamelModel):
    launch_message: str = Field(max_length=200)
    daily_offers: list[DailyOffer] = Field(max_length=5)
    wheel_prizes: list[WheelPrize] = Field(min_length=2, max_length=16)
    coupon_rule: CouponRule = "best"
    # Customers see the wheel and can spin. Saving offers doesn't change it: PUT /admin/wheel does.
    wheel_enabled: bool | None = None


class WheelSwitch(CamelModel):
    enabled: bool


class SoldItem(CamelModel):
    name: str
    qty: int


class SalesFigures(CamelModel):
    count: int  # sales: orders, or manual sales
    items: int  # units sold
    revenue: float
    investment: float  # purchase cost (MRP) of the items sold; profit is revenue - investment


class SalesSummary(CamelModel):
    """All-time totals, computed by the database so the dashboard doesn't download every order. Online
    orders and manual (in-person) sales separately, and together in revenue, investment and sold."""

    order_count: int  # every order placed, cancelled and unpaid ones too
    revenue: float
    investment: float  # purchase cost of the items sold
    sold: list[SoldItem]
    online: SalesFigures
    manual: SalesFigures


# --- manual (in-person) sales ---


class ManualSaleItemIn(CamelModel):
    product_id: int = Field(gt=0, le=MAX_ID)
    quantity: int = Field(gt=0, le=1000)


class ManualSaleIn(Stripped):
    items: list[ManualSaleItemIn] = Field(min_length=1, max_length=50)
    payment: ManualPayment = "Cash"
    # What was received. Left out: the shop's prices (MRP + markup, eggs as online).
    amount: float | None = Field(default=None, ge=0, le=100_000)
    note: str | None = Field(default=None, max_length=100)  # e.g. who bought it

    _check_amount = field_validator("amount")(classmethod(_price))


class ManualSaleItemOut(CamelModel):
    name: str
    qty: int
    price: float  # the shop's price each when sold


class ManualSaleOut(CamelModel):
    id: int
    created_at: int  # epoch milliseconds
    items: list[ManualSaleItemOut]
    total: float
    investment: float
    payment: ManualPayment
    note: str | None = None
    recorded_by: str | None = None
    cancelled: bool


# --- investment (stock bought and stock left) ---


class InvestmentItem(CamelModel):
    name: str
    emoji: str | None = None
    qty: int
    value: float  # at MRP, what the shop pays


class StockEntryOut(CamelModel):
    """Stock added or taken off by hand on the dashboard (quick changes by one person are one entry)."""

    id: int
    created_at: int  # epoch milliseconds
    name: str
    change: int  # units added (+) or taken off (−)
    price: float  # MRP each at the time
    recorded_by: str | None = None


class StockLeft(CamelModel):
    items: int  # different items in stock
    units: int
    value: float  # at MRP, what the shop paid
    sale_value: float  # at the shop's prices (eggs at MRP each: their markup is per order)


class Investment(CamelModel):
    """New stock bought (and taken off) in a period, and the stock left now, valued at MRP."""

    period: InvestmentPeriod
    start: str | None = None  # the period's first day (the shop's date, YYYY-MM-DD); None: all time
    end: str  # its last day
    tracking_since: int | None = None  # when the first stock change was recorded (epoch ms); None: none yet
    bought: float
    bought_units: int
    bought_items: list[InvestmentItem]  # per item, biggest value first
    taken_off: float
    taken_off_units: int
    entries: list[StockEntryOut]  # newest first, at most the latest 200
    entry_count: int  # every entry in the period
    left: StockLeft
    left_items: list[InvestmentItem]  # per item in stock, biggest value first


# --- profit (the admin's Profit page) ---


class ProfitFigures(CamelModel):
    count: int  # sales: orders, manual sales, or both
    items: int  # units sold
    revenue: float  # money received
    cost: float  # what the items sold cost (their MRP)
    gifts: float  # free gifts given with orders, at their price
    profit: float  # revenue - cost - gifts


class ProfitDay(CamelModel):
    day: str  # the shop's date (YYYY-MM-DD) the day starts on (see Profit.day_starts_at)
    orders: int
    manual_sales: int
    revenue: float
    cost: float
    gifts: float
    profit: float


class ProfitItem(CamelModel):
    name: str
    emoji: str | None = None
    qty: int  # units sold (or, for the stock left, in stock)
    revenue: float  # at the shop's prices: what they sold for (or would sell for)
    cost: float  # at MRP
    profit: float


class Profit(CamelModel):
    """Profit in a period, day by day and item by item, and the profit still in the stock left. Orders count
    once paid (UPI once its payment is confirmed); cancelled orders and undone manual sales don't count.
    item_profit + delivery_fees - discounts + amount_changes - total.gifts = total.profit."""

    period: InvestmentPeriod
    start: str | None = None  # the period's first day (YYYY-MM-DD); None: all time
    end: str  # its last day
    # The hour (shop's time, 0-23) each day starts: halfway through the hours the shop is closed, so a
    # night's sales after midnight count with that night. Negative: that hour the evening before.
    day_starts_at: int
    total: ProfitFigures
    online: ProfitFigures
    manual: ProfitFigures
    days: list[ProfitDay]  # every day of the period (all time: from the first sale), newest first
    items: list[ProfitItem]  # per item sold, at the shop's prices, most profit first
    item_profit: float  # the items' profit together
    delivery_fees: float  # room delivery fees paid
    discounts: float  # offers, coupons and first-order discounts
    amount_changes: float  # manual sales: amounts received other than the shop's prices (+ more, - less)
    awaiting: int  # UPI orders in the period whose payment isn't confirmed yet (not counted)
    awaiting_money: float
    stock: StockLeft  # right now, whatever the period
    stock_items: list[ProfitItem]  # per item in stock, most profit first


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
    on_shelf: bool = False  # it's in stock right now: nothing to ask for (nothing was saved)


# --- spin wheel ---


class CouponOut(CamelModel):
    code: str
    label: str
    short_label: str
    icon: str
    kind: CouponKind
    expires_at: int  # epoch milliseconds
    # Its terms (see services.CouponTerms): the cart it needs, and what it gives.
    min_order: int  # ₹, items subtotal
    min_items: int
    amount: int  # ₹ off; freeSnack100: the free item's value; 0 for the delivery coupons
    pick_up_to: int  # freeSnack100: the free item's MRP up to ₹ (else 0)


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
    # Her loyalty reward, used on this order: the free item she picked (see LoyaltyOut).
    loyalty_pick: str | None = Field(default=None, max_length=80)

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


class FulfilIn(CamelModel):
    # Handing over a UPI order whose payment isn't ticked yet: the shopkeeper says the money arrived,
    # which confirms the payment in the same step.
    payment_received: bool = False


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


# --- order alerts (a notification for each new order; see webpush.py) ---


class AlertKeyOut(CamelModel):
    public_key: str | None  # what the browser subscribes with; None: alerts aren't set up on the server


class AlertKeys(Stripped):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class AlertDeviceIn(Stripped):
    """A browser's push subscription (PushSubscription.toJSON(): endpoint and keys)."""

    endpoint: str = Field(max_length=webpush.MAX_ENDPOINT_LENGTH)
    keys: AlertKeys

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return webpush.check_endpoint(value)

    @model_validator(mode="after")
    def _keys(self) -> "AlertDeviceIn":
        self.keys.p256dh, self.keys.auth = webpush.check_keys(self.keys.p256dh, self.keys.auth)
        return self


class AlertEndpointIn(Stripped):
    """Which device: its push address."""

    endpoint: str = Field(min_length=1, max_length=webpush.MAX_ENDPOINT_LENGTH)


class LoyaltyOut(CamelModel):
    """A customer's loyalty card: every `every` completed (handed over, not cancelled) orders earn a free
    item she picks at checkout, MRP up to ₹pick_up_to."""

    active: bool  # the shop's loyalty offer card is switched on
    every: int
    pick_up_to: int
    stamps: int  # completed orders toward the next reward (0 to every - 1)
    rewards: int  # rewards earned and not used yet


class AlertTestOut(CamelModel):
    sent: bool
    detail: str | None = None  # why it wasn't sent


# --- site admin (/admin) ---


class SitePublic(Stripped):
    """Shop details every page uses: how to pay, whom to call, where to pick up, when the shop opens,
    which options are on, and the price rules. Set by the site admin."""

    upi_id: str = Field(default="7032767115@ibl", max_length=130)
    upi_name: str = Field(default="Mavidi Rajendra Prasad", min_length=1, max_length=60)
    # What the shop's own UPI QR says (upi://pay?pa=...), read from the QR image uploaded at /admin.
    # Customers scan a QR with exactly this text. None: a QR made from upi_id and the amount instead.
    upi_qr: str | None = Field(default=None, max_length=1000)
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

    @field_validator("upi_qr")
    @classmethod
    def _upi_qr(cls, value: str | None) -> str | None:
        if value and upi_qr_payee(value) is None:
            raise ValueError("That isn't a UPI payment QR (it should start with upi://pay and name a UPI ID).")
        return value or None

    @field_validator("shop_phone", "help_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return mobile_digits(value)

    @model_validator(mode="after")
    def _consistent(self):
        payee = upi_qr_payee(self.upi_qr) if self.upi_qr else None
        if payee and payee.lower() != self.upi_id.lower():
            raise ValueError(f"The QR pays {payee} but the UPI ID is {self.upi_id}. Upload the QR for {self.upi_id}, or remove the QR.")
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
    email: str | None = None  # an older customer account's email (no longer used)
    phone: str | None = None  # staff sign-in number
    mobile: str | None = None  # a customer's sign-in number
    is_customer: bool
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
    manual_sales_today: int
    revenue_today: float  # online orders (paid) and manual sales
    stock_value: float  # the stock left, at MRP
    open_orders: int
    awaiting_payment: int
    store_online: bool
    reset_requests: int  # customers waiting for a new password
