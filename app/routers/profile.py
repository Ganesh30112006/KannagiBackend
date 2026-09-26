from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db, takes_turns
from ..models import Profile, User
from ..schemas import ProfileBody
from ..security import current_user

router = APIRouter(prefix="/profile", tags=["profile"])


def _out(profile: Profile) -> ProfileBody:
    return ProfileBody(
        full_name=profile.full_name, phone=profile.phone, block=profile.block, room_number=profile.room_number  # type: ignore[arg-type]
    )


@router.get("", response_model=ProfileBody | None)
def get_profile(user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProfileBody | None:
    profile = db.get(Profile, user.id)
    return _out(profile) if profile else None


@router.put("", response_model=ProfileBody)
@takes_turns
def save_profile(body: ProfileBody, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProfileBody:
    # Her saves take turns (a double tap on a first save would otherwise create the profile twice).
    db.execute(select(User.id).where(User.id == user.id).with_for_update(key_share=True))
    profile = db.get(Profile, user.id) or Profile(user_id=user.id)
    profile.full_name = body.full_name
    profile.phone = body.phone
    profile.block = body.block
    profile.room_number = body.room_number
    db.add(profile)
    if user.email is not None:
        user.mobile = body.phone  # her one mobile number: for delivery and for reaching her
    db.commit()
    return _out(profile)
