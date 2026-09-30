from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .. import alerts, site, sync
from ..database import get_db, takes_turns
from ..models import Order, OrderItem, Product, Profile, Spin, User, utcnow
from ..schemas import IdPath, OrderIn, OrderOut, UtrIn
from ..security import current_user
from ..services import (
    best_deal,
    cart_summary,
    current_coupon,
    format_money,
    get_settings,
    loyalty,
    loyalty_pick_text,
    order_out,
    paise,
    rupees,
    sale_price,
    store_online,
)

router = APIRouter(prefix="/orders", tags=["orders"])


def _conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


def _label(order_id: int) -> str:
    return f"#{order_id:04d}"


def _check_utr_unused(db: Session, utr: str, order_id: int | None = None) -> None:
    """One UPI payment pays for one order: the same reference can't be sent for a second order."""
    query = select(Order.id).where(Order.utr == utr, Order.cancelled.is_(False))
    if order_id is not None:
        query = query.where(Order.id != order_id)
    other = db.scalar(query.limit(1))
    if other is not None:
        raise _conflict(
            f"This UPI reference is already on order {_label(other)}. Each payment can pay for one order only: "
            "check the reference in your UPI app."
        )


def _free_pick(db: Session, choice: str | None, up_to: int) -> Product | None:
    """The free item she picked (MRP up to ₹up_to), if it's on sale and in stock (a generic one is given otherwise)."""
    if not choice:
        return None
    return db.scalar(
        select(Product)
        .where(Product.name == choice, Product.active, Product.stock >= 1, Product.purchase_price <= up_to)
        .order_by(Product.id)
        .limit(1)
    )


def _take(db: Session, product_id: int, quantity: int) -> bool:
    """Take stock only if there's enough (conditional, so two orders can't both take the last one)."""
    taken = db.execute(
        update(Product).where(Product.id == product_id, Product.stock >= quantity).values(stock=Product.stock - quantity)
    )
    return taken.rowcount == 1


@router.get("", response_model=list[OrderOut], response_model_exclude_none=True)
def my_orders(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[OrderOut]:
    orders = db.scalars(
        select(Order).where(Order.user_id == user.id).options(selectinload(Order.items)).order_by(Order.id.desc())
    )
    return [order_out(order) for order in orders]


@router.post("", response_model=OrderOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
@takes_turns
def place_order(body: OrderIn, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)) -> OrderOut:
    # Every write takes its locks in one order: the customer, then the order, then products (by
    # number), then spin coupons, then the sync counters. Waiting only in that direction, two writes
    # can never deadlock. Locking her first also makes her own simultaneous orders take turns, and an
    # admin deleting her account at that moment can't miss this order.
    her = select(User.id).where(User.id == user.id, User.deleted_at.is_(None)).with_for_update(key_share=True)
    if db.scalar(her) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in again.")
    shop = site.values(db)
    turned_off = {
        "UPI": not shop.upi_enabled,
        "Pay on Delivery": not shop.cash_enabled,
        "Pickup": not shop.pickup_enabled,
        "Room Delivery": not shop.room_delivery_enabled,
    }
    for choice in (body.payment, body.delivery):
        if turned_off[choice]:
            raise _conflict(f"{choice} is turned off right now. Please choose another option.")
    if body.payment == "UPI" and not body.utr:
        # Orders keep their stock aside until paid; one unpaid UPI order at a time.
        unpaid = db.scalar(
            select(Order.id)
            .where(
                Order.user_id == user.id,
                Order.payment == "UPI",
                Order.utr.is_(None),
                Order.payment_confirmed.is_(False),
                Order.fulfilled.is_(False),
                Order.cancelled.is_(False),
            )
            .limit(1)
        )
        if unpaid is not None:
            raise _conflict(
                f"Order {_label(unpaid)} is still waiting for your UPI payment. Pay for it first "
                "(My Orders > Pay now), or choose Pay on Delivery."
            )
    if body.utr:
        _check_utr_unused(db, body.utr)
    quantities: dict[int, int] = {}
    for item in body.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(quantities), Product.active))}
    if len(products) != len(quantities):
        raise _conflict("A product in your cart is no longer available. Please refresh the shop.")
    lines = [(products[product_id], qty) for product_id, qty in quantities.items()]
    for product, qty in lines:
        if qty > product.stock:
            raise _conflict(f"Only {product.stock} {product.name} left. Please update your cart.")

    if body.delivery == "Room Delivery":
        if not (body.name and body.phone and body.room):
            raise HTTPException(422, "Name, contact number and room are required for room delivery.")
        details = f"{body.name}, {body.phone}, Block {body.block}, Room {body.room}"
        contact = (body.name, body.phone, body.block, body.room)
    else:
        details = shop.pickup_point
        # Pickup: the shopkeeper still needs to know who is coming, from her saved hostel details.
        profile = db.get(Profile, user.id)
        contact = (profile.full_name, profile.phone, profile.block, profile.room_number) if profile else (None, None, None, None)

    row = get_settings(db)
    cart = cart_summary(lines, shop.markup)
    delivery_fee = shop.delivery_fee * 100
    deal = best_deal(row, cart, body.delivery, not user.first_order_used, current_coupon(db, user), delivery_fee)
    fee = delivery_fee if body.delivery == "Room Delivery" else 0
    discount = max(0, min(deal.discount, cart.subtotal + fee))
    total = cart.subtotal + fee - discount
    if body.expected_total is not None and paise(body.expected_total) != total:
        # She may already have paid the old amount by UPI: never record a different one silently.
        raise _conflict(
            f"Prices or offers changed while you were checking out. Your new total is {format_money(total)}. "
            "Please check your cart and place the order again."
        )

    order = Order(
        user_id=user.id,
        total=rupees(total),
        delivery=body.delivery,
        details=details,
        payment=body.payment,
        discount=rupees(discount),
        discount_label=deal.label,
        coupon=deal.coupon.code if deal.coupon else None,
        utr=(body.utr or None) if body.payment == "UPI" else None,
        on_request=not store_online(row, shop.open_hour, shop.close_hour),
        freebies=list(deal.freebies),
        customer_name=contact[0],
        customer_phone=contact[1],
        customer_block=contact[2],
        customer_room=contact[3],
    )
    # Her loyalty reward, if she picked its free item. Counted under her lock (above), so two orders at
    # once can't both spend one reward.
    reward, reward_up_to = None, 0
    if body.loyalty_pick:
        card = loyalty(db, user.id, row)
        if not card.active:
            raise _conflict("The loyalty offer is switched off right now. Please place your order without the free item.")
        if card.rewards < 1:
            raise _conflict("You don't have a loyalty reward to use yet. Please place your order without the free item.")
        reward, reward_up_to = _free_pick(db, body.loyalty_pick, card.pick_up_to), card.pick_up_to
        if reward is None:
            raise _conflict(f"{body.loyalty_pick} can't be your free item right now. Please pick another one.")
    # Stock is taken in product-number order (the lock order above), the free picks included.
    free_pick = _free_pick(db, body.free_pick, deal.pick_up_to) if deal.free_pick else None
    given = None
    for product_id in sorted({*quantities, *([free_pick.id] if free_pick else []), *([reward.id] if reward else [])}):
        if product_id in quantities and not _take(db, product_id, quantities[product_id]):
            db.rollback()
            raise _conflict(f"{products[product_id].name} just sold out. Please update your cart.")
        if free_pick is not None and product_id == free_pick.id and _take(db, product_id, 1):
            given = free_pick.name
        if reward is not None and product_id == reward.id and not _take(db, product_id, 1):
            db.rollback()
            raise _conflict(f"{reward.name} just ran out. Please pick another free item.")
    for product, qty in lines:
        order.items.append(
            OrderItem(
                product_id=product.id,
                product_name=product.name,
                quantity=qty,
                purchase_price=product.purchase_price,
                sale_price=sale_price(product, shop.markup),
            )
        )
    if deal.free_pick:
        # The admin's Profit page and cancelling read these (siteadmin.picked_item).
        order.freebies.append(f"{given} (free ₹{deal.pick_value} pick)" if given else f"₹{deal.pick_value} free snack")
    if reward is not None:
        order.freebies.append(loyalty_pick_text(reward.name, reward_up_to))  # read by services.loyalty
    # Conditional updates: two orders sent at the same moment can't both use one coupon or the
    # first-order discount.
    if deal.coupon:
        used = db.execute(
            update(Spin).where(Spin.id == deal.coupon.id, Spin.used_at.is_(None)).values(used_at=utcnow())
        )
        if used.rowcount != 1:
            db.rollback()
            raise _conflict("Your spin coupon was just used on another order. Please check your cart again.")
    first = db.execute(
        update(User).where(User.id == user.id, User.first_order_used.is_(False)).values(first_order_used=True)
    )
    if deal.kind == "first" and first.rowcount != 1:
        db.rollback()
        raise _conflict("Your first-order discount was already used on another order. Please check your cart again.")
    db.add(order)
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    # The shopkeepers' phones and laptops with order alerts on get one, after the order is saved.
    background.add_task(alerts.new_order, order.id)
    return order_out(order)


@router.post("/{order_id}/utr", response_model=OrderOut, response_model_exclude_none=True)
@takes_turns
def report_payment(
    order_id: IdPath, body: UtrIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> OrderOut:
    """After paying by UPI, the customer adds the payment reference so the shopkeeper can find it."""
    # Her, then the order (the lock order of place_order): her own payment reports take turns, so one
    # reference can't be put on two orders at once, and a cancel or confirmation of this order waits.
    db.execute(select(User.id).where(User.id == user.id).with_for_update(key_share=True))
    order = db.get(Order, order_id, options=[selectinload(Order.items)], with_for_update=True)
    if order is None or order.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.cancelled:
        raise _conflict("This order was cancelled.")
    if order.payment != "UPI":
        raise _conflict("This order is paid on delivery, not by UPI.")
    if order.payment_confirmed:
        raise _conflict("The shopkeeper has already confirmed your payment for this order.")
    if order.fulfilled:
        raise _conflict("This order has already been handed over.")
    _check_utr_unused(db, body.utr, order.id)
    order.utr = body.utr
    sync.bump(db, sync.ORDERS)
    db.commit()
    return order_out(order)
