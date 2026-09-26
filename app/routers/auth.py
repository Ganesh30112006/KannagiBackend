from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import site
from ..database import get_db
from ..models import ResetRequest, User, utcnow
from ..schemas import AuthIn, AuthOut, EmailIn, MessageOut, SignupIn, StaffLoginIn, UserOut
from ..security import BLOCKED, DUMMY_HASH, SessionKind, create_token, current_user, hash_password, verify_password
from ..services import user_out
from ..throttle import FailureLimiter

router = APIRouter(prefix="/auth", tags=["auth"])

WINDOW = 15 * 60
# Per-IP limits are loose because a whole hostel can share one public IP.
_login_failures = FailureLimiter("login", 10, WINDOW, "Too many failed sign-ins. Try again in 15 minutes.")
_login_ip_failures = FailureLimiter("login-ip", 100, WINDOW, "Too many failed sign-ins. Try again in 15 minutes.")
# Admin and shopkeeper sign-ins open the dashboard or everything, so they get fewer guesses. Each
# account has its own password, capped at 5 guesses per number (switching numbers only moves on to a
# different password) and 20 per network. From everyone together it takes a flood from many networks
# to pause them, so one person can't keep every shopkeeper and admin from signing in. Devices already
# signed in stay signed in.
_staff_failures = FailureLimiter("staff", 5, WINDOW, "Too many wrong passwords. Try again in 15 minutes.")
_staff_ip_failures = FailureLimiter("staff-ip", 20, WINDOW, "Too many wrong passwords. Try again in 15 minutes.")
_staff_all_failures = FailureLimiter("staff-all", 100, WINDOW, "Too many wrong passwords. Sign-in is paused for 15 minutes.")
EVERYONE = "all"
# Reset requests are counted as "failures" so nobody can flood the admin's list.
HOUR = 60 * 60
_reset_requests = FailureLimiter("reset-request", 3, HOUR, "Your request is already with the shop. Please wait for them to contact you.")
_reset_ip_requests = FailureLimiter("reset-request-ip", 30, HOUR, "Too many reset requests. Try again in an hour.")
# New accounts per network per hour (loose: the whole hostel may sign up at once on one Wi-Fi).
_signups_ip = FailureLimiter("signup-ip", 60, HOUR, "Too many new accounts from this network. Try again in an hour.")

def _ip(request: Request) -> str:
    forwarded = request.headers.get("x-client-ip") if getattr(request.state, "trusted", False) else None
    return f"ip:{forwarded or (request.client.host if request.client else 'unknown')}"


@router.post("/signup", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)) -> AuthOut:
    """The email is only her username (nothing is ever emailed); the mobile number lets the shop reach her."""
    email = body.email.lower()
    if not site.values(db).signups_open:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "New accounts are closed right now. Please ask the shop.")
    _signups_ip.check(db, _ip(request))
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Please sign in.")
    user = User(email=email, mobile=body.mobile, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Please sign in.") from None
    _signups_ip.fail(db, _ip(request))
    return AuthOut(token=create_token(user), user=user_out(user))


@router.post("/login", response_model=AuthOut)
def login(body: AuthIn, request: Request, db: Session = Depends(get_db)) -> AuthOut:
    email = body.email.lower()
    _login_failures.check(db, email)
    _login_ip_failures.check(db, _ip(request))
    user = db.scalar(select(User).where(User.email == email))
    if not verify_password(body.password, user.password_hash if user else DUMMY_HASH) or user is None:
        _login_failures.fail(db, email)
        _login_ip_failures.fail(db, _ip(request))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")
    _login_failures.reset(db, email)
    if user.blocked:  # said only after the right password, so it doesn't reveal who has an account
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLOCKED)
    return AuthOut(token=create_token(user), user=user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return user_out(user)


@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(body: EmailIn, request: Request, db: Session = Depends(get_db)) -> MessageOut:
    """Asks the shop for a new password. Nothing is sent: the request waits at /admin, where the
    site admin sets a new password and tells her on her mobile number."""
    email = body.email.lower()
    _reset_requests.check(db, email)
    _reset_ip_requests.check(db, _ip(request))
    _reset_requests.fail(db, email)
    _reset_ip_requests.fail(db, _ip(request))
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and not user.blocked:
        request_row = db.scalar(select(ResetRequest).where(ResetRequest.user_id == user.id))
        if request_row is None:
            db.add(ResetRequest(user_id=user.id, requested_at=utcnow()))
        else:
            request_row.requested_at = utcnow()
        try:
            db.commit()
        except IntegrityError:  # the same request twice at once
            db.rollback()
    # Same answer either way, so this can't be used to find out who has an account.
    return MessageOut(
        message="Request sent. The shop will set a new password for you and send it to your mobile number."
    )


def _staff_login(body: StaffLoginIn, request: Request, db: Session, kind: SessionKind) -> AuthOut:
    """Mobile number and password: the admin gave these to the shopkeeper or admin."""
    ip = _ip(request)
    _staff_failures.check(db, body.phone)
    _staff_ip_failures.check(db, ip)
    _staff_all_failures.check(db, EVERYONE)
    user = db.scalar(select(User).where(User.phone == body.phone))
    if not verify_password(body.password, user.password_hash if user else DUMMY_HASH) or user is None:
        _staff_failures.fail(db, body.phone)
        _staff_ip_failures.fail(db, ip)
        _staff_all_failures.fail(db, EVERYONE)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect mobile number or password.")
    _staff_failures.reset(db, body.phone)
    # Said only after the right password, so these don't reveal which numbers have accounts.
    if user.blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLOCKED)
    if kind == "shopkeeper" and not user.is_shopkeeper:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account isn't a shopkeeper. Ask an admin.")
    if kind == "admin" and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account isn't an admin.")
    return AuthOut(token=create_token(user, kind), user=user_out(user))


@router.post("/shopkeeper-login", response_model=AuthOut)
def shopkeeper_login(body: StaffLoginIn, request: Request, db: Session = Depends(get_db)) -> AuthOut:
    """A shopkeeper opens the dashboard with her mobile number and the password an admin set."""
    return _staff_login(body, request, db, "shopkeeper")


@router.post("/admin-login", response_model=AuthOut)
def admin_login(body: StaffLoginIn, request: Request, db: Session = Depends(get_db)) -> AuthOut:
    """An admin opens /admin with their mobile number and password. The session lasts 12 hours."""
    return _staff_login(body, request, db, "admin")
