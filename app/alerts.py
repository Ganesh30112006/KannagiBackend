"""Order alerts: a notification for each new order on the phones and laptops where a shopkeeper or admin
turned them on (the dashboard's Order alerts card), even with the website closed. Delivery is Web Push
(webpush.py); this decides who gets what."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, selectinload

from . import webpush
from .config import settings
from .database import SessionLocal
from .models import AlertDevice, Order, User, utcnow
from .schemas import AlertTestOut
from .security import _password_version
from .services import format_money, paise

logger = logging.getLogger("kannagi")

# A phone that's offline gets an order's alert when it's back within this time; after that it's old news.
ORDER_ALERT_TTL = 60 * 60
# Each push service gets this long to answer. The order is already saved; this only bounds the wait.
SEND_TIMEOUT = 5
MAX_PARALLEL = 8
# Devices one account can have alerts on: turning them on on another one drops the one unused longest.
MAX_DEVICES = 10
GONE = (404, 410)  # the browser turned alerts off, was reset or uninstalled: its address no longer works


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
    role_kept = user.is_admin if device.role == "admin" else user.is_shopkeeper if device.role == "shopkeeper" else False
    return (
        user.deleted_at is None
        and not user.blocked
        and role_kept
        and device.password_version == _password_version(user)
        and device.session_expires_at > now
    )


def _opens(device: AlertDevice) -> str:
    """Where tapping the alert goes: the dashboard, or /admin for an admin's sign-in."""
    return "/admin" if device.role == "admin" else "/dashboard"


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
    return {"title": f"New order #{order.id:04d} · {format_money(paise(order.total))}", "body": "\n".join(lines), "tag": f"order-{order.id}"}


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


def new_order(order_id: int) -> None:
    """After an order is saved (a background task: the order is placed whatever happens here), its alert
    goes to every device with alerts on. Devices whose sign-in has ended, or that the push service says
    are gone, are removed."""
    try:
        key = signing_key()
        if key is None:
            return
        with SessionLocal() as db:
            order = db.get(Order, order_id, options=[selectinload(Order.items)])
            if order is None:
                return
            now = utcnow()
            devices, ended = [], []
            for device, user in db.execute(select(AlertDevice, User).join(User, User.id == AlertDevice.user_id)).tuples():
                (devices if signed_in(device, user, now) else ended).append(device)
            if ended:
                _forget(db, ended)
            if not devices:
                return
            message = order_message(order)
            answers = _deliver(key, [(device, {**message, "url": _opens(device)}) for device in devices], ORDER_ALERT_TTL)
            gone = []
            for device, answer in zip(devices, answers):
                if answer in GONE:
                    gone.append(device)
                elif not 200 <= answer < 300:
                    logger.warning("Order alert for #%04d not delivered: %s answered %s", order_id, _host(device.endpoint), answer or "nothing")
            if gone:
                _forget(db, gone)
    except Exception:
        logger.exception("Order alerts for order #%04d failed", order_id)


def send_test(db: Session, device: AlertDevice) -> AlertTestOut:
    """A test alert to this one device, now."""
    key = signing_key()
    if key is None:
        return AlertTestOut(sent=False, detail="Order alerts aren't set up for this shop yet.")
    message = {
        "title": "Order alerts are on ✓",
        "body": "New orders will show up like this, even when the website is closed.",
        "tag": "test",
        "url": _opens(device),
    }
    [answer] = _deliver(key, [(device, message)], ttl=10 * 60)
    if answer in GONE:
        _forget(db, [device])
        return AlertTestOut(sent=False, detail="This device's alerts were switched off. Turn them on again.")
    if not 200 <= answer < 300:
        logger.warning("Test alert not delivered: %s answered %s", _host(device.endpoint), answer or "nothing")
        return AlertTestOut(sent=False, detail="The browser's notification service didn't take it. Try again in a minute.")
    return AlertTestOut(sent=True)
