"""Shopkeeper-only endpoints."""

import re
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from .. import site, sync
from ..database import get_db, takes_turns
from ..models import ManualSale, ManualSaleItem, Order, OrderItem, Product, Profile, StockEntry, User, utcnow
from ..schemas import (
    AdminOrderOut,
    IdPath,
    ManualSaleIn,
    ManualSaleItemOut,
    ManualSaleOut,
    PaymentIn,
    OrderCustomer,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    Promotions,
    SalesFigures,
    SalesSummary,
    SoldItem,
    StoreStatus,
    StoreUpdate,
)
from ..security import shopkeeper
from ..images import ImageStoreError, delete_image, store_image
from ..services import cart_summary, get_settings, order_out, paise, product_out, rupees, sale_price, store_online, to_ms
from .orders import _take

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


def _get_product(db: Session, product_id: int, *, lock: bool = False) -> Product:
    """lock: read it afresh and lock it until the commit, so changes to one item take turns."""
    product = db.get(Product, product_id, with_for_update=True if lock else None, populate_existing=lock)
    if product is None or not product.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found.")
    return product


# --- products ---

MAX_STOCK = 100_000
# Changes to one item by the same person this soon after their last one add up in one stock entry.
STOCK_MERGE = timedelta(minutes=10)


def _log_stock(db: Session, product: Product, change: int, user: User) -> None:
    """Record stock added (bought) or taken off by hand, for the admin's Investment page. Changing the same
    item again within a few minutes adds to the same entry, so taps on + count as one purchase and a typo
    put right at once leaves nothing behind. Called with the item locked (or just made), so the entry
    it adds to can't change under it."""
    if not change:
        return
    by = user.phone or user.email
    now = utcnow()
    last = db.scalar(select(StockEntry).where(StockEntry.product_id == product.id).order_by(StockEntry.id.desc()).limit(1))
    if (
        last is not None
        and last.recorded_by == by
        and last.product_name == product.name
        and last.purchase_price == product.purchase_price
        and last.updated_at >= now - STOCK_MERGE
    ):
        last.change += change
        last.updated_at = now
        if last.change == 0:
            db.delete(last)
        return
    db.add(
        StockEntry(
            product_id=product.id,
            product_name=product.name,
            change=change,
            purchase_price=product.purchase_price,
            recorded_by=by,
            created_at=now,
            updated_at=now,
        )
    )


@router.post("/products", response_model=ProductOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
def create_product(body: ProductCreate, user: User = Depends(shopkeeper), db: Session = Depends(get_db)) -> ProductOut:
    """Its starting stock counts as stock bought."""
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
    db.flush()
    _log_stock(db, product, product.stock, user)
    sync.bump(db, sync.CATALOG)
    db.commit()
    return product_out(product)


@router.patch("/products/{product_id}", response_model=ProductOut, response_model_exclude_none=True)
def update_product(
    product_id: IdPath,
    body: ProductUpdate,
    background: BackgroundTasks,
    user: User = Depends(shopkeeper),
    db: Session = Depends(get_db),
) -> ProductOut:
    """Stock added counts as stock bought, and stock taken off as taken off (see _log_stock)."""
    if "stock" in body.model_fields_set and body.stock_delta:
        # Setting and adjusting in one request is ambiguous; the website sends one or the other.
        raise HTTPException(422, "Send either stock or stockDelta, not both.")
    _get_product(db, product_id)  # a missing item is refused before a photo is uploaded for it
    # Uploaded before the item is locked: an upload can take seconds, and orders for it would wait.
    image_url = _save_image(body.image) if "image" in body.model_fields_set else None
    product, old_image = _apply_update(db, product_id, body, image_url, user)
    if old_image != product.image_url:
        _forget_image(db, background, old_image)
    return product_out(product)


@takes_turns
def _apply_update(db: Session, product_id: int, body: ProductUpdate, image_url: str | None, user: User) -> tuple[Product, str | None]:
    """With the item locked, so quick taps or two shopkeepers changing its stock at once each start from
    the stock the one before left, and its stock entry adds up the same way."""
    product = _get_product(db, product_id, lock=True)
    old_image, old_stock = product.image_url, product.stock
    fields = body.model_fields_set
    if "name" in fields and body.name:
        product.name = body.name
    if "mrp" in fields and body.mrp is not None:
        product.purchase_price = body.mrp
    if "stock" in fields and body.stock is not None:
        product.stock = body.stock
    if body.stock_delta:
        wanted = product.stock + body.stock_delta
        if wanted > MAX_STOCK:
            raise HTTPException(422, f"Stock can be at most {MAX_STOCK:,}.")
        product.stock = max(wanted, 0)  # taking off more than there is leaves none
    if "threshold" in fields and body.threshold is not None:
        product.threshold = body.threshold
    if "image" in fields:
        product.image_url = image_url
    _log_stock(db, product, product.stock - old_stock, user)
    sync.bump(db, sync.CATALOG)
    db.commit()
    return product, old_image


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
@takes_turns
def delete_product(product_id: IdPath, background: BackgroundTasks, user: User = Depends(shopkeeper), db: Session = Depends(get_db)) -> Response:
    # Soft delete so past orders keep their history (orders store names, not photos). Locked, so two
    # deletes at once take its stock off only once.
    product = _get_product(db, product_id, lock=True)
    product.active = False
    _log_stock(db, product, -product.stock, user)  # its stock leaves the shop's stock
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
            # No details saved anywhere: her sign-in number still reaches her.
            phone=order.customer_phone if saved else (profile.phone if profile else (user.mobile if user else None)),
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
    """Online orders and manual sales, separately and together. Money figures leave out cancelled orders,
    UPI orders whose payment isn't confirmed (that money may never arrive) and undone manual sales."""
    counted = and_(Order.cancelled.is_(False), or_(Order.payment != "UPI", Order.payment_confirmed.is_(True)))
    kept = ManualSale.cancelled.is_(False)
    order_count = db.scalar(select(func.count(Order.id))) or 0

    def figures(sale, item, total, where) -> SalesFigures:
        count, revenue = db.execute(select(func.count(sale.id), func.coalesce(func.sum(total), 0)).where(where)).one()
        units, investment = db.execute(
            select(func.coalesce(func.sum(item.quantity), 0), func.coalesce(func.sum(item.purchase_price * item.quantity), 0))
            .join(sale)
            .where(where)
        ).one()
        return SalesFigures(count=count, items=int(units), revenue=round(float(revenue), 2), investment=round(float(investment), 2))

    online = figures(Order, OrderItem, Order.total, counted)
    manual = figures(ManualSale, ManualSaleItem, ManualSale.total, kept)
    sold: dict[str, int] = {}
    for item, sale, where in ((OrderItem, Order, counted), (ManualSaleItem, ManualSale, kept)):
        for name, qty in db.execute(select(item.product_name, func.sum(item.quantity)).join(sale).where(where).group_by(item.product_name)):
            sold[name] = sold.get(name, 0) + int(qty)
    return SalesSummary(
        order_count=order_count,
        revenue=round(online.revenue + manual.revenue, 2),
        investment=round(online.investment + manual.investment, 2),
        sold=[SoldItem(name=name, qty=qty) for name, qty in sorted(sold.items(), key=lambda row: (-row[1], row[0]))],
        online=online,
        manual=manual,
    )


# --- manual (in-person) sales ---

MANUAL_SALES_SHOWN = 50


def manual_sale_out(sale: ManualSale) -> ManualSaleOut:
    return ManualSaleOut(
        id=sale.id,
        created_at=to_ms(sale.created_at),
        items=[ManualSaleItemOut(name=item.product_name, qty=item.quantity, price=item.sale_price) for item in sale.items],
        total=sale.total,
        investment=round(sum(item.purchase_price * item.quantity for item in sale.items), 2),
        payment=sale.payment,  # type: ignore[arg-type]
        note=sale.note,
        recorded_by=sale.recorded_by,
        cancelled=sale.cancelled,
    )


@router.get("/manual-sales", response_model=list[ManualSaleOut], response_model_exclude_none=True)
def recent_manual_sales(limit: int = Query(MANUAL_SALES_SHOWN, ge=1, le=200), db: Session = Depends(get_db)) -> list[ManualSaleOut]:
    """Newest first, undone ones included (marked), so every entry stays accounted for."""
    sales = db.scalars(select(ManualSale).options(selectinload(ManualSale.items)).order_by(ManualSale.id.desc()).limit(limit))
    return [manual_sale_out(sale) for sale in sales]


@router.post("/manual-sales", response_model=ManualSaleOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
@takes_turns
def record_manual_sale(body: ManualSaleIn, user: User = Depends(shopkeeper), db: Session = Depends(get_db)) -> ManualSaleOut:
    """A sale made in person: its items come off the stock (never below what's there) and it counts in
    sales and profit. The amount is the shop's prices unless another amount was received."""
    quantities: dict[int, int] = {}
    for item in body.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(quantities), Product.active))}
    if len(products) != len(quantities):
        raise HTTPException(status.HTTP_409_CONFLICT, "An item in this sale is no longer in the shop. Refresh the page.")
    # Stock is taken in product-number order, the lock order of every write (see orders.place_order).
    for product_id in sorted(quantities):
        if not _take(db, product_id, quantities[product_id]):
            db.rollback()
            product = db.get(Product, product_id)
            left = product.stock if product is not None else 0
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Only {left} {products[product_id].name} in stock. If there are more, correct the stock first.",
            )
    shop = site.values(db)
    lines = [(products[product_id], qty) for product_id, qty in quantities.items()]
    total = paise(body.amount) if body.amount is not None else cart_summary(lines, shop.markup).subtotal
    sale = ManualSale(total=rupees(total), payment=body.payment, note=body.note or None, recorded_by=user.phone or user.email)
    for product, qty in lines:
        sale.items.append(
            ManualSaleItem(
                product_id=product.id,
                product_name=product.name,
                quantity=qty,
                purchase_price=product.purchase_price,
                sale_price=sale_price(product, shop.markup),
            )
        )
    db.add(sale)
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    return manual_sale_out(sale)


@router.post("/manual-sales/{sale_id}/undo", response_model=ManualSaleOut, response_model_exclude_none=True)
@takes_turns
def undo_manual_sale(sale_id: IdPath, db: Session = Depends(get_db)) -> ManualSaleOut:
    """A sale entered by mistake: its items go back on the shelf and it stops counting. Read under its
    lock, so two undos at once put the stock back only once."""
    sale = db.get(ManualSale, sale_id, options=[selectinload(ManualSale.items)], with_for_update=True)
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found.")
    if sale.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This sale was already undone.")
    back: dict[int, int] = {}
    for item in sale.items:
        if item.product_id is not None:
            back[item.product_id] = back.get(item.product_id, 0) + item.quantity
    for product_id in sorted(back):
        db.execute(update(Product).where(Product.id == product_id).values(stock=Product.stock + back[product_id]))
    sale.cancelled = True
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    return manual_sale_out(sale)


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
