"""Order requests: while the store is offline and the shop isn't taking orders (the switch beside Store
status on the dashboard), a customer sends what's in her cart to the shop instead of ordering. No stock is
taken and nothing is paid; the shopkeepers and admins get in touch with her and mark it done."""

from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import alerts, site, sync
from ..database import get_db, takes_turns
from ..models import OrderRequest, Product, Profile, User, utcnow
from ..schemas import OrderCustomer, OrderRequestIn, OrderRequestOut, RequestItem
from ..security import current_user
from ..services import get_settings, store_online, to_ms

router = APIRouter(prefix="/order-requests", tags=["orders"])

# Sending again (to change it) is fine at any time; the shop's phones get another alert only after this.
REALERT_AFTER = timedelta(minutes=10)


def taking_orders(db: Session) -> bool:
    """Orders go through: the store is online, or offline orders (on request) are allowed."""
    row, shop = get_settings(db), site.values(db)
    return bool(row.offline_orders) or store_online(row, shop.open_hour, shop.close_hour)


def request_out(request: OrderRequest, customer: OrderCustomer | None = None) -> OrderRequestOut:
    return OrderRequestOut(
        id=request.id,
        created_at=to_ms(request.created_at),
        items=[RequestItem(**item) for item in request.items],
        delivery=request.delivery,  # type: ignore[arg-type]
        note=request.note,
        customer=customer,
    )


def with_customers(db: Session, requests: list[OrderRequest]) -> list[OrderRequestOut]:
    """For the shop: who asked and where she is (her saved hostel details, else her sign-in number)."""
    user_ids = {request.user_id for request in requests}
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(user_ids)))} if user_ids else {}
    profiles = {p.user_id: p for p in db.scalars(select(Profile).where(Profile.user_id.in_(user_ids)))} if user_ids else {}
    result = []
    for request in requests:
        user, profile = users.get(request.user_id), profiles.get(request.user_id)
        customer = OrderCustomer(
            name=profile.full_name if profile else None,
            phone=(profile.phone if profile else None) or (user.mobile if user else None),
            block=profile.block if profile else None,  # type: ignore[arg-type]
            room=profile.room_number if profile else None,
            email=user.mobile if user else None,
        )
        result.append(request_out(request, customer))
    return result


@router.get("/mine", response_model=OrderRequestOut | None, response_model_exclude_none=True)
def my_request(user: User = Depends(current_user), db: Session = Depends(get_db)) -> OrderRequestOut | None:
    request = db.scalar(select(OrderRequest).where(OrderRequest.user_id == user.id))
    return request_out(request) if request else None


@router.post("", response_model=OrderRequestOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
@takes_turns
def send_request(body: OrderRequestIn, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)) -> OrderRequestOut:
    # Her first (place_order's lock order): her own requests at once take turns, and an admin deleting her
    # account at that moment can't miss this one.
    if db.scalar(select(User.id).where(User.id == user.id, User.deleted_at.is_(None)).with_for_update(key_share=True)) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in again.")
    if taking_orders(db):
        raise HTTPException(status.HTTP_409_CONFLICT, "The shop is taking orders right now. Please place your order instead.")
    shop = site.values(db)
    if not (shop.pickup_enabled if body.delivery == "Pickup" else shop.room_delivery_enabled):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.delivery} is turned off right now. Please choose another option.")
    quantities: dict[int, int] = {}
    for item in body.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(quantities), Product.active))}
    if len(products) != len(quantities):
        raise HTTPException(status.HTTP_409_CONFLICT, "A product in your cart is no longer available. Please refresh the shop.")
    for product_id, qty in quantities.items():
        if qty > products[product_id].stock:
            product = products[product_id]
            raise HTTPException(status.HTTP_409_CONFLICT, f"Only {product.stock} {product.name} left. Please update your cart.")
    items = [{"productId": product_id, "name": products[product_id].name, "qty": qty} for product_id, qty in quantities.items()]
    request = db.scalar(select(OrderRequest).where(OrderRequest.user_id == user.id).with_for_update())
    now = utcnow()
    alert = request is None or request.created_at <= now - REALERT_AFTER
    if request is None:
        request = OrderRequest(user_id=user.id)
        db.add(request)
    request.items, request.delivery, request.note, request.created_at = items, body.delivery, body.note or None, now
    sync.bump(db, sync.ORDERS)
    db.commit()
    if alert:
        background.add_task(alerts.new_request, request.id)
    return request_out(request)


@router.delete("/mine", status_code=status.HTTP_204_NO_CONTENT)
def withdraw_request(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    if db.execute(delete(OrderRequest).where(OrderRequest.user_id == user.id)).rowcount:
        sync.bump(db, sync.ORDERS)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
