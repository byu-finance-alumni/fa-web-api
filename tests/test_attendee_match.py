"""Conference-attendee matching (#612).

Covers the rules the issue calls non-negotiable, plus Jake's four scoping
answers (2026-08-04):

  * email match WINS over a name match, and drops the name-only candidates;
  * an ambiguous name returns EVERY candidate, never a silent top pick;
  * preferred names, nicknames and maiden / birth names all match;
  * unmappable columns are IGNORED, never a row or file error;
  * re-running the same file does not double-add attendance.

No real DATABASE_URL is required: the propose() tests drive a hand-rolled
in-memory session (CI has no DB), and the parsing / scoring rules are pure
functions exercised directly.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import attendee_match


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["full_access"],
    )


def _csv(text: str) -> bytes:
    return text.strip().encode("utf-8")


def _candidate(**kwargs) -> dict:
    base = {
        "alumni_id": 1,
        "name": "",
        "first_name": None,
        "middle_name": None,
        "last_name": None,
        "preferred_first_name": None,
        "birth_name": None,
        "net_id": None,
        "graduation_year": None,
        "is_alumni": True,
        "employer": None,
        "title": None,
        "city": None,
        "state": None,
        "personal_email": None,
        "work_email": None,
    }
    base.update(kwargs)
    return base


def _db_row(**kwargs):
    """A stand-in for one candidate row exactly as ``_candidate_select()``
    returns it — DB column names, not the reshaped candidate dict."""
    candidate = _candidate(**kwargs)
    return SimpleNamespace(
        alumni_id=candidate["alumni_id"],
        net_id=candidate["net_id"],
        first_name=candidate["first_name"],
        middle_name=candidate["middle_name"],
        last_name=candidate["last_name"],
        preferred_first_name=candidate["preferred_first_name"],
        birth_name=candidate["birth_name"],
        graduation_year=candidate["graduation_year"],
        is_alumni=candidate["is_alumni"],
        personal_email=candidate["personal_email"],
        work_email=candidate["work_email"],
        current_employer=candidate["employer"],
        current_title=candidate["title"],
        current_city=candidate["city"],
        current_state=candidate["state"],
    )


# --- Stage 1: parsing + column mapping ---------------------------------------


def test_unmappable_columns_are_ignored_not_an_error():
    """Jake, 2026-08-04: columns that don't map are IGNORED, never an error."""
    rows, header_errors, ignored = attendee_match.parse_and_map(
        _csv(
            "First Name,Last Name,Company,Registration ID,Dietary Restrictions,"
            "Table Number\n"
            "Michael,Smith,Goldman Sachs,REG-88213,Vegetarian,7\n"
        )
    )
    assert header_errors == []
    assert len(rows) == 1
    # Reported so the operator SEES what was dropped -- but not fatal.
    assert set(ignored) == {"Registration ID", "Dietary Restrictions", "Table Number"}
    row = rows[0]
    assert row["first_name"] == "Michael"
    assert row["last_name"] == "Smith"
    assert row["company"] == "Goldman Sachs"
    # The friend payload carries only real DB fields; the junk never appears.
    assert row["payload"]["is_alumni"] is False
    assert "Registration ID" not in row["payload"]


def test_a_file_of_only_unmappable_columns_is_a_header_error_not_a_crash():
    _rows, header_errors, _ignored = attendee_match.parse_and_map(
        _csv("Registration ID,Table Number\nREG-1,7\n")
    )
    assert header_errors
    assert "name" in header_errors[0].lower()


def test_conference_header_spellings_alias_onto_db_fields():
    """A raw registration export uploads untouched: Email / Organization /
    Job Title / Mobile are all recognised."""
    rows, header_errors, ignored = attendee_match.parse_and_map(
        _csv(
            "Name,E-mail Address,Organization,Job Title,Mobile,City,State\n"
            "Kate Nielsen,kate@example.com,Deseret Trust,Analyst,801-555-0100,"
            "Salt Lake City,Utah\n"
        )
    )
    assert header_errors == []
    assert ignored == []
    payload = rows[0]["payload"]
    assert rows[0]["first_name"] == "Kate"
    assert rows[0]["last_name"] == "Nielsen"
    assert payload["contact"]["personal_email"] == "kate@example.com"
    assert payload["career"]["current_employer"] == "Deseret Trust"
    assert payload["career"]["current_title"] == "Analyst"
    assert payload["contact"]["phone"] == "801-555-0100"
    assert payload["career"]["current_city"] == "Salt Lake City"
    assert payload["career"]["current_state"] == "Utah"


def test_friend_payload_carries_everything_that_maps_to_a_column():
    """Jake: "everything we have on them that matches a field in the db"."""
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv(
            "First name,Last name,Email,Work Email,Company,Title,Phone,City,"
            "State,Country,Zip,LinkedIn,Notes,Badge Colour\n"
            "Ada,Byrne,ada@x.com,ada@firm.com,Byrne Capital,Partner,555-0100,"
            "Provo,Utah,United States,84604,https://linkedin.com/in/ada,"
            "Keynote speaker,Gold\n"
        )
    )
    labels = attendee_match._friend_field_labels(rows[0]["payload"])
    for expected in (
        "first_name",
        "last_name",
        "linkedin_url",
        "notes",
        "contact.personal_email",
        "contact.work_email",
        "contact.phone",
        "career.current_employer",
        "career.current_title",
        "career.current_city",
        "career.current_state",
        "career.current_country",
        "career.current_zip",
    ):
        assert expected in labels, expected


def test_a_bad_cell_warns_and_never_rejects_the_row():
    rows, header_errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name,Grad Year\nMike,Smith,class of 09\n")
    )
    assert header_errors == []
    assert rows[0]["cell_warnings"]  # reported ...
    assert rows[0]["last_name"] == "Smith"  # ... but the row still matches


def test_combined_name_column_splits_first_and_last():
    assert attendee_match._split_full_name("Michael J Smith") == ("Michael J", "Smith")
    assert attendee_match._split_full_name("Smith, Michael") == ("Michael", "Smith")
    assert attendee_match._split_full_name("Cher") == ("Cher", "")


def test_duplicate_mapped_column_is_rejected():
    _rows, header_errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name,Email,Email\nA,B,a@x.com,b@x.com\n")
    )
    assert any("Duplicate column" in e for e in header_errors)


# --- Name knowledge ----------------------------------------------------------


def test_nicknames_and_preferred_names_agree():
    assert attendee_match.given_names_agree("Mike", ["Michael", None, None]) == "nickname"
    assert attendee_match.given_names_agree("Kate", ["Katherine", None, None]) == "nickname"
    # The record's PREFERRED name is checked too -- this app stores and shows it
    # everywhere, so a badge that says "Kate" must find preferred_first_name.
    assert attendee_match.given_names_agree("Kate", ["Katherine", "Kate", None]) == "exact"
    assert attendee_match.given_names_agree("J", ["John", None, None]) == "initial"
    assert attendee_match.given_names_agree("Sarah", ["Michael", None, None]) is None


def test_surname_keys_cover_hyphens_accents_and_suffixes():
    assert "obrien" in attendee_match.surname_keys("O'Brien")
    assert attendee_match.surname_keys("Nunez") == attendee_match.surname_keys("Nuñez")
    keys = attendee_match.surname_keys("Smith-Jones")
    assert {"smith", "jones", "smithjones"} <= keys
    assert "smith" in attendee_match.surname_keys("Smith Jr")


def test_company_corroborates_across_legal_suffixes():
    assert attendee_match.companies_corroborate("Goldman", "Goldman Sachs & Co.")
    assert attendee_match.companies_corroborate("Goldman Sachs", "Goldman Sachs LLC")
    assert not attendee_match.companies_corroborate("Goldman Sachs", "Morgan Stanley")
    # Missing on either side is not corroboration -- and never a rejection.
    assert not attendee_match.companies_corroborate(None, "Goldman Sachs")


# --- Scoring: precedence, ambiguity, maiden names ----------------------------


def _row(**kwargs) -> dict:
    base = {
        "row": 2,
        "display_name": "",
        "first_name": None,
        "preferred_first_name": None,
        "last_name": None,
        "maiden_name": None,
        "emails": [],
        "company": None,
        "graduation_year": None,
        "note": None,
        "payload": {},
        "cell_warnings": [],
    }
    base.update(kwargs)
    return base


def test_email_match_beats_name_match_and_drops_name_candidates():
    row = _row(first_name="Michael", last_name="Smith", emails=["mike@goldman.com"])
    by_email = _candidate(
        alumni_id=10,
        first_name="Mike",
        last_name="Andersen",
        personal_email="mike@goldman.com",
    )
    by_name = _candidate(alumni_id=11, first_name="Michael", last_name="Smith")
    ranked = attendee_match.rank_candidates(row, [by_name, by_email])
    assert [c["alumni_id"] for c in ranked] == [10]
    assert ranked[0]["tier"] == attendee_match.TIER_EMAIL
    assert ranked[0]["confidence"] == "high"


def test_name_match_is_used_when_the_row_has_no_email():
    row = _row(first_name="Michael", last_name="Smith")
    ranked = attendee_match.rank_candidates(
        row, [_candidate(alumni_id=11, first_name="Michael", last_name="Smith")]
    )
    assert len(ranked) == 1
    assert ranked[0]["tier"] == attendee_match.TIER_NAME


def test_maiden_name_matches_the_records_birth_name():
    """An alumna who married after graduating: the file carries her married
    surname, the record keeps the maiden surname in birth_name (#216)."""
    row = _row(first_name="Kate", last_name="Nielsen")
    candidate = _candidate(
        alumni_id=20,
        first_name="Katherine",
        last_name="Nielsen",
        birth_name="Barker",
    )
    ranked = attendee_match.rank_candidates(row, [candidate])
    assert ranked and ranked[0]["alumni_id"] == 20

    # ... and the reverse: the file gives the MAIDEN name, the record the
    # married one.
    row_maiden = _row(first_name="Kate", last_name="Barker")
    candidate_married = _candidate(
        alumni_id=21,
        first_name="Katherine",
        last_name="Nielsen",
        birth_name="Barker",
    )
    ranked = attendee_match.rank_candidates(row_maiden, [candidate_married])
    assert ranked and ranked[0]["alumni_id"] == 21
    assert any("Maiden name" in e for e in ranked[0]["evidence"])


def test_married_surname_we_never_recorded_is_rescued_by_the_employer():
    """No surname agreement at all -- only the employer saves it, and it lands
    in the LOWEST tier so the reviewer treats it with suspicion."""
    row = _row(first_name="Kate", last_name="Nielsen", company="Goldman Sachs")
    candidate = _candidate(
        alumni_id=30,
        first_name="Katherine",
        last_name="Barker",
        employer="Goldman Sachs & Co.",
    )
    ranked = attendee_match.rank_candidates(row, [candidate])
    assert ranked[0]["tier"] == attendee_match.TIER_NAME_COMPANY
    assert ranked[0]["confidence"] == "low"
    assert any("surname does NOT" in e for e in ranked[0]["evidence"])


def test_a_company_mismatch_never_rejects_a_name_candidate():
    row = _row(first_name="John", last_name="Smith", company="Vanguard")
    ranked = attendee_match.rank_candidates(
        row,
        [
            _candidate(
                alumni_id=40,
                first_name="John",
                last_name="Smith",
                employer="Fidelity",
            )
        ],
    )
    assert len(ranked) == 1
    assert any("Employer differs" in e for e in ranked[0]["evidence"])


def test_company_only_raises_confidence_it_is_never_the_key():
    row = _row(first_name="John", last_name="Smith", company="Goldman Sachs")
    with_company = attendee_match.score_candidate(
        row,
        _candidate(
            alumni_id=1,
            first_name="John",
            last_name="Smith",
            employer="Goldman Sachs",
        ),
    )
    without = attendee_match.score_candidate(
        row, _candidate(alumni_id=2, first_name="John", last_name="Smith")
    )
    assert with_company["score"] > without["score"]
    assert with_company["confidence"] == "high"
    assert without["confidence"] == "medium"
    # Company alone, with no name agreement whatsoever, is not a match at all.
    assert (
        attendee_match.score_candidate(
            row, _candidate(alumni_id=3, first_name="Zoe", last_name="Vaughn",
                            employer="Goldman Sachs")
        )
        is None
    )


def test_two_john_smiths_come_back_as_a_choice_never_a_silent_pick():
    row = _row(first_name="John", last_name="Smith", company="Goldman Sachs")
    ranked = attendee_match.rank_candidates(
        row,
        [
            _candidate(
                alumni_id=51,
                first_name="John",
                last_name="Smith",
                employer="Goldman Sachs",
                graduation_year=2010,
            ),
            _candidate(
                alumni_id=52,
                first_name="John",
                last_name="Smith",
                employer="Fidelity",
                graduation_year=1998,
            ),
        ],
    )
    # Ranking the better-corroborated one first is fine; hiding the other is not.
    assert [c["alumni_id"] for c in ranked] == [51, 52]


def test_no_confidence_threshold_can_auto_apply():
    """There is deliberately no knob that turns a score into a write: the
    scoring surface exposes proposals only."""
    import inspect

    source = inspect.getsource(attendee_match)
    assert "auto_approve" not in source
    assert "auto_apply" not in source
    # propose() is the only DB-touching entry point and it never writes.
    for forbidden in ("session.add(", "session.delete(", "session.commit("):
        assert forbidden not in inspect.getsource(attendee_match.propose)


# --- Stage 2: propose (batched, per-row status) ------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def _iter(self):
        return [r[0] if isinstance(r, tuple) else r for r in self._rows]


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeSession:
    """Returns the candidate pool for the SELECT legs and the roster for the
    attendance SELECT. Records how many queries ran, so a regression to
    one-query-per-row is caught."""

    def __init__(self, candidates, attending=()):
        self._candidates = list(candidates)
        self._attending = list(attending)
        self.queries = 0

    async def execute(self, stmt):
        self.queries += 1
        compiled = str(stmt)
        if "event_attendance" in compiled:
            return _ScalarResult(self._attending)
        return _Result(self._candidates)


def _event():
    return SimpleNamespace(
        event_id=7, event_name="Spring Finance Conference", event_date=None
    )


@pytest.mark.anyio
async def test_propose_reports_matched_ambiguous_and_no_match():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv(
            "First name,Last name,Company\n"
            "Michael,Smith,Goldman Sachs\n"
            "John,Doe,Vanguard\n"
            "Zelda,Nonexistent,Nowhere Ltd\n"
        )
    )
    session = _FakeSession(
        [
            _db_row(alumni_id=1, first_name="Michael", last_name="Smith",
                    employer="Goldman Sachs"),
            _db_row(alumni_id=2, first_name="John", last_name="Doe",
                    graduation_year=2001),
            _db_row(alumni_id=3, first_name="John", last_name="Doe",
                    graduation_year=2014),
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    by_row = {r["row"]: r for r in report["rows"]}
    assert by_row[2]["status"] == "matched"
    assert by_row[3]["status"] == "ambiguous"
    assert len(by_row[3]["candidates"]) == 2
    assert by_row[4]["status"] == "no_match"
    assert report["summary"] == {
        "total_rows": 3,
        "matched": 1,
        "ambiguous": 1,
        "no_match": 1,
        "already_attending": 0,
    }
    assert report["event"]["event_id"] == 7


@pytest.mark.anyio
async def test_propose_batches_queries_and_never_runs_one_per_row():
    """Performance guard: the whole file resolves in a bounded number of
    queries (candidate legs + the roster), never one query per attendee."""
    body = "First name,Last name,Company\n" + "".join(
        f"Person{i},Surname{i},Firm{i}\n" for i in range(200)
    )
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv(body))
    session = _FakeSession([])
    await attendee_match.propose(session, _event(), rows)
    assert session.queries <= 4, session.queries


@pytest.mark.anyio
async def test_candidates_already_on_the_roster_are_flagged():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name\nMichael,Smith\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="Michael", last_name="Smith")],
        attending=[1],
    )
    report = await attendee_match.propose(session, _event(), rows)
    assert report["rows"][0]["candidates"][0]["already_attending"] is True
    assert report["summary"]["already_attending"] == 1


@pytest.mark.anyio
async def test_a_row_repeated_in_the_file_is_warned_not_merged_away():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name\nMichael,Smith\nMichael,Smith\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="Michael", last_name="Smith")]
    )
    report = await attendee_match.propose(session, _event(), rows)
    assert report["rows"][1]["warnings"]
    assert "row 2" in report["rows"][1]["warnings"][0]


# --- Routes ------------------------------------------------------------------


class _RouteSession:
    """Enough of an AsyncSession for the approve route: an event, a roster, and
    an alumni lookup. Records the EventAttendance rows added."""

    def __init__(self, event, alumni, attending=()):
        self._event = event
        self._alumni = alumni
        self._attending = list(attending)
        self.added: list = []
        self.committed = 0

    async def get(self, model, pk):
        if model.__name__ == "Event":
            return self._event
        return self._alumni.get(pk)

    async def execute(self, _stmt):
        return _ScalarResult(self._attending)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def _alumnus(alumni_id: int, **kwargs):
    return SimpleNamespace(
        alumni_id=alumni_id,
        archived=False,
        first_name=kwargs.get("first_name", "Michael"),
        preferred_first_name=kwargs.get("preferred_first_name"),
        last_name=kwargs.get("last_name", "Smith"),
        graduation_year=kwargs.get("graduation_year"),
    )


@pytest.fixture
def approve_client():
    state = {}

    def _make(session):
        state["session"] = session

        async def _dep():
            yield session

        app.dependency_overrides[get_session] = _dep
        app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_approve_requires_auth(approve_client):
    app.dependency_overrides.clear()

    async def _none():
        yield None

    app.dependency_overrides[get_session] = _none
    with TestClient(app) as client:
        response = client.post(
            "/events/1/attendees/match/approve", json={"approvals": []}
        )
    assert response.status_code == 401


def test_approve_adds_attendance_for_the_approved_id(approve_client):
    session = _RouteSession(_event(), {5: _alumnus(5)})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5, "row": 2}]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 1
    assert body["items"][0]["status"] == "added"
    assert body["items"][0]["name"] == "Michael Smith"
    assert any(type(o).__name__ == "EventAttendance" for o in session.added)


def test_rerunning_the_same_file_never_double_adds(approve_client):
    """Idempotent per (event, alumni): the second approval of the same person is
    a reported no-op, not a second attendance row and not a 409."""
    session = _RouteSession(_event(), {5: _alumnus(5)}, attending=[5])
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5}]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 0
    assert body["already_attending"] == 1
    assert not [o for o in session.added if type(o).__name__ == "EventAttendance"]


def test_the_same_id_approved_twice_in_one_batch_is_written_once(approve_client):
    session = _RouteSession(_event(), {5: _alumnus(5)})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5}, {"alumni_id": 5}]},
        )
    assert response.json()["added"] == 1
    assert (
        len([o for o in session.added if type(o).__name__ == "EventAttendance"]) == 1
    )


def test_approving_an_unknown_or_archived_alumnus_is_reported_not_written(
    approve_client,
):
    archived = _alumnus(6)
    archived.archived = True
    session = _RouteSession(_event(), {6: archived})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 6}, {"alumni_id": 99}]},
        )
    body = response.json()
    assert body["not_found"] == 2
    assert body["added"] == 0


def test_approve_rejects_unknown_keys(approve_client):
    """extra='forbid': no undocumented knob (a confidence threshold, an
    "approve all") can be smuggled into the approval body."""
    session = _RouteSession(_event(), {5: _alumnus(5)})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5}], "min_confidence": 0.9},
        )
    assert response.status_code == 422


def test_approve_404s_for_an_unknown_event(approve_client):
    session = _RouteSession(None, {})
    with approve_client(session) as client:
        response = client.post(
            "/events/999/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5}]},
        )
    assert response.status_code == 404


def test_template_downloads_a_starting_point_csv(approve_client):
    session = _RouteSession(_event(), {})
    with approve_client(session) as client:
        response = client.get("/events/attendees/match/template")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Maiden name" in response.text
