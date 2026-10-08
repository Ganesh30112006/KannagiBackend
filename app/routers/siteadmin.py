"""The site admin's controls (/admin on the website): shop details, admins and shopkeepers (their
accounts and passwords), customers (including new passwords for those who forgot theirs), deleting
accounts, every order, investment (stock bought and the stock left) and profit (day by day and item by
item). Products, offers and store status use the shopkeeper endpoints, which admins may also call.

Anyone else gets 404 from these endpoints, the same as for a path that doesn't exist."""

import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import alerts, site, sync
from ..config import settings
from ..database import get_db, takes_turns
from ..models import (
    AlertDevice,
    ManualSale,
    ManualSaleItem,
    Order,
    OrderItem,
    Product,
    Profile,
    OrderRequest,
    ResetRequest,
    Spin,
    StockEntry,
    User,
    WishRequest,
    utcnow,
)
from ..schemas import (
    MAX_ID,
    AdminOrderOut,
    AdminOverview,
    AdminPasswordIn,
    AdminUserOut,
    AuthOut,
    BlockIn,
    IdPath,
    Investment,
    InvestmentItem,
    InvestmentPeriod,
    NewPasswordIn,
    ProfileBody,
    Profit,
    ProfitDay,
    ProfitFigures,
    ProfitItem,
    SiteAdminOut,
    SiteValues,
    StaffIn,
    StockEntryOut,
    StockLeft,
    StoredProfile,
    WishIn,
)
from ..security import create_token, hash_password, site_admin, verify_password
from ..services import MARKUP, get_settings, is_egg, markup_of, paise, picked_item, rupees, sale_price, shop_now, store_online, to_ms, user_out
from ..staff import is_owner
from .admin import _with_customers
from .shop import clear_wishes

router = APIRouter(prefix="/site-admin", tags=["site admin"], dependencies=[Depends(site_admin)])

ACTIVE = User.deleted_at.is_(None)  # deleted accounts kept only for their orders are left out
MAIN_ADMIN = "the main admin is set in the server settings (ADMIN_MOBILE, ADMIN_PASSWORD)"


def _admin_out(db: Session) -> SiteAdminOut:
    return SiteAdminOut(**site.values(db).model_dump(), photo_uploads_enabled=settings.cloudinary_url != "")


def _sign_out(user: User) -> None:
    """Every device this account is signed in on has to sign in again."""
    user.password_changed_at = utcnow()


def _conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


def _day_starts(day: date) -> datetime:
    """When a shop day (SHOP_TIMEZONE) starts, as the database's naive UTC."""
    return datetime.combine(day, time(), tzinfo=settings.tz).astimezone(timezone.utc).replace(tzinfo=None)


# --- overview ---


@router.get("/overview", response_model=AdminOverview)
def overview(db: Session = Depends(get_db)) -> AdminOverview:
    midnight = _day_starts(shop_now().date())
    live = Order.cancelled.is_(False)
    paid = or_(Order.payment != "UPI", Order.payment_confirmed.is_(True))
    manual_today = and_(ManualSale.cancelled.is_(False), ManualSale.created_at >= midnight)
    online_today = float(db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(live, paid, Order.created_at >= midnight)) or 0)
    manual_money_today = float(db.scalar(select(func.coalesce(func.sum(ManualSale.total), 0)).where(manual_today)) or 0)
    shop = site.values(db)

    def people(*where) -> int:
        return db.scalar(select(func.count()).select_from(User).where(ACTIVE, *where)) or 0

    return AdminOverview(
        customers=people(User.is_customer.is_(True)),
        shopkeepers=people(User.is_shopkeeper.is_(True)),
        admins=people(User.is_admin.is_(True)),
        blocked=people(User.blocked.is_(True)),
        orders_today=db.scalar(select(func.count()).where(live, Order.created_at >= midnight)) or 0,
        manual_sales_today=db.scalar(select(func.count(ManualSale.id)).where(manual_today)) or 0,
        revenue_today=round(online_today + manual_money_today, 2),
        stock_value=round(float(db.scalar(select(func.coalesce(func.sum(Product.purchase_price * Product.stock), 0)).where(Product.active)) or 0), 2),
        open_orders=db.scalar(select(func.count()).where(live, Order.fulfilled.is_(False))) or 0,
        awaiting_payment=db.scalar(select(func.count()).where(live, Order.payment == "UPI", Order.payment_confirmed.is_(False))) or 0,
        store_online=store_online(get_settings(db), shop.open_hour, shop.close_hour),
        reset_requests=db.scalar(select(func.count(ResetRequest.id))) or 0,
    )


# --- investment ---

STOCK_ENTRIES_SHOWN = 200


def _period_days(period: InvestmentPeriod, today: date | None = None) -> tuple[date | None, date]:
    """The period's first and last day (the shop's dates, up to today); no first day for all time."""
    today = today or shop_now().date()
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    if period == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    return None, today


@router.get("/investment", response_model=Investment, response_model_exclude_none=True)
def investment(period: InvestmentPeriod = "month", db: Session = Depends(get_db)) -> Investment:
    """What went into stock, at MRP (what the shop pays): new stock bought in the period (stock added by
    hand on the dashboard, new items' starting stock included) and stock taken off by hand (corrections,
    deleted items), from the stock record; and the stock left right now, from the items on sale."""
    first, last = _period_days(period)
    when = [StockEntry.created_at < _day_starts(last + timedelta(days=1))]
    if first is not None:
        when.append(StockEntry.created_at >= _day_starts(first))
    value = StockEntry.change * StockEntry.purchase_price

    def moved(which) -> tuple[int, float]:
        units, money = db.execute(
            select(func.coalesce(func.sum(StockEntry.change), 0), func.coalesce(func.sum(value), 0)).where(*when, which)
        ).one()
        return abs(int(units)), round(abs(float(money)), 2)

    bought_units, bought = moved(StockEntry.change > 0)
    taken_off_units, taken_off = moved(StockEntry.change < 0)
    per_item = db.execute(
        select(StockEntry.product_name, func.sum(StockEntry.change), func.sum(value))
        .where(*when, StockEntry.change > 0)
        .group_by(StockEntry.product_name)
    )
    bought_items = sorted(
        (InvestmentItem(name=name, qty=int(units), value=round(float(money), 2)) for name, units, money in per_item),
        key=lambda item: (-item.value, item.name),
    )
    entries = db.scalars(
        select(StockEntry).where(*when).order_by(StockEntry.created_at.desc(), StockEntry.id.desc()).limit(STOCK_ENTRIES_SHOWN)
    )
    since = db.scalar(select(func.min(StockEntry.created_at)))

    in_stock = list(db.scalars(select(Product).where(Product.active, Product.stock > 0)))
    left_items = sorted(
        (
            InvestmentItem(name=product.name, emoji=product.emoji, qty=product.stock, value=rupees(paise(product.purchase_price) * product.stock))
            for product in in_stock
        ),
        key=lambda item: (-item.value, item.name),
    )
    return Investment(
        period=period,
        start=first.isoformat() if first else None,
        end=last.isoformat(),
        tracking_since=to_ms(since) if since else None,
        bought=bought,
        bought_units=bought_units,
        bought_items=bought_items,
        taken_off=taken_off,
        taken_off_units=taken_off_units,
        entries=[
            StockEntryOut(
                id=entry.id,
                created_at=to_ms(entry.created_at),
                name=entry.product_name,
                change=entry.change,
                price=entry.purchase_price,
                recorded_by=entry.recorded_by,
            )
            for entry in entries
        ],
        entry_count=db.scalar(select(func.count(StockEntry.id)).where(*when)) or 0,
        left=StockLeft(
            items=len(in_stock),
            units=sum(product.stock for product in in_stock),
            value=rupees(sum(paise(product.purchase_price) * product.stock for product in in_stock)),
            sale_value=rupees(sum(paise(sale_price(product)) * product.stock for product in in_stock)),
        ),
        left_items=left_items,
    )


# --- profit ---

GIFT_PRICE = re.compile(r"₹(\d+(?:\.\d+)?)")  # "₹5 chocolate (free)", "₹10 free snack"
Line = tuple[str, int, int, int]  # an item sold: name, units, MRP each and price each (paise)


def _day_starts_at(open_hour: int, close_hour: int) -> int:
    """The hour (shop's time) a day starts for daily profit: halfway through the hours the shop is closed,
    so one night's sales (11 PM to 1 AM: noon to noon) are one day, named after the date the shop opened
    on. Negative: that hour the evening before, for a shop that opens after midnight."""
    closed = (open_hour - close_hour) % 24
    hour = (close_hour + closed // 2) % 24
    return hour if open_hour >= hour else hour - 24


def _shop_day_starts(day: date, starts_at: int) -> datetime:
    """When that day starts, as the database's naive UTC."""
    local = datetime.combine(day, time(), tzinfo=settings.tz) + timedelta(hours=starts_at)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _shop_day(moment: datetime, starts_at: int) -> date:
    """The day a (naive UTC) moment counts in."""
    local = moment.replace(tzinfo=timezone.utc).astimezone(settings.tz).replace(tzinfo=None)
    return (local - timedelta(hours=starts_at)).date()


@dataclass
class _Tally:
    """Sales added up, in paise."""

    count: int = 0
    units: int = 0
    revenue: int = 0
    cost: int = 0
    gifts: int = 0

    def add(self, revenue: int, cost: int, units: int, gifts: int = 0) -> None:
        self.count += 1
        self.units += units
        self.revenue += revenue
        self.cost += cost
        self.gifts += gifts

    @property
    def profit(self) -> int:
        return self.revenue - self.cost - self.gifts

    def figures(self) -> ProfitFigures:
        return ProfitFigures(
            count=self.count,
            items=self.units,
            revenue=rupees(self.revenue),
            cost=rupees(self.cost),
            gifts=rupees(self.gifts),
            profit=rupees(self.profit),
        )


def _sales(db: Session, columns: list, item, link, where) -> list[tuple[tuple, list[Line]]]:
    """Each sale's columns (its id first) and its items, read in one query, so an order cancelled at this
    moment is either counted whole or not at all."""
    rows = db.execute(
        select(*columns, item.product_name, item.quantity, item.purchase_price, item.sale_price)
        .outerjoin(item, link)
        .where(where)
        .order_by(columns[0], item.id)
    )
    sales: dict[int, tuple[tuple, list[Line]]] = {}
    for row in rows:
        *head, name, qty, cost, price = row
        lines = sales.setdefault(head[0], (tuple(head), []))[1]
        if name is not None:
            lines.append((name, qty, paise(cost), paise(price)))
    return list(sales.values())


@router.get("/profit", response_model=Profit, response_model_exclude_none=True)
def profit(period: InvestmentPeriod = "month", db: Session = Depends(get_db)) -> Profit:
    """Profit is the money received minus what the items sold cost (their MRP) and the free gifts given
    with orders. Day by day (each day runs from _day_starts_at, so a night stays whole), and item by item
    at the shop's prices, with delivery fees, discounts and changed manual amounts beside them so it all
    adds up; and the profit the stock left would make at the shop's prices."""
    shop = site.values(db)
    starts_at = _day_starts_at(shop.open_hour, shop.close_hour)
    first, last = _period_days(period, _shop_day(utcnow(), starts_at))

    def within(column) -> list:
        when = [column < _shop_day_starts(last + timedelta(days=1), starts_at)]
        if first is not None:
            when.append(column >= _shop_day_starts(first, starts_at))
        return when

    # Counted as in the dashboard's Summary: UPI orders once their payment is confirmed; not cancelled
    # orders or undone manual sales.
    counted = and_(Order.cancelled.is_(False), or_(Order.payment != "UPI", Order.payment_confirmed.is_(True)), *within(Order.created_at))
    kept = and_(ManualSale.cancelled.is_(False), *within(ManualSale.created_at))
    orders = _sales(db, [Order.id, Order.created_at, Order.total, Order.discount, Order.freebies], OrderItem, OrderItem.order_id == Order.id, counted)
    manual_sales = _sales(db, [ManualSale.id, ManualSale.created_at, ManualSale.total], ManualSaleItem, ManualSaleItem.sale_id == ManualSale.id, kept)

    # Items by name now (on sale first, then the newest): emojis, and what a free pick cost.
    picks = {name for (*_, freebies), _ in orders for gift in freebies or [] if (name := picked_item(gift))}
    names = {name for _, lines in (*orders, *manual_sales) for name, *_ in lines} | picks
    catalog = {
        product.name: product
        for product in db.scalars(select(Product).where(Product.name.in_(names)).order_by(Product.active, Product.id))
    } if names else {}

    def egg_markup(name: str) -> int:
        """In paise: the eggs' markup, as on their card (a sale doesn't record it)."""
        eggs = catalog.get(name)
        return (MARKUP if eggs is None else markup_of(eggs)) * 100

    def gifts_cost(freebies) -> int:
        """At the price of the gift: a free pick at its item's MRP; "₹5 chocolate (free)" at ₹5."""
        cost = 0
        for gift in freebies or []:
            if not isinstance(gift, str):
                continue
            name = picked_item(gift)
            picked = catalog.get(name) if name else None
            if picked is not None:
                cost += paise(picked.purchase_price)
            elif found := GIFT_PRICE.search(gift):
                cost += paise(float(found.group(1)))
        return cost

    per_item: dict[str, list[int]] = {}  # name -> [units, at the shop's prices, cost]

    def items_value(lines: list[Line]) -> tuple[int, int, int]:
        """A sale's items at the shop's prices, what they cost and how many, also added to per_item. Eggs
        are MRP each plus their markup once (the eggs' own markup: the sale doesn't record it)."""
        value = cost_total = units = 0
        for name, qty, cost, price in lines:
            line_value = price * qty + (egg_markup(name) if is_egg(name) and qty > 0 else 0)
            item = per_item.setdefault(name, [0, 0, 0])
            item[0] += qty
            item[1] += line_value
            item[2] += cost * qty
            value, cost_total, units = value + line_value, cost_total + cost * qty, units + qty
        return value, cost_total, units

    online, manual = _Tally(), _Tally()
    days: dict[date, tuple[_Tally, _Tally]] = {}
    fees = discounts = changes = 0
    for (_, created_at, total, discount, freebies), lines in orders:
        value, cost, units = items_value(lines)
        received, off, gifts = paise(total), paise(discount), gifts_cost(freebies)
        fees += received + off - value  # total = items + delivery fee - discount
        discounts += off
        online.add(received, cost, units, gifts)
        days.setdefault(_shop_day(created_at, starts_at), (_Tally(), _Tally()))[0].add(received, cost, units, gifts)
    for (_, created_at, total), lines in manual_sales:
        value, cost, units = items_value(lines)
        received = paise(total)
        changes += received - value
        manual.add(received, cost, units)
        days.setdefault(_shop_day(created_at, starts_at), (_Tally(), _Tally()))[1].add(received, cost, units)

    together = _Tally(
        count=online.count + manual.count,
        units=online.units + manual.units,
        revenue=online.revenue + manual.revenue,
        cost=online.cost + manual.cost,
        gifts=online.gifts,
    )
    day_rows = []
    day = last
    while day >= (first or min(days, default=last)):
        day_online, day_manual = days.get(day, (_Tally(), _Tally()))
        day_rows.append(
            ProfitDay(
                day=day.isoformat(),
                orders=day_online.count,
                manual_sales=day_manual.count,
                revenue=rupees(day_online.revenue + day_manual.revenue),
                cost=rupees(day_online.cost + day_manual.cost),
                gifts=rupees(day_online.gifts),
                profit=rupees(day_online.profit + day_manual.profit),
            )
        )
        day -= timedelta(days=1)

    def item_out(name: str, qty: int, value: int, cost: int) -> ProfitItem:
        product = catalog.get(name)
        return ProfitItem(
            name=name,
            emoji=product.emoji if product else None,
            qty=qty,
            revenue=rupees(value),
            cost=rupees(cost),
            profit=rupees(value - cost),
        )

    sold = sorted((item_out(name, *figures) for name, figures in per_item.items()), key=lambda item: (-item.profit, item.name))
    in_stock = list(db.scalars(select(Product).where(Product.active, Product.stock > 0)))
    left = [
        (product, paise(sale_price(product)) * product.stock, paise(product.purchase_price) * product.stock)
        for product in in_stock
    ]
    stock_items = sorted(
        (
            ProfitItem(name=product.name, emoji=product.emoji, qty=product.stock, revenue=rupees(value), cost=rupees(cost), profit=rupees(value - cost))
            for product, value, cost in left
        ),
        key=lambda item: (-item.profit, item.name),
    )
    waiting, waiting_money = db.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0)).where(
            Order.cancelled.is_(False), Order.payment == "UPI", Order.payment_confirmed.is_(False), *within(Order.created_at)
        )
    ).one()
    return Profit(
        period=period,
        start=first.isoformat() if first else None,
        end=last.isoformat(),
        day_starts_at=starts_at,
        total=together.figures(),
        online=online.figures(),
        manual=manual.figures(),
        days=day_rows,
        items=sold,
        item_profit=rupees(sum(value - cost for _, value, cost in per_item.values())),
        delivery_fees=rupees(fees),
        discounts=rupees(discounts),
        amount_changes=rupees(changes),
        awaiting=waiting,
        awaiting_money=rupees(paise(float(waiting_money))),
        stock=StockLeft(
            items=len(in_stock),
            units=sum(product.stock for product in in_stock),
            value=rupees(sum(cost for *_, cost in left)),
            sale_value=rupees(sum(value for _, value, _ in left)),
        ),
        stock_items=stock_items,
    )


# --- shop details ---


@router.get("/settings", response_model=SiteAdminOut)
def get_site_settings(db: Session = Depends(get_db)) -> SiteAdminOut:
    return _admin_out(db)


@router.put("/settings", response_model=SiteAdminOut)
def save_site_settings(body: SiteValues, db: Session = Depends(get_db)) -> SiteAdminOut:
    """Open pages pick the new details up within seconds (next sync)."""
    if "markup" not in body.model_fields_set:
        body.markup = site.values(db).markup  # not on the form any more: keep what's stored
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
        statement = statement.where(User.is_customer.is_(True))
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
            is_customer=user.is_customer,
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
    q matches name, mobile number or room (or an older account's email)."""
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
    if user.is_customer and user.mobile is None:
        user.mobile = body.phone  # an account from before sign-up asked for a number signs in with this one
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
    for model in (Profile, WishRequest, Spin, ResetRequest, AlertDevice, OrderRequest):
        db.execute(delete(model).where(model.user_id == user.id))
    if db.scalar(select(Order.id).where(Order.user_id == user.id).limit(1)) is None:
        db.delete(user)
    else:
        user.email = user.phone = user.mobile = None
        user.is_admin = user.is_shopkeeper = user.is_customer = user.blocked = False
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        user.deleted_at = utcnow()
        _sign_out(user)
    if voted:
        sync.bump(db, sync.WISHES)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- wishlist requests ---


@router.post("/wishes/remove", status_code=status.HTTP_204_NO_CONTENT)
def remove_wish(body: WishIn, db: Session = Depends(get_db)) -> Response:
    """Take an item's requests off the wishlist, every customer's (the list shows them as one line).
    Nothing to remove (already removed, e.g. by another admin) is fine too."""
    clear_wishes(db, body.name)
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
            conditions += [Order.customer_phone.like(f"%{digits}%"), User.phone.like(f"%{digits}%"), User.mobile.like(f"%{digits}%")]
        statement = statement.where(or_(*conditions))
    orders = list(db.scalars(statement.order_by(Order.id.desc()).limit(limit)))
    return _with_customers(db, orders)


@router.post("/orders/{order_id}/cancel", response_model=AdminOrderOut, response_model_exclude_none=True)
@takes_turns
def cancel_order(order_id: IdPath, background: BackgroundTasks, db: Session = Depends(get_db)) -> AdminOrderOut:
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
        if name := picked_item(freebie):
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
    background.add_task(alerts.order_update, order.id, "cancelled")  # her devices hear it
    return _with_customers(db, [order])[0]
