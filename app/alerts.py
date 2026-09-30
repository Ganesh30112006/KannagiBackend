"""Notifications on the phones and laptops where someone turned them on, even with the website closed
(Web Push, webpush.py); this decides who gets what:

- shopkeepers and admins: each new order (the dashboard's Order alerts);
- admins: the day's summary at closing time;
- customers: their order confirmed, ready or cancelled, and an item they asked for arriving in the shop.

Every send is a background task after the change is saved: a notification that fails never undoes it."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Literal
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, selectinload

from . import site, webpush
from .config import settings
from .database import SessionLocal
from .models import AlertDevice, Order, Product, User, utcnow
from .schemas import AlertTestOut
from .security import _password_version
from .services import format_money, paise, shop_now

logger = logging.getLogger("kannagi")

# A phone that's offline gets an order's alert when it's back within this time; after that it's old news.
ORDER_ALERT_TTL = 60 * 60
# A customer's news keeps longer: her phone may be off until morning.
CUSTOMER_TTL = 12 * 60 * 60
SUMMARY_TTL = 6 * 60 * 60
# Each push service gets this long to answer. The change is already saved; this only bounds the wait.
SEND_TIMEOUT = 5
MAX_PARALLEL = 8
# Devices one account can have alerts on: turning them on on another one drops the one unused longest.
MAX_DEVICES = 10
GONE = (404, 410)  # the browser turned alerts off, was reset or uninstalled: its address no longer works
STAFF = ("shopkeeper", "admin")
LOW_STOCK_SHOWN = 4

OrderEvent = Literal["confirmed", "fulfilled", "cancelled"]


def signing_key():
    """The shop's VAPID key, or None when alerts aren't set up (or the setting isn't a key)."""
    try:
        return settings.vapid_key
    except ValueError:
        return None


def public_key() -> str | None:
    key = signing_key()
    return webpush.public_key(key) if key is not None else None


def signed_in(device: AlertDevice, user: User, now: datetime) -> bool:
    """Whether the sign-in that turned alerts on is still good: the same checks as every request."""
    role_kept = {"admin": user.is_admin, "shopkeeper": user.is_shopkeeper, "customer": user.is_customer}.get(device.role, False)
    return (
        user.deleted_at is None
        and not user.blocked
        and role_kept
        and device.password_version == _password_version(user)
        and device.session_expires_at > now
    )


def _opens(device: AlertDevice) -> str:
    """Where tapping it goes: the dashboard, /admin for an admin's sign-in, the shop for a customer."""
    return {"admin": "/admin", "shopkeeper": "/dashboard"}.get(device.role, "/shop")


def order_label(order_id: int) -> str:
    return f"#{order_id:04d}"


def order_message(order: Order) -> dict[str, str]:
    """What the alert says: order number and amount, who and how many items, where and how it's paid."""
    count = sum(item.quantity for item in order.items)
    who = " ".join((order.customer_name or "").split())[:40] or "A customer"
    if order.delivery == "Room Delivery":
        place = f"Room delivery: Block {order.customer_block}, Room {order.customer_room}" if order.customer_room else "Room delivery"
    else:
        place = "Pickup"
    lines = [f"{who} · {count} item{'' if count == 1 else 's'}", f"{place} · {'UPI' if order.payment == 'UPI' else 'Pay on delivery'}"]
    if order.on_request:
        lines.append("Store offline: ordered on request")
    return {"title": f"New order {order_label(order.id)} · {format_money(paise(order.total))}", "body": "\n".join(lines), "tag": f"order-{order.id}"}


def _host(endpoint: str) -> str:
    """For the logs: which push service (the full address is the device's secret)."""
    return urlsplit(endpoint).hostname or "?"


def _deliver(key, messages: list[tuple[AlertDevice, dict]], ttl: int) -> list[int]:
    """Sends each message to its device at the same time; returns the push services' answers in order."""

    def one(item: tuple[AlertDevice, dict]) -> int:
        device, message = item
        return webpush.send(key, settings.push_contact, device.endpoint, device.p256dh, device.auth, message, ttl=ttl, timeout=SEND_TIMEOUT)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(messages))) as pool:
        return list(pool.map(one, messages))


def _forget(db: Session, devices: list[AlertDevice]) -> None:
    """Removes devices, each only as it was read: if it was turned on again meanwhile (a new sign-in), it stays."""
    for device in devices:
        db.execute(delete(AlertDevice).where(and_(AlertDevice.id == device.id, AlertDevice.updated_at == device.updated_at)))
    db.commit()


def _send(db: Session, key, where, message: Callable[[AlertDevice], dict], ttl: int, what: str) -> int:
    """Sends to the devices `where` selects whose sign-in is still good; devices whose sign-in has ended,
    or that the push service says are gone, are removed. Returns how many were delivered."""
    now = utcnow()
    devices, ended = [], []
    for device, user in db.execute(select(AlertDevice, User).join(User, User.id == AlertDevice.user_id).where(where)).tuples():
        (devices if signed_in(device, user, now) else ended).append(device)
    if ended:
        _forget(db, ended)
    if not devices:
        return 0
    answers = _deliver(key, [(device, {**message(device), "url": _opens(device)}) for device in devices], ttl)
    gone, delivered = [], 0
    for device, answer in zip(devices, answers):
        if answer in GONE:
            gone.append(device)
        elif 200 <= answer < 300:
            delivered += 1
        else:
            logger.warning("%s not delivered: %s answered %s", what, _host(device.endpoint), answer or "nothing")
    if gone:
        _forget(db, gone)
    return delivered


def new_order(order_id: int) -> None:
    """After an order is saved, its alert goes to every shopkeeper's and admin's device with alerts on."""
    try:
        key = signing_key()
        if key is None:
            return
        with SessionLocal() as db:
            order = db.get(Order, order_id, options=[selectinload(Order.items)])
            if order is None:
                return
            message = order_message(order)
            _send(db, key, AlertDevice.role.in_(STAFF), lambda _: message, ORDER_ALERT_TTL, f"Order alert for {order_label(order_id)}")
    except Exception:
        logger.exception("Order alerts for order %s failed", order_label(order_id))


# --- customers ---


def _to_customers(user_ids: list[str], message: dict, what: str) -> None:
    key = signing_key()
    if key is None or not user_ids:
        return
    with SessionLocal() as db:
        where = and_(AlertDevice.role == "customer", AlertDevice.user_id.in_(user_ids))
        _send(db, key, where, lambda _: message, CUSTOMER_TTL, what)


def back_in_stock(name: str, user_ids: list[str]) -> None:
    """An item customers asked for is in the shop now (their requests were just cleared): tell them."""
    try:
        message = {
            "title": f"{name} is in the shop now 🎉",
            "body": "You asked for it. Get yours before it's gone!",
            "tag": f"wish-{' '.join(name.lower().split())}"[:60],
        }
        _to_customers(user_ids, message, f"In-stock news for {name!r}")
    except Exception:
        logger.exception("In-stock news for %r failed", name)


def customer_order_message(order: Order, event: OrderEvent, pickup_point: str, shop_phone: str) -> dict[str, str]:
    label = order_label(order.id)
    if event == "confirmed":
        title, body = f"Payment received ✓ Order {label}", "Your order is confirmed. We're getting it ready."
    elif event == "fulfilled":
        if order.delivery == "Room Delivery":
            where = f"Block {order.customer_block}, Room {order.customer_room}" if order.customer_room else "your room"
            title, body = f"Order {label} is on its way 🛵", f"It's coming to {where}."
        else:
            title, body = f"Order {label} is ready ✓", f"Pick it up at {pickup_point}."
    else:
        title, body = f"Order {label} was cancelled", f"Your items went back on the shelf. Questions? Call {shop_phone}."
    return {"title": title, "body": body, "tag": f"my-order-{order.id}"}


def order_update(order_id: int, event: OrderEvent) -> None:
    """The customer's devices hear about her order: payment confirmed, ready (handed over) or cancelled."""
    try:
        if signing_key() is None:
            return
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            if order is None:
                return
            shop = site.values(db)
            message = customer_order_message(order, event, shop.pickup_point, shop.shop_phone)
            user_id = order.user_id
        _to_customers([user_id], message, f"Order update ({event}) for {order_label(order_id)}")
    except Exception:
        logger.exception("Order update (%s) for %s failed", event, order_label(order_id))


# --- the day's summary, for admins ---


def summary_message(db: Session) -> dict[str, str]:
    """Today's orders, sales and profit (as on the admin's Profit page: the shop day so far), UPI payments
    still to confirm, and what's running low."""
    from .routers.siteadmin import profit  # the Profit page's own figures (imported here: it imports this module)

    today = profit(period="today", db=db)
    orders, manual = today.online.count, today.manual.count
    lines = [
        f"{orders} order{'' if orders == 1 else 's'}"
        + (f" + {manual} in person" if manual else "")
        + f" · profit {format_money(paise(today.total.profit))}"
    ]
    if today.awaiting:
        lines.append(f"UPI to confirm: {today.awaiting} ({format_money(paise(today.awaiting_money))})")
    low = list(db.scalars(select(Product).where(Product.active, Product.stock <= Product.threshold).order_by(Product.stock, Product.name)))
    if low:
        shown = ", ".join(f"{product.name} ({product.stock})" for product in low[:LOW_STOCK_SHOWN])
        lines.append(f"Running low: {shown}" + (f" +{len(low) - LOW_STOCK_SHOWN} more" if len(low) > LOW_STOCK_SHOWN else ""))
    else:
        lines.append("Stock looks fine ✓")
    return {
        "title": f"Today at the Night Mart: {format_money(paise(today.total.revenue))}",
        "body": "\n".join(lines),
        "tag": f"summary-{today.end}",
    }


def send_summary(db: Session, device: AlertDevice | None = None) -> int:
    """The summary to every admin's device with alerts on (or to one device). Returns how many got it."""
    key = signing_key()
    if key is None:
        return 0
    message = summary_message(db)
    where = AlertDevice.id == device.id if device is not None else AlertDevice.role == "admin"
    return _send(db, key, where, lambda _: message, SUMMARY_TTL, "Day's summary")


def scheduled_summary() -> str:
    """Run every hour (the Lambda schedule): at the shop's closing hour, the day's summary goes to the
    admins. The closing hour is remembered from the last time the shop's settings were read, so the other
    hours don't wake the database just to check the time."""
    try:
        hour = shop_now().hour
        if site.last_close_hour is not None and hour != site.last_close_hour:
            return "not closing time"
        with SessionLocal() as db:
            if hour != site.values(db).close_hour:
                return "not closing time"
            return f"summary sent to {send_summary(db)} device(s)"
    except Exception:
        logger.exception("The day's summary failed")
        return "failed"


def send_test(db: Session, device: AlertDevice) -> AlertTestOut:
    """A test alert to this one device, now."""
    key = signing_key()
    customer = device.role == "customer"
    what = "notifications" if customer else "alerts"
    if key is None:
        return AlertTestOut(sent=False, detail=f"{'Notifications' if customer else 'Order alerts'} aren't set up for this shop yet.")
    if customer:
        message = {"title": "Notifications are on ✓", "body": "You'll hear when your order is ready, and when something you asked for is in the shop.", "tag": "test"}
    else:
        message = {"title": "Order alerts are on ✓", "body": "New orders will show up like this, even when the website is closed.", "tag": "test"}
    [answer] = _deliver(key, [(device, {**message, "url": _opens(device)})], ttl=10 * 60)
    if answer in GONE:
        _forget(db, [device])
        return AlertTestOut(sent=False, detail=f"This device's {what} were switched off. Turn them on again.")
    if not 200 <= answer < 300:
        logger.warning("Test alert not delivered: %s answered %s", _host(device.endpoint), answer or "nothing")
        return AlertTestOut(sent=False, detail="The browser's notification service didn't take it. Try again in a minute.")
    return AlertTestOut(sent=True)
