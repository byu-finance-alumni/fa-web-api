"""Tests for `services.images.normalise_headshot`.

The two behaviours worth stating plainly, because both were established by
measurement rather than by reading documentation:

  * a JPEG-with-payload-appended polyglot passes `Image.verify()` AND
    `Image.load()`. Only re-encoding removes the payload. The polyglot tests
    here assert the payload is GONE from the output, not merely that the file
    was accepted or rejected.
  * naively stripping metadata rotates phone photos, because the rotation lives
    in the EXIF being stripped. The orientation test asserts the PIXELS moved.
"""

import io

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.core.errors import InvalidRequestError
from app.services.images import normalise_headshot

# EXIF tag numbers, so this file needs no library beyond Pillow itself.
_ORIENTATION = 0x0112
_MAKE = 0x010F
_GPS_IFD = 0x8825

PAYLOAD = b"<html><script>alert(document.domain)</script></html>"


def _jpeg(size=(400, 300), colour=(120, 90, 200), **save_kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG", **save_kw)
    return buf.getvalue()


def _png(size=(400, 300), mode="RGB") -> bytes:
    # Fill colour has to match the mode: an RGB triple is invalid for "L".
    colour = {"RGB": (10, 200, 60), "L": 128, "P": 3}.get(mode, (10, 200, 60))
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------- polyglots ----


def test_a_jpeg_with_an_appended_payload_comes_back_without_it():
    hostile = _jpeg() + PAYLOAD
    assert PAYLOAD in hostile, "test fixture is wrong — no payload to strip"

    out = normalise_headshot(hostile)

    assert PAYLOAD not in out
    assert out.startswith(b"\xff\xd8\xff")
    Image.open(io.BytesIO(out)).load()  # still a usable picture


def test_the_prefix_sniff_this_replaces_would_have_accepted_it():
    """Pins WHY this module exists: the old check passes the hostile file."""
    hostile = _jpeg() + PAYLOAD
    assert hostile.startswith(b"\xff\xd8\xff")  # what `_sniff_image_mime` asked


def test_verify_does_not_catch_it_either():
    """Guards against someone 'simplifying' this module into a verify() call.

    If a future Pillow makes verify() catch appended data this test fails, which
    is the right prompt to revisit the comment in `images.py` — not a false
    alarm to silence.
    """
    hostile = _jpeg() + PAYLOAD
    img = Image.open(io.BytesIO(hostile))
    img.verify()  # does NOT raise


def test_html_wearing_a_jpeg_magic_number_is_rejected():
    with pytest.raises(InvalidRequestError):
        normalise_headshot(b"\xff\xd8\xff\xe0" + PAYLOAD)


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"junk" * 10],
)
def test_non_images_are_rejected(data):
    with pytest.raises(InvalidRequestError):
        normalise_headshot(data)


def test_a_truncated_image_is_rejected():
    with pytest.raises(InvalidRequestError):
        normalise_headshot(_jpeg()[: 400])


# ----------------------------------------------------------------- EXIF -----


def _jpeg_with_gps_and_orientation(orientation: int = 6) -> bytes:
    """A JPEG carrying what a phone actually writes: orientation, make, and GPS."""
    image = Image.new("RGB", (800, 600), (120, 90, 200))
    exif = image.getexif()
    exif[_ORIENTATION] = orientation
    exif[_MAKE] = "Pixel"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    gps[2] = (IFDRational(40, 1), IFDRational(14, 1), IFDRational(0, 1))  # Provo
    gps[3] = "W"
    gps[4] = (IFDRational(111, 1), IFDRational(39, 1), IFDRational(0, 1))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _tags(data: bytes) -> tuple[dict, dict]:
    exif = Image.open(io.BytesIO(data)).getexif()
    return dict(exif), dict(exif.get_ifd(_GPS_IFD))


def test_gps_and_every_other_tag_are_gone():
    src = _jpeg_with_gps_and_orientation()
    before, before_gps = _tags(src)
    assert before_gps, "fixture has no GPS to strip"
    assert before, "fixture has no EXIF to strip"

    after, after_gps = _tags(normalise_headshot(src))

    assert after_gps == {}
    assert after == {}


def test_a_sideways_phone_photo_is_rotated_rather_than_left_sideways():
    """The classic trap: strip the tag, keep the pixels, ship a rotated photo."""
    src = _jpeg_with_gps_and_orientation(orientation=6)
    assert Image.open(io.BytesIO(src)).size == (800, 600)

    out = normalise_headshot(src)

    # 6 means "rotate 90°", so width and height must have swapped.
    assert Image.open(io.BytesIO(out)).size == (600, 800)
    assert _tags(out)[0] == {}  # and the instruction is gone


def test_an_upright_photo_is_not_rotated():
    src = _jpeg_with_gps_and_orientation(orientation=1)
    assert Image.open(io.BytesIO(normalise_headshot(src))).size == (800, 600)


# ------------------------------------------------ size, mode, and shape -----


def test_a_large_photo_is_scaled_down_and_gets_much_smaller():
    src = _jpeg(size=(4000, 3000))
    out = normalise_headshot(src)
    assert max(Image.open(io.BytesIO(out)).size) == 1024
    assert len(out) < len(src)


def test_a_small_photo_is_not_upscaled():
    out = normalise_headshot(_jpeg(size=(200, 150)))
    assert Image.open(io.BytesIO(out)).size == (200, 150)


def test_aspect_ratio_is_preserved_and_nothing_is_cropped():
    out = normalise_headshot(_jpeg(size=(4000, 2000)))
    w, h = Image.open(io.BytesIO(out)).size
    assert (w, h) == (1024, 512)


def test_transparency_is_flattened_onto_white_not_black():
    buf = io.BytesIO()
    Image.new("RGBA", (50, 50), (255, 255, 255, 0)).save(buf, format="PNG")

    out = normalise_headshot(buf.getvalue())

    r, g, b = Image.open(io.BytesIO(out)).convert("RGB").getpixel((25, 25))
    assert (r, g, b) != (0, 0, 0), "transparent pixels came back black"
    assert r > 240 and g > 240 and b > 240


@pytest.mark.parametrize("mode", ["RGB", "L", "P"])
def test_other_modes_are_accepted(mode):
    out = normalise_headshot(_png(mode=mode))
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_png_and_webp_inputs_all_come_out_as_jpeg():
    for fmt in ("PNG", "WEBP"):
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (5, 5, 5)).save(buf, format=fmt)
        assert Image.open(io.BytesIO(normalise_headshot(buf.getvalue()))).format == "JPEG"


# ------------------------------------------------- decompression bombs ------


def test_a_decompression_bomb_is_refused_without_decoding_it():
    """A ~400 KB file that would allocate ~432 MB if decoded.

    The point is that this is CHEAP to refuse: the dimensions come from the
    header, so the rejection happens while the pixels are still unread. If this
    test ever starts taking seconds or ballooning memory, the check has moved
    after `load()` and the guard is gone.
    """
    buf = io.BytesIO()
    Image.new("RGB", (12000, 12000), (255, 255, 255)).save(buf, format="PNG")
    bomb = buf.getvalue()
    assert len(bomb) < 3_000_000, "fixture is not actually a bomb"

    with pytest.raises(InvalidRequestError):
        normalise_headshot(bomb)


def test_a_realistic_48_megapixel_phone_photo_is_still_accepted():
    """The limit must sit above a real modern handset, not below it."""
    out = normalise_headshot(_jpeg(size=(8000, 6000)))
    assert max(Image.open(io.BytesIO(out)).size) == 1024


# ------------------------------------------------------------ messages ------


def test_the_rejection_message_never_echoes_the_uploaded_bytes():
    """This surfaces to a PUBLIC survey respondent."""
    try:
        normalise_headshot(b"\xff\xd8\xff" + b"SECRETMARKER")
    except InvalidRequestError as exc:
        assert "SECRETMARKER" not in str(exc)
        assert "Traceback" not in str(exc)
    else:
        pytest.fail("expected a rejection")
