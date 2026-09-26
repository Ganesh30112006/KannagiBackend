"""The site admin's controls (/admin on the website): shop details, admins and shopkeepers (their
accounts and passwords), customers (including new passwords for those who forgot theirs), deleting
accounts, and every order. Products, offers and store status use the shopkeeper endpoints, which admins
may also call.

Anyone else gets 404 from these endpoints, the same as for a path that doesn't exist."""

import secrets
from datetime import datetime, time, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import site, sync
from ..config import settings
from ..database import get_db, takes_turns
from ..models import Order, Product, Profile, ResetRequest, Spin, User, WishRequest, utcnow
from ..schemas import (
    MAX_ID,
    AdminOrderOut,
    AdminOverview,
    AdminPasswordIn,
    AdminUserOut,
    AuthOut,
    BlockIn,
    IdPath,
    NewPasswordIn,
    ProfileBody,
    SiteAdminOut,
    SiteValues,
    StaffIn,
    StoredProfile,
)
from ..security import create_token, hash_password, site_admin, verify_password
from ..services import get_settings, shop_now, store_online, to_ms, user_out
from ..staff import is_owner
from .admin import _with_customers

router = APIRouter(prefix="/site-admin", tags=["site admin"], dependencies=[Depends(site_admin)])

FREE_PICK_SUFFIX = " (free ₹10 pick)"  # how orders.py records a free item taken from stock
ACTIVE = User.deleted_at.is_(None)  # deleted accounts kept only for their orders are left out
MAIN_ADMIN = "the main admin is set in the server settings (ADMIN_MOBILE, ADMIN_PASSWORD)"


def _admin_out(db: Session) -> SiteAdminOut:
    return SiteAdminOut(**site.values(db).model_dump(), photo_uploads_enabled=settings.cloudinary_url != "")


def _sign_out(user: User) -> None:
    """Every device this account is signed in on has to sign in again."""
    user.password_changed_at = utcnow()


def _conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


# --- overview ---


@router.get("/overview", response_model=AdminOverview)
def overview(db: Session = Depends(get_db)) -> AdminOverview:
    midnight = datetime.combine(shop_now().date(), time(), tzinfo=settings.tz).astimezone(timezone.utc).replace(tzinfo=None)
    live = Order.cancelled.is_(False)
    paid = or_(Order.payment != "UPI", Order.payment_confirmed.is_(True))
    shop = site.values(db)

    def people(*where) -> int:
        return db.scalar(select(func.count()).select_from(User).where(ACTIVE, *where)) or 0

    return AdminOverview(
        customers=people(User.email.is_not(None)),
        shopkeepers=people(User.is_shopkeeper.is_(True)),
        admins=people(User.is_admin.is_(True)),
        blocked=people(User.blocked.is_(True)),
        orders_today=db.scalar(select(func.count()).where(live, Order.created_at >= midnight)) or 0,
        revenue_today=round(float(db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(live, paid, Order.created_at >= midnight)) or 0), 2),
        open_orders=db.scalar(select(func.count()).where(live, Order.fulfilled.is_(False))) or 0,
        awaiting_payment=db.scalar(select(func.count()).where(live, Order.payment == "UPI", Order.payment_confirmed.is_(False))) or 0,
        store_online=store_online(get_settings(db), shop.open_hour, shop.close_hour),
        reset_requests=db.scalar(select(func.count(ResetRequest.id))) or 0,
    )


# --- shop details ---


@router.get("/settings", response_model=SiteAdminOut)
def get_site_settings(db: Session = Depends(get_db)) -> SiteAdminOut:
    return _admin_out(db)


@router.put("/settings", response_model=SiteAdminOut)
def save_site_settings(body: SiteValues, db: Session = Depends(get_db)) -> SiteAdminOut:
    """Open pages pick the new details up within seconds (next sync)."""
    site.save(db, body)
    db.commit()
    return _admin_out(db)


# --- your own password ---


@router.put("/me/password", response_model=AuthOut)
def change_my_password(body: AdminPasswordIn, db: Session = Depends(get_db), admin: User = Depends(site_admin)) -> AuthOut:
    """Signs out this admin's other sessions; this one gets a new sign-in."""
    if is_owner(admin):
        raise _conflict("Your password is ADMIN_PASSWORD in the server settings: change it there, and the next start uses it.")
    if not verify_password(body.current, admin.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is incorrect.")
    admin.password_hash = hash_password(body.new)
    _sign_out(admin)
    db.commit()
    return AuthOut(token=create_token(admin, "admin"), user=user_out(admin))


# --- people ---

Role = Literal["all", "customers", "shopkeepers", "admins", "blocked", "resets"]


def _user_rows(db: Session, user_ids: list[str] | None = None, *, role: Role = "all", query: str = "", limit: int = 200) -> list[AdminUserOut]:
    stats = (
        select(Order.user_id, func.count(Order.id).label("orders"), func.coalesce(func.sum(Order.total), 0).label("spent"))
        .where(Order.cancelled.is_(False))
        .group_by(Order.user_id)
        .subquery()
    )
    statement = (
        select(User, Profile, stats.c.orders, stats.c.spent, ResetRequest.requested_at)
        .outerjoin(Profile, Profile.user_id == User.id)
        .outerjoin(stats, stats.c.user_id == User.id)
        .outerjoin(ResetRequest, ResetRequest.user_id == User.id)
        .where(ACTIVE)
    )
    if user_ids is not None:
        statement = statement.where(User.id.in_(user_ids))
    if role == "customers":
        statement = statement.where(User.email.is_not(None))
    elif role == "shopkeepers":
        statement = statement.where(User.is_shopkeeper.is_(True))
    elif role == "admins":
        statement = statement.where(User.is_admin.is_(True))
    elif role == "blocked":
        statement = statement.where(User.blocked.is_(True))
    elif role == "resets":
        statement = statement.where(ResetRequest.id.is_not(None))
    if query:
        like = f"%{query.lower()}%"
        digits = "".join(character for character in query if character.isdigit())
        conditions = [func.lower(User.email).like(like), func.lower(Profile.full_name).like(like), func.lower(Profile.room_number).like(like)]
        if len(digits) >= 3:
            conditions += [User.phone.like(f"%{digits}%"), User.mobile.like(f"%{digits}%"), Profile.phone.like(f"%{digits}%")]
        statement = statement.where(or_(*conditions))
    # Waiting for a new password: oldest request first. Otherwise newest account first.
    order = (ResetRequest.requested_at, User.id) if role == "resets" else (User.created_at.desc(), User.id)
    rows = db.execute(statement.order_by(*order).limit(limit)).all()
    return [
        AdminUserOut(
            id=user.id,
            email=user.email,
            phone=user.phone,
            mobile=user.mobile,
            is_shopkeeper=user.is_shopkeeper,
            is_admin=user.is_admin,
            is_owner=is_owner(user),
            blocked=user.blocked,
            created_at=to_ms(user.created_at),
            profile=StoredProfile(full_name=profile.full_name, phone=profile.phone, block=profile.block, room_number=profile.room_number) if profile else None,
            order_count=orders or 0,
            spent=round(float(spent or 0), 2),
            reset_requested_at=to_ms(requested_at) if requested_at else None,
        )
        for user, profile, orders, spent, requested_at in rows
    ]


def _person(db: Session, user_id: str, *, lock: bool = False) -> User:
    user = db.get(User, user_id, with_for_update=True if lock else None)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    return user


def _one(db: Session, user: User) -> AdminUserOut:
    return _user_rows(db, [user.id])[0]


def _not_main_admin(user: User, action: str) -> None:
    if is_owner(user):
        raise _conflict(f"The main admin can't be {action} here: {MAIN_ADMIN}.")


@router.get("/users", response_model=list[AdminUserOut], response_model_exclude_none=True)
def list_users(
    role: Role = "all",
    q: str = Query("", max_length=100),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[AdminUserOut]:
    """Newest first (role=resets: who asked for a new password, oldest request first).
    q matches email, name, mobile number or room."""
    return _user_rows(db, role=role, query=q.strip(), limit=limit)


@router.post("/staff", response_model=AdminUserOut, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED)
def create_staff(body: StaffIn, db: Session = Depends(get_db)) -> AdminUserOut:
    """A new shopkeeper or admin, who signs in with this mobile number and password. The admin tells
    them (the shop sends nothing)."""
    taken = "This mobile number already has an account. Find it in People."
    if db.scalar(select(User.id).where(User.phone == body.phone)):
        raise _conflict(taken)
    user = User(
        phone=body.phone,
        password_hash=hash_password(body.password),
        is_shopkeeper=body.role == "shopkeeper",
        is_admin=body.role == "admin",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # the same number added twice at once
        db.rollback()
        raise _conflict(taken) from None
    return _one(db, user)


@router.post("/users/{user_id}/block", response_model=AdminUserOut, response_model_exclude_none=True)
def block_user(user_id: str, body: BlockIn, db: Session = Depends(get_db), admin: User = Depends(site_admin)) -> AdminUserOut:
    """A blocked account is signed out everywhere and can't sign in or order until unblocked."""
    user = _person(db, user_id)
    if body.blocked:
        _not_main_admin(user, "blocked")
        if user.id == admin.id:
            raise _conflict("You can't block your own account.")
    user.blocked = body.blocked
    db.commit()
    return _one(db, user)


@router.post("/users/{user_id}/sign-out", response_model=AdminUserOut, response_model_exclude_none=True)
def sign_out_user(user_id: str, db: Session = Depends(get_db)) -> AdminUserOut:
    user = _person(db, user_id)
    _sign_out(user)
    db.commit()
    return _one(db, user)


@router.put("/users/{user_id}/password", response_model=AdminUserOut, response_model_exclude_none=True)
def set_password(user_id: str, body: NewPasswordIn, db: Session = Depends(get_db), admin: User = Depends(site_admin)) -> AdminUserOut:
    """A new password for a customer who forgot hers, a shopkeeper or another admin (the admin tells
    them; the shop sends nothing). Every device they're signed in on is signed out, and a password
    request is done."""
    user = _person(db, user_id)
    _not_main_admin(user, "given a new password")
    if user.id == admin.id:
        raise _conflict("Change your own password in Access.")
    user.password_hash = hash_password(body.password)
    _sign_out(user)
    db.execute(delete(ResetRequest).where(ResetRequest.user_id == user.id))
    db.commit()
    return _one(db, user)


@router.delete("/users/{user_id}/reset-request", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_reset_request(user_id: str, db: Session = Depends(get_db)) -> Response:
    """Take a password request off the list without changing anything (e.g. she remembered it)."""
    db.execute(delete(ResetRequest).where(ResetRequest.user_id == _person(db, user_id).id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/users/{user_id}/profile", response_model=AdminUserOut, response_model_exclude_none=True)
@takes_turns
def edit_profile(user_id: str, body: ProfileBody, db: Session = Depends(get_db)) -> AdminUserOut:
    user = _person(db, user_id, lock=True)  # saves take turns: a double click can't create the profile twice
    profile = db.get(Profile, user.id) or Profile(user_id=user.id)
    profile.full_name, profile.phone, profile.block, profile.room_number = body.full_name, body.phone, body.block, body.room_number
    db.add(profile)
    if user.email is not None:
        user.mobile = body.phone  # her one mobile number: for delivery and for reaching her
    db.commit()
    return _one(db, user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
@takes_turns
def delete_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(site_admin)) -> Response:
    """Deletes a customer, shopkeeper or admin: the account can't sign in again, and its hostel
    details, wishlist votes, spin coupons and password request are deleted. Its past orders stay in
    Orders with the name, mobile and room they were placed with; an account that has orders is kept,
    emptied, only so they still belong to someone."""
    # Locked first (see orders.place_order), so an order she places at this moment is either counted
    # below or refused; it can't be deleted along with an account that looked like it had no orders.
    user = _person(db, user_id, lock=True)
    _not_main_admin(user, "deleted")
    if user.id == admin.id:
        raise _conflict("You can't delete your own account. Another admin can.")
    profile = db.get(Profile, user.id)
    if profile is not None:
        # Orders from before contact details were saved on each order keep them.
        db.execute(
            update(Order)
            .where(Order.user_id == user.id, Order.customer_phone.is_(None))
            .values(customer_name=profile.full_name, customer_phone=profile.phone, customer_block=profile.block, customer_room=profile.room_number)
        )
    voted = db.scalar(select(func.count()).select_from(WishRequest).where(WishRequest.user_id == user.id))
    for model in (Profile, WishRequest, Spin, ResetRequest):
        db.execute(delete(model).where(model.user_id == user.id))
    if db.scalar(select(Order.id).where(Order.user_id == user.id).limit(1)) is None:
        db.delete(user)
    else:
        user.email = user.phone = user.mobile = None
        user.is_admin = user.is_shopkeeper = user.blocked = False
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        user.deleted_at = utcnow()
        _sign_out(user)
    if voted:
        sync.bump(db, sync.WISHES)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- orders ---

OrderStatus = Literal["all", "open", "awaiting", "fulfilled", "cancelled"]


@router.get("/orders", response_model=list[AdminOrderOut], response_model_exclude_none=True)
def all_orders(
    status_filter: OrderStatus = Query("all", alias="status"),
    q: str = Query("", max_length=100),
    limit: int = Query(300, ge=1, le=1000),
    before: int | None = Query(None, ge=1, le=MAX_ID),
    user: str | None = Query(None, max_length=36),
    db: Session = Depends(get_db),
) -> list[AdminOrderOut]:
    """Every order ever (newest first), including cancelled ones. q matches the order number, customer
    name, mobile, room, email or UPI reference; user: one account's orders; before: orders older than
    that order number (the next page)."""
    statement = select(Order).outerjoin(User, User.id == Order.user_id).options(selectinload(Order.items))
    if before is not None:
        statement = statement.where(Order.id < before)
    if user:
        statement = statement.where(Order.user_id == user)
    live = Order.cancelled.is_(False)
    statement = statement.where(
        {
            "all": true(),
            "open": and_(live, Order.fulfilled.is_(False)),
            "awaiting": and_(live, Order.payment == "UPI", Order.payment_confirmed.is_(False)),
            "fulfilled": and_(live, Order.fulfilled.is_(True)),
            "cancelled": Order.cancelled.is_(True),
        }[status_filter]
    )
    query = q.strip().lower()
    if query:
        like = f"%{query}%"
        conditions = [
            func.lower(Order.customer_name).like(like),
            func.lower(Order.customer_room).like(like),
            func.lower(Order.utr).like(like),
            func.lower(User.email).like(like),
        ]
        digits = "".join(character for character in query if character.isdigit())
        if digits and int(digits) <= MAX_ID:  # a mobile number is too big to be an order number
            conditions.append(Order.id == int(digits))
        if len(digits) >= 3:
            conditions += [Order.customer_phone.like(f"%{digits}%"), User.phone.like(f"%{digits}%")]
        statement = statement.where(or_(*conditions))
    orders = list(db.scalars(statement.order_by(Order.id.desc()).limit(limit)))
    return _with_customers(db, orders)


@router.post("/orders/{order_id}/cancel", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def cancel_order(order_id: IdPath, db: Session = Depends(get_db)) -> AdminOrderOut:
    """Cancels an order that hasn't been handed over: its items (and any free pick) go back on the
    shelf, an unexpired spin coupon it used works again, and a first-order discount is available again
    if this was her only order. It no longer counts in sales. If she paid by UPI, the money is
    returned outside the app."""
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
    for freebie in order.freebies or []:
        if freebie.endswith(FREE_PICK_SUFFIX):
            name = freebie.removesuffix(FREE_PICK_SUFFIX)
            product_id = db.scalar(select(Product.id).where(Product.name == name, Product.active).order_by(Product.id).limit(1))
            if product_id is not None:
                back[product_id] = back.get(product_id, 0) + 1
    for product_id in sorted(back):
        db.execute(update(Product).where(Product.id == product_id).values(stock=Product.stock + back[product_id]))
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
