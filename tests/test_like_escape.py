"""Tests for LIKE/ILIKE wildcard-injection hardening.

``escape_like`` is unit-tested directly, and every query builder that embeds a
user-supplied term in an ``.ilike(...)`` is compiled to Postgres SQL to assert
the term's metacharacters are escaped and an ``ESCAPE '\\'`` clause is emitted.
No database needed (mirrors ``test_alumni_search`` / ``test_audit_search``).
"""

import re

from sqlalchemy.dialects import postgresql

from app.repositories.alumni import build_alumni_query
from app.repositories.audit import build_audit_query
from app.utils.sql import escape_like


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _params(stmt) -> dict:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return dict(compiled.params)


# The escape char compiles to a doubled backslash in the rendered SQL literal.
ESCAPE_CLAUSE = "ESCAPE '\\\\'"


# --- escape_like -------------------------------------------------------------


def test_escape_percent():
    assert escape_like("100%") == "100\\%"


def test_escape_underscore():
    assert escape_like("a_b") == "a\\_b"


def test_escape_backslash_first():
    # Backslash must be escaped too, and must not double-escape the metachars.
    assert escape_like("a\\b") == "a\\\\b"


def test_escape_combined():
    assert escape_like("50%_\\x") == "50\\%\\_\\\\x"


def test_escape_noop_on_plain_text():
    assert escape_like("Goldman Sachs") == "Goldman Sachs"


def test_escape_empty_string():
    assert escape_like("") == ""


# --- alumni query builder ----------------------------------------------------


def test_alumni_q_deletes_wildcards_instead_of_escaping_them():
    # Since #620 the free-text q is matched on the NORMALIZED form, which keeps
    # only [a-z0-9] -- so a LIKE metacharacter cannot survive into the pattern at
    # all and there is nothing left to escape. Strictly safer than escaping.
    stmt = build_alumni_query(q="50%_admin")
    values = list(_params(stmt).values())
    assert "%50admin%" in values
    assert not any(isinstance(v, str) and ("%_" in v or "\\" in v) for v in values)


def test_alumni_employer_escapes_wildcards():
    stmt = build_alumni_query(employer="A_B%")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert "A\\_B\\%" in _params(stmt).values()


def test_alumni_industry_escapes_wildcards():
    # One ESCAPE clause, not two: since #584 ``industry`` matches the primary
    # column only, the secondary column is reached via ``secondary_industry``.
    stmt = build_alumni_query(industry="Tech_%")
    sql = _sql(stmt)
    assert sql.count(ESCAPE_CLAUSE) == 1
    assert "Tech\\_\\%" in _params(stmt).values()


def test_alumni_secondary_industry_escapes_wildcards():
    stmt = build_alumni_query(secondary_industry="Tech_%")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert "Tech\\_\\%" in _params(stmt).values()


def test_alumni_employment_status_escapes_wildcards():
    stmt = build_alumni_query(employment_status="Full_time%")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert "Full\\_time\\%" in _params(stmt).values()


def test_alumni_q_column_set_unchanged():
    # Normalization must not change WHICH alumni-row columns are searched. The
    # free-text q still reaches 8 of them: first, last, preferred, birth (maiden
    # #216), middle, byu_id, net_id, other_designations (#404) -- plus, since
    # #620, the employment record (covered in tests/test_alumni_search.py).
    sql = _sql(build_alumni_query(q="smith"))
    for column in (
        "first_name",
        "last_name",
        "preferred_first_name",
        "birth_name",
        "middle_name",
        "byu_id",
        "net_id",
        "other_designations",
    ):
        assert re.search(rf"alumni_search_norm\(alumni_\d+\.{column}\)", sql)
    assert "alumni_program_engagement" not in sql


# --- audit query builder -----------------------------------------------------


def test_audit_user_email_escapes_wildcards():
    stmt = build_audit_query(user="a%_b")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert any(p == "%a\\%\\_b%" for p in _params(stmt).values())
