"""Conference-attendee matching (#612, #537).

Covers the rules the issue calls non-negotiable, plus Jake's four scoping
answers (2026-08-04):

  * email match WINS over a name match, and drops the name-only candidates;
  * an ambiguous name returns EVERY candidate, never a silent top pick;
  * preferred names, nicknames and maiden / birth names all match;
  * unmappable columns are IGNORED, never a row or file error;
  * re-running the same file does not double-add attendance.

And the Net ID tier (#537, Jake 2026-09-15 -- "if the Net ID matches then no
need to approve; if emails match but no Net ID then ask for approval"):

  * an exact Net ID hit is matched AND auto_confirmed;
  * a Net ID we don't know is NOT a silent name fallback -- the row falls
    through to email / name as a proposal with the reason on the row;
  * Net ID -> A but email / name -> B is ambiguous with both listed;
  * casing and whitespace variants normalise to one key;
  * an empty Net ID column behaves exactly as a file without one;
  * the apply path applies an auto_confirmed row without approval and does NOT
    apply a merely proposed email row.

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
    """There is deliberately no knob that turns a SCORE into a write: the
    scoring surface exposes proposals only. (#537's Net ID confirmation is an
    exact identifier, not a threshold, and is still written only by /approve
    -- see the tier-0 section below.)"""
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
        "not_reviewed": 0,
        "already_attending": 0,
        "auto_confirmed": 0,
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
        net_id=kwargs.get("net_id"),
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
    # #537: the template documents the Net ID column and its example row
    # carries one, so staff know to supply it.
    header, example, *_rest = response.text.splitlines()
    assert header.split(",")[0] == "Net ID"
    assert example.split(",")[0] == "msmith07"


# --- Disclosure budget (security review, 2026-08-04) -------------------------


@pytest.mark.anyio
async def test_the_review_stops_disclosing_once_the_budget_is_spent(monkeypatch):
    """A preview may surface only so many alumni records. Past the budget the
    row is reported ``not_reviewed`` -- NOT ``no_match``, which would read as
    "she isn't in the database" and invite a duplicate friend record."""
    monkeypatch.setattr(attendee_match, "MAX_CANDIDATES_TOTAL", 2)
    body = "First name,Last name\n" + "".join(
        f"Michael,Surname{i}\n" for i in range(4)
    )
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv(body))
    session = _FakeSession(
        [
            _db_row(alumni_id=100 + i, first_name="Michael", last_name=f"Surname{i}")
            for i in range(4)
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    assert report["summary"]["not_reviewed"] == 2
    tail = report["rows"][-1]
    assert tail["status"] == "not_reviewed"
    assert tail["candidates"] == []
    assert any(w["code"] == "disclosure_cap" for w in report["warnings"])


@pytest.mark.anyio
async def test_the_report_names_whose_records_were_disclosed():
    """The audit trail has to answer "whose data left the system", not only
    "how many rows" -- an ambiguous row surfaces near-misses who have nothing
    to do with the conference."""
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name\nJohn,Doe\n")
    )
    session = _FakeSession(
        [
            _db_row(alumni_id=11, first_name="John", last_name="Doe"),
            _db_row(alumni_id=12, first_name="John", last_name="Doe"),
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    assert report["_disclosed_alumni_ids"] == [11, 12]


def test_friend_identity_key_is_stable_across_spelling():
    key = attendee_match.friend_identity_key
    assert key("Michael", "Smith", "Goldman Sachs & Co.") == key(
        " michael ", "SMITH", "Goldman Sachs"
    )
    assert key("Michael", "Smith", "Goldman") != key("Michael", "Smith", "Fidelity")


# --- Route authorization -----------------------------------------------------


@pytest.mark.parametrize("role", ["view_only", "student", "professor"])
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/events/attendees/match/template", {}),
        (
            "post",
            "/events/7/attendees/match/preview",
            {"files": {"file": ("a.csv", b"First name\nA\n")}},
        ),
        (
            "post",
            "/events/7/attendees/match/approve",
            {"json": {"approvals": []}},
        ),
        (
            "post",
            "/events/7/attendees/match/friends",
            {
                "files": {"file": ("a.csv", b"First name\nA\n")},
                "data": {"rows": "2"},
            },
        ),
    ],
)
def test_every_leg_is_full_access_only(role, method, path, kwargs):
    """All four legs sit at full_access -- the same rung as the attendee CSV
    export, which already discloses the same columns. /preview reads real alumni
    PII to be reviewable at all, so it must not sit on a looser guard than the
    writes."""
    session = _RouteSession(_event(), {})

    async def _dep():
        yield session

    app.dependency_overrides[get_session] = _dep
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    try:
        with TestClient(app) as client:
            response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


# --- Tier 0: Net ID (#537) ----------------------------------------------------
#
# Jake, 2026-09-15: "if the Net ID matches then no need to approve; if emails
# match but no Net ID then ask for approval."


def test_exact_net_id_hit_is_tier_zero_certain_and_drops_homonyms():
    row = _row(first_name="John", last_name="Smith", net_id="jsmith12")
    by_net_id = _candidate(
        alumni_id=1, first_name="John", last_name="Smith", net_id="jsmith12"
    )
    homonym = _candidate(alumni_id=2, first_name="John", last_name="Smith")
    ranked = attendee_match.rank_candidates(row, [homonym, by_net_id])
    assert [c["alumni_id"] for c in ranked] == [1]
    assert ranked[0]["tier"] == attendee_match.TIER_NETID
    assert ranked[0]["confidence"] == attendee_match.CONFIDENCE_CERTAIN
    assert ranked[0]["corroborated"] is True
    assert ranked[0]["evidence"][0] == "Net ID matches (jsmith12)"


@pytest.mark.parametrize(
    "cell", ["jsmith12", "JSMITH12", " jsmith12 ", "JSmith12", "\tJSMITH12\t"]
)
def test_net_id_casing_and_whitespace_variants_normalise(cell):
    rows, header_errors, _ignored = attendee_match.parse_and_map(
        _csv(f"Net ID,First name,Last name\n{cell},John,Smith\n")
    )
    assert header_errors == []
    assert rows[0]["net_id"] == "jsmith12"
    # ...and a stored value in any casing still hits (the DB column is
    # validated lower-case, but the comparison does not rely on it).
    candidate = _candidate(alumni_id=1, first_name="John", last_name="Smith",
                           net_id="JSmith12 ")
    ranked = attendee_match.rank_candidates(rows[0], [candidate])
    assert ranked and ranked[0]["tier"] == attendee_match.TIER_NETID


def test_net_id_header_aliases_map_onto_the_column():
    for header in ("Net ID", "net id", "NetID", "netid", "NET_ID"):
        rows, header_errors, ignored = attendee_match.parse_and_map(
            _csv(f"{header},First name,Last name\nabc12,John,Smith\n")
        )
        assert header_errors == [] and ignored == [], header
        assert rows[0]["net_id"] == "abc12", header


@pytest.mark.anyio
async def test_net_id_hit_is_matched_and_auto_confirmed():
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name,Email\n"
             "JSMITH12,John,Smith,john@example.com\n")
    )
    session = _FakeSession(
        [
            _db_row(alumni_id=1, first_name="John", last_name="Smith",
                    net_id="jsmith12", personal_email="john@example.com"),
            _db_row(alumni_id=2, first_name="John", last_name="Smith"),
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "matched"
    assert row["auto_confirmed"] is True
    assert row["match_key"] == "netid"
    assert row["attendee"]["net_id"] == "jsmith12"
    assert [c["alumni_id"] for c in row["candidates"]] == [1]
    assert row["reason"] == "Net ID matches John Smith; no approval needed."
    assert row["friend_eligible"] is False
    assert row["warnings"] == []
    assert report["summary"]["matched"] == 1
    assert report["summary"]["auto_confirmed"] == 1


@pytest.mark.anyio
async def test_unknown_net_id_is_not_a_silent_name_fallback():
    """A Net ID we don't know does NOT become a no-match (our own record may
    simply lack one, and a no-match would invite a duplicate friend record),
    and it does NOT quietly turn into a name match: the row falls through to
    email / name as a PROPOSAL and the reason is on the row."""
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\nnobody99,John,Smith\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="John", last_name="Smith",
                 net_id="jsmith12")]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "matched"
    assert row["auto_confirmed"] is False  # proposed, needs approval
    assert row["match_key"] == "name"
    assert row["reason"].startswith("Net ID 'nobody99' is not on any record")
    assert row["reason"] in row["warnings"]
    assert row["candidates"][0]["tier"] == attendee_match.TIER_NAME
    assert "Net ID differs (file: nobody99; record: jsmith12)" in (
        row["candidates"][0]["evidence"]
    )
    assert row["friend_eligible"] is False
    assert report["summary"]["auto_confirmed"] == 0


@pytest.mark.anyio
async def test_unknown_net_id_with_no_email_or_name_match_is_a_no_match():
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\nnobody99,Zelda,Nonexistent\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="John", last_name="Smith",
                 net_id="jsmith12")]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "no_match"
    assert row["candidates"] == []
    assert row["auto_confirmed"] is False
    assert row["friend_eligible"] is True  # failed EVERY tier
    assert "nobody99" in row["reason"]


@pytest.mark.anyio
async def test_malformed_net_id_is_reported_and_falls_through():
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\njohn.smith@byu.edu,John,Smith\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="John", last_name="Smith")]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "matched" and row["auto_confirmed"] is False
    assert "is not a valid Net ID" in row["reason"]
    # A malformed value never reaches SQL as a Net ID key.
    assert session.queries <= 3


@pytest.mark.anyio
async def test_net_id_to_a_but_email_to_b_is_ambiguous_with_both_listed():
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name,Email\n"
             "asmith01,Alice,Smith,shared@example.com\n")
    )
    a = _db_row(alumni_id=1, first_name="Alice", last_name="Smith",
                net_id="asmith01")
    b = _db_row(alumni_id=2, first_name="Bob", last_name="Jones",
                net_id="bjones02", personal_email="shared@example.com")
    report = await attendee_match.propose(_FakeSession([a, b]), _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "ambiguous"
    assert row["auto_confirmed"] is False
    assert row["match_key"] == "netid"
    assert [c["alumni_id"] for c in row["candidates"]] == [1, 2]
    assert row["candidates"][0]["tier"] == attendee_match.TIER_NETID
    assert row["candidates"][1]["tier"] == attendee_match.TIER_EMAIL
    assert row["reason"] == (
        "Net ID matches Alice Smith but the email matches Bob Jones. Choose one."
    )
    assert row["friend_eligible"] is False
    assert report["summary"] == {
        "total_rows": 1,
        "matched": 0,
        "auto_confirmed": 0,
        "ambiguous": 1,
        "no_match": 0,
        "not_reviewed": 0,
        "already_attending": 0,
    }


@pytest.mark.anyio
async def test_net_id_to_a_but_name_to_b_is_ambiguous_with_both_listed():
    """The file says Net ID -> Bob Jones but the name on the row is Kate
    Nielsen, and we HAVE a Kate Nielsen: two candidates, no silent pick."""
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\nbjones02,Kate,Nielsen\n")
    )
    a = _db_row(alumni_id=1, first_name="Bob", last_name="Jones",
                net_id="bjones02")
    b = _db_row(alumni_id=2, first_name="Katherine", last_name="Nielsen")
    report = await attendee_match.propose(_FakeSession([a, b]), _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "ambiguous"
    assert row["auto_confirmed"] is False
    assert [c["alumni_id"] for c in row["candidates"]] == [1, 2]
    assert row["reason"] == (
        "Net ID matches Bob Jones but the name matches Katherine Nielsen. "
        "Choose one."
    )


@pytest.mark.anyio
async def test_a_homonym_does_not_contradict_a_corroborated_net_id():
    """Net ID -> John Smith #1 whose name agrees with the file; John Smith #2
    is just another John Smith. The exact identifier already told them apart,
    so this is confirmed, not ambiguous -- otherwise every common name would
    need a click and the Net ID column would be pointless."""
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\njsmith12,John,Smith\n")
    )
    session = _FakeSession(
        [
            _db_row(alumni_id=1, first_name="John", last_name="Smith",
                    net_id="jsmith12"),
            _db_row(alumni_id=2, first_name="John", last_name="Smith",
                    net_id="jsmith99"),
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "matched" and row["auto_confirmed"] is True
    assert [c["alumni_id"] for c in row["candidates"]] == [1]


@pytest.mark.anyio
async def test_net_id_hit_with_a_disagreeing_name_is_confirmed_but_warned():
    """Nothing else in the file agrees with the record (a married surname we
    never stored, or a typo in the Net ID). Jake's rule stands -- the exact
    identifier confirms -- but the reviewer is told the name did not agree."""
    rows, _e, _i = attendee_match.parse_and_map(
        _csv("Net ID,First name,Last name\nbjones02,Kate,Nielsen\n")
    )
    session = _FakeSession(
        [_db_row(alumni_id=1, first_name="Bob", last_name="Jones",
                 net_id="bjones02")]
    )
    report = await attendee_match.propose(session, _event(), rows)
    row = report["rows"][0]
    assert row["status"] == "matched" and row["auto_confirmed"] is True
    assert row["candidates"][0]["corroborated"] is False
    assert any(w.startswith("Only the Net ID matches") for w in row["warnings"])
    assert any("does NOT agree" in e for e in row["candidates"][0]["evidence"])


_TODAY_FIXTURE_ROWS = (
    "Michael,Smith,Goldman Sachs,mike@goldman.com\n"
    "John,Doe,Vanguard,\n"
    "Kate,Nielsen,Deseret Trust,\n"
    "Zelda,Nonexistent,Nowhere Ltd,\n"
)
_TODAY_FIXTURE_DB = [
    _db_row(alumni_id=1, first_name="Michael", last_name="Andersen",
            personal_email="mike@goldman.com", net_id="mand01"),
    _db_row(alumni_id=2, first_name="John", last_name="Doe", graduation_year=2001),
    _db_row(alumni_id=3, first_name="John", last_name="Doe", graduation_year=2014),
    _db_row(alumni_id=4, first_name="Katherine", last_name="Nielsen",
            net_id="knielsen"),
]


@pytest.mark.anyio
async def test_an_empty_net_id_column_behaves_exactly_like_no_column():
    """Jake: "empty Net ID column -> behaviour is exactly as today (email, then
    name)". Proven by running the same fixtures with no Net ID column, with an
    all-blank one, and with a blank cell per row, and comparing the reports."""
    without_column, _e, _i = attendee_match.parse_and_map(
        _csv("First name,Last name,Company,Email\n" + _TODAY_FIXTURE_ROWS)
    )
    with_blank_column, _e, _i = attendee_match.parse_and_map(
        _csv(
            "Net ID,First name,Last name,Company,Email\n"
            + "".join(f",{line}\n" for line in _TODAY_FIXTURE_ROWS.splitlines())
        )
    )
    with_whitespace_cells, _e, _i = attendee_match.parse_and_map(
        _csv(
            "First name,Last name,Company,Email,netid\n"
            + "".join(f"{line},   \n" for line in _TODAY_FIXTURE_ROWS.splitlines())
        )
    )
    reports = [
        await attendee_match.propose(_FakeSession(_TODAY_FIXTURE_DB), _event(), r)
        for r in (without_column, with_blank_column, with_whitespace_cells)
    ]
    assert reports[0] == reports[1] == reports[2]
    baseline = reports[0]
    statuses = [r["status"] for r in baseline["rows"]]
    assert statuses == ["matched", "ambiguous", "matched", "no_match"]
    assert all(r["auto_confirmed"] is False for r in baseline["rows"])
    assert all(r["reason"] is None for r in baseline["rows"])
    assert all(r["attendee"]["net_id"] is None for r in baseline["rows"])
    assert [r["match_key"] for r in baseline["rows"]] == [
        "email", "name", "name", "name"
    ]
    assert baseline["summary"]["auto_confirmed"] == 0
    # Nothing in the email / name path reaches for a Net ID.
    assert all(
        c["tier"] != attendee_match.TIER_NETID
        for r in baseline["rows"]
        for c in r["candidates"]
    )


@pytest.mark.anyio
async def test_a_file_with_net_ids_still_resolves_in_a_bounded_number_of_queries():
    body = "Net ID,First name,Last name,Company\n" + "".join(
        f"user{i:04d},Person{i},Surname{i},Firm{i}\n" for i in range(200)
    )
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv(body))
    session = _FakeSession([])
    await attendee_match.propose(session, _event(), rows)
    assert session.queries <= 5, session.queries


@pytest.mark.anyio
async def test_friend_offer_only_for_rows_that_failed_every_tier():
    """Jake did not answer which category drives the friend prompt, so the
    SAFER default: only a row that failed every tier, never a row that merely
    lacks a Net ID -- that would create a duplicate profile for an alum we
    matched on email or name."""
    rows, _e, _i = attendee_match.parse_and_map(
        _csv(
            "Net ID,First name,Last name,Email\n"
            "jsmith12,John,Smith,\n"            # confirmed on Net ID
            ",Michael,Andersen,mike@goldman.com\n"  # no Net ID, email hit
            "nobody99,Kate,Nielsen,\n"          # unknown Net ID, name hit
            ",Zelda,Nonexistent,\n"             # nothing at all
        )
    )
    session = _FakeSession(
        [
            _db_row(alumni_id=1, first_name="John", last_name="Smith",
                    net_id="jsmith12"),
            _db_row(alumni_id=2, first_name="Michael", last_name="Andersen",
                    personal_email="mike@goldman.com"),
            _db_row(alumni_id=4, first_name="Katherine", last_name="Nielsen"),
        ]
    )
    report = await attendee_match.propose(session, _event(), rows)
    assert [(r["status"], r["friend_eligible"]) for r in report["rows"]] == [
        ("matched", False),
        ("matched", False),
        ("matched", False),
        ("no_match", True),
    ]


# --- Tier 0 through the routes: the SAME apply path --------------------------


class _PreviewSession(_FakeSession):
    """The preview route on top of the propose() fake: an event lookup, the
    audit-log add and the commit."""

    def __init__(self, event, candidates, attending=()):
        super().__init__(candidates, attending)
        self._event = event
        self.added: list = []
        self.committed = 0

    async def get(self, _model, _pk):
        return self._event

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def test_auto_confirmed_approvals_selects_only_the_net_id_rows():
    report = {
        "rows": [
            {"row": 2, "status": "matched", "auto_confirmed": True,
             "attendee": {"net_id": "jsmith12"},
             "candidates": [{"alumni_id": 1}]},
            {"row": 3, "status": "matched", "auto_confirmed": False,
             "attendee": {"net_id": None},
             "candidates": [{"alumni_id": 2}]},
            {"row": 4, "status": "ambiguous", "auto_confirmed": False,
             "attendee": {"net_id": "asmith01"},
             "candidates": [{"alumni_id": 3}, {"alumni_id": 4}]},
        ]
    }
    assert attendee_match.auto_confirmed_approvals(report) == [
        {"alumni_id": 1, "row": 2, "net_id": "jsmith12"}
    ]


def test_preview_then_apply_writes_the_net_id_row_and_not_the_email_row(
    approve_client,
):
    """End to end through the routes: the preview reports the Net ID row
    auto_confirmed and the email row proposed; feeding ONLY the auto-confirmed
    rows to the SAME /approve endpoint writes attendance for the Net ID row,
    the email row stays in the human queue, and the audit entry says so."""
    net_id_alum = _db_row(alumni_id=1, first_name="John", last_name="Smith",
                          net_id="jsmith12")
    email_alum = _db_row(alumni_id=2, first_name="Michael", last_name="Andersen",
                         personal_email="mike@goldman.com")
    preview_session = _PreviewSession(_event(), [net_id_alum, email_alum])
    with approve_client(preview_session) as client:
        response = client.post(
            "/events/7/attendees/match/preview",
            files={
                "file": (
                    "a.csv",
                    _csv(
                        "Net ID,First name,Last name,Email\n"
                        "JSMITH12,John,Smith,\n"
                        ",Michael,Andersen,mike@goldman.com\n"
                    ),
                )
            },
        )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert [r["auto_confirmed"] for r in preview["rows"]] == [True, False]
    assert [r["status"] for r in preview["rows"]] == ["matched", "matched"]
    assert preview["summary"]["auto_confirmed"] == 1
    assert preview["rows"][1]["candidates"][0]["tier"] == "email"
    # The preview never wrote attendance; it audited the disclosure.
    assert [type(o).__name__ for o in preview_session.added] == ["AuditLog"]
    assert "auto_confirmed=1" in preview_session.added[0].new_value

    approvals = attendee_match.auto_confirmed_approvals(preview)
    assert approvals == [{"alumni_id": 1, "row": 2, "net_id": "jsmith12"}]

    apply_session = _RouteSession(
        _event(),
        {
            1: _alumnus(1, first_name="John", net_id="jsmith12"),
            2: _alumnus(2, first_name="Michael", last_name="Andersen"),
        },
    )
    with approve_client(apply_session) as client:
        response = client.post(
            "/events/7/attendees/match/approve", json={"approvals": approvals}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["added"] == 1 and body["net_id_mismatch"] == 0
    assert body["items"] == [
        {"alumni_id": 1, "row": 2, "status": "added", "name": "John Smith",
         "message": None}
    ]
    attendance = [o for o in apply_session.added if type(o).__name__ == "EventAttendance"]
    assert [a.alumni_id for a in attendance] == [1]  # the email row is NOT written
    audit = [o for o in apply_session.added if type(o).__name__ == "AuditLog"]
    assert audit[0].new_value == (
        "1: John Smith (Net ID match jsmith12, confirmed without approval)"
    )


def test_a_human_approval_is_still_labelled_as_one(approve_client):
    session = _RouteSession(_event(), {5: _alumnus(5, net_id="msmith07")})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5, "row": 2}]},
        )
    assert response.json()["added"] == 1
    audit = [o for o in session.added if type(o).__name__ == "AuditLog"]
    assert audit[0].new_value.endswith("(approved match)")


def test_a_net_id_the_record_does_not_bear_is_refused_not_downgraded(
    approve_client,
):
    """A client cannot label an arbitrary id as a Net ID confirmation: the
    server checks the record's own Net ID and writes nothing on a mismatch."""
    session = _RouteSession(
        _event(),
        {5: _alumnus(5, net_id="msmith07"), 6: _alumnus(6, net_id=None)},
    )
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={
                "approvals": [
                    {"alumni_id": 5, "net_id": "someoneelse"},
                    {"alumni_id": 6, "net_id": "msmith07"},
                    {"alumni_id": 5, "net_id": " MSMITH07 "},  # normalised hit
                ]
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["net_id_mismatch"] == 2
    assert body["added"] == 0  # alumni 5 was de-duplicated by its first entry
    assert [i["status"] for i in body["items"]] == [
        "net_id_mismatch", "net_id_mismatch"
    ]
    assert not [o for o in session.added if type(o).__name__ == "EventAttendance"]
    # Each refusal leaves an audit row attributed to the caller -- a run of
    # them is what a tampered payload looks like -- without the claimed value.
    audit = [o for o in session.added if type(o).__name__ == "AuditLog"]
    assert [a.action_type for a in audit] == ["attendee_net_id_mismatch"] * 2
    assert all(a.entity_type == "event" and a.entity_id == 7 for a in audit)
    assert all("someoneelse" not in a.new_value for a in audit)
    assert [a.new_value.split(":")[0] for a in audit] == ["5", "6"]


def test_a_normalised_net_id_approval_is_written(approve_client):
    session = _RouteSession(_event(), {5: _alumnus(5, net_id="msmith07")})
    with approve_client(session) as client:
        response = client.post(
            "/events/7/attendees/match/approve",
            json={"approvals": [{"alumni_id": 5, "net_id": " MSMITH07 "}]},
        )
    assert response.json()["added"] == 1


# The friends route's identity / idempotency tests live in
# tests/test_friend_identity.py (#538): dedupe by email across events, the
# name + employer fallback, the alumnus guard, and the visible friend id.
