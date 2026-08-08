"""Invisible-character rules on names, emails and URLs (2026-08-08).

The gap: `_has_control_chars` tested Unicode category `Cc` only, so bidi
overrides and zero-width characters (category `Cf`) passed every name check on
BOTH the staff and survey paths. Not code execution — but enough to make a
stored name render as something other than what it is, and enough to walk a
duplicate record past `hygiene.detect_duplicates`, which lower-cases without
normalizing.

The half of this that is easy to get wrong is the PERMISSIVE half: the two
joiners are orthographically required in real names, and a fix that rejects
them breaks legitimate alumni. Those cases are tested first, deliberately.
"""

import pytest
from pydantic import ValidationError

from app.schemas.alumni import AlumniBase
from app.services.survey_responses import _valid_email, _valid_name

RLO = "‮"  # RIGHT-TO-LEFT OVERRIDE
ZWSP = "​"  # ZERO WIDTH SPACE
BOM = "﻿"
ZWNJ = "‌"  # legitimate in Persian / Hindi
ZWJ = "‍"


def _staff_name(value: str) -> str | None:
    """The staff create/edit rule, exercised through the real model."""
    return AlumniBase(first_name=value).first_name


# ---------------------------------------------- real names must still pass ---


@pytest.mark.parametrize(
    "name",
    [
        "O'Brien-Smith",
        "D'Angelo-Núñez",
        "Renée",
        "José",
        "St. John",
        "Anne-Marie",
        "N'Diaye",
        "李",
        "Мария",  # a real Cyrillic name, not a homoglyph attack
        f"من{ZWNJ}صور",  # Persian, ZWNJ is required orthography
        f"क{ZWJ}ष",  # Devanagari conjunct via ZWJ
    ],
)
def test_a_real_name_is_still_accepted(name):
    """The joiners are the point: banning all of category Cf breaks these."""
    assert _staff_name(name) == name
    assert _valid_name(name) is True


# ------------------------------------------- invisible characters rejected ---


@pytest.mark.parametrize("bad", [RLO, ZWSP, BOM, "‏", "⁦", "⁠", "؜"])
def test_an_invisible_character_is_rejected_in_a_name(bad):
    value = f"Jo{bad}hn"
    assert _valid_name(value) is False, "survey path accepted an invisible character"
    with pytest.raises(ValidationError):
        _staff_name(value)


def test_the_staff_and_survey_paths_agree():
    """Both must move together — they share one function precisely so that a
    rule cannot be tightened on one path and silently left off the other."""
    for value in (f"A{RLO}B", f"A{ZWSP}B", f"A{BOM}B"):
        assert _valid_name(value) is False
        with pytest.raises(ValidationError):
            _staff_name(value)


def test_the_duplicate_check_evasion_is_closed():
    """A zero-width space was enough to make one name two distinct strings.

    `detect_duplicates` compares with `lower()` and no normalization, so the
    defence has to be refusing the character at write time.
    """
    assert _valid_name(f"Caleb{ZWSP}") is False
    assert _valid_name(f"Cal{ZWSP}eb") is False
    assert _valid_name("Caleb") is True


# ------------------------------------ emails and URLs: the STRICTER rule -----


@pytest.mark.parametrize("bad", [ZWSP, RLO, BOM, ZWNJ, ZWJ])
def test_no_invisible_character_survives_in_an_email(bad):
    """Joiners included: an address is a machine identifier, not a name."""
    assert _valid_email(f"alum{bad}@dev.example") is False


@pytest.mark.parametrize(
    "good", ["alum@dev.example", "first.last@byu.edu", "a+tag@sub.domain.org"]
)
def test_an_ordinary_address_still_passes(good):
    assert _valid_email(good) is True


@pytest.mark.parametrize("bad", [ZWSP, RLO, ZWNJ, ZWJ, BOM])
def test_no_invisible_character_survives_in_a_linkedin_url(bad):
    with pytest.raises(ValidationError):
        AlumniBase(linkedin_url=f"https://linkedin{bad}.com/in/someone")
    with pytest.raises(ValidationError):
        AlumniBase(linkedin_url=f"https://linkedin.com/in/some{bad}one")


def test_an_ordinary_linkedin_url_still_passes():
    url = "https://www.linkedin.com/in/jane-doe-1234"
    assert AlumniBase(linkedin_url=url).linkedin_url == url


def test_the_backslash_differential_is_still_rejected():
    """Regression guard: the 2026-08-07 parser-differential fix must survive."""
    with pytest.raises(ValidationError):
        AlumniBase(linkedin_url="https://evil.example\\@linkedin.com/in/x")
