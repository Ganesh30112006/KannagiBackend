"""Shopkeeper-only endpoints."""

import re
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import alerts, sync
from ..database import get_db, takes_turns
from ..models import ManualSale, ManualSaleItem, Order, OrderItem, OrderRequest, Product, Profile, Spin, StockEntry, User, utcnow
from ..schemas import (
    AdminOrderOut,
    FulfilIn,
    GiftIn,
    IdPath,
    ManualSaleIn,
    ManualSaleItemOut,
    ManualSaleOut,
    OfflineOrdersSwitch,
    PaymentIn,
    OrderCustomer,
    OrderRequestOut,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    Promotions,
    SalesFigures,
    SalesSummary,
    SoldItem,
    StoreStatus,
    StoreUpdate,
    WheelSwitch,
)
from ..security import shopkeeper
from ..images import ImageStoreError, delete_image, store_image
from ..services import (
    GIFT,
    GIVEN,
    canonical_prize,
    cart_summary,
    free_stock,
    get_settings,
    held_stock,
    item_named,
    order_out,
    paise,
    product_out,
    put_back,
    rupees,
    sale_price,
    to_ms,
)
from .orders import _take
from .requests import with_customers as requests_with_customers
from .shop import clear_wishes, promotions, store_status

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


def _name_taken(db: Session, name: str, product_id: int | None = None) -> None:
    """One item per name: free items, wishlist requests and Profit find items by their names."""
    other = item_named(db, name, besides=product_id)
    if other is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"There's already an item called {other.name}. Change that item's stock on its card, or use another name.",
        )


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
def create_product(body: ProductCreate, background: BackgroundTasks, user: User = Depends(shopkeeper), db: Session = Depends(get_db)) -> ProductOut:
    """Its starting stock counts as stock bought. Customers who asked for it hear that it's here."""
    _name_taken(db, body.name)
    image_url = _save_image(body.image)
    product = Product(
        name=body.name,
        emoji=body.emoji or "🛍️",
        purchase_price=body.mrp,
        markup=body.markup,
        stock=body.stock,
        threshold=body.threshold,
        category=body.category or "Snacks",
        image_url=image_url,
    )
    db.add(product)
    db.flush()
    _log_stock(db, product, product.stock, user)
    asked = clear_wishes(db, product.name) if product.stock > 0 else []  # they asked for it; now they can buy it
    sync.bump(db, sync.CATALOG)
    db.commit()
    if asked:
        background.add_task(alerts.back_in_stock, product.name, asked)
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
    fields = body.model_fields_set
    if sum(("stock" in fields and body.stock is not None, bool(body.stock_delta), "shelf" in fields and body.shelf is not None)) > 1:
        # Setting and adjusting in one request is ambiguous; the website sends one of them.
        raise HTTPException(422, "Send one of stock, stockDelta or shelf, not more.")
    _get_product(db, product_id)  # a missing item is refused before a photo is uploaded for it
    # Uploaded before the item is locked: an upload can take seconds, and orders for it would wait.
    image_url = _save_image(body.image) if "image" in body.model_fields_set else None
    product, old_image, asked = _apply_update(db, product_id, body, image_url, user)
    if old_image != product.image_url:
        _forget_image(db, background, old_image)
    if asked:
        background.add_task(alerts.back_in_stock, product.name, asked)
    return product_out(product, held_stock(db).get(product.id, 0))


@takes_turns
def _apply_update(db: Session, product_id: int, body: ProductUpdate, image_url: str | None, user: User) -> tuple[Product, str | None, list[str]]:
    """With the item locked, so quick taps or two shopkeepers changing its stock at once each start from
    the stock the one before left, and its stock entry adds up the same way."""
    product = _get_product(db, product_id, lock=True)
    old_image, old_stock, old_name = product.image_url, product.stock, product.name
    fields = body.model_fields_set
    if "name" in fields and body.name and body.name != product.name:
        _name_taken(db, body.name, product.id)
        product.name = body.name
    if "mrp" in fields and body.mrp is not None:
        product.purchase_price = body.mrp
    if "markup" in fields and body.markup is not None:
        product.markup = body.markup
    counted = body.shelf if "shelf" in fields else body.stock if "stock" in fields else None
    if counted is not None:
        # Read under the item's lock: an order for it now either went in before (and is counted here) or
        # waits, and then takes its stock from what this leaves.
        product.stock = max(0, counted - held_stock(db).get(product.id, 0))
    if body.stock_delta:
        wanted = product.stock + body.stock_delta
        if wanted > MAX_STOCK:
            raise HTTPException(422, f"Stock can be at most {MAX_STOCK:,}.")
        product.stock = max(wanted, 0)  # taking off more than there is leaves none
    if "threshold" in fields and body.threshold is not None:
        product.threshold = body.threshold
    if "category" in fields and body.category:
        product.category = body.category
    if "image" in fields:
        product.image_url = image_url
    _log_stock(db, product, product.stock - old_stock, user)
    asked: list[str] = []
    if product.stock > 0 and (product.stock > old_stock or product.name != old_name):
        # Restocked (customers don't see an item with no stock, so they may have asked for it) or renamed
        # to what they asked for: now they can buy it.
        asked = clear_wishes(db, product.name)
    sync.bump(db, sync.CATALOG)
    db.commit()
    return product, old_image, asked


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
@takes_turns
def delete_product(product_id: IdPath, background: BackgroundTasks, user: User = Depends(shopkeeper), db: Session = Depends(get_db)) -> Response:
    # Soft delete so past orders keep their history (orders store names, not photos). Locked, so two
    # deletes at once take its stock off only once.
    product = _get_product(db, product_id, lock=True)
    product.active = False
    _log_stock(db, product, -product.stock, user)  # its stock leaves the shop's stock
    product.stock = 0
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
    lines = [(products[product_id], qty) for product_id, qty in quantities.items()]
    total = paise(body.amount) if body.amount is not None else cart_summary(lines).subtotal
    sale = ManualSale(total=rupees(total), payment=body.payment, note=body.note or None, recorded_by=user.phone or user.email)
    for product, qty in lines:
        sale.items.append(
            ManualSaleItem(
                product_id=product.id,
                product_name=product.name,
                quantity=qty,
                purchase_price=product.purchase_price,
                sale_price=sale_price(product),
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
    put_back(db, back)
    sale.cancelled = True
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    return manual_sale_out(sale)


def _locked_order(db: Session, order_id: int) -> Order | None:
    """The order, locked until the commit: a cancel, a payment and a hand-over of the same order at the
    same moment take turns, each seeing what the one before it did."""
    return db.get(Order, order_id, options=[selectinload(Order.items)], with_for_update=True)


def _set_fulfilled(db: Session, order_id: int, fulfilled: bool, payment_received: bool = False) -> tuple[AdminOrderOut, bool]:
    """Returns the order and whether it was just handed over (not already)."""
    order = _locked_order(db, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order was cancelled.")
    if fulfilled and order.payment == "UPI" and not order.payment_confirmed:
        # A UPI order is only confirmed once its payment is: ticked before, or said to have arrived now.
        if not payment_received:
            raise HTTPException(status.HTTP_409_CONFLICT, "Confirm the UPI payment first: tap Payment received once you see it in PhonePe.")
        order.payment_confirmed = True
    handed_over = fulfilled and not order.fulfilled
    order.fulfilled = fulfilled
    sync.bump(db, sync.ORDERS, sync.CATALOG)  # the shop's items show what open orders hold
    db.commit()
    return _with_customers(db, [order])[0], handed_over


@router.post("/orders/{order_id}/fulfill", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def fulfill_order(order_id: IdPath, background: BackgroundTasks, body: FulfilIn | None = None, db: Session = Depends(get_db)) -> AdminOrderOut:
    """Hand the order over. The body is optional (no body: the payment must already be confirmed). The
    customer's devices hear that it's ready (or on its way)."""
    order, handed_over = _set_fulfilled(db, order_id, True, body is not None and body.payment_received)
    if handed_over:
        background.add_task(alerts.order_update, order.id, "fulfilled")
    return order


@router.post("/orders/{order_id}/gift", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def give_gift(order_id: IdPath, body: GiftIn, db: Session = Depends(get_db)) -> AdminOrderOut:
    """Which item the shop gave for an order's free chocolate or snack: it comes off the stock (and goes
    back if the order is cancelled), and Profit counts it at its MRP. productId null: none from stock
    (one recorded before goes back on the shelf)."""
    placed = db.get(Order, order_id)
    if placed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    # The lock order of placing an order: the customer, the order, then items by number.
    db.execute(select(User.id).where(User.id == placed.user_id).with_for_update(key_share=True))
    order = db.get(Order, order_id, options=[selectinload(Order.items)], with_for_update=True, populate_existing=True)
    if order.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order was cancelled.")
    freebies = list(order.freebies or [])
    if body.index >= len(freebies):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That free item isn't on this order.")
    text = freebies[body.index]
    given, generic = GIVEN.fullmatch(text), GIFT.fullmatch(text)
    if given is None and generic is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That isn't a free chocolate or snack.")
    if given is not None:
        value, kind = given.group(2), given.group(3)
    else:
        value, kind = generic.group(1), "chocolate" if generic.group(2).startswith("chocolate") else "snack"
    # The item given before (it goes back): kept beside the freebie, or found by its name on older orders.
    ids = list(order.free_items or [])
    ids += [None] * (len(freebies) - len(ids))
    previous = (free_stock(db, [([text], [ids[body.index]])]) or [None])[0] if given is not None else None
    chosen = db.scalar(select(Product).where(Product.id == body.product_id, Product.active)) if body.product_id else None
    if body.product_id and chosen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That item isn't on sale. Refresh the page.")
    if chosen is not None and chosen.id == previous:
        return _with_customers(db, [order])[0]  # already recorded
    for product_id in sorted({previous, chosen.id if chosen else None} - {None}):
        db.execute(select(Product.id).where(Product.id == product_id).with_for_update())
        if product_id == previous:
            put_back(db, {product_id: 1})
        elif not _take(db, product_id, 1):
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, f"No {chosen.name} left in stock. Pick another item, or correct its stock first.")
    if chosen is not None:
        freebies[body.index] = f"{chosen.name} (free ₹{value} {kind})"
    else:
        freebies[body.index] = f"₹{value} chocolate (free)" if kind == "chocolate" else f"₹{value} free snack"
    ids[body.index] = chosen.id if chosen is not None else None
    order.freebies, order.free_items = freebies, ids
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    return _with_customers(db, [order])[0]


def cancel(db: Session, order_id: int) -> AdminOrderOut:
    """Cancels an order that hasn't been handed over: its items (and any free item from stock) go back on
    the shelf, an unexpired spin coupon it used works again, and a first-order discount is available again
    if this was her only order. It no longer counts in sales. If she paid by UPI, the money is returned
    outside the app."""
    placed = db.get(Order, order_id)
    if placed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    # The lock order of orders.place_order: the customer, then the order, then products by number. The
    # order is read again under its lock, so two cancels at once put the stock back only once.
    db.execute(select(User.id).where(User.id == placed.user_id).with_for_update(key_share=True))
    order = db.get(Order, order_id, options=[selectinload(Order.items)], with_for_update=True, populate_existing=True)
    if order.cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order is already cancelled.")
    if order.fulfilled:
        raise HTTPException(status.HTTP_409_CONFLICT, "This order was handed over. Undo 'fulfilled' first.")
    back: dict[int, int] = {}  # product id -> how many go back on the shelf
    for item in order.items:
        if item.product_id is not None:
            back[item.product_id] = back.get(item.product_id, 0) + item.quantity
    for product_id in free_stock(db, [(order.freebies, order.free_items)]):
        back[product_id] = back.get(product_id, 0) + 1
    put_back(db, back)
    if order.coupon:
        coupon = db.scalar(
            select(Spin)
            .where(Spin.user_id == order.user_id, Spin.code == order.coupon, Spin.used_at.is_not(None))
            .order_by(Spin.used_at.desc())
            .limit(1)
        )
        if coupon is not None and coupon.expires_at is not None and coupon.expires_at > utcnow():
            coupon.used_at = None
    order.cancelled = True
    others = db.scalar(select(func.count()).where(Order.user_id == order.user_id, Order.id != order.id, Order.cancelled.is_(False)))
    customer = db.get(User, order.user_id)
    if customer is not None and not others:
        customer.first_order_used = False
    sync.bump(db, sync.ORDERS, sync.CATALOG)
    db.commit()
    return _with_customers(db, [order])[0]


@router.post("/orders/{order_id}/cancel", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def cancel_order(order_id: IdPath, background: BackgroundTasks, db: Session = Depends(get_db)) -> AdminOrderOut:
    """For an order that won't be handed over (never paid for, nobody came): see cancel. Her devices hear it."""
    order = cancel(db, order_id)
    background.add_task(alerts.order_update, order.id, "cancelled")
    return order


@router.post("/orders/{order_id}/payment", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def confirm_payment(order_id: IdPath, body: PaymentIn, background: BackgroundTasks, db: Session = Depends(get_db)) -> AdminOrderOut:
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
    newly_confirmed = body.received and not order.payment_confirmed
    order.payment_confirmed = body.received
    sync.bump(db, sync.ORDERS)
    db.commit()
    if newly_confirmed:
        background.add_task(alerts.order_update, order.id, "confirmed")  # her devices: it's confirmed
    return _with_customers(db, [order])[0]


@router.post("/orders/{order_id}/unfulfill", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def unfulfill_order(order_id: IdPath, db: Session = Depends(get_db)) -> AdminOrderOut:
    """Undo a mistaken "Mark as Fulfilled" tap."""
    return _set_fulfilled(db, order_id, False)[0]


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
    offers = [offer.model_dump(by_alias=True, exclude_none=True) for offer in body.daily_offers]
    if not any(offer["id"] == "loyalty" for offer in offers):
        # A page from before the loyalty card existed doesn't send it: keep it as it is.
        offers += [offer for offer in row.daily_offers if offer.get("id") == "loyalty"]
    row.daily_offers = offers
    # What each slice says is made from what it gives (services.prize_text), whatever text was sent.
    row.wheel_prizes = [canonical_prize(prize.model_dump(by_alias=True)) for prize in body.wheel_prizes]
    row.coupon_rule = body.coupon_rule
    sync.bump(db, sync.PROMOTIONS)
    db.commit()
    return promotions(db)


@router.put("/wheel", response_model=Promotions)
def switch_wheel(body: WheelSwitch, db: Session = Depends(get_db)) -> Promotions:
    """Spin & Win on or off for customers, at once (not with the offers' Save, so unsaved edits there
    don't go with it). Coupons already won keep working until they expire."""
    row = get_settings(db)
    if row.wheel_enabled != body.enabled:
        row.wheel_enabled = body.enabled
        sync.bump(db, sync.PROMOTIONS)
        db.commit()
    return promotions(db)


@router.put("/store", response_model=StoreStatus)
def set_store_status(body: StoreUpdate, db: Session = Depends(get_db)) -> StoreStatus:
    row = get_settings(db)
    row.store_override = body.override
    db.commit()
    return store_status(db)


@router.put("/offline-orders", response_model=StoreStatus)
def switch_offline_orders(body: OfflineOrdersSwitch, db: Session = Depends(get_db)) -> StoreStatus:
    """While the store is offline: take orders (on request), or not (customers send a request instead).
    Orders already placed stay as they are."""
    row = get_settings(db)
    row.offline_orders = body.enabled
    db.commit()
    return store_status(db)


@router.get("/order-requests", response_model=list[OrderRequestOut], response_model_exclude_none=True)
def order_requests(db: Session = Depends(get_db)) -> list[OrderRequestOut]:
    """Customers' requests while the shop wasn't taking orders, newest first."""
    requests = list(db.scalars(select(OrderRequest).order_by(OrderRequest.created_at.desc(), OrderRequest.id.desc()).limit(200)))
    return requests_with_customers(db, requests)


@router.delete("/order-requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def finish_order_request(request_id: IdPath, db: Session = Depends(get_db)) -> Response:
    """Done (the shop got in touch). Already done by someone else is fine too."""
    if db.execute(delete(OrderRequest).where(OrderRequest.id == request_id)).rowcount:
        sync.bump(db, sync.ORDERS)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
