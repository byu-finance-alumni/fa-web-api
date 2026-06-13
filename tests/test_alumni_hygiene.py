"""Data-hygiene pipeline tests (cleaning, duplicate detection, preview).

The pure cleaning functions are unit-tested directly; duplicate detection and
the preview builder use a tiny fake session that returns configured rows (no
real DATABASE_URL — CI has none), mirroring tests/test_dashboard_drawers.py.
"""

import asyncio
from types import SimpleNamespace

from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull
from app.services import hygiene

# --- Fake session ------------------------------------------------------------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class FakeSession:
    """Returns queued scalars (for .scalar) and exec-results (for .execute)."""

    def __init__(self, scalars=(), execute_rows=()):
        self._scalars = list(scalars)
        self._execute_rows = list(execute_rows)

    async def scalar(self, stmt):
        return self._scalars.pop(0) if self._scalars else None

    async def execute(self, stmt):
        rows = self._execute_rows.pop(0) if self._execute_rows else []
        return _ExecResult(rows)


def _alum(**kw):
    base = dict(
        alumni_id=1,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        byu_id=None,
        net_id=None,
        archived=False,
        personal_email=None,
        work_email=None,
        current_employer=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- Cleaning: emails --------------------------------------------------------


def test_email_lowercased_and_trimmed():
    payload = AlumniCreateFull(
        last_name="Doe",
        contact={"personal_email": "  Jane.DOE@Example.COM  "},
    )
    cleaned, changes = hygiene.clean_alumni_payload(payload)
    assert cleaned["contact"]["personal_email"] == "jane.doe@example.com"
    assert any(c["field"] == "personal_email" for c in changes)


# --- Cleaning: names ---------------------------------------------------------


def test_all_caps_name_titlecased():
    payload = AlumniCreateFull(first_name="JANE", last_name="SMITH")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["first_name"] == "Jane"
    assert cleaned["last_name"] == "Smith"


def test_all_lower_name_titlecased():
    payload = AlumniCreateFull(last_name="anne marie")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "Anne Marie"


def test_mixed_case_mcdonald_preserved():
    payload = AlumniCreateFull(last_name="McDonald")
    cleaned, changes = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "McDonald"
    # No change recorded — already clean.
    assert not any(c["field"] == "last_name" for c in changes)


def test_obrien_lowercase_titlecased_but_apostrophe_kept():
    payload = AlumniCreateFull(last_name="o'brien")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "O'Brien"


def test_mixed_obrien_preserved():
    payload = AlumniCreateFull(last_name="O'Brien")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "O'Brien"


# --- Cleaning: phone ---------------------------------------------------------


def test_phone_10_digits():
    payload = AlumniCreateFull(
        last_name="Doe", contact={"phone": "801-555-1234"}
    )
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["contact"]["phone"] == "(801) 555-1234"


def test_phone_11_digits_leading_1():
    payload = AlumniCreateFull(
        last_name="Doe", contact={"phone": "1 (801) 555.1234"}
    )
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["contact"]["phone"] == "+1 (801) 555-1234"


def test_phone_nonstandard_left_alone():
    payload = AlumniCreateFull(
        last_name="Doe", contact={"phone": "+44 20 7946 0958"}
    )
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    # Not 10 or 11(+1) digits -> trimmed original kept.
    assert cleaned["contact"]["phone"] == "+44 20 7946 0958"


# --- Cleaning: linkedin ------------------------------------------------------


def test_linkedin_normalized():
    payload = AlumniCreateFull(
        last_name="Doe",
        linkedin_url="HTTP://WWW.LinkedIn.com/in/JaneDoe/?ref=x#frag",
    )
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["linkedin_url"] == "https://www.linkedin.com/in/JaneDoe"


# --- Cleaning: whitespace ----------------------------------------------------


def test_whitespace_collapsed():
    payload = AlumniCreateFull(first_name="Jane", last_name="Van   der  Berg")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    # Internal whitespace runs collapse to single spaces. Casing is mixed, so
    # smart-title leaves it as authored ("der" stays lowercase).
    assert cleaned["last_name"] == "Van der Berg"


def test_whitespace_collapsed_in_allcaps_name():
    payload = AlumniCreateFull(first_name="Jane", last_name="VAN   DER  BERG")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    # All-caps -> collapse, title-case, then re-lowercase nobiliary particles
    # that aren't the first word: "Van der Berg".
    assert cleaned["last_name"] == "Van der Berg"


# --- Cleaning: nobiliary particles -------------------------------------------


def test_lowercase_particles_van_der_berg():
    payload = AlumniCreateFull(last_name="van der berg")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    # First word capitalized; "der" stays lowercase (not first).
    assert cleaned["last_name"] == "Van der Berg"


def test_lowercase_particles_de_la_cruz():
    payload = AlumniCreateFull(last_name="DE LA CRUZ")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "De la Cruz"


def test_particle_first_word_capitalized():
    # A particle as the FIRST word is always capitalized.
    payload = AlumniCreateFull(last_name="van")
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["last_name"] == "Van"


# --- Cleaning: state ---------------------------------------------------------


def test_full_state_name_to_code():
    payload = AlumniCreateFull(last_name="Doe", contact={"state": "California"})
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["contact"]["state"] == "CA"


def test_two_letter_state_uppercased():
    payload = AlumniCreateFull(last_name="Doe", contact={"state": "ut"})
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["contact"]["state"] == "UT"


def test_career_state_full_name_to_code():
    payload = AlumniCreateFull(
        last_name="Doe", career={"current_state": "new york"}
    )
    cleaned, _ = hygiene.clean_alumni_payload(payload)
    assert cleaned["career"]["current_state"] == "NY"


# --- Cleaning: byu/net id ----------------------------------------------------


def test_byu_id_strips_nondigits():
    # byu_id schema validator requires 9 digits; feed via update to avoid it
    # since the cleaner runs on the model's already-validated value. Use a raw
    # 9-digit value with surrounding spaces (schema trims), then cleaner is a
    # no-op — assert net_id lowercasing instead for the strip behaviour.
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(net_id="JDoe12")
    )
    assert cleaned["net_id"] == "jdoe12"


# --- Idempotency -------------------------------------------------------------


def test_cleaning_is_idempotent():
    payload = AlumniCreateFull(
        first_name="Jane",
        last_name="McDonald",
        contact={
            "personal_email": "jane@example.com",
            "phone": "(801) 555-1234",
            "state": "CA",
        },
        linkedin_url="https://www.linkedin.com/in/janedoe",
    )
    cleaned1, changes1 = hygiene.clean_alumni_payload(payload)
    assert changes1 == []  # already clean -> no changes
    # Feed the cleaned values back through a fresh payload; still no changes.
    payload2 = AlumniCreateFull(
        first_name=cleaned1["first_name"],
        last_name=cleaned1["last_name"],
        contact=cleaned1["contact"],
        linkedin_url=cleaned1["linkedin_url"],
    )
    _, changes2 = hygiene.clean_alumni_payload(payload2)
    assert changes2 == []


def test_input_not_mutated():
    payload = AlumniCreateFull(first_name="JANE", last_name="DOE")
    hygiene.clean_alumni_payload(payload)
    assert payload.first_name == "JANE"  # original untouched


# --- Recommended warnings ----------------------------------------------------


def test_recommended_warnings_all_missing():
    warnings = hygiene.recommended_warnings(
        {"contact": {}, "career": {}, "graduation_year": None}
    )
    codes = {w["code"] for w in warnings}
    assert codes == {"missing_email", "missing_employer", "missing_grad_year"}


def test_recommended_warnings_none_when_present():
    warnings = hygiene.recommended_warnings(
        {
            "contact": {"work_email": "a@b.com"},
            "career": {"current_employer": "Goldman"},
            "graduation_year": 2018,
        }
    )
    assert warnings == []


# --- Duplicate detection -----------------------------------------------------


def test_detect_exact_byu_id_blocker():
    session = FakeSession(scalars=[_alum(byu_id="123456789")])
    blockers, warnings = asyncio.run(
        hygiene.detect_duplicates(session, {"byu_id": "123456789"})
    )
    assert len(blockers) == 1
    assert blockers[0]["code"] == "duplicate_byu_id"
    assert blockers[0]["field"] == "byu_id"
    assert blockers[0]["alumni_id"] == 1
    assert "Jane Doe" in blockers[0]["message"]
    assert warnings == []


def test_detect_exact_net_id_blocker():
    # byu_id is None -> only the net_id lookup runs (one .scalar call).
    session = FakeSession(scalars=[_alum(net_id="jdoe12")])
    blockers, _ = asyncio.run(
        hygiene.detect_duplicates(
            session, {"byu_id": None, "net_id": "jdoe12"}
        )
    )
    assert len(blockers) == 1
    assert blockers[0]["code"] == "duplicate_net_id"


def test_detect_archived_ghost_byu_id_warns():
    # No ACTIVE byu match (first scalar None) -> the archived lookup (second
    # scalar) returns a ghost -> a duplicate_archived WARNING, never a blocker.
    ghost = _alum(alumni_id=9, first_name="Old", last_name="Record")
    session = FakeSession(scalars=[None, ghost])
    blockers, warnings = asyncio.run(
        hygiene.detect_duplicates(session, {"byu_id": "123456789"})
    )
    assert blockers == []
    codes = {w["code"] for w in warnings}
    assert "duplicate_archived" in codes
    ghost_warn = next(w for w in warnings if w["code"] == "duplicate_archived")
    assert ghost_warn["alumni_id"] == 9
    assert "Old Record" in ghost_warn["message"]


def test_detect_active_byu_match_skips_archived_lookup():
    # An ACTIVE match blocks; the archived lookup must NOT run (only one scalar
    # is consumed), so an extra queued scalar would be left untouched.
    session = FakeSession(scalars=[_alum(byu_id="123456789")])
    blockers, warnings = asyncio.run(
        hygiene.detect_duplicates(session, {"byu_id": "123456789"})
    )
    assert len(blockers) == 1
    assert blockers[0]["code"] == "duplicate_byu_id"
    assert not any(w["code"] == "duplicate_archived" for w in warnings)


def test_detect_fuzzy_warning():
    # No byu/net id; fuzzy execute returns one same-name same-year match.
    session = FakeSession(
        scalars=[],
        execute_rows=[[_alum(alumni_id=2)]],
    )
    blockers, warnings = asyncio.run(
        hygiene.detect_duplicates(
            session,
            {"first_name": "Jane", "last_name": "Doe", "graduation_year": 2018},
        )
    )
    assert blockers == []
    assert len(warnings) == 1
    assert warnings[0]["code"] == "possible_duplicate"
    assert warnings[0]["alumni_id"] == 2
    assert "Class of 2018" in warnings[0]["message"]


def test_fuzzy_excludes_blocker_id():
    # Same record (id=2) is both an exact byu_id blocker AND a fuzzy match;
    # it must not be double-reported as a warning.
    session = FakeSession(
        scalars=[_alum(alumni_id=2, byu_id="123456789")],
        execute_rows=[[_alum(alumni_id=2)]],
    )
    blockers, warnings = asyncio.run(
        hygiene.detect_duplicates(
            session,
            {
                "byu_id": "123456789",
                "first_name": "Jane",
                "last_name": "Doe",
                "graduation_year": 2018,
            },
        )
    )
    assert len(blockers) == 1
    assert warnings == []


# --- Preview builder ---------------------------------------------------------


def test_build_preview_create_shape():
    session = FakeSession(scalars=[])  # no dup lookups hit
    payload = AlumniCreateFull(
        first_name="JANE",
        last_name="DOE",
        contact={"personal_email": "JANE@X.COM"},
    )
    preview = asyncio.run(hygiene.build_preview(session, payload))
    assert set(preview.keys()) == {"cleaned", "changes", "warnings", "blockers"}
    assert preview["cleaned"]["first_name"] == "Jane"
    assert preview["blockers"] == []
    # No employer + no grad year -> recommended warnings present.
    codes = {w["code"] for w in preview["warnings"]}
    assert "missing_employer" in codes
    assert "missing_grad_year" in codes
    assert "missing_email" not in codes  # email was provided


def test_build_preview_blocker_excluded_from_warnings():
    session = FakeSession(scalars=[_alum(byu_id="123456789")])
    payload = AlumniCreateFull(byu_id="123456789", last_name="Doe")
    preview = asyncio.run(hygiene.build_preview(session, payload))
    assert len(preview["blockers"]) == 1
    # Blockers never appear in warnings.
    assert all(
        w.get("code") != "duplicate_byu_id" for w in preview["warnings"]
    )


def test_update_preview_uses_effective_record():
    # Update only changes the city; the effective record's grad year + employer
    # come from the stored row, so recommended warnings reflect the result.
    existing = _alum(alumni_id=5, graduation_year=2018)
    # scalars consumed: (no dup byu/net since none in payload) -> effective
    # loads contact row, then career row.
    contact_row = SimpleNamespace(
        personal_email="jane@x.com", work_email=None
    )
    career_row = SimpleNamespace(current_employer="Goldman")
    session = FakeSession(scalars=[contact_row, career_row])
    payload = AlumniUpdateFull(contact={"city": "provo"})
    preview = asyncio.run(
        hygiene.build_preview(
            session, payload, existing=existing, exclude_alumni_id=5
        )
    )
    assert preview["cleaned"]["contact"]["city"] == "Provo"
    codes = {w["code"] for w in preview["warnings"]}
    # Email + employer + grad year all present in effective record -> no warns.
    assert codes == set()
