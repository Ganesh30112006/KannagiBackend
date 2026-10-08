"""Combined reads, so the website makes one call instead of several."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import site, sync
from ..database import get_db
from ..models import User
from ..security import current_user, has_shop_access
from ..services import get_settings, loyalty, user_out
from .admin import MANUAL_SALES_SHOWN, order_requests, recent_manual_sales, recent_orders, sales_summary
from .orders import my_orders
from .requests import my_request
from .profile import get_profile
from .shop import list_products, list_wishes, promotions, store_status
from .spin import spin_status

router = APIRouter(tags=["session"])


def _json(value: BaseModel | list | None, *, drop_none: bool = False) -> Any:
    """Serialised exactly like the single-purpose endpoints (camelCase; optional fields dropped where they drop them)."""
    if value is None:
        return None
    if isinstance(value, list):
        return [_json(item, drop_none=drop_none) for item in value]
    return value.model_dump(mode="json", by_alias=True, exclude_none=drop_none)


@router.get("/bootstrap")
def bootstrap(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything the shop page shows on load, plus the revisions it reflects (for /sync)."""
    # Revisions first: if something changes while the rest is read, the next sync fetches it again.
    revs = sync.revisions(db)
    return {
        "revs": revs,
        "site": _json(site.public(db)),
        "user": _json(user_out(user)),
        "products": _json(list_products(user=user, db=db), drop_none=True),
        "orders": _json(my_orders(user, db), drop_none=True),
        "myRequest": _json(my_request(user, db), drop_none=True),
        "profile": _json(get_profile(user, db)),
        "promotions": _json(promotions(db)),
        "store": _json(store_status(db)),
        "wishes": _json(list_wishes(db)),
        "spin": _json(spin_status(user, db)),
        "loyalty": _json(loyalty(db, user.id, get_settings(db))),
    }


# The shopkeeper's order list is capped like /admin/orders; all-time figures come from the summary.
ADMIN_ORDERS = 100


def _rev(name: str) -> Any:
    """A revision the page already has; -1 when it has none yet."""
    return Query(-1, ge=-1, le=2**62, alias=name)


@router.get("/sync")
def sync_changes(
    catalog: int = _rev("catalog"),
    orders: int = _rev("orders"),
    admin_orders: int = _rev("adminOrders"),
    promotions_rev: int = _rev("promotions"),
    wishes: int = _rev("wishes"),
    site_rev: int = _rev("site"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What changed since the revisions the page already has. Store status and the customer's coupon
    are always included (they change with the clock); everything else only when its revision moved,
    so a check with no changes is a few bytes. -1 means "I don't have it yet"."""
    revs = sync.revisions(db)
    result: dict[str, Any] = {
        "revs": revs,
        "store": _json(store_status(db)),
        "spin": _json(spin_status(user, db)),
    }
    if site_rev != revs[sync.SITE]:
        result["site"] = _json(site.public(db))
    if catalog != revs[sync.CATALOG]:
        result["products"] = _json(list_products(user=user, db=db), drop_none=True)
    if promotions_rev != revs[sync.PROMOTIONS]:
        result["promotions"] = _json(promotions(db))
    if wishes != revs[sync.WISHES]:
        result["wishes"] = _json(list_wishes(db))
    if orders != revs[sync.ORDERS]:
        result["orders"] = _json(my_orders(user, db), drop_none=True)
        result["myRequest"] = _json(my_request(user, db), drop_none=True)
        result["firstOrderAvailable"] = not user.first_order_used
    if orders != revs[sync.ORDERS] or promotions_rev != revs[sync.PROMOTIONS]:
        # Her stamps move when an order is handed over; the card's terms when the offers change.
        result["loyalty"] = _json(loyalty(db, user.id, get_settings(db)))
    if has_shop_access(user) and admin_orders != revs[sync.ORDERS]:
        result["admin"] = {
            "orders": _json(recent_orders(limit=ADMIN_ORDERS, db=db), drop_none=True),
            "summary": _json(sales_summary(db)),
            "manualSales": _json(recent_manual_sales(limit=MANUAL_SALES_SHOWN, db=db), drop_none=True),
            "requests": _json(order_requests(db=db), drop_none=True),
        }
    return result


@router.get("/live")
def live(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stock, store status and the customer's coupon. Kept for website builds from before /sync."""
    return {
        "products": _json(list_products(user=user, db=db), drop_none=True),
        "store": _json(store_status(db)),
        "spin": _json(spin_status(user, db)),
    }
