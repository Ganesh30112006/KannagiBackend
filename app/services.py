"""Business rules shared by the routers: pricing, offers, coupons, shop hours."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from .config import settings
from .models import MartSettings, Order, OrderItem, Product, Spin, User, utcnow
from .schemas import CouponOut, LoyaltyOut, OrderItemOut, OrderOut, ProductOut, UserOut
from .security import has_shop_access, is_site_admin
from .seed import DEFAULT_WHEEL_PRIZES
from .staff import is_owner

# Defaults; the site admin can change the delivery fee and hours at /admin (see site.py), and the shop
# each item's markup on its card.
MARKUP = 5  # a new item sells at MRP + ₹5 (eggs: + ₹5 once per bundle)
ROOM_DELIVERY_FEE = 10
PREMIUM_PRICE = 45
COUPON_HOURS = 48
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
    return [canonical_prize(prize) for prize in (prizes if len(prizes) >= 2 else DEFAULT_WHEEL_PRIZES)]


# --- pricing ---
# All money maths is in integer paise, so totals are exact (no 0.1 + 0.2 surprises) and match the
# website's checkout preview (frontend/src/lib/pricing.ts) to the paisa. Keep the two in step.

ROOM_DELIVERY_FEE_PAISE = ROOM_DELIVERY_FEE * 100
# The free ₹10 item of a spin coupon, and of the ₹100+ offer unless the shop sets its own limit: any item
# with an MRP up to ₹12.
FREE_PICK_VALUE, FREE_PICK_MAX_PRICE = 10, 12
# What each offer gives unless the shop set its own amounts (see schemas.DailyOffer).
FIRST_ORDER_PERCENT, BULK_PERCENT, TIER50_GIFT = 10, 20, 5
# Loyalty: every 10th completed order earns a free item she picks, MRP up to ₹10 (the card's amounts).
LOYALTY_EVERY, LOYALTY_PICK_UP_TO = 10, 10
# How an order records the loyalty reward it used: "Munch (loyalty free ₹10 pick)".
LOYALTY_PICK = re.compile(r".+ \(loyalty free ₹\d+(?:\.\d+)? pick\)")


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


def markup_of(product: Product) -> int:
    """The item's own markup in rupees (set on its card)."""
    return MARKUP if product.markup is None else product.markup


def sale_price(product: Product) -> float:
    """Price per item: MRP + its markup, except eggs, which are MRP each (+ their markup once per order)."""
    return product.purchase_price if is_egg(product.name) else product.purchase_price + markup_of(product)


def line_total(product: Product, qty: int) -> int:
    """In paise."""
    if is_egg(product.name):
        return paise(product.purchase_price) * qty + (markup_of(product) * 100 if qty > 0 else 0)
    return (paise(product.purchase_price) + markup_of(product) * 100) * qty


@dataclass
class Cart:
    subtotal: int  # paise
    item_count: int
    premium_count: int


def cart_summary(lines: list[tuple[Product, int]]) -> Cart:
    return Cart(
        subtotal=sum(line_total(product, qty) for product, qty in lines),
        item_count=sum(qty for _, qty in lines),
        premium_count=sum(qty for product, qty in lines if paise(sale_price(product)) >= PREMIUM_PRICE * 100),
    )


# --- spin coupons ---
# A wheel slice gives one kind of reward, on the cart conditions the shop set on it (minOrder: items
# subtotal ₹, minItems, amount ₹). A won coupon keeps the terms of its slice at that moment. Keep in step
# with couponTerms() in frontend/src/lib/pricing.ts.
DELIVERY_COUPONS = ("free60", "halfDelivery")  # room delivery only: the whole fee, or half of it
PREMIUM_ITEMS = 2  # premium5: on 2 items of ₹45+
# (minOrder, minItems, amount) of each kind when a slice or coupon doesn't set them: the original rules.
# three5 and four10 are both "₹ off" (the shop sets its conditions).
COUPON_DEFAULTS = {
    "free60": (60, 0, 0),
    "halfDelivery": (0, 0, 0),
    "three5": (0, 3, 5),
    "four10": (0, 4, 10),
    "premium5": (0, 0, 5),
    "freeSnack100": (100, 0, FREE_PICK_VALUE),
}


@dataclass(frozen=True)
class CouponTerms:
    kind: str
    min_order: int  # ₹, items subtotal
    min_items: int
    amount: int  # ₹ off; freeSnack100: the free item's value; 0 for the delivery coupons
    pick_up_to: int  # freeSnack100: the free item's MRP up to ₹ (else 0)


def coupon_terms(kind: str, min_order: int | None = None, min_items: int | None = None, amount: int | None = None) -> CouponTerms:
    default_order, default_items, default_amount = COUPON_DEFAULTS[kind]
    value = 0 if kind in DELIVERY_COUPONS else default_amount if amount is None else amount
    # A slice that doesn't set the free item's value keeps the original rule: ₹10, any item up to ₹12.
    up_to = (FREE_PICK_MAX_PRICE if amount is None else amount) if kind == "freeSnack100" else 0
    return CouponTerms(
        kind=kind,
        min_order=default_order if min_order is None else min_order,
        min_items=default_items if min_items is None else min_items,
        amount=value,
        pick_up_to=up_to,
    )


def prize_terms(prize: dict) -> CouponTerms | None:
    """A wheel slice's reward (None: Better Luck)."""
    kind = prize.get("kind")
    if kind not in COUPON_DEFAULTS:
        return None
    return coupon_terms(kind, prize.get("minOrder"), prize.get("minItems"), prize.get("amount"))


def spin_terms(spin: Spin) -> CouponTerms | None:
    if spin.kind not in COUPON_DEFAULTS:
        return None
    return coupon_terms(spin.kind, spin.min_order, spin.min_items, spin.amount)


def prize_text(terms: CouponTerms | None) -> tuple[str, str]:
    """What a slice says (its label, and the short text on the wheel), made from what it gives, so the
    two always match. Keep in step with prizeText() in frontend/src/lib/pricing.ts."""
    if terms is None:
        return "Better Luck Next Time", "BETTER LUCK!"
    amount = terms.amount
    gives, short = {
        "free60": ("FREE Delivery", "FREE DELIVERY"),
        "halfDelivery": ("50% OFF Room Delivery", "½ DELIVERY"),
        "premium5": (f"₹{amount} OFF on {PREMIUM_ITEMS} Premium Items", f"{PREMIUM_ITEMS} PREMIUM ₹{amount} OFF"),
        "freeSnack100": (f"Free ₹{amount} Snack", f"FREE ₹{amount} SNACK"),
    }.get(terms.kind, (f"₹{amount} OFF", f"₹{amount} OFF"))
    if terms.min_order:
        gives, short = f"{gives} on ₹{terms.min_order}+ Orders", f"{short} ₹{terms.min_order}+"
    if terms.min_items:
        gives, short = f"{gives} with {terms.min_items}+ Items", f"{short} · {terms.min_items}+ ITEMS"
    return gives, short


def canonical_prize(prize: dict) -> dict:
    """A slice as stored: its amounts written out (the original ones when it had none), "₹ off" as
    three5, and its text made from them."""
    kind = prize.get("kind")
    if kind == "four10":
        kind = "three5"  # both are "₹ off"; four10's own amounts are written out below
    terms = prize_terms(prize)
    if terms is not None and kind != terms.kind:
        terms = coupon_terms(kind, terms.min_order, terms.min_items, terms.amount)
    label, short = prize_text(terms)
    return {
        "code": prize["code"],
        "label": label,
        "shortLabel": short,
        "icon": prize.get("icon", ""),
        "kind": kind if terms is not None else None,
        "active": bool(prize.get("active")),
        "minOrder": terms.min_order if terms else None,
        "minItems": terms.min_items if terms else None,
        "amount": terms.amount if terms and terms.kind not in DELIVERY_COUPONS else None,
    }


def coupon_eligible(terms: CouponTerms, cart: Cart, delivery: str) -> bool:
    if terms.kind in DELIVERY_COUPONS and delivery != "Room Delivery":
        return False
    if terms.kind == "premium5" and cart.premium_count < PREMIUM_ITEMS:
        return False
    return cart.subtotal >= terms.min_order * 100 and cart.item_count >= terms.min_items


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


def coupon_value(terms: CouponTerms, delivery_fee: int = ROOM_DELIVERY_FEE_PAISE) -> int:
    """Paise off for a coupon. The delivery coupons follow the delivery fee: free60 is the whole fee
    ("FREE Delivery"), halfDelivery half of it."""
    if terms.kind == "free60":
        return delivery_fee
    if terms.kind == "halfDelivery":
        return delivery_fee // 2
    return terms.amount * 100


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

    terms = spin_terms(coupon) if coupon else None
    if coupon and terms is not None and coupon_eligible(terms, cart, delivery):
        if terms.kind == "freeSnack100":
            savings = terms.amount * 100
            deal = Deal(kind="coupon", label=coupon.label, coupon=coupon, free_pick=True, pick_value=terms.amount, pick_up_to=terms.pick_up_to)
        else:
            savings = min(coupon_value(terms, delivery_fee), cart.subtotal + fee)
            deal = Deal(kind="coupon", label=coupon.label, discount=savings, coupon=coupon)
        # A coupon that would save nothing here (free room delivery when delivery is already free) is kept.
        if savings > 0 and row.coupon_rule == "coupon":
            return deal
        candidates.append((savings, deal))

    best, best_savings = Deal(), 0
    for savings, deal in candidates:
        if savings > best_savings:
            best, best_savings = deal, savings
    return best


# --- loyalty ---


def loyalty_pick_text(name: str, up_to: int) -> str:
    return f"{name} (loyalty free ₹{up_to} pick)"


def loyalty_terms(row: MartSettings) -> tuple[bool, int, int]:
    """(switched on, every how many completed orders, the free pick's MRP up to ₹), from its offer card."""
    card = next((offer for offer in row.daily_offers if offer.get("id") == "loyalty"), None)
    if card is None:
        return False, LOYALTY_EVERY, LOYALTY_PICK_UP_TO
    every, up_to = card.get("every"), card.get("pickUpTo")
    return bool(card.get("active")), LOYALTY_EVERY if every is None else every, LOYALTY_PICK_UP_TO if up_to is None else up_to


def loyalty(db: Session, user_id: str, row: MartSettings) -> LoyaltyOut:
    """Her card, counted from her orders: completed ones (handed over, not cancelled) earn stamps, and an
    order that used a reward (not cancelled) spends one. Nothing else is stored, so cancelling an order
    that used a reward gives it back, and changing "every" on the card applies at once."""
    active, every, up_to = loyalty_terms(row)
    completed = used = 0
    for fulfilled, freebies in db.execute(
        select(Order.fulfilled, Order.freebies).where(Order.user_id == user_id, Order.cancelled.is_(False))
    ):
        completed += bool(fulfilled)
        used += sum(1 for gift in freebies or [] if isinstance(gift, str) and LOYALTY_PICK.fullmatch(gift))
    return LoyaltyOut(active=active, every=every, pick_up_to=up_to, stamps=completed % every, rewards=max(0, completed // every - used))


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
    terms = spin_terms(spin) if spin is not None else None
    if spin is None or terms is None or spin.expires_at is None:
        return None
    return CouponOut(
        code=spin.code,
        label=spin.label,
        short_label=spin.label,
        icon=spin.icon,
        kind=spin.kind,  # type: ignore[arg-type]
        expires_at=to_ms(spin.expires_at),
        min_order=terms.min_order,
        min_items=terms.min_items,
        amount=terms.amount,
        pick_up_to=terms.pick_up_to,
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


# A free item taken from stock, as an order lists it: one she picked ("Munch (free ₹10 pick)", with her
# loyalty reward "Munch (loyalty free ₹10 pick)"), or the item the shop gave for a free chocolate or
# snack ("5 Star Mini (free ₹5 chocolate)", "Munch (free ₹10 snack)").
FREE_ITEM = re.compile(r"(.+) \((?:loyalty )?free ₹\d+(?:\.\d+)? (?:pick|chocolate|snack)\)")
# A free chocolate or snack before the shop says which item it gave ("₹5 chocolate (free)", "₹10 free
# snack"), and after ("5 Star Mini (free ₹5 chocolate)").
GIFT = re.compile(r"₹(\d+(?:\.\d+)?) (chocolate \(free\)|free snack)")
GIVEN = re.compile(r"(.+) \(free ₹(\d+(?:\.\d+)?) (chocolate|snack)\)")


def picked_item(freebie: object) -> str | None:
    """The item's name, when an order's freebie is an item taken from stock."""
    match = FREE_ITEM.fullmatch(freebie) if isinstance(freebie, str) else None
    return match.group(1) if match else None


def name_key(name: str) -> str:
    """Item names match whatever the capitals and spacing ("Dark  Fantasy" = "dark fantasy")."""
    return " ".join(name.lower().split())


def item_named(db: Session, name: str, *, besides: int | None = None) -> Product | None:
    """The item on sale with this name (capitals and spacing aside), other than item `besides`."""
    key = name_key(name)
    for product in db.scalars(select(Product).where(Product.active).order_by(Product.id)):
        if product.id != besides and name_key(product.name) == key:
            return product
    return None


def free_stock(db: Session, orders: list[tuple[list | None, list | None]]) -> list[int]:
    """The items the orders' freebies took from stock, one product id per unit, from each order's
    (freebies, free_items). Orders from before free_items was kept name the item only: the first item on
    sale with that name stands in."""
    taken: list[int] = []
    unknown: list[str] = []
    for freebies, free_items in orders:
        ids = free_items or []
        for index, gift in enumerate(freebies or []):
            if not (name := picked_item(gift)):
                continue
            product_id = ids[index] if index < len(ids) else None
            if isinstance(product_id, int):
                taken.append(product_id)
            else:
                unknown.append(name)
    if unknown:
        ids_by_name: dict[str, int] = {}
        for product_id, name in db.execute(select(Product.id, Product.name).where(Product.name.in_(unknown), Product.active).order_by(Product.id)).tuples():
            ids_by_name.setdefault(name, product_id)
        taken += [ids_by_name[name] for name in unknown if name in ids_by_name]
    return taken


def put_back(db: Session, back: dict[int, int]) -> None:
    """Units back on the shelf (a cancelled order, an undone sale). An item deleted since gives them to the
    item on sale with its name, if there is one (deleted and added again); otherwise they stay with the
    deleted item, which nothing counts."""
    deleted = {product.id: product.name for product in db.scalars(select(Product).where(Product.id.in_(back), Product.active.is_(False)))} if back else {}
    target: dict[int, int] = {}
    for product_id, quantity in back.items():
        if product_id in deleted and (same := item_named(db, deleted[product_id])) is not None:
            product_id = same.id
        target[product_id] = target.get(product_id, 0) + quantity
    for product_id in sorted(target):  # the lock order of every write (see orders.place_order)
        db.execute(update(Product).where(Product.id == product_id).values(stock=Product.stock + target[product_id]))


def held_stock(db: Session) -> dict[int, int]:
    """Units of each item in orders not handed over yet (and not cancelled), free items included. They
    came off the stock when ordered but are still on the shop's shelf."""
    still_open = and_(Order.fulfilled.is_(False), Order.cancelled.is_(False))
    lines = (
        select(OrderItem.product_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .where(still_open, OrderItem.product_id.is_not(None))
        .group_by(OrderItem.product_id)
    )
    held = {product_id: int(quantity) for product_id, quantity in db.execute(lines).tuples()}
    for product_id in free_stock(db, list(db.execute(select(Order.freebies, Order.free_items).where(still_open)).tuples())):
        held[product_id] = held.get(product_id, 0) + 1
    return held


def product_out(product: Product, held: int | None = None) -> ProductOut:
    return ProductOut(
        held=held,
        id=product.id,
        name=product.name,
        emoji=product.emoji,
        mrp=product.purchase_price,
        markup=markup_of(product),
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
