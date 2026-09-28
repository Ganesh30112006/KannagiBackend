import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from .config import settings
from .routers import admin, alerts, auth, orders, profile, session, shop, siteadmin, spin
from .startup import prepare_database

PUBLIC_PATHS = {"/api/health"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.check()
    prepare_database()  # tables, upgrades, default rows, and the main admin from ADMIN_MOBILE / ADMIN_PASSWORD
    yield


docs = settings.enable_docs and not settings.is_production
app = FastAPI(
    title="Kannagi Night Mart API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if docs else None,
)


def _has_internal_key(request: Request) -> bool:
    if not settings.internal_api_key:
        return not settings.is_production  # dev without a key: open locally
    supplied = request.headers.get("x-internal-key", "")
    return hmac.compare_digest(supplied.encode(), settings.internal_api_key.encode())


@app.middleware("http")
async def guard(request: Request, call_next) -> Response:
    path = request.url.path
    trusted = _has_internal_key(request)
    if not trusted and path not in PUBLIC_PATHS and not (docs and path in {"/docs", "/openapi.json"}):
        # Answer 404 rather than 401 so the API doesn't advertise that it exists.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    # Only the website server (holder of the key) may tell us the real client IP.
    request.state.trusted = trusted
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_request_bytes:
        return JSONResponse({"detail": "Request is too large."}, status_code=413)
    # Postgres can't store a NUL character: refuse one in the address rather than fail on it later.
    # (Request bodies are checked field by field, see schemas.CamelModel.)
    if "\x00" in path or any("\x00" in part for item in request.query_params.multi_items() for part in item):
        return JSONResponse({"detail": "The request has characters that aren't allowed."}, status_code=400)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, error: RequestValidationError) -> JSONResponse:
    """Where and why, without echoing what was sent. FastAPI's own answer repeats the input, and some
    inputs (NaN, Infinity, broken unicode) can't be written back as JSON, which turned a 422 into a 500."""
    detail = [{"type": item["type"], "loc": list(item["loc"]), "msg": item["msg"]} for item in error.errors()]
    return JSONResponse({"detail": detail}, status_code=422)


for module in (auth, profile, shop, spin, orders, admin, siteadmin, session, alerts):
    app.include_router(module.router, prefix="/api")


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
