"""The shop's settings rows, inserted on first start: the offers and spin-wheel prizes (switched on or
off in the dashboard) and the shop details. No products: the shopkeeper adds the real ones."""

from sqlalchemy.orm import Session

from . import site, sync
from .models import MartSettings

# No launch banner until the shopkeeper writes one (an empty message shows nothing).
DEFAULT_LAUNCH_MESSAGE = ""
# The demo banner earlier versions put in every new shop (long expired). Taken down on start if it's
# still exactly this; a banner the shopkeeper wrote is never touched.
OLD_DEMO_LAUNCH_MESSAGE = "🚀 LAUNCHING OFFER — Valid 14th – 18th September 2026 Only!"

# Every 10th completed order earns a free item she picks, MRP up to ₹10 (services.loyalty). Added once
# to shops from before it existed; the shopkeeper can switch it off or change it like the other cards.
LOYALTY_OFFER = {
    "id": "loyalty",
    "title": "Every 10th order",
    "note": "Collect a stamp per order: the 10th gets a free snack 🎟️",
    "icon": "🎟️",
    "active": True,
    "every": 10,
    "pickUpTo": 10,
}

DEFAULT_DAILY_OFFERS = [
    {"id": "tier50", "title": "Spend ₹50+", "note": "Free ₹5 chocolate added at checkout 🍫", "icon": "🍫", "active": True},
    {"id": "tier100", "title": "Spend ₹100+", "note": "Pick any free ₹10 item you like 🎁", "icon": "🎁", "active": True},
    {"id": "first", "title": "First order? 10% OFF", "note": "Applied automatically at checkout ♡", "icon": "♡", "active": True},
    {"id": "bulk", "title": "Cart over ₹200", "note": "Flat 20% OFF bulk midnight order 🌙", "icon": "☾", "active": True},
    LOYALTY_OFFER,
]


def _prize(code: str, label: str, short: str, icon: str, kind: str | None) -> dict:
    return {"code": code, "label": label, "shortLabel": short, "icon": icon, "kind": kind, "active": True}


DEFAULT_WHEEL_PRIZES = [
    _prize("DROP60", "FREE Delivery on ₹60+ Orders", "FREE DELIVERY ₹60+", "🚚", "free60"),
    _prize("TRIO5", "Buy Any 3 Items & Get ₹5 OFF", "3 ITEMS ₹5 OFF", "🍪", "three5"),
    _prize("SNACK100", "Free ₹10 Snack on ₹100+ Orders", "FREE SNACK ₹100+", "🎁", "freeSnack100"),
    _prize("HALFDROP", "50% OFF Room Delivery", "½ DELIVERY", "🛵", "halfDelivery"),
    _prize("FOUR10", "Buy 4 Items & Get ₹10 OFF", "4 ITEMS ₹10 OFF", "🎉", "four10"),
    _prize("PREMIUM5", "₹5 OFF on 2 Premium Items", "2 PREMIUM ₹5 OFF", "⭐", "premium5"),
    _prize("LUCK", "Better Luck Next Time", "BETTER LUCK!", "✨", None),
    _prize("TRIO5B", "Buy Any 3 Items & Get ₹5 OFF", "3 ITEMS ₹5 OFF", "🍪", "three5"),
    _prize("DROP60B", "FREE Delivery on ₹60+ Orders", "FREE DELIVERY ₹60+", "🚚", "free60"),
    _prize("SNACK100B", "Free ₹10 Snack on ₹100+ Orders", "FREE SNACK ₹100+", "🎁", "freeSnack100"),
]


def seed(db: Session) -> None:
    row = db.get(MartSettings, 1)
    if row is not None and row.launch_message == OLD_DEMO_LAUNCH_MESSAGE:
        row.launch_message = DEFAULT_LAUNCH_MESSAGE
        sync.bump(db, sync.PROMOTIONS)  # open pages drop it on their next sync
    if row is not None and not any(offer.get("id") == "loyalty" for offer in row.daily_offers or []):
        row.daily_offers = [*(row.daily_offers or []), dict(LOYALTY_OFFER)]  # a new list, so it's saved
        sync.bump(db, sync.PROMOTIONS)
    if row is None:
        db.add(
            MartSettings(
                id=1,
                launch_message=DEFAULT_LAUNCH_MESSAGE,
                daily_offers=DEFAULT_DAILY_OFFERS,
                wheel_prizes=DEFAULT_WHEEL_PRIZES,
                store_override="auto",
                coupon_rule="best",
            )
        )
    site.seed(db)
    db.commit()
