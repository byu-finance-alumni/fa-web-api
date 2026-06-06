"""Geographic aggregation for the Alumni Geography dashboard.

Location comes from ``alumni_contact_info`` (city/state/country/region); employer,
title, and industry from ``current_employment``; tags via ``alumni_tags``. Every
metric is computed in PostgreSQL with ``COUNT(DISTINCT alumni_id)`` (so a stray
duplicate contact/employment row can't inflate a count) and the same filter set
is applied uniformly. Nothing loads the full alumni set into memory.
"""

from __future__ import annotations

from sqlalchemy import String, and_, cast, desc, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.tags import AlumniTag, Tag

# US state / territory abbreviation → display name.
STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Map common full-name / mixed-case state values to the 2-letter code, so data
# entered either way ("UT" or "Utah") aggregates together.
_NAME_TO_CODE = {name.lower(): code for code, name in STATE_NAMES.items()}
_CODES = set(STATE_NAMES)


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.upper() in _CODES:
        return v.upper()
    return _NAME_TO_CODE.get(v.lower())


# A normalized 2-letter state expression usable in SQL GROUP BY: upper-cased,
# trimmed. (Full-name normalization is best-effort in Python on read.)
_STATE = func.upper(func.trim(cast(AlumniContactInfo.state, String)))
_ALUMNI = func.count(func.distinct(Alumni.alumni_id))


def _filter_conditions(filters: dict) -> list:
    """Shared WHERE conditions for every geography query."""
    conds = [
        Alumni.archived.is_(False),
        AlumniContactInfo.state.is_not(None),
        func.trim(cast(AlumniContactInfo.state, String)) != "",
    ]
    if filters.get("employer"):
        conds.append(CurrentEmployment.current_employer == filters["employer"])
    if filters.get("industry"):
        conds.append(CurrentEmployment.current_industry == filters["industry"])
    if filters.get("year"):
        conds.append(Alumni.graduation_year == filters["year"])
    if filters.get("region"):
        conds.append(AlumniContactInfo.region == filters["region"])
    if filters.get("tag"):
        conds.append(
            exists(
                select(AlumniTag.alumni_tag_id)
                .join(Tag, Tag.tag_id == AlumniTag.tag_id)
                .where(
                    AlumniTag.alumni_id == Alumni.alumni_id,
                    Tag.tag_name == filters["tag"],
                )
            )
        )
    return conds


def _base():
    """Alumni joined to their location (inner) and current employment (outer)."""
    return (
        select(Alumni.alumni_id)
        .select_from(Alumni)
        .join(
            AlumniContactInfo,
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
        )
        .outerjoin(
            CurrentEmployment,
            CurrentEmployment.alumni_id == Alumni.alumni_id,
        )
    )


def _full_name(a: Alumni) -> str:
    name = " ".join(
        p
        for p in (a.preferred_first_name or a.first_name, a.last_name)
        if p
    ).strip()
    return name or f"Alumni #{a.alumni_id}"


async def get_states(session: AsyncSession, filters: dict) -> list[dict]:
    rows = (
        await session.execute(
            _base()
            .with_only_columns(_STATE.label("state"), _ALUMNI.label("count"))
            .where(*_filter_conditions(filters))
            .group_by(_STATE)
            .order_by(desc("count"))
        )
    ).all()
    # Fold any full-name spellings into their 2-letter code, then re-sort.
    out: dict[str, int] = {}
    for raw, count in rows:
        code = normalize_state(raw)
        if code:
            out[code] = out.get(code, 0) + int(count)
    return sorted(
        (
            {"state": c, "state_name": STATE_NAMES.get(c, c), "alumni_count": n}
            for c, n in out.items()
        ),
        key=lambda r: r["alumni_count"],
        reverse=True,
    )


async def _top(session, filters, column, label, *, extra=None, limit=10):
    """Generic top-N GROUP BY over the filtered, located alumni set."""
    conds = list(_filter_conditions(filters))
    if extra is not None:
        conds.append(extra)
    rows = (
        await session.execute(
            _base()
            .with_only_columns(column.label("key"), _ALUMNI.label("count"))
            .where(*conds, column.is_not(None))
            .group_by(column)
            .order_by(desc("count"))
            .limit(limit)
        )
    ).all()
    return [{label: k, "count": int(c)} for k, c in rows]


async def get_state_detail(session, state: str, filters: dict) -> dict:
    code = normalize_state(state)
    if code is None:
        return {
            "state": state,
            "state_name": STATE_NAMES.get(state.upper(), state),
            "alumni_count": 0,
            "cities": [],
            "employers": [],
            "industries": [],
            "by_graduation_year": [],
        }
    state_match = _STATE == code
    total = await session.scalar(
        _base()
        .with_only_columns(_ALUMNI)
        .where(*_filter_conditions(filters), state_match)
    )
    cities = await _top(
        session, filters, AlumniContactInfo.city, "city", extra=state_match
    )
    employers = await _top(
        session, filters, CurrentEmployment.current_employer, "employer",
        extra=state_match,
    )
    industries = await _top(
        session, filters, CurrentEmployment.current_industry, "industry",
        extra=state_match,
    )
    year_rows = (
        await session.execute(
            _base()
            .with_only_columns(
                Alumni.graduation_year.label("year"), _ALUMNI.label("count")
            )
            .where(
                *_filter_conditions(filters),
                state_match,
                Alumni.graduation_year.is_not(None),
            )
            .group_by(Alumni.graduation_year)
            .order_by(Alumni.graduation_year)
        )
    ).all()
    return {
        "state": code,
        "state_name": STATE_NAMES.get(code, code),
        "alumni_count": int(total or 0),
        "cities": cities,
        "employers": employers,
        "industries": industries,
        "by_graduation_year": [
            {"year": y, "count": int(c)} for y, c in year_rows
        ],
    }


_SORTS = {
    "name": (Alumni.last_name, Alumni.first_name),
    "year": (desc(Alumni.graduation_year), Alumni.last_name),
    "city": (AlumniContactInfo.city, Alumni.last_name),
}


async def get_state_alumni(
    session, state: str, filters: dict, *, limit: int, offset: int, sort: str
) -> dict:
    code = normalize_state(state)
    if code is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    conds = [*_filter_conditions(filters), _STATE == code]
    total = await session.scalar(
        _base().with_only_columns(_ALUMNI).where(*conds)
    )
    order = _SORTS.get(sort, _SORTS["name"])
    rows = (
        await session.execute(
            select(
                Alumni,
                AlumniContactInfo.city,
                CurrentEmployment.current_employer,
                CurrentEmployment.current_title,
            )
            .select_from(Alumni)
            .join(
                AlumniContactInfo,
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
            )
            .outerjoin(
                CurrentEmployment,
                CurrentEmployment.alumni_id == Alumni.alumni_id,
            )
            .where(*conds)
            .order_by(*order)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "items": [
            {
                "alumni_id": a.alumni_id,
                "name": _full_name(a),
                "city": city,
                "graduation_year": a.graduation_year,
                "current_employer": employer,
                "current_title": title,
            }
            for a, city, employer, title in rows
        ],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


async def get_city_detail(session, state: str, city: str, filters: dict) -> dict:
    code = normalize_state(state)
    conds = [
        *_filter_conditions(filters),
        AlumniContactInfo.city == city,
    ]
    if code is not None:
        conds.append(_STATE == code)
    total = await session.scalar(
        _base().with_only_columns(_ALUMNI).where(*conds)
    )
    city_match = AlumniContactInfo.city == city
    state_match = (_STATE == code) if code is not None else None
    extra = and_(city_match, state_match) if state_match is not None else city_match

    employers = await _top(
        session, filters, CurrentEmployment.current_employer, "employer",
        extra=extra,
    )
    industries = await _top(
        session, filters, CurrentEmployment.current_industry, "industry",
        extra=extra,
    )
    year_rows = (
        await session.execute(
            _base()
            .with_only_columns(
                Alumni.graduation_year.label("year"), _ALUMNI.label("count")
            )
            .where(*conds, Alumni.graduation_year.is_not(None))
            .group_by(Alumni.graduation_year)
            .order_by(Alumni.graduation_year)
        )
    ).all()
    alumni = (
        await session.execute(
            select(Alumni, CurrentEmployment.current_employer)
            .select_from(Alumni)
            .join(
                AlumniContactInfo,
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
            )
            .outerjoin(
                CurrentEmployment,
                CurrentEmployment.alumni_id == Alumni.alumni_id,
            )
            .where(*conds)
            .order_by(Alumni.last_name, Alumni.first_name)
            .limit(50)
        )
    ).all()
    return {
        "state": code or state,
        "state_name": STATE_NAMES.get(code or "", state),
        "city": city,
        "alumni_count": int(total or 0),
        "employers": employers,
        "industries": industries,
        "by_graduation_year": [
            {"year": y, "count": int(c)} for y, c in year_rows
        ],
        "alumni": [
            {
                "alumni_id": a.alumni_id,
                "name": _full_name(a),
                "graduation_year": a.graduation_year,
                "current_employer": employer,
            }
            for a, employer in alumni
        ],
    }


async def _distinct(session, column) -> list:
    rows = (
        await session.execute(
            select(column)
            .select_from(Alumni)
            .join(
                AlumniContactInfo,
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
            )
            .outerjoin(
                CurrentEmployment,
                CurrentEmployment.alumni_id == Alumni.alumni_id,
            )
            .where(Alumni.archived.is_(False), column.is_not(None))
            .distinct()
            .order_by(column)
        )
    ).all()
    return [r[0] for r in rows]


_BREAKDOWN_TITLES = {
    "states": "States",
    "cities": "Cities",
    "employers": "Employers",
    "industries": "Industries",
}


async def get_breakdown(session, dimension: str, filters: dict) -> dict:
    """Full ranked list for one dimension (no top-N cap) — backs the 'View all'
    breakdown table. ``key`` is what a row links to on the map."""
    title = _BREAKDOWN_TITLES.get(dimension)
    if title is None:
        return {"dimension": dimension, "title": dimension, "items": []}

    if dimension == "states":
        items = [
            {
                "key": s["state"],
                "label": s["state_name"],
                "sublabel": s["state"],
                "count": s["alumni_count"],
            }
            for s in await get_states(session, filters)
        ]
    elif dimension == "cities":
        rows = (
            await session.execute(
                _base()
                .with_only_columns(
                    AlumniContactInfo.city.label("city"),
                    _STATE.label("state"),
                    _ALUMNI.label("count"),
                )
                .where(*_filter_conditions(filters), AlumniContactInfo.city.is_not(None))
                .group_by(AlumniContactInfo.city, _STATE)
                .order_by(desc("count"))
            )
        ).all()
        items = [
            {
                "key": normalize_state(st) or st,
                "label": city,
                "sublabel": normalize_state(st) or st,
                "count": int(n),
            }
            for city, st, n in rows
        ]
    else:
        column = (
            CurrentEmployment.current_employer
            if dimension == "employers"
            else CurrentEmployment.current_industry
        )
        rows = await _top(session, filters, column, "label", limit=1000)
        items = [
            {"key": r["label"], "label": r["label"], "sublabel": None, "count": r["count"]}
            for r in rows
        ]

    return {"dimension": dimension, "title": title, "items": items}


async def get_summary(session, filters: dict) -> dict:
    conds = _filter_conditions(filters)
    total = await session.scalar(_base().with_only_columns(_ALUMNI).where(*conds))
    states_count = await session.scalar(
        select(func.count(func.distinct(_STATE)))
        .select_from(Alumni)
        .join(AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id)
        .outerjoin(
            CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id
        )
        .where(*conds)
    )
    cities_count = await session.scalar(
        select(func.count(func.distinct(AlumniContactInfo.city)))
        .select_from(Alumni)
        .join(AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id)
        .outerjoin(
            CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id
        )
        .where(*conds, AlumniContactInfo.city.is_not(None))
    )
    top_employers = await _top(
        session, filters, CurrentEmployment.current_employer, "employer", limit=8
    )
    top_industries = await _top(
        session, filters, CurrentEmployment.current_industry, "industry", limit=8
    )
    city_rows = (
        await session.execute(
            _base()
            .with_only_columns(
                AlumniContactInfo.city.label("city"),
                _STATE.label("state"),
                _ALUMNI.label("count"),
            )
            .where(*conds, AlumniContactInfo.city.is_not(None))
            .group_by(AlumniContactInfo.city, _STATE)
            .order_by(desc("count"))
            .limit(8)
        )
    ).all()
    top_cities = [
        {
            "city": c,
            "state": normalize_state(st) or st,
            "count": int(n),
        }
        for c, st, n in city_rows
    ]

    return {
        "total_alumni": int(total or 0),
        "states_represented": int(states_count or 0),
        "cities_represented": int(cities_count or 0),
        "top_employer": top_employers[0] if top_employers else None,
        "top_employers": top_employers,
        "top_industries": top_industries,
        "top_cities": top_cities,
        "largest_hub": top_cities[0] if top_cities else None,
        "options": {
            "employers": await _distinct(
                session, CurrentEmployment.current_employer
            ),
            "industries": await _distinct(
                session, CurrentEmployment.current_industry
            ),
            "graduation_years": await _distinct(session, Alumni.graduation_year),
            "regions": await _distinct(session, AlumniContactInfo.region),
            "tags": [
                r[0]
                for r in (
                    await session.execute(
                        select(Tag.tag_name).order_by(Tag.tag_name)
                    )
                ).all()
            ],
        },
    }
