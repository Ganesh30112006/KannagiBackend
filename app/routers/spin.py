"""Spin & Win: the server picks the prize so the daily limit and coupons can't be faked."""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Spin, User, utcnow
from ..schemas import SpinResult, SpinStatus, WheelPrize
from ..security import current_user
from ..services import COUPON_HOURS, active_prizes, coupon_out, current_coupon, get_settings, shop_date

router = APIRouter(prefix="/spin", tags=["spin"])


def _spun_today(db: Session, user: User) -> bool:
    return db.scalar(select(Spin.id).where(Spin.user_id == user.id, Spin.spun_on == shop_date())) is not None


@router.get("", response_model=SpinStatus)
def spin_status(user: User = Depends(current_user), db: Session = Depends(get_db)) -> SpinStatus:
    return SpinStatus(spun_today=_spun_today(db, user), coupon=coupon_out(current_coupon(db, user)))


@router.post("", response_model=SpinResult)
def spin(user: User = Depends(current_user), db: Session = Depends(get_db)) -> SpinResult:
    if _spun_today(db, user):
        raise HTTPException(status.HTTP_409_CONFLICT, "You already spun today. Come back tomorrow! 💫")
    prizes = active_prizes(get_settings(db))
    index = secrets.randbelow(len(prizes))
    prize = prizes[index]
    # A new spin replaces any previous coupon, including a "Better Luck" result.
    row = Spin(
        user_id=user.id,
        spun_on=shop_date(),
        code=prize["code"],
        kind=prize.get("kind"),
        label=prize["label"],
        icon=prize.get("icon", ""),
        expires_at=utcnow() + timedelta(hours=COUPON_HOURS) if prize.get("kind") else None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "You already spun today. Come back tomorrow! 💫") from None
    return SpinResult(
        index=index,
        prize=WheelPrize.model_validate(prize),
        prizes=[WheelPrize.model_validate(p) for p in prizes],
        coupon=coupon_out(row),
    )
