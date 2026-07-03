"""CSV ⇄ DB schema drift guard (#146).

Every CSV the site imports or exports has columns that must line up with real
database columns. If the schema changes but a template/parser doesn't (or vice
versa), imports start silently rejecting rows or writing to the wrong field and
nobody notices until a user hits it in production.

This check asserts, for each CSV surface, that:

  * every template/importer header is bound to a *real* column on the SQLAlchemy
    model that backs it (the models mirror ``database/schema.sql`` — they are
    what the app actually reads/writes), and
  * the set of bound headers exactly matches the surface's declared header list
    (so adding a header without wiring it — or wiring a header that no longer
    exists — fails here, not in production), and
  * any controlled-vocabulary column a CSV constrains resolves to a non-empty
    canonical source.

Covered surfaces (extend ``CHECKS`` for future ones):

  * Alumni  — import ``app.services.import_csv`` + export ``app.services.alumni_export``
  * Events  — import ``app.services.import_events``
  * Donations — import ``app.services.import_donations``

Run it standalone (exit 0 = in sync, exit 1 = drift):

    .venv/Scripts/python -m scripts.check_csv_schema_sync

It's importable too: :func:`collect_errors` returns the list of drift messages
(empty when everything is in sync), which the pytest wrapper asserts on.
"""

from __future__ import annotations

import sys

from app.core.dropdowns import INDUSTRIES, validate_industry
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.donation import Donation
from app.models.employment import CurrentEmployment, EducationHistory
from app.models.engagement import AlumniProgramEngagement

# Section keys used by the importers/exporter -> the model whose table columns
# they must resolve against. ``import_csv`` uses "core"; ``alumni_export`` uses
# "alumni"; both mean the top-level ``alumni`` row.
SECTION_MODEL = {
    "core": Alumni,
    "alumni": Alumni,
    "contact": AlumniContactInfo,
    "career": CurrentEmployment,
    "education": EducationHistory,
    "engagement": AlumniProgramEngagement,
}

# Fields an importer payload carries that are intentionally NOT a direct column
# on the section's table (resolved to something else before the DB write). Keyed
# by (section, field) so a genuine typo in any other field still fails.
PAYLOAD_ONLY = {
    # "Spouse BYU ID" is looked up and stored as ``spouse_alumni_id`` — which IS
    # a real Alumni column, so it is validated normally; nothing to exempt yet.
}


def _columns(model) -> set[str]:
    """Real column names on a model's table (mirrors schema.sql)."""
    return set(model.__table__.columns.keys())


def _check_column(errors: list[str], surface: str, header: str, section: str, field: str) -> None:
    model = SECTION_MODEL.get(section)
    if model is None:
        errors.append(
            f"[{surface}] header {header!r} maps to unknown section {section!r} "
            f"(known: {', '.join(sorted(set(SECTION_MODEL)))})."
        )
        return
    if (section, field) in PAYLOAD_ONLY:
        return
    if field not in _columns(model):
        errors.append(
            f"[{surface}] header {header!r} maps to {model.__name__}.{field}, "
            f"which is not a column on that table — schema/CSV drift."
        )


def _check_header_sets(
    errors: list[str], surface: str, declared: set[str], bound: set[str]
) -> None:
    for missing in sorted(declared - bound):
        errors.append(
            f"[{surface}] template header {missing!r} has no DB binding — "
            f"add it to the header→column map or remove it from the template."
        )
    for extra in sorted(bound - declared):
        errors.append(
            f"[{surface}] header {extra!r} is bound to a column but is not in the "
            f"template's expected headers — the template and parser have drifted."
        )


def _check_alumni_import(errors: list[str]) -> None:
    from app.services import import_csv

    surface = "alumni import (import_csv)"
    mapping = import_csv._MAPPING
    _check_header_sets(
        errors, surface, set(import_csv.EXPECTED_HEADERS), set(mapping)
    )
    for header, (section, field, kind) in mapping.items():
        _check_column(errors, surface, header, section, field)
        if kind == "industry" and not INDUSTRIES:
            errors.append(
                f"[{surface}] header {header!r} constrains to the industry vocab, "
                f"but INDUSTRIES is empty — the canonical source is missing."
            )


def _check_alumni_export(errors: list[str]) -> None:
    from app.services import alumni_export

    surface = "alumni export (alumni_export)"
    for col in alumni_export.CATALOG:
        _check_column(errors, surface, col.label, col.source, col.attr)


def _check_events_import(errors: list[str]) -> None:
    from app.services import import_events as ie

    surface = "events import (import_events)"
    # One CSV = one event's roster (#149): the event identity is entered in the
    # wizard, so the CSV columns are just the attendee's Net ID (the match key)
    # and Name (confirmation only, never persisted).
    bindings: dict[str, tuple[object, str] | None] = {
        ie.COL_NET_ID: (Alumni, "net_id"),  # attendee match key
        ie.COL_NAME: None,  # confirmation only
    }
    _check_header_sets(errors, surface, set(ie.EXPECTED_HEADERS), set(bindings))
    for header, target in bindings.items():
        if target is None:
            continue
        model, column = target
        if column not in _columns(model):
            errors.append(
                f"[{surface}] header {header!r} maps to {model.__name__}.{column}, "
                f"which is not a column on that table — schema/CSV drift."
            )


def _check_donations_import(errors: list[str]) -> None:
    from app.services import import_donations as idn

    surface = "donations import (import_donations)"
    bindings: dict[str, tuple[object, str] | None] = {
        idn.COL_MSTID: (Alumni, "mst_id"),  # primary donor match key
        idn.COL_FIRST: (Alumni, "first_name"),  # name-fallback match key
        idn.COL_LAST: (Alumni, "last_name"),  # name-fallback match key
        idn.COL_MONTH: (Donation, "donation_month"),
        idn.COL_YEAR: (Donation, "donation_year"),
        idn.COL_AMOUNT: (Donation, "amount"),
    }
    _check_header_sets(errors, surface, set(idn.EXPECTED_HEADERS), set(bindings))
    for header, target in bindings.items():
        if target is None:
            continue
        model, column = target
        if column not in _columns(model):
            errors.append(
                f"[{surface}] header {header!r} maps to {model.__name__}.{column}, "
                f"which is not a column on that table — schema/CSV drift."
            )


def _check_industry_vocab(errors: list[str]) -> None:
    """The one vocab the CSV path validates: every canonical industry must pass
    the validator that the importer runs each cell through."""
    for value in INDUSTRIES:
        try:
            validate_industry(value)
        except ValueError:
            errors.append(
                f"[vocab] industry {value!r} is in INDUSTRIES but validate_industry "
                f"rejects it — the vocab and its validator have drifted."
            )


CHECKS = (
    _check_alumni_import,
    _check_alumni_export,
    _check_events_import,
    _check_donations_import,
    _check_industry_vocab,
)


def collect_errors() -> list[str]:
    errors: list[str] = []
    for check in CHECKS:
        check(errors)
    return errors


def main() -> int:
    errors = collect_errors()
    if errors:
        print("CSV ⇄ DB schema drift detected:\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nFix the template/parser or the schema so every CSV column maps to "
            "a real DB column.",
            file=sys.stderr,
        )
        return 1
    print("OK: all CSV import/export headers map to real DB columns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
