"""Response schemas for the Alumni Geography dashboard endpoints.

These mirror exactly what ``app/services/geography.py`` returns (the services
build plain dicts; FastAPI validates them against these models on the way out).
They exist so the geography contract is part of the OpenAPI schema — previously
these routes returned bare ``dict`` / ``list[dict]``, so the frontend could only
hand-write the shapes and nothing caught drift. See fa-web-app #99 / #88.

Read-only. Nullability matches the service output (e.g. a row's city/employer
can be null when unknown).
"""

from __future__ import annotations

from pydantic import BaseModel

# --- shared count rows -------------------------------------------------------
# The service labels each top-N row by its dimension, so the key name differs
# per list (employer / industry / city / year). Small explicit models keep the
# OpenAPI contract faithful to that.


class EmployerCount(BaseModel):
    employer: str
    count: int


class IndustryCount(BaseModel):
    industry: str
    count: int


class CityCount(BaseModel):
    city: str
    count: int


class YearCount(BaseModel):
    year: int
    count: int


class TopCity(BaseModel):
    city: str
    state: str
    count: int


# --- /geography/states -------------------------------------------------------


class StateCount(BaseModel):
    state: str
    state_name: str
    alumni_count: int


# --- /geography/counties -----------------------------------------------------


class CountyCount(BaseModel):
    """Per-county alumni count for the national county choropleth.

    ``county_fips`` is the 5-digit FIPS code (matching the us-atlas county ids
    the map renders)."""

    county_fips: str
    count: int


# --- /geography/countries ----------------------------------------------------


class CountryCount(BaseModel):
    """Per-country alumni count for the world map (international alumni only)."""

    country: str
    alumni_count: int


# --- /geography/summary ------------------------------------------------------


class GeoOptions(BaseModel):
    """Filter-dropdown option lists (capped server-side)."""

    employers: list[str]
    cities: list[str]
    industries: list[str]
    graduation_years: list[int]
    regions: list[str]
    tags: list[str]


class GeoSummary(BaseModel):
    total_alumni: int
    states_represented: int
    cities_represented: int
    top_employer: EmployerCount | None
    top_employers: list[EmployerCount]
    top_industries: list[IndustryCount]
    top_cities: list[TopCity]
    largest_hub: TopCity | None
    options: GeoOptions


# --- /geography/states/{state} ----------------------------------------------


class StateDetail(BaseModel):
    state: str
    state_name: str
    alumni_count: int
    cities: list[CityCount]
    employers: list[EmployerCount]
    industries: list[IndustryCount]
    by_graduation_year: list[YearCount]


# --- /geography/states/{state}/alumni ---------------------------------------


class GeoAlumniRow(BaseModel):
    alumni_id: int
    name: str
    city: str | None
    graduation_year: int | None
    current_employer: str | None
    current_title: str | None


class GeoAlumniPage(BaseModel):
    items: list[GeoAlumniRow]
    total: int
    limit: int
    offset: int


# --- /geography/radius (proximity search) -----------------------------------


class RadiusAlumniRow(BaseModel):
    alumni_id: int
    name: str
    city: str | None
    state: str | None
    graduation_year: int | None
    current_employer: str | None
    current_title: str | None
    distance_miles: float


class RadiusPage(BaseModel):
    items: list[RadiusAlumniRow]
    total: int
    limit: int
    offset: int
    center_lat: float
    center_lng: float
    radius_miles: float


# --- /geography/breakdown ----------------------------------------------------


class BreakdownItem(BaseModel):
    key: str
    label: str
    sublabel: str | None
    count: int


class Breakdown(BaseModel):
    dimension: str
    title: str
    items: list[BreakdownItem]


# --- /geography/cities -------------------------------------------------------


class CityAlumniRow(BaseModel):
    alumni_id: int
    name: str
    graduation_year: int | None
    current_employer: str | None


class CityDetail(BaseModel):
    state: str
    state_name: str
    city: str
    alumni_count: int
    employers: list[EmployerCount]
    industries: list[IndustryCount]
    by_graduation_year: list[YearCount]
    alumni: list[CityAlumniRow]
