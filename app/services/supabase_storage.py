"""Thin client for Supabase Storage (private buckets) via the Storage REST API.

Mirrors ``supabase_admin.py``: authenticates with the service-role key (which
MUST never reach the browser — these calls only run server-side), keeps errors
opaque (``ServiceError`` -> 502) and NEVER surfaces the upstream Supabase status
or body to the caller. Authorization (who may upload/view) is the calling route's
responsibility; this module performs none itself.

Buckets here are PRIVATE — objects are only ever reachable through short-lived
signed URLs minted below, so alumni images are never publicly enumerable.
"""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.core.errors import ServiceError

# Uploads can be a touch slower than the auth-admin JSON calls.
_TIMEOUT_SECONDS = 15.0


def _base_and_key() -> tuple[str, str]:
    settings = get_settings()
    base_url = settings.supabase_url
    service_key = settings.supabase_service_role_key
    if not base_url or not service_key:
        raise ServiceError("File storage is unavailable: storage is not configured.")
    return f"{base_url.rstrip('/')}/storage/v1", service_key


def _headers(service_key: str, *, content_type: str | None = None, upsert: bool = False) -> dict:
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    if content_type:
        headers["Content-Type"] = content_type
    if upsert:
        headers["x-upsert"] = "true"
    return headers


async def upload_object(
    bucket: str, path: str, data: bytes, content_type: str
) -> None:
    """Upsert an object into a private bucket (overwrites any existing object)."""
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers=_headers(key, content_type=content_type, upsert=True),
                content=data,
            )
    except httpx.HTTPError as exc:
        # Log the exception TYPE only — never the URL (carries the key) or body.
        raise ServiceError("Could not reach the file storage service to upload.") from exc
    if not response.is_success:
        raise ServiceError("The file storage service rejected the upload.")


async def download_object(bucket: str, path: str) -> bytes:
    """Download an object's raw bytes from a private bucket (service key auth).

    Used server-side to promote a staged survey photo into the alum's real
    headshot. Raises ``ServiceError`` (-> 502) if the object can't be reached or
    the storage service rejects the read; never surfaces the upstream body."""
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_headers(key))
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service to download.") from exc
    if not response.is_success:
        raise ServiceError("The file storage service rejected the download.")
    return response.content


async def create_signed_url(
    bucket: str, path: str, *, expires_in: int = 3600
) -> str | None:
    """Return a short-lived signed URL for a private object, or ``None`` if the
    object does not exist. ``expires_in`` is seconds (default 1 hour)."""
    base, key = _base_and_key()
    url = f"{base}/object/sign/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers=_headers(key, content_type="application/json"),
                json={"expiresIn": expires_in},
            )
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service.") from exc
    # A missing object comes back as 400 or 404 (with a "not found" body); that is
    # not an error for us — the alumnus simply has no headshot yet.
    if response.status_code in (400, 404):
        return None
    if not response.is_success:
        raise ServiceError("The file storage service rejected the signed-URL request.")
    try:
        signed = response.json().get("signedURL")
    except ValueError as exc:
        raise ServiceError(
            "The file storage service returned an unreadable response."
        ) from exc
    if not signed:
        return None
    # Supabase returns a relative path like "/object/sign/<bucket>/<path>?token=…"
    # under the storage v1 base; join it back to an absolute URL.
    return f"{base}{signed}" if signed.startswith("/") else f"{base}/{signed}"


async def create_signed_upload_url(bucket: str, path: str) -> str:
    """Return an absolute, short-lived signed UPLOAD url the browser can PUT the
    file to DIRECTLY (bypassing our serverless functions' ~4.5 MB request-body
    limit). The URL is scoped to exactly this ``bucket``/``path`` and carries its
    own single-use token, so the browser needs neither the service key nor any
    other credential; ``x-upsert`` lets it overwrite an existing image. Supabase
    still enforces the bucket's size + MIME allow-list on the eventual PUT."""
    base, key = _base_and_key()
    url = f"{base}/object/upload/sign/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers=_headers(key, content_type="application/json", upsert=True),
                json={},
            )
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service.") from exc
    if not response.is_success:
        raise ServiceError("The file storage service rejected the upload-URL request.")
    try:
        signed = response.json().get("url")
    except ValueError as exc:
        raise ServiceError(
            "The file storage service returned an unreadable response."
        ) from exc
    if not signed:
        raise ServiceError("The file storage service returned no upload URL.")
    # Supabase returns a relative "/object/upload/sign/<bucket>/<path>?token=…"
    # under the storage v1 base; join it back to an absolute URL for the browser.
    return f"{base}{signed}" if signed.startswith("/") else f"{base}/{signed}"


async def probe_object(bucket: str, path: str) -> tuple[str | None, int | None]:
    """Best-effort read of an object's ``(content_type, size)`` WITHOUT
    downloading it, via a 1-byte ranged GET. Used as defense-in-depth on the
    direct-upload path (the bucket's own allow-list/size-limit is the primary
    guard). Returns ``(None, None)`` if the object is missing or unreadable so
    callers FAIL OPEN — a probe hiccup must never reject a legitimate upload."""
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    headers = _headers(key)
    headers["Range"] = "bytes=0-0"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return (None, None)
    if response.status_code not in (200, 206):
        return (None, None)
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    size: int | None = None
    # A 206 carries the true total after the slash: "bytes 0-0/<total>".
    content_range = response.headers.get("content-range")
    if content_range and "/" in content_range:
        try:
            size = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            size = None
    if size is None:
        try:
            size = int(response.headers["content-length"])
        except (KeyError, ValueError):
            size = None
    return (content_type or None, size)


async def delete_object(bucket: str, path: str) -> None:
    """Delete an object. A missing object (404) is treated as success."""
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.delete(url, headers=_headers(key))
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service to delete.") from exc
    if response.status_code == 404:
        return
    if not response.is_success:
        raise ServiceError("The file storage service rejected the delete.")
