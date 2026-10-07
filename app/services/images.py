"""Normalise an uploaded headshot into bytes we are willing to store.

WHY THIS EXISTS
---------------
Every headshot check before this one looked at the FIRST FEW BYTES of a file and
took the rest on trust. That is not a check on the file, it is a check on its
label. A JPEG magic number followed by an HTML payload passed every one of them,
and was then promoted to an alumnus's real headshot unchanged.

The fix is not a better sniff. It is refusing to store the uploader's bytes at
all: decode the image, then write OUR OWN encoding of the pixels. Whatever was
appended, embedded or hidden in the original is not in the output, because the
output was produced from a pixel buffer rather than copied.

⚠️ `Image.verify()` DOES NOT DO THIS and is the obvious wrong answer. It is a
no-op for JPEG in Pillow (`PIL/Image.py`'s base implementation is `pass`; only
the PNG plugin overrides it with real work). Measured against a real
JPEG-with-HTML-appended file, 2026-08-08: `verify()` PASSES it, `load()` PASSES
it, and only a re-encode removes the payload. Do not "simplify" this module into
a verify() call.

The same operation also drops EXIF — including GPS. A phone photo carries the
coordinates it was taken at, and a headshot is very often taken at home.

And it shrinks the file, which is why this doubles as the answer to headshots
eating the storage quota: an uncropped phone photo is measured in megabytes and
is displayed as a 288px avatar.

RELATIONSHIP TO THE CLIENT-SIDE CROPPER
---------------------------------------
`HeadshotCropper.tsx` already does a canvas re-encode at 1024px/q0.9, and a
canvas re-encode strips metadata as a side effect. That is a QUALITY feature,
not a control: the browser holds a signed upload URL and can PUT anything to it,
and a survey respondent holding a token can post to the API directly. The
constants here deliberately match the cropper's so that a photo which did go
through it is not visibly degraded by passing through here as well.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

from app.core.errors import InvalidRequestError

try:  # littleCMS ships in Pillow's wheels, but a source build can omit it.
    from PIL import ImageCms
except ImportError:  # pragma: no cover - exercised only on a stripped Pillow
    ImageCms = None  # type: ignore[assignment]

# Matches HeadshotCropper.tsx (MAX_OUTPUT = 1024, quality 0.9) so a cropped
# photo re-encoded here is a near-identical second generation rather than a
# visible drop. Also matches compress-headshots.py, which performs the same
# operation offline against already-stored objects.
_MAX_EDGE = 1024
_JPEG_QUALITY = 90

# ⚠️ THE GUARD THAT MAKES DECODING SAFE. Without it this module is a memory
# exhaustion vector and STRICTLY WORSE than the prefix sniff it replaces,
# because today nothing decodes attacker-supplied bytes at all.
#
# A decompression bomb is a tiny file that decodes enormous. Measured with
# Pillow 12.3.0, 2026-08-08: a 419,971-byte PNG decodes to 12000x12000 — a
# ~432 MB RGB buffer — in 0.6s. The function this runs in has 2 GB, SHARED
# between concurrent invocations on the same instance, so a handful of those
# in flight together is an out-of-memory that kills co-tenant requests.
#
# Pillow's own default (89,478,485) does NOT save us: between one and two times
# that value it emits a DecompressionBombWarning and decodes anyway. So the
# dimensions are checked explicitly BEFORE `load()` — `Image.open` only parses
# the header, so `.size` is known while the pixels are still on disk — and the
# warning is promoted to an error as a second line of defence.
#
# TWO CEILINGS, because the header size and the decoded size are not the same
# thing for a JPEG (#597):
#
#   * `_MAX_HEADER_PIXELS` (50 Mpx) is what a file may CLAIM. It sits just above
#     a 48 MP phone camera, so a full-resolution photo from a modern handset is
#     still accepted. Pillow's own open-time bomb check is pinned to it.
#   * `_MAX_PIXELS` (25 Mpx, ~75 MB as RGB) is what we will actually ALLOCATE.
#     A JPEG is decoded through `draft()`, which has libjpeg scale by 1/2, 1/4
#     or 1/8 DURING decode — a 48 MP photo bound for a 1024px avatar decodes at
#     ~3 Mpx and never touches this limit. PNG and WebP have no reduced-size
#     decode, so for them the header size IS the decoded size, and anything
#     over 25 Mpx is refused.
#
# Halving the real allocation matters because the function's 2 GB is shared
# with co-tenant requests; see the alumni headshot-normalise docstring.
_MAX_HEADER_PIXELS = 50_000_000
_MAX_PIXELS = 25_000_000

Image.MAX_IMAGE_PIXELS = _MAX_HEADER_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)

# The ONLY decoders hostile bytes may reach. Without `formats=` Pillow tries
# every plugin it ships (TIFF, PSD, ICO, EPS, FITS, ...), each one a parser we
# never meant to expose. This matches `_BAD_IMAGE` and the survey route's
# magic-byte gate. (A phone JPEG carrying a multi-picture extension still opens
# through the JPEG plugin — Pillow reports it as "MPO" — and is accepted.)
_ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")

_BAD_IMAGE = "That file could not be read as a JPEG, PNG or WebP image."
_TOO_LARGE = "That image is too large. Please use a photo under 25 megapixels."


def normalise_headshot(data: bytes) -> bytes:
    """Decode *data* and return OUR re-encoded JPEG of the same picture.

    Raises `InvalidRequestError` when the bytes are not a decodable image or are
    too large to decode safely. It REJECTS rather than skipping: this runs on
    the write path, where refusing is the safe outcome. (`compress-headshots.py`
    skips unreadable objects instead, because it runs over data already stored
    and must not destroy something it merely failed to parse.)
    """
    try:
        image = Image.open(io.BytesIO(data), formats=_ALLOWED_FORMATS)
    except Image.DecompressionBombWarning as exc:  # promoted to an error above
        raise InvalidRequestError(_TOO_LARGE) from exc
    except Exception as exc:
        # Pillow raises a wide and version-dependent range here (UnidentifiedImageError,
        # OSError, SyntaxError, struct.error, ...). The caller only needs to know
        # the file was not a usable image, and the reason must not be echoed back
        # to a public survey respondent.
        raise InvalidRequestError(_BAD_IMAGE) from exc

    # `.size` comes from the header alone, so this runs while the pixels are
    # still unread — the whole point of checking here rather than after load().
    width, height = image.size
    if width * height > _MAX_HEADER_PIXELS:
        raise InvalidRequestError(_TOO_LARGE)

    try:
        # Ask libjpeg to decode at the smallest 1/2^n scale that still covers
        # the output size. The request is square so it holds whichever way
        # `exif_transpose` later turns the picture. A no-op for PNG/WebP.
        image.draft(None, (_MAX_EDGE, _MAX_EDGE))
    except Exception as exc:
        raise InvalidRequestError(_BAD_IMAGE) from exc

    # Re-checked AFTER draft(): this is the buffer that will really be allocated.
    width, height = image.size
    if width * height > _MAX_PIXELS:
        raise InvalidRequestError(_TOO_LARGE)

    # Read before exif_transpose, which builds a new image object.
    icc = image.info.get("icc_profile")

    try:
        # ⚠️ ORIENTATION MUST COME FIRST, AND MUST NOT BE SKIPPED.
        #
        # A phone writes the sensor's pixels and records "rotate this on display"
        # in an EXIF tag. Stripping metadata therefore ROTATES EVERY PHONE PHOTO
        # taken in portrait, because we drop the instruction and keep the
        # sideways pixels. Verified 2026-08-08: an 800x600 image with
        # orientation=6 re-encodes naively to 800x600 and displays sideways;
        # through exif_transpose it becomes 600x800 and displays upright.
        #
        # `exif_transpose` bakes the rotation into the pixels and clears the tag,
        # which is exactly what we want before discarding the rest of the EXIF.
        image = ImageOps.exif_transpose(image)

        if icc:
            image = _to_srgb(image, icc)

        if image.mode in ("RGBA", "LA", "P"):
            # JPEG has no alpha. A plain convert("RGB") makes transparent pixels
            # BLACK, so a PNG headshot with a cut-out background would come back
            # with a black halo. Compositing onto white matches both the cropper
            # and the page the avatar is shown on.
            image = image.convert("RGBA")
            flattened = Image.new("RGB", image.size, (255, 255, 255))
            flattened.paste(image, mask=image.split()[-1])
            image = flattened
        elif image.mode != "RGB":
            image = image.convert("RGB")

        # Never upscale — enlarging a small photo adds no detail and only costs
        # bytes. Only the longest edge is bounded; the aspect ratio is preserved
        # and nothing is cropped, because cropping an uncropped bulk-imported
        # photo could cut someone's head off.
        if max(image.size) > _MAX_EDGE:
            image.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.LANCZOS)

        buffer = io.BytesIO()
        # NO icc_profile= here, deliberately (#597). The uploader's profile is
        # an attacker-chosen blob that would otherwise be stored and served to
        # every browser that renders the avatar. The colours it described were
        # already baked into the pixels by `_to_srgb` above, and an untagged
        # JPEG is displayed as sRGB, so the output looks the same without it.
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    except InvalidRequestError:
        raise
    except Image.DecompressionBombWarning as exc:
        raise InvalidRequestError(_TOO_LARGE) from exc
    except Exception as exc:
        # A file whose header parsed but whose pixel data is truncated or corrupt
        # fails here rather than at open(). Same disposition: we will not store
        # something we could not read.
        raise InvalidRequestError(_BAD_IMAGE) from exc

    return buffer.getvalue()


def _to_srgb(image: Image.Image, icc: bytes) -> Image.Image:
    """Convert *image* from its embedded ICC profile into plain sRGB pixels.

    WHY NOT JUST DROP THE PROFILE: a modern phone photo is very often Display
    P3. Its pixel values only mean the right colours when read through that
    profile; treated as sRGB (which is what an untagged JPEG is) they come out
    visibly washed out — skin tones especially. Converting first means dropping
    the profile afterwards changes nothing a viewer can see.

    BEST EFFORT, NEVER A REJECTION: a missing littleCMS, a malformed profile or
    one that does not match the image's mode (an RGB profile on a greyscale
    PNG) all fall back to the unconverted pixels — the pre-#597 look minus the
    tag, at worst a mild colour shift. Refusing a real photo over its colour
    metadata would be the worse outcome for someone with one shot at a link.
    """
    if ImageCms is None or image.mode not in ("RGB", "RGBA", "CMYK"):
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        converted = ImageCms.profileToProfile(
            image,
            source,
            ImageCms.createProfile("sRGB"),
            outputMode="RGBA" if image.mode == "RGBA" else "RGB",
        )
    except Exception:
        return image
    return converted if converted is not None else image
