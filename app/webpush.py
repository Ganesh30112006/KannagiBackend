"""Web Push, the standard browsers use for notifications from a website that isn't open: RFC 8030 (delivery),
RFC 8291 (encryption) and RFC 8292 (VAPID, the shop's own key that signs each push).

A browser that turns alerts on gives the website a push address at its maker's push service (Google for
Chrome, Edge on Android and Samsung Internet; Apple for Safari; Mozilla for Firefox; Microsoft for Edge on
Windows) and two keys. Each alert is encrypted for that browser alone and posted to its address; the push
service passes it on to the phone or laptop, which shows it (public/sw.js on the website).

`python -m app.webpush` prints a new private key for the VAPID_PRIVATE_KEY setting.
"""

import base64
import binascii
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# The push services browsers use. Only these get the API's requests, so a made-up "push address" can't
# point it at anything else (another server, or an address inside a network).
PUSH_SERVICES = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
MAX_ENDPOINT_LENGTH = 1000
RECORD_SIZE = 4096  # one record holds the whole alert (RFC 8188): at most RECORD_SIZE - 17 bytes of it
MAX_PAYLOAD = RECORD_SIZE - 17
KEY_HELP = "VAPID_PRIVATE_KEY must be a key made by `python -m app.webpush` (43 letters, digits, - and _)."


def b64(data: bytes) -> str:
    """URL-safe base64 without padding, as the Web Push keys are written."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    """Base64 in either alphabet, padded or not. Raises ValueError for anything else (the standard
    decoder would quietly skip characters that don't belong)."""
    standard = text.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        return base64.b64decode(standard + "=" * (-len(standard) % 4), validate=True)
    except binascii.Error:
        raise ValueError("not base64") from None


def private_key(text: str) -> ec.EllipticCurvePrivateKey:
    """The shop's VAPID key from its setting: the 32-byte P-256 private number, base64url."""
    try:
        raw = unb64(text)
        if len(raw) != 32:
            raise ValueError
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except ValueError:
        raise ValueError(KEY_HELP) from None


def new_private_key() -> str:
    return b64(ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big"))


def public_key(key: ec.EllipticCurvePrivateKey) -> str:
    """What browsers subscribe with (applicationServerKey): the public point, uncompressed, base64url."""
    return b64(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def check_endpoint(url: str) -> str:
    """A browser's push address: https on a known push service. Raises ValueError otherwise."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    known = any(host == service or host.endswith("." + service) for service in PUSH_SERVICES)
    printable = re.fullmatch(r"[\x21-\x7e]+", url) is not None  # no spaces, line breaks or other characters
    if len(url) > MAX_ENDPOINT_LENGTH or not printable or parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password or not known:
        raise ValueError("This isn't a browser's push address.")
    return url


def check_keys(p256dh: str, auth: str) -> tuple[str, str]:
    """A browser's two push keys (its public key and auth secret), rewritten as unpadded base64url.
    Raises ValueError if they aren't a P-256 public point and 16 bytes."""
    try:
        point, secret = unb64(p256dh), unb64(auth)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise ValueError("These aren't a browser's push keys.") from None
    if len(point) != 65 or point[0] != 4 or len(secret) != 16:
        raise ValueError("These aren't a browser's push keys.")
    return b64(point), b64(secret)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """The request body for one browser (RFC 8291, aes128gcm): only that browser can read it."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("The alert is too long for a push message.")
    receiver_point, auth_secret = unb64(p256dh), unb64(auth)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_point)
    sender = ec.generate_private_key(ec.SECP256R1())  # a new key for every message
    sender_point = sender.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = sender.exchange(ec.ECDH(), receiver)
    key_info = b"WebPush: info\x00" + receiver_point + sender_point
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret, info=key_info).derive(shared)
    salt = os.urandom(16)
    content_key = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)  # 0x02: the last (only) record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_point)]) + sender_point
    return header + record


def vapid_header(key: ec.EllipticCurvePrivateKey, endpoint: str, contact: str) -> str:
    """The Authorization header that proves the push comes from this shop (RFC 8292): a JWT for the push
    service's origin, valid 12 hours (the most is 24), signed with the shop's key."""
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 60 * 60, "sub": contact}
    token = jwt.encode(claims, key, algorithm="ES256")
    return f"vapid t={token}, k={public_key(key)}"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # A push service answers; it never sends us somewhere else. Following a redirect would let a push
    # address send the API's request to any server.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def send(key: ec.EllipticCurvePrivateKey, contact: str, endpoint: str, p256dh: str, auth: str, message: dict, *, ttl: int, timeout: float) -> int:
    """Posts one alert. Returns the push service's HTTP status (201: accepted; 404 or 410: that browser
    turned alerts off or was reset), or 0 when it couldn't be reached in time."""
    try:
        check_endpoint(endpoint)
        check_keys(p256dh, auth)
    except ValueError:
        return 410  # not a push address and keys (any more): it can never get an alert, like one that's gone
    body = encrypt(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(), p256dh, auth)
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": vapid_header(key, endpoint, contact),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),  # how long the push service keeps it for a phone that's offline
            "Urgency": "high",  # delivered at once, even to a phone saving battery
        },
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (OSError, ValueError):  # no connection, timeout, TLS failure
        return 0


if __name__ == "__main__":
    print(f"VAPID_PRIVATE_KEY={new_private_key()}")
