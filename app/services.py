"""Business rules shared by the routers: pricing, offers, coupons, shop hours."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import MartSettings, Order, Product, Spin, User, utcnow
from .schemas import CouponOut, OrderItemOut, OrderOut, ProductOut, UserOut
from .security import has_shop_access, is_site_admin
from .seed import DEFAULT_WHEEL_PRIZES
from .staff import is_owner

# Defaults; the site admin can change the markup, delivery fee and hours at /admin (see site.py).
MARKUP = 5  # every item sells at MRP + ₹5 (eggs: + ₹5 once per bundle)
ROOM_DELIVERY_FEE = 10
PREMIUM_PRICE = 45
COUPON_HOURS = 48
# Rupees off for each coupon kind (freeSnack100 gives a free ₹10 item instead).
COUPON_VALUES = {"free60": 10, "three5": 5, "freeSnack100": 10, "halfDelivery": 5, "four10": 10, "premium5": 5}
OPEN_HOUR, CLOSE_HOUR = 23, 1  # 11:00 PM – 1:00 AM


def to_ms(value: datetime) -> int:
    return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)


def shop_now() -> datetime:
    return datetime.now(settings.tz)


def shop_date() -> str:
    return shop_now().date().isoformat()


def get_settings(db: Session) -> MartSettings:
    row = db.get(MartSettings, 1)
    if row is None:  # seeded at startup, so this only happens if someone deletes it
        raise RuntimeError("mart_settings row is missing")
    return row


def within_hours(hour: int, open_hour: int, close_hour: int) -> bool:
    """open_hour up to (not including) close_hour; the window may wrap past midnight (23 -> 1)."""
    if open_hour < close_hour:
        return open_hour <= hour < close_hour
    return hour >= open_hour or hour < close_hour


def store_online(row: MartSettings, open_hour: int = OPEN_HOUR, close_hour: int = CLOSE_HOUR) -> bool:
    if row.store_override == "online":
        return True
    if row.store_override == "offline":
        return False
    return within_hours(shop_now().hour, open_hour, close_hour)


def active_prizes(row: MartSettings) -> list[dict]:
    prizes = [prize for prize in row.wheel_prizes if prize.get("active")]
    return prizes if len(prizes) >= 2 else DEFAULT_WHEEL_PRIZES


# --- pricing ---
# All money maths is in integer paise, so totals are exact (no 0.1 + 0.2 surprises) and match the
# website's checkout preview (frontend/src/lib/pricing.ts) to the paisa. Keep the two in step.

ROOM_DELIVERY_FEE_PAISE = ROOM_DELIVERY_FEE * 100
# The free ₹10 item of a spin coupon, and of the ₹100+ offer unless the shop sets its own limit: any item
# with an MRP up to ₹12.
FREE_PICK_VALUE, FREE_PICK_MAX_PRICE = 10, 12
# What each offer gives unless the shop set its own amounts (see schemas.DailyOffer).
FIRST_ORDER_PERCENT, BULK_PERCENT, TIER50_GIFT = 10, 20, 5


def paise(rupees: float) -> int:
    return int((Decimal(str(rupees)) * 100).to_integral_value(ROUND_HALF_UP))


def rupees(amount: int) -> float:
    return amount / 100


def format_money(amount: int) -> str:
    """₹70 or ₹67.50 (amount in paise)."""
    return f"₹{amount // 100:,}" if amount % 100 == 0 else f"₹{amount / 100:,.2f}"


def percent_off(subtotal: int, percent: int) -> int:
    """A percentage of the subtotal, rounded half-up to whole rupees (2.5 -> 3), in paise."""
    return (subtotal * percent + 5000) // 10000 * 100


def is_egg(name: str) -> bool:
    return name.strip().lower() == "eggs"


def sale_price(product: Product, markup: int = MARKUP) -> float:
    """Price per item: MRP + markup (₹5), except eggs, which are MRP each (+ markup once per order)."""
    return product.purchase_price if is_egg(product.name) else product.purchase_price + markup


def line_total(product: Product, qty: int, markup: int = MARKUP) -> int:
    """In paise."""
    if is_egg(product.name):
        return paise(product.purchase_price) * qty + (markup * 100 if qty > 0 else 0)
    return (paise(product.purchase_price) + markup * 100) * qty


@dataclass
class Cart:
    subtotal: int  # paise
    item_count: int
    premium_count: int


def cart_summary(lines: list[tuple[Product, int]], markup: int = MARKUP) -> Cart:
    return Cart(
        subtotal=sum(line_total(product, qty, markup) for product, qty in lines),
        item_count=sum(qty for _, qty in lines),
        premium_count=sum(qty for product, qty in lines if paise(sale_price(product, markup)) >= PREMIUM_PRICE * 100),
    )


def coupon_eligible(kind: str, cart: Cart, delivery: str) -> bool:
    room = delivery == "Room Delivery"
    return {
        "free60": cart.subtotal >= 6000 and room,
        "three5": cart.item_count >= 3,
        "freeSnack100": cart.subtotal >= 10000,
        "halfDelivery": room,
        "four10": cart.item_count >= 4,
        "premium5": cart.premium_count >= 2,
    }.get(kind, False)


@dataclass
class Deal:
    kind: str | None = None  # "coupon", "bulk", "first", "tier100", "tier50"
    label: str | None = None
    discount: int = 0  # paise (a room delivery fee it waives included)
    coupon: Spin | None = None
    free_pick: bool = False  # the customer gets a free item of her choice...
    pick_value: int = FREE_PICK_VALUE  # ...worth ₹ (how the order names it)...
    pick_up_to: int = FREE_PICK_MAX_PRICE  # ...with an MRP up to ₹
    freebies: list[str] = field(default_factory=list)


def gift_text(rupees: int) -> str:
    """A free chocolate as an order lists it (the admin's Profit page reads the ₹ amount)."""
    return f"₹{rupees} chocolate (free)"


def coupon_value(kind: str, delivery_fee: int = ROOM_DELIVERY_FEE_PAISE) -> int:
    """Paise off for a coupon. The delivery coupons follow the delivery fee: free60 is the whole fee
    ("FREE Delivery"), halfDelivery half of it."""
    if kind == "free60":
        return delivery_fee
    if kind == "halfDelivery":
        return delivery_fee // 2
    return COUPON_VALUES[kind] * 100


def best_deal(
    row: MartSettings,
    cart: Cart,
    delivery: str,
    first_order: bool,
    coupon: Spin | None,
    delivery_fee: int = ROOM_DELIVERY_FEE_PAISE,
) -> Deal:
    """Only one reward applies per order. With COUPON_RULE "best" the one that saves the most wins
    (ties keep the automatic offer, so the coupon stays for later); with "coupon" an eligible spin
    coupon always wins. delivery_fee is in paise. The offers give the amounts the shop set on their cards
    (schemas.DailyOffer). Keep in step with quote() in frontend/src/lib/pricing.ts."""
    fee = delivery_fee if delivery == "Room Delivery" else 0
    offers = {offer["id"]: offer for offer in row.daily_offers}

    def active(offer_id: str) -> bool:
        return bool(offers.get(offer_id, {}).get("active"))

    def title(offer_id: str, fallback: str) -> str:
        return offers.get(offer_id, {}).get("title") or fallback

    def amount(offer_id: str, name: str, default):
        value = offers.get(offer_id, {}).get(name)
        return default if value is None else value

    # (savings, deal) in priority order; ties keep the earlier one.
    candidates: list[tuple[int, Deal]] = []
    if active("bulk") and cart.subtotal > 20000:
        percent = amount("bulk", "percent", BULK_PERCENT)
        off = percent_off(cart.subtotal, percent)
        waived = fee if amount("bulk", "freeDelivery", False) else 0
        gift = amount("bulk", "gift", 0)
        label = title("bulk", f"Bulk order {percent}% OFF")
        deal = Deal(kind="bulk", label=label, discount=off + waived, freebies=[gift_text(gift)] if gift else [])
        candidates.append((off + waived + gift * 100, deal))
    if active("first") and first_order:
        percent = amount("first", "percent", FIRST_ORDER_PERCENT)
        off = percent_off(cart.subtotal, percent)
        candidates.append((off, Deal(kind="first", label=title("first", f"First order {percent}% OFF"), discount=off)))
    if active("tier100") and cart.subtotal >= 10000:
        up_to = amount("tier100", "pickUpTo", None)
        value, limit = (FREE_PICK_VALUE, FREE_PICK_MAX_PRICE) if up_to is None else (up_to, up_to)
        deal = Deal(kind="tier100", label=title("tier100", "₹100+ Offer"), free_pick=True, pick_value=value, pick_up_to=limit)
        candidates.append((value * 100, deal))
    elif active("tier50") and cart.subtotal >= 5000:
        gift = amount("tier50", "gift", TIER50_GIFT)
        candidates.append((gift * 100, Deal(kind="tier50", label=title("tier50", "₹50+ Offer"), freebies=[gift_text(gift)] if gift else [])))

    if coupon and coupon.kind and coupon_eligible(coupon.kind, cart, delivery):
        if coupon.kind == "freeSnack100":
            savings, deal = FREE_PICK_VALUE * 100, Deal(kind="coupon", label=coupon.label, coupon=coupon, free_pick=True)
        else:
            savings = min(coupon_value(coupon.kind, delivery_fee), cart.subtotal + fee)
            deal = Deal(kind="coupon", label=coupon.label, discount=savings, coupon=coupon)
        if row.coupon_rule == "coupon":
            return deal
        candidates.append((savings, deal))

    best, best_savings = Deal(), 0
    for savings, deal in candidates:
        if savings > best_savings:
            best, best_savings = deal, savings
    return best


# --- coupons ---


def current_coupon(db: Session, user: User) -> Spin | None:
    """The customer's latest win, if unused and unexpired. A "Better Luck" spin doesn't erase it;
    a newer win replaces it (so she holds at most one coupon)."""
    latest_win = db.scalar(
        select(Spin)
        .where(Spin.user_id == user.id, Spin.kind.is_not(None))
        .order_by(Spin.created_at.desc(), Spin.id.desc())
        .limit(1)
    )
    if latest_win is None or latest_win.used_at is not None:
        return None
    if latest_win.expires_at is None or latest_win.expires_at <= utcnow():
        return None
    return latest_win


def coupon_out(spin: Spin | None) -> CouponOut | None:
    if spin is None or spin.kind is None or spin.expires_at is None:
        return None
    return CouponOut(
        code=spin.code,
        label=spin.label,
        short_label=spin.label,
        icon=spin.icon,
        kind=spin.kind,  # type: ignore[arg-type]
        expires_at=to_ms(spin.expires_at),
    )


# --- serializers ---


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        phone=user.phone,
        mobile=user.mobile,
        # What this sign-in opens (a shopkeeper account can't sign in as a customer: it isn't one).
        is_shopkeeper=has_shop_access(user),
        is_admin=is_site_admin(user),
        is_owner=is_site_admin(user) and is_owner(user),
        first_order_available=not user.first_order_used,
    )


def product_out(product: Product) -> ProductOut:
    return ProductOut(
        id=product.id,
        name=product.name,
        emoji=product.emoji,
        mrp=product.purchase_price,
        stock=product.stock,
        threshold=product.threshold,
        category=product.category,
        image=product.image_url,
    )


def order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        order_number=order.id,
        created_at=to_ms(order.created_at),
        items=[OrderItemOut(name=i.product_name, qty=i.quantity, purchase_price=i.purchase_price) for i in order.items],
        total=order.total,
        delivery=order.delivery,  # type: ignore[arg-type]
        details=order.details,
        payment=order.payment,  # type: ignore[arg-type]
        fulfilled=order.fulfilled,
        discount=order.discount,
        discount_label=order.discount_label,
        coupon=order.coupon,
        utr=order.utr,
        payment_confirmed=bool(order.payment_confirmed),
        cancelled=bool(order.cancelled),
        on_request=order.on_request,
        freebies=list(order.freebies or []),
    )
