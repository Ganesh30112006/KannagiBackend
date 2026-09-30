"""Notifications on a phone or laptop (see alerts.py): turning them on and off for one device, a test,
and (admins) the day's summary now. For any signed-in account: what a device gets follows the sign-in
that turned it on (new orders for shopkeepers and admins, her own orders and requests for a customer)."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import alerts
from ..database import get_db
from ..models import AlertDevice, User, utcnow
from ..schemas import AlertDeviceIn, AlertEndpointIn, AlertKeyOut, AlertTestOut
from ..security import _password_version, current_user, is_site_admin

router = APIRouter(prefix="/alerts", tags=["notifications"], dependencies=[Depends(current_user)])

NOT_SET_UP = "Notifications aren't set up for this shop yet."


@router.get("/key", response_model=AlertKeyOut)
def key() -> AlertKeyOut:
    """What browsers turn alerts on with (null: alerts aren't set up on the server)."""
    return AlertKeyOut(public_key=alerts.public_key())


@router.put("/device", status_code=status.HTTP_204_NO_CONTENT)
def turn_on(body: AlertDeviceIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Alerts on for this device, for this sign-in. The dashboard sends it again each time it opens, which
    keeps the device on the latest sign-in there."""
    if alerts.signing_key() is None:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_SET_UP)
    now = utcnow()
    values = {
        "user_id": user.id,
        "p256dh": body.keys.p256dh,
        "auth": body.keys.auth,
        "role": user.session_kind,  # type: ignore[attr-defined]
        "password_version": _password_version(user),
        "session_expires_at": user.session_expires_at,  # type: ignore[attr-defined]
        "updated_at": now,
    }
    # One row per device, even when it's sent twice at once (a double tap, two tabs).
    insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(AlertDevice).values(endpoint=body.endpoint, created_at=now, **values).on_conflict_do_update(index_elements=[AlertDevice.endpoint], set_=values))
    # Only the devices used most recently keep alerts on.
    oldest = db.scalars(
        select(AlertDevice.id)
        .where(AlertDevice.user_id == user.id)
        .order_by(AlertDevice.updated_at.desc(), AlertDevice.id.desc())
        .offset(alerts.MAX_DEVICES)
    ).all()
    if oldest:
        db.execute(delete(AlertDevice).where(AlertDevice.id.in_(oldest)))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/device/off", status_code=status.HTTP_204_NO_CONTENT)
def turn_off(body: AlertEndpointIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    """Alerts off for this device (also when signing out on it). Nothing to do if they weren't on."""
    db.execute(delete(AlertDevice).where(AlertDevice.endpoint == body.endpoint, AlertDevice.user_id == user.id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/test", response_model=AlertTestOut, response_model_exclude_none=True)
def test(body: AlertEndpointIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> AlertTestOut:
    """A test alert to this device, so the shopkeeper sees what an order's looks like."""
    device = db.scalar(select(AlertDevice).where(AlertDevice.endpoint == body.endpoint, AlertDevice.user_id == user.id))
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifications aren't on for this device. Turn them on again.")
    return alerts.send_test(db, device)


@router.post("/summary", response_model=AlertTestOut, response_model_exclude_none=True)
def summary_now(body: AlertEndpointIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> AlertTestOut:
    """Admins: today's summary (sent to every admin at closing time) to this device, now."""
    if not is_site_admin(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    device = db.scalar(select(AlertDevice).where(AlertDevice.endpoint == body.endpoint, AlertDevice.user_id == user.id))
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifications aren't on for this device. Turn them on again.")
    if alerts.signing_key() is None:
        return AlertTestOut(sent=False, detail=NOT_SET_UP)
    if alerts.send_summary(db, device) != 1:
        return AlertTestOut(sent=False, detail="The browser's notification service didn't take it. Try again in a minute.")
    return AlertTestOut(sent=True)
