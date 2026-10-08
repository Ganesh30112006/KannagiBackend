"""Read-only shop data for signed-in customers, plus wishlist requests."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import site, sync
from ..database import get_db
from ..models import Order, OrderItem, Product, User, WishRequest, utcnow
from ..schemas import ProductOut, Promotions, StoreStatus, WishIn, WishOut, WishResult
from ..security import current_user, has_shop_access
from ..services import get_settings, held_stock, product_out, store_online

router = APIRouter(tags=["shop"], dependencies=[Depends(current_user)])

MAX_WISHES_PER_CUSTOMER = 10
WISHES_SHOWN = 50
# Shelf badges: "Popular" on the best sellers of the last two weeks (at least 3 sold), "New" on the
# newest items added in the last week (only a few, so a new shop's whole shelf isn't "new").
POPULAR_DAYS, POPULAR_SHOWN, POPULAR_MIN_SOLD = 14, 3, 3
NEW_DAYS, NEW_SHOWN = 7, 6


def _best_sellers(db: Session) -> set[int]:
    """Units sold online (not cancelled) in the last POPULAR_DAYS, one query."""
    units = func.sum(OrderItem.quantity)
    rows = db.execute(
        select(OrderItem.product_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.cancelled.is_(False), Order.created_at >= utcnow() - timedelta(days=POPULAR_DAYS), OrderItem.product_id.is_not(None))
        .group_by(OrderItem.product_id)
        .having(units >= POPULAR_MIN_SOLD)
        .order_by(units.desc(), OrderItem.product_id)
        .limit(POPULAR_SHOWN)
    )
    return {product_id for (product_id,) in rows}


@router.get("/products", response_model=list[ProductOut], response_model_exclude_none=True)
def list_products(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ProductOut]:
    """Customers see only what they can buy (in stock); the shop sees its whole inventory, with what open
    orders hold of each item (still on its shelf)."""
    query = select(Product).where(Product.active)
    shop = has_shop_access(user)
    if not shop:
        query = query.where(Product.stock > 0)
    held = held_stock(db) if shop else {}
    products = list(db.scalars(query.order_by(Product.id)))
    popular = _best_sellers(db)
    since = utcnow() - timedelta(days=NEW_DAYS)
    newest = sorted((p for p in products if p.created_at and p.created_at >= since), key=lambda p: (p.created_at, p.id), reverse=True)
    fresh = {p.id for p in newest[:NEW_SHOWN]}
    out = []
    for product in products:
        item = product_out(product, held.get(product.id, 0) if shop else None)
        item.popular = True if product.id in popular else None
        item.is_new = True if product.id in fresh else None
        out.append(item)
    return out


@router.get("/store", response_model=StoreStatus)
def store_status(db: Session = Depends(get_db)) -> StoreStatus:
    row, shop = get_settings(db), site.values(db)
    online = store_online(row, shop.open_hour, shop.close_hour)
    return StoreStatus(override=row.store_override, online=online, offline_orders=bool(row.offline_orders))  # type: ignore[arg-type]


@router.get("/promotions", response_model=Promotions)
def promotions(db: Session = Depends(get_db)) -> Promotions:
    row = get_settings(db)
    return Promotions.model_validate(
        {
            "launchMessage": row.launch_message,
            "dailyOffers": row.daily_offers,
            "wheelPrizes": row.wheel_prizes,
            "couponRule": row.coupon_rule,
            "wheelEnabled": bool(row.wheel_enabled),
        }
    )


def wish_key(name: str) -> str:
    """Requests for the same item match whatever the capitals and spacing ("Dark  Fantasy" = "dark fantasy")."""
    return " ".join(name.lower().split())


def clear_wishes(db: Session, name: str) -> list[str]:
    """The requests for an item, taken off the list (not committed). Returns who had asked (to tell them
    it's here); each request is read as it's deleted, so one made meanwhile isn't missed."""
    removed = db.execute(delete(WishRequest).where(WishRequest.item_key == wish_key(name)).returning(WishRequest.user_id)).all()
    if removed:
        sync.bump(db, sync.WISHES)
    return sorted({user_id for (user_id,) in removed})


def _wish_counts(db: Session) -> list[WishOut]:
    rows = db.execute(
        select(WishRequest.item_key, func.min(WishRequest.label), func.count())
        .group_by(WishRequest.item_key)
        .order_by(func.count().desc(), func.min(WishRequest.created_at))
        .limit(WISHES_SHOWN)
    )
    return [WishOut(name=label, count=count) for _, label, count in rows]


@router.get("/wishes", response_model=list[WishOut])
def list_wishes(db: Session = Depends(get_db)) -> list[WishOut]:
    return _wish_counts(db)


@router.post("/wishes", response_model=WishResult)
def add_wish(body: WishIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> WishResult:
    key = wish_key(body.name)
    # Asking for something on the shelf right now: say so instead (it's hidden only when sold out).
    for (on_shelf,) in db.execute(select(Product.name).where(Product.active, Product.stock > 0)):
        if wish_key(on_shelf) == key:
            return WishResult(name=on_shelf, count=0, already_requested=False, on_shelf=True)
    already = db.scalar(select(WishRequest.id).where(WishRequest.user_id == user.id, WishRequest.item_key == key))
    if not already:
        mine = db.scalar(select(func.count()).where(WishRequest.user_id == user.id)) or 0
        if mine >= MAX_WISHES_PER_CUSTOMER:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"You've asked for {MAX_WISHES_PER_CUSTOMER} items already. Thanks! The shopkeeper will look at them.",
            )
        existing_label = db.scalar(select(WishRequest.label).where(WishRequest.item_key == key).limit(1))
        db.add(WishRequest(user_id=user.id, item_key=key, label=existing_label or body.name))
        try:
            sync.bump(db, sync.WISHES)  # writes the request first, so a double submit is caught here
            db.commit()
        except IntegrityError:  # double submit
            db.rollback()
            already = True
    count = db.scalar(select(func.count()).where(WishRequest.item_key == key)) or 0
    label = db.scalar(select(WishRequest.label).where(WishRequest.item_key == key).limit(1)) or body.name
    return WishResult(name=label, count=count, already_requested=bool(already))
