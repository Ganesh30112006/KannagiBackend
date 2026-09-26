"""Read-only shop data for signed-in customers, plus wishlist requests."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import site, sync
from ..database import get_db
from ..models import Product, User, WishRequest
from ..schemas import ProductOut, Promotions, StoreStatus, WishIn, WishOut, WishResult
from ..security import current_user
from ..services import get_settings, product_out, store_online

router = APIRouter(tags=["shop"], dependencies=[Depends(current_user)])

MAX_WISHES_PER_CUSTOMER = 10
WISHES_SHOWN = 50


@router.get("/products", response_model=list[ProductOut], response_model_exclude_none=True)
def list_products(db: Session = Depends(get_db)) -> list[ProductOut]:
    products = db.scalars(select(Product).where(Product.active).order_by(Product.id))
    return [product_out(product) for product in products]


@router.get("/store", response_model=StoreStatus)
def store_status(db: Session = Depends(get_db)) -> StoreStatus:
    row, shop = get_settings(db), site.values(db)
    online = store_online(row, shop.open_hour, shop.close_hour)
    return StoreStatus(override=row.store_override, online=online)  # type: ignore[arg-type]


@router.get("/promotions", response_model=Promotions)
def promotions(db: Session = Depends(get_db)) -> Promotions:
    row = get_settings(db)
    return Promotions.model_validate(
        {
            "launchMessage": row.launch_message,
            "dailyOffers": row.daily_offers,
            "wheelPrizes": row.wheel_prizes,
            "couponRule": row.coupon_rule,
        }
    )


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
    key = " ".join(body.name.lower().split())
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
