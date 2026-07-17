"""Offline tests for "Best Contact" -> preferred_contact_method reconciliation (#284).

The intake sheet's "Best Contact" column is free text holding a literal VALUE (an
email or a phone number); ``preferred_contact_method`` names a validated METHOD.
On import the importer resolves the former into the latter and clears the free
text, so the two fields can't drift apart and contradict each other.

Pure parse/map level — no database, no session.
"""

import pytest

from app.services import import_csv

# --- CSV building helpers ----------------------------------------------------

HEADERS = import_csv.EXPECTED_HEADERS

_BY_FIELD = {
    "first_name": "First name",
    "last_name": "Last Name",
    "personal_email": "Personal Email",
    "work_email": "Work Email",
    "phone": "Phone #",
    "best_contact": "Best Contact",
}


def _row_values(**overrides) -> list[str]:
    values = {h: "" for h in HEADERS}
    for field, value in overrides.items():
        values[_BY_FIELD[field]] = value
    return [values[h] for h in HEADERS]


def _csv_bytes(*rows: list[str]) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _contact_of(**overrides) -> dict:
    """Map one row through the importer and return its contact payload section."""
    rows, header_errors = import_csv.parse_and_map(_csv_bytes(_row_values(**overrides)))
    assert header_errors == []
    assert len(rows) == 1
    assert rows[0]["error"] is None
    return rows[0]["payload"].get("contact", {})


# --- Resolves against each of the three fields -------------------------------


def test_best_contact_matching_personal_email_resolves():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        work_email="ada.byron@corp.com",
        phone="555-123-4567",
        best_contact="ada@example.com",
    )
    assert contact["preferred_contact_method"] == "personal_email"
    # Resolved cleanly -> the free text is cleared so it can't drift.
    assert "best_contact" not in contact


def test_best_contact_matching_work_email_resolves():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        work_email="ada.byron@corp.com",
        phone="555-123-4567",
        best_contact="ada.byron@corp.com",
    )
    assert contact["preferred_contact_method"] == "work_email"
    assert "best_contact" not in contact


def test_best_contact_matching_phone_resolves():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        phone="555-123-4567",
        best_contact="555-123-4567",
    )
    assert contact["preferred_contact_method"] == "phone"
    assert "best_contact" not in contact


# --- Emails match case-insensitively -----------------------------------------


@pytest.mark.parametrize(
    "best",
    [
        "ADA@EXAMPLE.COM",
        "Ada@Example.Com",
        "  ada@example.com  ",
        "  ADA@Example.com ",
    ],
)
def test_email_match_is_case_insensitive_and_trimmed(best):
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        best_contact=best,
    )
    assert contact["preferred_contact_method"] == "personal_email"
    assert "best_contact" not in contact


def test_stored_email_case_does_not_block_the_match():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="Ada.Byron@Example.COM",
        best_contact="ada.byron@example.com",
    )
    assert contact["preferred_contact_method"] == "personal_email"


# --- Phones match across formatting variants ---------------------------------


@pytest.mark.parametrize(
    "best",
    [
        "555-123-4567",
        "(555) 123 4567",
        "(555)123-4567",
        "555.123.4567",
        "5551234567",
        "555 123 4567",
        " 555-123-4567 ",
        "+1 555-123-4567",
        "1-555-123-4567",
    ],
)
def test_phone_match_normalizes_formatting(best):
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        phone="(555) 123-4567",
        best_contact=best,
    )
    assert contact["preferred_contact_method"] == "phone"
    assert "best_contact" not in contact


@pytest.mark.parametrize(
    "stored",
    ["555-123-4567", "(555) 123 4567", "5551234567", "+1 (555) 123-4567"],
)
def test_phone_match_normalizes_the_stored_side_too(stored):
    contact = _contact_of(
        first_name="Ada", last_name="Byron", phone=stored, best_contact="5551234567"
    )
    assert contact["preferred_contact_method"] == "phone"


def test_different_phone_number_does_not_match():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        phone="555-123-4567",
        best_contact="555-987-6543",
    )
    assert "preferred_contact_method" not in contact
    assert contact["best_contact"] == "555-987-6543"


# --- No match: the free text is retained -------------------------------------


def test_unmatched_best_contact_is_retained_as_free_text():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        work_email="ada.byron@corp.com",
        phone="555-123-4567",
        best_contact="ada.new@elsewhere.org",
    )
    # A new address we don't otherwise have: keep it, don't guess a method.
    assert contact["best_contact"] == "ada.new@elsewhere.org"
    assert "preferred_contact_method" not in contact


def test_best_contact_with_no_other_contact_fields_is_retained():
    contact = _contact_of(
        first_name="Ada", last_name="Byron", best_contact="ada@example.com"
    )
    assert contact["best_contact"] == "ada@example.com"
    assert "preferred_contact_method" not in contact


def test_non_contactish_free_text_is_retained():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        best_contact="reach her through her assistant",
    )
    assert contact["best_contact"] == "reach her through her assistant"
    assert "preferred_contact_method" not in contact


def test_blank_best_contact_sets_nothing():
    contact = _contact_of(
        first_name="Ada", last_name="Byron", personal_email="ada@example.com"
    )
    assert "best_contact" not in contact
    assert "preferred_contact_method" not in contact


# --- An explicit preferred_contact_method wins -------------------------------


def test_existing_preferred_contact_method_is_not_overwritten():
    # The intake sheet has no "Preferred Contact Method" column, so exercise the
    # reconciler directly: an explicit method (e.g. one already stored on the
    # record, which update mode feeds in) must survive untouched.
    contact = {
        "personal_email": "ada@example.com",
        "work_email": "ada.byron@corp.com",
        "phone": "555-123-4567",
        "best_contact": "ada@example.com",
        "preferred_contact_method": "linkedin",
    }
    resolved = import_csv._reconcile_best_contact(contact)

    assert resolved is None
    assert contact["preferred_contact_method"] == "linkedin"
    # Explicit method wins, so the free text is left in place rather than being
    # silently dropped against a method it doesn't correspond to.
    assert contact["best_contact"] == "ada@example.com"


@pytest.mark.parametrize("explicit", ["personal_email", "work_email", "phone"])
def test_explicit_method_wins_even_when_best_contact_names_another(explicit):
    contact = {
        "personal_email": "ada@example.com",
        "work_email": "ada.byron@corp.com",
        "phone": "555-123-4567",
        # Names work_email, but an explicit choice is already on record.
        "best_contact": "ada.byron@corp.com",
        "preferred_contact_method": explicit,
    }
    assert import_csv._reconcile_best_contact(contact) is None
    assert contact["preferred_contact_method"] == explicit


# --- Precedence + the review warning -----------------------------------------


def test_personal_email_wins_when_both_emails_are_identical():
    contact = _contact_of(
        first_name="Ada",
        last_name="Byron",
        personal_email="ada@example.com",
        work_email="ada@example.com",
        best_contact="ada@example.com",
    )
    assert contact["preferred_contact_method"] == "personal_email"


def test_reconcile_returns_the_resolved_method():
    contact = {"phone": "(555) 123-4567", "best_contact": "555.123.4567"}
    assert import_csv._reconcile_best_contact(contact) == "phone"


def test_match_best_contact_does_not_mutate():
    contact = {"personal_email": "ada@example.com", "best_contact": "ada@example.com"}
    before = dict(contact)
    assert import_csv._match_best_contact(contact) == "personal_email"
    assert contact == before


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("555-123-4567", "5551234567"),
        ("(555) 123 4567", "5551234567"),
        ("+1 555 123 4567", "5551234567"),
        ("1 (555) 123-4567", "5551234567"),
        ("ada@example.com", ""),
        ("no digits here", ""),
    ],
)
def test_phone_key_normalization(raw, expected):
    assert import_csv._phone_key(raw) == expected
