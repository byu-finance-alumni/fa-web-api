"""Pydantic schema for the state -> region crosswalk endpoint (#283).

Read-only reference data. The payload is derived from
:mod:`app.services.state_regions` — the same module the write path uses to derive
``contact.region`` from ``career.current_state`` — so the client that renders the
form and the server that persists the record cannot disagree about a region.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StateRegionMap(BaseModel):
    """The 50-states + DC -> region crosswalk, plus the valid region list.

    ``region_by_state`` is keyed by the canonical FULL state name (matching
    :data:`app.core.us_states.STATE_NAME_BY_CODE`'s values), because that is the
    form the state values are normalized to before region is derived. Clients
    holding a 2-letter code should expand it to the full name before looking up.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "regions": [
                    "Northeast",
                    "Southeast",
                    "Midwest",
                    "Southwest",
                    "West",
                    "Mountain West",
                ],
                "region_by_state": {
                    "Connecticut": "Northeast",
                    "Florida": "Southeast",
                    "Ohio": "Midwest",
                    "Texas": "Southwest",
                    "California": "West",
                    "Utah": "Mountain West",
                },
            }
        }
    )

    regions: list[str] = Field(
        description=(
            "The valid regions, in display order — the full option set for a "
            "Region dropdown."
        )
    )
    region_by_state: dict[str, str] = Field(
        description=(
            "Canonical full state name -> region, for all 50 states + DC. Every "
            "value is one of ``regions``."
        )
    )
