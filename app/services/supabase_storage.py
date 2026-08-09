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


async def list_objects(
    bucket: str, *, prefix: str = "", limit: int = 100, offset: int = 0
) -> list[dict]:
    """One page of a bucket's contents: the raw Supabase rows, name-sorted.

    Each row carries ``name`` plus a ``metadata`` object with ``size``,
    ``mimetype`` and ``eTag``. ⚠️ A row whose ``metadata`` is ``None`` is a
    FOLDER placeholder, not a file — Supabase synthesises one per path segment
    (``survey-pending``, say). Callers must drop those; downloading one 404s.

    Paging is the caller's job: ask for successive ``offset``s until a short page
    comes back. The listing is metadata only — no image bytes cross the wire —
    which is what lets the sweep decide what to work on for almost nothing.
    """
    base, key = _base_and_key()
    url = f"{base}/object/list/{bucket}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers=_headers(key, content_type="application/json"),
                json={
                    "prefix": prefix,
                    "limit": limit,
                    "offset": offset,
                    # Stable order across pages. Without it the same object can
                    # appear on two pages (or on none) while paging.
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service to list objects.") from exc
    if not response.is_success:
        raise ServiceError("The file storage service rejected the listing.")
    try:
        rows = response.json()
    except ValueError as exc:
        raise ServiceError("The file storage service returned an unreadable listing.") from exc
    if not isinstance(rows, list):
        raise ServiceError("The file storage service returned an unreadable listing.")
    return rows


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


async def probe_object_head(
    bucket: str, path: str, *, head_bytes: int = 16
) -> tuple[str | None, int | None, bytes | None]:
    """Best-effort read of an object's ``(content_type, size, first bytes)``
    WITHOUT downloading it, via a small ranged GET.

    Used as defense-in-depth on the direct-upload paths (the bucket's own
    allow-list / size limit is the primary guard). The leading bytes let the
    caller SNIFF the real image type — a browser-supplied ``Content-Type`` on a
    direct PUT is just a label, exactly like a multipart header, so the magic
    bytes are the only trustworthy signal. 16 bytes covers every signature we
    recognise (JPEG 3, PNG 8, WebP 12).

    Returns ``(None, None, None)`` if the object is missing or unreadable so
    callers FAIL OPEN — a probe hiccup must never reject a legitimate upload.

    ⚠️ ``head`` is ``None`` ONLY when the probe itself failed. A successful read
    of a genuinely EMPTY object returns ``b""``, not ``None``, and callers must
    therefore test ``head is not None`` rather than truthiness. Conflating the
    two let a 0-byte upload skip the magic-byte check entirely and be confirmed
    as a valid headshot — the exact control this function exists to feed
    (found re-reviewing the #419 fix, 2026-08-07)."""
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    headers = _headers(key)
    headers["Range"] = f"bytes=0-{max(head_bytes, 1) - 1}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return (None, None, None)
    if response.status_code not in (200, 206):
        return (None, None, None)
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    size: int | None = None
    # A 206 carries the true total after the slash: "bytes 0-15/<total>".
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
    # A server that ignored the Range header returns 200 with the WHOLE object;
    # trim so callers only ever see the head they asked for.
    # Always bytes on a successful read — `b""` for an empty object, which is a
    # real answer ("we looked, there is nothing there"), not a failure. Every
    # early return above is the failure case and yields None explicitly.
    head = response.content[:head_bytes]
    return (content_type or None, size, head)


async def probe_object(bucket: str, path: str) -> tuple[str | None, int | None]:
    """``(content_type, size)`` for an object, or ``(None, None)`` when it can't
    be read. Thin wrapper over {@link probe_object_head} for callers that don't
    need the leading bytes."""
    content_type, size, _ = await probe_object_head(bucket, path)
    return (content_type, size)


async def delete_object(bucket: str, path: str) -> None:
    """Delete an object. An object that is already gone is treated as success.

    Deleting something that no longer exists is the outcome the caller wanted,
    so it must never raise. Supabase Storage is inconsistent about how it says
    "not found" — a 404 for some keys, but a 400 whose body carries a
    ``not_found`` / "Object not found" marker for others — so matching only on
    404 turned an already-deleted file into a hard failure. That is exactly what
    blocked an engineer resetting a survey campaign for an alumnus whose staged
    photo had already been promoted onto their profile.
    """
    base, key = _base_and_key()
    url = f"{base}/object/{bucket}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.delete(url, headers=_headers(key))
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach the file storage service to delete.") from exc
    if response.status_code == 404 or _is_missing_object(response):
        return
    if not response.is_success:
        raise ServiceError("The file storage service rejected the delete.")


def _is_missing_object(response: httpx.Response) -> bool:
    """Whether a non-2xx delete response means "it wasn't there anyway"."""
    if response.is_success or response.status_code not in (400, 404):
        return False
    try:
        body = response.json()
    except ValueError:
        body = {}
    marker = " ".join(
        str(body.get(field, "")) for field in ("error", "message", "statusCode")
    ).lower()
    return "not_found" in marker or "not found" in marker
