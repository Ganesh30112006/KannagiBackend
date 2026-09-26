"""Shopkeeper-only endpoints."""

import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from .. import site, sync
from ..database import get_db, takes_turns
from ..models import Order, OrderItem, Product, Profile, User
from ..schemas import (
    AdminOrderOut,
    IdPath,
    PaymentIn,
    OrderCustomer,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    Promotions,
    SalesSummary,
    SoldItem,
    StoreStatus,
    StoreUpdate,
)
from ..security import shopkeeper
from ..images import ImageStoreError, delete_image, store_image
from ..services import get_settings, order_out, product_out, store_online

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(shopkeeper)])


# Raster photos only (no SVG, which can carry scripts), or an https link (http breaks on an https site).
_DATA_IMAGE = re.compile(r"data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}")
_HTTPS_IMAGE = re.compile(r"https://[^\s\"'<>]+")


def _check_image(image: str | None) -> None:
    if image and not (_DATA_IMAGE.fullmatch(image) or _HTTPS_IMAGE.fullmatch(image)):
        raise HTTPException(422, "Image must be an uploaded photo (PNG, JPEG, WebP or GIF) or an https:// link.")


def _save_image(image: str | None) -> str | None:
    """Validate, then upload to Cloudinary (when configured) and return the URL to store."""
    _check_image(image)
    if not image:
        return None
    try:
        return store_image(image)
    except ImageStoreError as error:
        raise HTTPException(422, str(error)) from None


def _forget_image(db: Session, background: BackgroundTasks, url: str | None) -> None:
    """After a commit: delete a photo from Cloudinary once no product on sale still shows it."""
    if url and not db.scalar(select(func.count()).where(Product.image_url == url, Product.active)):
        background.add_task(delete_image, url)


def _get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or not product.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found.")
    return product


# --- products ---


@router.post("/products", response_model=ProductOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
def create_product(body: ProductCreate, db: Session = Depends(get_db)) -> ProductOut:
    image_url = _save_image(body.image)
    product = Product(
        name=body.name,
        emoji=body.emoji or "🛍️",
        purchase_price=body.mrp,
        stock=body.stock,
        threshold=body.threshold,
        category=body.category or "Snacks",
        image_url=image_url,
    )
    db.add(product)
    sync.bump(db, sync.CATALOG)
    db.commit()
    return product_out(product)


@router.patch("/products/{product_id}", response_model=ProductOut, response_model_exclude_none=True)
def update_product(
    product_id: IdPath, body: ProductUpdate, background: BackgroundTasks, db: Session = Depends(get_db)
) -> ProductOut:
    product = _get_product(db, product_id)
    old_image = product.image_url
    fields = body.model_fields_set
    if "stock" in fields and body.stock_delta:
        # Setting and adjusting in one request is ambiguous; the website sends one or the other.
        raise HTTPException(422, "Send either stock or stockDelta, not both.")
    if "name" in fields and body.name:
        product.name = body.name
    if "mrp" in fields and body.mrp is not None:
        product.purchase_price = body.mrp
    if "stock" in fields and body.stock is not None:
        product.stock = body.stock
    if body.stock_delta:
        new_stock = Product.stock + body.stock_delta
        db.execute(
            update(Product).where(Product.id == product_id).values(stock=case((new_stock < 0, 0), else_=new_stock)),
            execution_options={"synchronize_session": False},
        )
        db.flush()
        db.refresh(product)
    if "threshold" in fields and body.threshold is not None:
        product.threshold = body.threshold
    if "image" in fields:
        product.image_url = _save_image(body.image)
    sync.bump(db, sync.CATALOG)
    db.commit()
    if old_image != product.image_url:
        _forget_image(db, background, old_image)
    return product_out(product)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: IdPath, background: BackgroundTasks, db: Session = Depends(get_db)) -> Response:
    # Soft delete so past orders keep their history (orders store names, not photos).
    product = _get_product(db, product_id)
    product.active = False
    sync.bump(db, sync.CATALOG)
    db.commit()
    _forget_image(db, background, product.image_url)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- orders ---


def _with_customers(db: Session, orders: list[Order]) -> list[AdminOrderOut]:
    """Orders plus who placed them. Orders from before details were saved on the order fall back to
    the customer's current hostel details. Two extra queries in total, not two per order."""
    user_ids = {order.user_id for order in orders}
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(user_ids)))} if user_ids else {}
    profiles = {p.user_id: p for p in db.scalars(select(Profile).where(Profile.user_id.in_(user_ids)))} if user_ids else {}
    result = []
    for order in orders:
        user, profile = users.get(order.user_id), profiles.get(order.user_id)
        saved = order.customer_phone is not None
        customer = OrderCustomer(
            name=order.customer_name if saved else (profile.full_name if profile else None),
            phone=order.customer_phone if saved else (profile.phone if profile else None),
            block=order.customer_block if saved else (profile.block if profile else None),  # type: ignore[arg-type]
            room=order.customer_room if saved else (profile.room_number if profile else None),
            email=(user.email or user.phone) if user else None,
        )
        result.append(AdminOrderOut(**order_out(order).model_dump(), customer=customer))
    return result


@router.get("/orders", response_model=list[AdminOrderOut], response_model_exclude_none=True)
def recent_orders(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> list[AdminOrderOut]:
    """Newest first, with each customer's name, mobile and delivery details. Polled every 30 s, so it's
    capped; all-time figures come from /summary."""
    orders = list(db.scalars(select(Order).options(selectinload(Order.items)).order_by(Order.id.desc()).limit(limit)))
    return _with_customers(db, orders)


@router.get("/summary", response_model=SalesSummary)
def sales_summary(db: Session = Depends(get_db)) -> SalesSummary:
    """Money figures leave out cancelled orders, and UPI orders whose payment isn't confirmed: that
    money may never arrive."""
    counted = and_(Order.cancelled.is_(False), or_(Order.payment != "UPI", Order.payment_confirmed.is_(True)))
    order_count = db.scalar(select(func.count(Order.id))) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(counted))
    investment = db.scalar(
        select(func.coalesce(func.sum(OrderItem.purchase_price * OrderItem.quantity), 0)).join(Order).where(counted)
    )
    sold = db.execute(
        select(OrderItem.product_name, func.sum(OrderItem.quantity))
        .join(Order)
        .where(counted)
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.quantity).desc(), OrderItem.product_name)
    )
    return SalesSummary(
        order_count=order_count,
        revenue=round(float(revenue), 2),
        investment=round(float(investment), 2),
        sold=[SoldItem(name=name, qty=int(qty)) for name, qty in sold],
    )


def _locked_order(db: Session, order_id: int) -> Order | None:
    """The order, locked until the commit: a cancel, a payment and a hand-over of the same order at the
    same moment take turns, each seeing what the one before it did."""
    return db.get(Order, order_id, options=[selectinload(Order.items)], with_for_update=True)


def _set_fulfilled(db: Session, order_id: int, fulfilled: bool) -> AdminOrderOut:
    order = _locked_order(db, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order was cancelled.")
    if fulfilled and order.payment == "UPI" and not order.payment_confirmed:
        # A UPI order is only confirmed once its payment is.
        raise HTTPException(status.HTTP_409_CONFLICT, "Confirm the UPI payment first: tap Payment received once you see it in PhonePe.")
    order.fulfilled = fulfilled
    sync.bump(db, sync.ORDERS)
    db.commit()
    return _with_customers(db, [order])[0]


@router.post("/orders/{order_id}/fulfill", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def fulfill_order(order_id: IdPath, db: Session = Depends(get_db)) -> AdminOrderOut:
    return _set_fulfilled(db, order_id, True)


@router.post("/orders/{order_id}/payment", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def confirm_payment(order_id: IdPath, body: PaymentIn, db: Session = Depends(get_db)) -> AdminOrderOut:
    """The shopkeeper saw the UPI money arrive, which confirms the order (or undoes a mistaken tick)."""
    order = _locked_order(db, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order was cancelled.")
    if order.payment != "UPI":
        raise HTTPException(status.HTTP_409_CONFLICT, "This order is paid on delivery; there's no UPI payment to confirm.")
    if not body.received and order.fulfilled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order is already fulfilled. Undo that first.")
    order.payment_confirmed = body.received
    sync.bump(db, sync.ORDERS)
    db.commit()
    return _with_customers(db, [order])[0]


@router.post("/orders/{order_id}/unfulfill", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def unfulfill_order(order_id: IdPath, db: Session = Depends(get_db)) -> AdminOrderOut:
    """Undo a mistaken "Mark as Fulfilled" tap."""
    return _set_fulfilled(db, order_id, False)


# --- promotions & store status ---


@router.put("/promotions", response_model=Promotions)
def save_promotions(body: Promotions, db: Session = Depends(get_db)) -> Promotions:
    active = [prize for prize in body.wheel_prizes if prize.active]
    if len(active) < 2:
        raise HTTPException(422, "Keep at least two wheel slices active.")
    if not any(prize.kind is None for prize in active):
        raise HTTPException(422, "Keep one Better Luck slice active.")
    if len({prize.code for prize in body.wheel_prizes}) != len(body.wheel_prizes):
        raise HTTPException(422, "Wheel slice codes must be unique.")
    if len({offer.id for offer in body.daily_offers}) != len(body.daily_offers):
        raise HTTPException(422, "Each daily offer can appear only once.")

    row = get_settings(db)
    row.launch_message = body.launch_message.strip()
    row.daily_offers = [offer.model_dump(by_alias=True) for offer in body.daily_offers]
    row.wheel_prizes = [prize.model_dump(by_alias=True) for prize in body.wheel_prizes]
    row.coupon_rule = body.coupon_rule
    sync.bump(db, sync.PROMOTIONS)
    db.commit()
    return body


@router.put("/store", response_model=StoreStatus)
def set_store_status(body: StoreUpdate, db: Session = Depends(get_db)) -> StoreStatus:
    row = get_settings(db)
    row.store_override = body.override
    db.commit()
    shop = site.values(db)
    return StoreStatus(override=body.override, online=store_online(row, shop.open_hour, shop.close_hour))
