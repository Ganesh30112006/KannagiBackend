"""Product photos: uploaded to Cloudinary from the server, so the API secret never reaches a browser."""

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import settings

logger = logging.getLogger("kannagi")

UPLOAD_TIMEOUT_SECONDS = 15
# Served resized to the card size, in the best format each phone supports.
DELIVERY_TRANSFORM = "f_auto,q_auto,c_limit,w_600"
# .../image/upload/<transformations>/v<version>/<public id>.<ext>
_PUBLIC_ID = re.compile(r"/image/upload/(?:[a-z]{1,3}_[^/]*/)*(?:v\d+/)?(?P<id>[^?#]+)\.\w+$")


class ImageStoreError(Exception):
    """Shown to the shopkeeper as-is."""


def sign(params: dict[str, str], api_secret: str) -> str:
    """Cloudinary request signature: SHA-1 of the sorted params joined with & plus the secret."""
    to_sign = "&".join(f"{key}={value}" for key, value in sorted(params.items()) if value not in (None, ""))
    return hashlib.sha1((to_sign + api_secret).encode()).hexdigest()


def store_image(image: str) -> str:
    """Return the URL to save for a product photo (a data URL or an https link).

    With Cloudinary configured, the photo is uploaded and its Cloudinary URL returned. Without it
    (local development) the image is kept as given.
    """
    account = settings.cloudinary
    if account is None:
        return image
    if image.startswith(f"https://res.cloudinary.com/{account.cloud_name}/"):
        return image  # already ours

    params = {"folder": settings.cloudinary_folder, "timestamp": str(int(time.time()))}
    body = {**params, "file": image, "api_key": account.api_key, "signature": sign(params, account.api_secret)}
    request = urllib.request.Request(
        f"https://api.cloudinary.com/v1_1/{urllib.parse.quote(account.cloud_name)}/image/upload",
        data=urllib.parse.urlencode(body).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode(errors="replace")
        if error.code in (401, 403):
            # Wrong secret or a key without upload permission: a setup problem, not a bad photo.
            logger.error("Cloudinary refused the credentials (%s): %s", error.code, detail)
            raise ImageStoreError("Photo uploads aren't set up correctly on the server. Ask the site admin to check the Cloudinary key.") from None
        logger.warning("Cloudinary upload rejected (%s): %s", error.code, detail)
        if error.code in (400, 404) and not image.startswith("data:"):
            raise ImageStoreError("That image link couldn't be downloaded. Try uploading the photo instead.") from None
        raise ImageStoreError("The photo service rejected this image. Please try another photo.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.warning("Cloudinary upload failed", exc_info=True)
        raise ImageStoreError("Couldn't reach the photo service. Please try again.") from None

    url = result.get("secure_url") if isinstance(result, dict) else None
    if not isinstance(url, str) or not url.startswith("https://res.cloudinary.com/"):
        raise ImageStoreError("The photo service returned an unexpected answer. Please try again.")
    return url.replace("/image/upload/", f"/image/upload/{DELIVERY_TRANSFORM}/", 1)


def delete_image(url: str | None) -> None:
    """Remove a replaced or unused photo from Cloudinary. Best effort: never raises.

    Only the shop's own uploads (this cloud, inside CLOUDINARY_FOLDER) are ever deleted."""
    account = settings.cloudinary
    if account is None or not url or not url.startswith(f"https://res.cloudinary.com/{account.cloud_name}/"):
        return
    match = _PUBLIC_ID.search(urllib.parse.urlsplit(url).path)
    if not match or not match["id"].startswith(f"{settings.cloudinary_folder}/"):
        return
    params = {"invalidate": "true", "public_id": match["id"], "timestamp": str(int(time.time()))}
    body = {**params, "api_key": account.api_key, "signature": sign(params, account.api_secret)}
    request = urllib.request.Request(
        f"https://api.cloudinary.com/v1_1/{urllib.parse.quote(account.cloud_name)}/image/destroy",
        data=urllib.parse.urlencode(body).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS) as response:
            result = json.load(response)
        if result.get("result") not in ("ok", "not found"):
            logger.warning("Cloudinary didn't delete %s: %s", match["id"], result)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        logger.warning("Couldn't delete old photo %s from Cloudinary", match["id"], exc_info=True)
