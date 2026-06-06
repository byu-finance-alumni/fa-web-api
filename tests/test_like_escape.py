"""Tests for LIKE/ILIKE wildcard-injection hardening.

``escape_like`` is unit-tested directly, and every query builder that embeds a
user-supplied term in an ``.ilike(...)`` is compiled to Postgres SQL to assert
the term's metacharacters are escaped and an ``ESCAPE '\\'`` clause is emitted.
No database needed (mirrors ``test_alumni_search`` / ``test_audit_search``).
"""

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


def test_alumni_q_escapes_wildcards_and_emits_escape_clause():
    stmt = build_alumni_query(q="50%_admin")
    sql = _sql(stmt)
    assert ESCAPE_CLAUSE in sql
    # The bound search term carries escaped metacharacters, not raw wildcards.
    assert any(p == "%50\\%\\_admin%" for p in _params(stmt).values())


def test_alumni_employer_escapes_wildcards():
    stmt = build_alumni_query(employer="A_B%")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert "A\\_B\\%" in _params(stmt).values()


def test_alumni_industry_escapes_wildcards():
    stmt = build_alumni_query(industry="Tech_%")
    sql = _sql(stmt)
    assert sql.count(ESCAPE_CLAUSE) == 2  # primary + secondary industry
    assert "Tech\\_\\%" in _params(stmt).values()


def test_alumni_ilike_count_unchanged():
    # Escaping must not change which columns are searched.
    assert _sql(build_alumni_query(q="smith")).count("ILIKE") == 6


# --- audit query builder -----------------------------------------------------


def test_audit_user_email_escapes_wildcards():
    stmt = build_audit_query(user="a%_b")
    assert ESCAPE_CLAUSE in _sql(stmt)
    assert any(p == "%a\\%\\_b%" for p in _params(stmt).values())
