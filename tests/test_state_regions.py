"""State -> region map + the employment-state-driven region derivation (#283).

Two layers, both pure (no session, no DATABASE_URL):
  * ``app.services.state_regions`` — the 50-states + DC map and its lookup.
  * ``hygiene.derive_region`` / ``clean_alumni_payload`` — the auto-fill rules
    (only on change, overridable, never clears, US-only).
"""

from app.core.us_states import STATE_NAME_BY_CODE
from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull
from app.services import hygiene, state_regions

# --- The map -----------------------------------------------------------------


def test_map_covers_all_50_states_plus_dc_exactly_once():
    mapped = [s for states in state_regions.STATES_BY_REGION.values() for s in states]
    assert len(mapped) == 51  # 50 states + DC
    assert len(set(mapped)) == 51  # no state in two regions
    # Every canonical name from the us_states crosswalk is mapped, and nothing
    # that isn't a real state has snuck in.
    assert set(mapped) == set(STATE_NAME_BY_CODE.values())


def test_only_the_six_valid_regions_exist_in_display_order():
    assert set(state_regions.STATES_BY_REGION) == set(state_regions.REGIONS)
    # Order is the Region dropdown's option order (served by
    # GET /vocabulary/state-regions): Mountain West is APPENDED, not sorted in.
    assert state_regions.REGIONS == (
        "Northeast",
        "Southeast",
        "Midwest",
        "Southwest",
        "West",
        "Mountain West",
    )


def test_mountain_states_are_their_own_region_not_west():
    # This is a BYU database: the Mountain states are ~1/3 of all alumni, so
    # folding them into West buried them with California (stakeholder, 7/16).
    assert state_regions.STATES_BY_REGION["Mountain West"] == (
        "Colorado",
        "Idaho",
        "Montana",
        "Nevada",
        "Utah",
        "Wyoming",
    )
    for state in ("Utah", "Colorado", "Idaho", "Montana", "Wyoming", "Nevada"):
        assert state_regions.region_for_state(state) == "Mountain West"


def test_west_is_the_pacific_coast_plus_alaska_and_hawaii():
    assert state_regions.STATES_BY_REGION["West"] == (
        "Alaska",
        "California",
        "Hawaii",
        "Oregon",
        "Washington",
    )


def test_delaware_maryland_and_dc_stay_in_the_southeast():
    # Confirmed by the stakeholder; a recurring "shouldn't these be Northeast?"
    # question, so it is pinned rather than left to the map's reader.
    for state in ("Delaware", "Maryland", "District of Columbia"):
        assert state_regions.region_for_state(state) == "Southeast"


def test_region_for_state_accepts_full_name_code_and_any_casing():
    assert state_regions.region_for_state("Utah") == "Mountain West"
    assert state_regions.region_for_state("utah") == "Mountain West"
    assert state_regions.region_for_state("UTAH") == "Mountain West"
    assert state_regions.region_for_state("UT") == "Mountain West"
    assert state_regions.region_for_state("ut") == "Mountain West"
    assert state_regions.region_for_state("  Utah  ") == "Mountain West"


def test_seed_mock_data_locations_agree_with_the_map():
    # Dev seed data must not contradict the map the write path enforces.
    from scripts.seed_mock_data import _LOCATIONS

    for city, state, region in _LOCATIONS:
        assert state_regions.region_for_state(state) == region, city


def test_region_for_state_spot_checks_each_region():
    assert state_regions.region_for_state("New York") == "Northeast"
    assert state_regions.region_for_state("Georgia") == "Southeast"
    assert state_regions.region_for_state("District of Columbia") == "Southeast"
    assert state_regions.region_for_state("Illinois") == "Midwest"
    assert state_regions.region_for_state("Texas") == "Southwest"
    assert state_regions.region_for_state("California") == "West"


def test_region_for_state_returns_none_for_blank_or_non_us():
    assert state_regions.region_for_state(None) is None
    assert state_regions.region_for_state("") is None
    assert state_regions.region_for_state("   ") is None
    assert state_regions.region_for_state("Ontario") is None
    assert state_regions.region_for_state("London") is None


# --- Derivation on write -----------------------------------------------------


def test_region_derived_from_employment_state_on_update():
    # An update derives only against a KNOWN stored state (here: the record had
    # none), so the caller must say what it is replacing — see
    # test_update_without_a_known_stored_state_never_derives.
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "TX"}), stored_state=None
    )
    assert cleaned["contact"]["region"] == "Southwest"
    assert {
        "section": "contact",
        "field": "region",
        "label": "Region",
        "before": None,
        "after": "Southwest",
    } in changes


def test_region_keys_off_the_normalized_full_state_name():
    # The cleaner normalizes "ny" -> "New York" first; the map must see that.
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "  ny  "}), stored_state=None
    )
    assert cleaned["career"]["current_state"] == "New York"
    assert cleaned["contact"]["region"] == "Northeast"


def test_employment_state_wins_over_residence_state():
    # Lives in Utah, works in New York -> region follows the WORK state (#283).
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            contact={"state": "Utah", "city": "Provo"},
            career={"current_state": "New York"},
        ),
        stored_state=None,
    )
    assert cleaned["contact"]["state"] == "Utah"
    assert cleaned["contact"]["region"] == "Northeast"


def test_residence_state_alone_never_derives_a_region():
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(contact={"state": "Utah"}), stored_state=None
    )
    assert "region" not in cleaned["contact"]
    assert not any(c["field"] == "region" for c in changes)


def test_explicit_region_is_not_clobbered():
    # The escape hatch: a hand-entered region beats the map. stored_state makes
    # this a genuine state CHANGE, so the map would otherwise fire.
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            contact={"region": "Northeast"},
            career={"current_state": "Texas"},
        ),
        stored_state="Utah",
    )
    assert cleaned["contact"]["region"] == "Northeast"
    assert not any(c["field"] == "region" for c in changes)


def test_explicit_null_region_is_an_intentional_clear():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            contact={"region": None},
            career={"current_state": "Texas"},
        ),
        stored_state="Utah",
    )
    assert cleaned["contact"]["region"] is None


def test_untouched_employment_state_leaves_region_alone():
    # An edit to some other career field must not move the region.
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_employer": "Goldman Sachs"}),
        stored_state="Utah",
    )
    assert "region" not in cleaned.get("contact", {})
    assert not any(c["field"] == "region" for c in changes)


def test_blank_employment_state_leaves_region_alone():
    # CLEARING the work state is a change, but a blank state resolves to no
    # region — so the stored region stays rather than being blanked with it.
    for blank in (None, "", "   "):
        cleaned, _ = hygiene.clean_alumni_payload(
            AlumniUpdateFull(career={"current_state": blank}), stored_state="Texas"
        )
        assert "region" not in cleaned.get("contact", {})


def test_non_us_country_leaves_region_alone():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            career={"current_state": "Texas", "current_country": "Canada"}
        ),
        stored_state="Utah",
    )
    assert "region" not in cleaned.get("contact", {})


def test_us_country_spellings_still_derive():
    for country in ("USA", "usa", "US", "United States", "united states of america"):
        cleaned, _ = hygiene.clean_alumni_payload(
            AlumniUpdateFull(
                career={"current_state": "Texas", "current_country": country}
            ),
            stored_state=None,
        )
        assert cleaned["contact"]["region"] == "Southwest", country


# --- Derivation fires only when the state actually CHANGED (#283) ------------


def test_unchanged_state_derives_nothing():
    # Tanya's override has to survive an unrelated save: re-submitting the state
    # a record already has is not a move, so the region must not be recomputed.
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "Texas"}), stored_state="Texas"
    )
    assert "region" not in cleaned.get("contact", {})
    assert not any(c["field"] == "region" for c in changes)


def test_state_code_against_a_stored_full_name_is_not_a_change():
    # "TX" vs a stored "Texas" is the same state — both sides normalize first, so
    # this must not read as a change and re-derive.
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "tx"}), stored_state="Texas"
    )
    assert "region" not in cleaned.get("contact", {})


def test_changed_state_derives_the_new_region():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "Texas"}), stored_state="Utah"
    )
    assert cleaned["contact"]["region"] == "Southwest"


def test_explicit_region_still_wins_on_a_changed_state():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            career={"current_state": "Texas"}, contact={"region": "West"}
        ),
        stored_state="Utah",
    )
    assert cleaned["contact"]["region"] == "West"


def test_update_without_a_known_stored_state_never_derives():
    # An update whose caller can't say what's stored must not guess "supplied
    # means changed" — that would make /preview promise a region change the write
    # path (which does know) declines to make.
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "Texas"})
    )
    assert "region" not in cleaned.get("contact", {})
    assert not any(c["field"] == "region" for c in changes)


def test_create_still_derives_without_a_stored_state():
    # A create has nothing stored by definition, so "supplied" is the right
    # trigger and the default must keep working.
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniCreateFull(last_name="Doe", career={"current_state": "Texas"})
    )
    assert cleaned["contact"]["region"] == "Southwest"


def test_unrecognized_state_leaves_region_alone():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniUpdateFull(career={"current_state": "Ontario"}), stored_state="Utah"
    )
    assert "region" not in cleaned.get("contact", {})


def test_region_derived_on_create():
    cleaned, _ = hygiene.clean_alumni_payload(
        AlumniCreateFull(
            last_name="Doe",
            contact={"city": "Chicago", "state": "Illinois"},
            career={"current_employer": "Citadel", "current_state": "IL"},
        )
    )
    assert cleaned["contact"]["region"] == "Midwest"


def test_derivation_is_idempotent():
    # Re-saving already-derived data reports no further change (the region is
    # now explicitly present, so the "don't clobber" rule short-circuits it).
    payload = AlumniCreateFull(
        last_name="Doe",
        contact={"region": "Southwest"},
        career={"current_state": "Texas"},
    )
    _, changes = hygiene.clean_alumni_payload(payload)
    assert changes == []


def test_derive_region_helper_is_directly_callable():
    assert hygiene.derive_region({"career": {"current_state": "Utah"}}) == "Mountain West"
    assert hygiene.derive_region({"career": {}}) is None
    assert hygiene.derive_region({}) is None


def test_derive_region_helper_honors_the_stored_state():
    cleaned = {"career": {"current_state": "Utah"}}
    # Unchanged -> nothing derived; changed -> the new region.
    assert hygiene.derive_region(cleaned, stored_state="Utah") is None
    assert hygiene.derive_region(cleaned, stored_state="UT") is None
    assert hygiene.derive_region(cleaned, stored_state="Texas") == "Mountain West"
    # None means "the record has no work state", which a real state differs from.
    assert hygiene.derive_region(cleaned, stored_state=None) == "Mountain West"
