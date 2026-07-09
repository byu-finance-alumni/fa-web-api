"""Tests for the CSV ⇄ DB schema drift guard (#146).

The positive test is the guard itself: on the current tree every CSV header must
map to a real DB column. The negative tests prove the guard actually catches
drift rather than passing vacuously.
"""

from scripts import check_csv_schema_sync as guard


def test_all_csv_headers_map_to_real_columns():
    """The live check: no drift between the CSV surfaces and the schema."""
    errors = guard.collect_errors()
    assert errors == [], "CSV/DB drift:\n" + "\n".join(errors)


def test_friend_import_surface_is_covered():
    """The friend template↔model binding is one of the guarded surfaces (#294)."""
    assert guard._check_friend_import in guard.CHECKS
    errors: list[str] = []
    guard._check_friend_import(errors)
    assert errors == [], "Friend CSV/DB drift:\n" + "\n".join(errors)


def test_friend_headers_are_subset_without_academic_fields():
    """Friend columns are a strict subset of alumni columns and exclude the
    alumni-only academic / identity fields."""
    from app.services import import_csv

    friend = set(import_csv.FRIEND_EXPECTED_HEADERS)
    assert friend
    assert friend <= set(import_csv.EXPECTED_HEADERS)
    for banned in (
        "BYU ID (9 digits)",
        "Net ID",
        "Graduation Year",
        "Graduation Semester",
        "Finance program admitted year",
        "Degree year",
    ):
        assert banned not in friend


def test_check_column_flags_missing_column():
    errors: list[str] = []
    guard._check_column(errors, "surface", "Bogus", "core", "no_such_column")
    assert len(errors) == 1
    assert "no_such_column" in errors[0]


def test_check_column_flags_unknown_section():
    errors: list[str] = []
    guard._check_column(errors, "surface", "Bogus", "not_a_section", "field")
    assert len(errors) == 1
    assert "unknown section" in errors[0]


def test_check_column_accepts_real_column():
    errors: list[str] = []
    guard._check_column(errors, "surface", "Net ID", "core", "net_id")
    assert errors == []


def test_header_set_mismatch_is_flagged_both_ways():
    errors: list[str] = []
    guard._check_header_sets(
        errors, "surface", declared={"A", "B"}, bound={"B", "C"}
    )
    # "A" is declared but unbound; "C" is bound but not declared.
    joined = "\n".join(errors)
    assert "'A'" in joined
    assert "'C'" in joined
