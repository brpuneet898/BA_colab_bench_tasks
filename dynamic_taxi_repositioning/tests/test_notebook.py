"""
Unit tests for the Dynamic Taxi Repositioning Efficiency benchmark.

This benchmark evaluates whether the analyst correctly reconstructs
operational taxi-driver repositioning behavior from imperfect trip and
dispatch telemetry.

The benchmark intentionally contains several realistic operational data
issues that must be discovered and corrected:

  1. Driver IDs are NOT globally unique.
     Driver identifiers reset monthly after a fleet migration.
     Correct uniqueness key:
         (driver_id, service_month)

  2. Timestamp mismatch.
     pickup_datetime is stored in America/New_York local time.
     dropoff_datetime is stored in UTC.
     Correct repositioning chains require timezone normalization.

  3. Voided trip entries.
     Some trips carry negative fare amounts representing billing reversals.
     These are not real completed trips and must be excluded before reconstruction.

  4. Duplicate dispatch retries.
     Dispatch retries generate duplicate rows within 15 seconds.
     These should be deduplicated.

  5. Airport queue exemptions.
     Long idle windows at JFK/LGA are operationally valid.
     These should not be classified as reposition inefficiency.

  6. Repositioning distance is implicit.
     Deadhead movement must be inferred from:
         previous_dropoff_zone -> next_pickup_zone
     using zone_adjacency.csv.

  7. Shared rides overlap operationally.
     Some overlapping trips are legitimate pooled rides.
     These should not be discarded as corrupted telemetry.

  8. GPS dropout zones.
     A small number of trips have no recorded dropoff zone.
     Chains from those trips must appear in the summary under zone code 0.
     A naive groupby silently drops NaN keys, causing zone 0 to vanish.

  9. NaN service_month dispatch events.
     A small number of dispatch events have no recorded service_month.
     These must be deduplicated as their own independent group.
     pandas groupby() silently drops NaN keys by default (dropna=True),
     causing all NaN-service_month events to appear non-duplicate.

  10. Phantom duplicate zone pairs in zone_adjacency.csv.
      Some zone pairs appear more than once with inflated wrong distances due to
      secondary data source ingestion.  A dict-based distance lookup retains the
      last (inflated) value, overstating avg_reposition_km for affected zones.
      A naive left merge creates duplicate chain rows with mixed distances, also
      inflating avg_reposition_km.  Correct solution: drop_duplicates on join keys
      before merging, retaining the first (correct) occurrence.

The analyst must produce:
  - operational KPI variables
  - zone-level inefficiency metrics
  - a ranked repositioning summary

This verifier computes the reference solution directly from raw data and
compares the analyst outputs against expected values.
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ============================================================
# Paths
# ============================================================

# --- ROBUST PATH RESOLUTION ---
if Path("/workspace/data").exists():
    DATA_DIR = Path("/workspace/data")
    WORKSPACE = Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR = Path("../environment/data")
    WORKSPACE = Path("..")
elif Path("environment/data").exists():
    DATA_DIR = Path("environment/data")
    WORKSPACE = Path(".")
else:
    DATA_DIR = Path("data")
    WORKSPACE = Path(".")

VARIABLES_PATH = Path(
    os.environ.get("VARIABLES_PATH", "/logs/verifier/notebook_variables.json")
)
SUMMARY_PATH = WORKSPACE / "zone_repositioning_summary.csv"

TRIPS_PATH = DATA_DIR / "trips.csv"
DRIVERS_PATH = DATA_DIR / "drivers.csv"
ZONES_PATH = DATA_DIR / "zones.csv"
DISPATCH_PATH = DATA_DIR / "dispatch_events.csv"
SHARED_PATH = DATA_DIR / "shared_rides.csv"
AIRPORT_QUEUE_PATH = DATA_DIR / "airport_queue_periods.csv"
ADJ_PATH = DATA_DIR / "zone_adjacency.csv"


# ============================================================
# Constants
# ============================================================

AIRPORT_ZONES = {"JFK", "LGA"}

IDLE_THRESHOLD_MINUTES = 30
DUPLICATE_WINDOW_SECONDS = 15

LOCAL_TIMEZONE = "America/New_York"


# ============================================================
# Raw Loading
# ============================================================


def _load_raw():
    """Load all benchmark source files."""

    return {
        "trips": pd.read_csv(TRIPS_PATH),
        "drivers": pd.read_csv(DRIVERS_PATH),
        "zones": pd.read_csv(ZONES_PATH),
        "dispatch": pd.read_csv(DISPATCH_PATH),
        "shared": pd.read_csv(SHARED_PATH),
        "airport": pd.read_csv(AIRPORT_QUEUE_PATH),
        "adj": pd.read_csv(ADJ_PATH),
    }


# ============================================================
# Trap 1 — Driver Identity Resolution
# ============================================================


def _build_driver_key(raw):
    """
    Correctly reconstruct globally unique driver identifiers.

    Driver IDs reset monthly.

    Correct unique entity:
        driver_id + service_month
    """

    trips = raw["trips"].copy()

    trips["driver_key"] = (
        trips["driver_id"].astype(str)
        + "_"
        + trips["service_month"].astype(str)
    )

    return trips


# ============================================================
# Trap 3 — Fare Reversal Filtering
# ============================================================


def _remove_cancelled_trips(trips):
    """Remove billing reversal entries before reconstruction."""

    trips = trips.copy()

    cancelled_count = int(
        (trips["fare_amount"] < 0).sum()
    )

    trips = trips[trips["fare_amount"] >= 0].copy()

    return trips, cancelled_count


# ============================================================
# Trap 2 — Timestamp Normalization
# ============================================================


def _normalize_timestamps(trips):
    """
    Normalize pickup/dropoff timestamps.

    pickup_datetime:
        stored in local NYC time

    dropoff_datetime:
        stored in UTC
    """

    trips = trips.copy()

    trips["pickup_local"] = pd.to_datetime(trips["pickup_datetime"])

    trips["pickup_utc"] = (
        trips["pickup_local"]
        .dt.tz_localize(LOCAL_TIMEZONE, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert("UTC")
    )

    trips["dropoff_utc"] = pd.to_datetime(
        trips["dropoff_datetime"], utc=True
    )

    trips["trip_duration_minutes"] = (
        trips["dropoff_utc"] - trips["pickup_utc"]
    ).dt.total_seconds() / 60

    return trips

# ============================================================
# Additional Operational Cleaning
# ============================================================


def _remove_invalid_trip_durations(trips):
    """
    Remove impossible negative-duration trips after timezone normalization.
    """

    trips = trips.copy()

    invalid_count = int(
        (trips["trip_duration_minutes"] < 0).sum()
    )

    trips = trips[
        trips["trip_duration_minutes"] >= 0
    ].copy()

    return trips, invalid_count


def _build_sessions(trips):
    """
    Build operational driver sessions.

    Large temporal gaps should not create reposition chains.
    """

    trips = trips.sort_values([
        "driver_key",
        "pickup_utc",
    ])

    trips["prev_dropoff"] = (
        trips.groupby("driver_key")["dropoff_utc"]
        .shift(1)
    )

    trips["session_gap_hours"] = (
        trips["pickup_utc"] - trips["prev_dropoff"]
    ).dt.total_seconds() / 3600

    trips["new_session"] = (
        trips["session_gap_hours"].isna()
        | (trips["session_gap_hours"] > 4)
    )

    trips["session_id"] = (
        trips.groupby("driver_key")["new_session"]
        .cumsum()
    )

    return trips


def _remove_impossible_repositions(chains):
    """
    Remove physically impossible reposition chains unless
    associated with shared rides.

    Two cases are excluded:
      1. Overlapping trips (idle_minutes < 0): a non-shared driver cannot
         pick up a new passenger before dropping off the previous one.
      2. Speed > 120 km/h: physically impossible repositioning speed.
    """

    chains = chains.copy()

    chains["reposition_hours"] = (
        chains["idle_minutes"] / 60
    )

    chains["implied_speed_kmh"] = np.where(
        chains["reposition_hours"] > 0,
        chains["distance_km"] / chains["reposition_hours"],
        0,
    )

    chains = chains[
        (
            (chains["idle_minutes"] >= 0)
            & (chains["implied_speed_kmh"] <= 120)
        )
        | (chains["is_shared"])
        | (chains["next_is_shared"])
    ].copy()

    return chains


# ============================================================
# Trap 3 — Zone Name Drift
# ============================================================


def _canonicalize_zones(raw):
    """
    Normalize zone naming drift.

    Analysts should aggregate using stable zone_id values.
    """

    zones = raw["zones"].copy()

    zones = zones.drop_duplicates(subset=["zone_id"])

    return zones


# ============================================================
# Trap 4 — Dispatch Deduplication
# ============================================================


def _deduplicate_dispatches(raw):
    """
    Deduplicate dispatch retries.

    Duplicate dispatches occur within 15 seconds for the same:
      - driver
      - service_month (including NaN — treated as its own independent group)
      - pickup zone

    dropna=False is required so that events with no recorded service_month
    are deduplicated within their own NaN group rather than silently dropped.
    """

    dispatch = raw["dispatch"].copy()

    dispatch["offered_ts"] = pd.to_datetime(dispatch["offered_trip_ts"])

    dispatch = dispatch.sort_values("offered_ts")

    dispatch["delta"] = (
        dispatch.groupby(
            ["driver_id", "service_month", "pickup_zone_id"],
            dropna=False,
        )["offered_ts"]
        .diff()
        .dt.total_seconds()
    )

    dedup = dispatch[
        (dispatch["delta"].isna())
        | (dispatch["delta"] > DUPLICATE_WINDOW_SECONDS)
    ].copy()

    return dedup


# ============================================================
# Trap 5 — Shared Ride Handling
# ============================================================


def _valid_trip_rows(raw, trips):
    """
    Keep overlapping trips that are legitimate shared rides.
    """

    shared = raw["shared"]

    valid_shared_ids = set(shared["trip_id"])

    trips = trips.copy()

    trips["is_shared"] = trips["trip_id"].isin(valid_shared_ids)

    return trips


# ============================================================
# Trip Sequencing
# ============================================================


def _build_trip_chains(trips):
    """
    Build operational trip sequences within sessions.
    """

    trips = trips.sort_values(["driver_key", "pickup_utc"])

    trips["next_trip_pickup"] = (
        trips.groupby(
            ["driver_key", "session_id"]
        )["pickup_utc"].shift(-1)
    )

    trips["next_pickup_zone"] = (
        trips.groupby(
            ["driver_key", "session_id"]
        )["pickup_zone_id"].shift(-1)
    )

    trips["next_is_shared"] = (
        trips.groupby(
            ["driver_key", "session_id"]
        )["is_shared"].shift(-1).fillna(False)
    )

    trips["idle_minutes"] = (
        trips["next_trip_pickup"] - trips["dropoff_utc"]
    ).dt.total_seconds() / 60

    # Trips with missing dropoff_zone_id represent GPS dropouts.
    # Assign them to zone code 0 per the instruction's convention.
    trips["reposition_from_zone"] = (
        trips["dropoff_zone_id"].fillna(0).astype(int)
    )
    trips["reposition_to_zone"] = trips["next_pickup_zone"]

    trips = trips[
        trips["next_trip_pickup"].notna()
    ].copy()

    return trips


# ============================================================
# Trap 6 — Repositioning Distance Inference
# ============================================================


def _attach_reposition_distance(raw, chains):
    """
    Infer deadhead reposition distance from adjacency table.

    zone_adjacency.csv may contain duplicate (from_zone_id, to_zone_id) pairs
    from multiple data source ingestion runs.  The reference table must be
    deduplicated on its join keys before the merge; a non-unique right-hand
    table creates phantom duplicate chain rows that inflate KPI aggregates.
    """

    adj = raw["adj"].copy()

    # Deduplicate adjacency table before merge to prevent phantom duplicate rows
    adj = adj.drop_duplicates(subset=["from_zone_id", "to_zone_id"])

    merged = chains.merge(
        adj,
        left_on=[
            "reposition_from_zone",
            "reposition_to_zone",
        ],
        right_on=[
            "from_zone_id",
            "to_zone_id",
        ],
        how="left",
    )

    merged["distance_km"] = merged["distance_km"].fillna(0)

    return merged


# ============================================================
# Trap 7 — Airport Queue Exemptions
# ============================================================


def _apply_airport_exemptions(raw, chains):
    """
    Long airport idle windows during valid queue periods
    should not count as inefficiency.
    """

    airport = raw["airport"].copy()

    airport["queue_start_utc"] = pd.to_datetime(
        airport["queue_start"],
        utc=True,
    )

    airport["queue_end_utc"] = pd.to_datetime(
        airport["queue_end"],
        utc=True,
    )

    chains = chains.copy()

    airport_zone_ids = set(airport["zone_id"])

    # Compute exemptions on a side table to avoid duplicating chain rows
    # (left merge would create N rows per chain for each matching queue period)
    airport_check = chains[
        chains["dropoff_zone_id"].isin(airport_zone_ids)
    ][[
        "trip_id",
        "dropoff_zone_id",
        "dropoff_utc",
        "idle_minutes",
    ]].merge(
        airport[["zone_id", "queue_start_utc", "queue_end_utc"]],
        left_on="dropoff_zone_id",
        right_on="zone_id",
    )

    exempt_ids = set(
        airport_check.loc[
            (airport_check["dropoff_utc"] >= airport_check["queue_start_utc"])
            & (airport_check["dropoff_utc"] <= airport_check["queue_end_utc"]),
            "trip_id",
        ]
    )

    chains["airport_exempt"] = chains["trip_id"].isin(exempt_ids)

    return chains


# ============================================================
# KPI Computation
# ============================================================


def _compute_kpis(chains):
    """
    Compute operational repositioning KPIs.
    """

    chains = chains.copy()

    chains["inefficient_idle"] = (
        (chains["idle_minutes"] >= IDLE_THRESHOLD_MINUTES)
        & (~chains["airport_exempt"])
    )

    zone_summary = (
        chains.groupby("reposition_from_zone")
        .agg(
            total_trips=("trip_id", "count"),
            avg_idle_minutes=("idle_minutes", "mean"),
            avg_reposition_km=("distance_km", "mean"),
            inefficient_repositions=("inefficient_idle", "sum"),
        )
        .reset_index()
    )

    zone_summary["inefficiency_rate"] = (
        zone_summary["inefficient_repositions"]
        / zone_summary["total_trips"]
    )

    zone_summary = zone_summary.sort_values(
        ["inefficiency_rate", "total_trips"],
        ascending=[False, False],
    )

    return zone_summary


# ============================================================
# Ground Truth Fixture
# ============================================================


@pytest.fixture(scope="module")
def ground_truth():
    """Compute reference benchmark outputs."""

    raw = _load_raw()

    trips = _build_driver_key(raw)

    # Normalize timestamps before any filtering so negative_duration_trip_count
    # is computed from the full dataset — order-independent of fare filtering.
    trips = _normalize_timestamps(trips)

    invalid_duration_count = int(
        (trips["trip_duration_minutes"] < 0).sum()
    )

    trips, cancelled_count = _remove_cancelled_trips(trips)

    trips = trips[trips["trip_duration_minutes"] >= 0].copy()

    trips = _valid_trip_rows(raw, trips)

    dispatch = _deduplicate_dispatches(raw)

    trips = _build_sessions(trips)

    chains = _build_trip_chains(trips)

    chains = _attach_reposition_distance(raw, chains)

    chains = _remove_impossible_repositions(chains)

    chains = _apply_airport_exemptions(raw, chains)

    summary = _compute_kpis(chains)

    return {
        "trip_row_count": len(raw["trips"]),
        "cancelled_trip_count": cancelled_count,
        "deduplicated_dispatch_count": len(dispatch),
        "negative_duration_trip_count": invalid_duration_count,
        "airport_exemption_count": int(
            chains["airport_exempt"].sum()
        ),
        "summary": summary,
    }


# ============================================================
# Notebook Variables Fixture
# ============================================================


@pytest.fixture(scope="module")
def notebook_vars():
    """Load notebook-scoped variables."""

    if not VARIABLES_PATH.exists():
        pytest.skip("notebook_variables.json not found")

    with open(VARIABLES_PATH) as f:
        return json.load(f)


# ============================================================
# Summary Fixture
# ============================================================


@pytest.fixture(scope="module")
def summary_df():
    """Load analyst summary CSV."""

    if not SUMMARY_PATH.exists():
        pytest.fail(
            f"zone_repositioning_summary.csv not found at {SUMMARY_PATH}"
        )

    return pd.read_csv(SUMMARY_PATH)


# ============================================================
# Variable Tests
# ============================================================


def test_cancelled_trip_count(notebook_vars, ground_truth):
    """
    Verify voided and reversed trip entries were identified and excluded.

    trips.csv contains negative fare_amount records representing
    billing reversals. These are not real operational trips and must
    be removed before session and chain reconstruction.
    """

    assert "cancelled_trip_count" in notebook_vars, (
        "cancelled_trip_count not defined. "
        "Check fare validation step in the reconstruction pipeline."
    )

    expected = ground_truth["cancelled_trip_count"]
    actual = notebook_vars["cancelled_trip_count"]

    assert actual == expected, (
        f"Expected cancelled_trip_count={expected}, got {actual}. "
        "Fare reversal filtering may be missing or applied incorrectly."
    )



def test_deduplicated_dispatch_count(notebook_vars, ground_truth):
    """
    Verify duplicate dispatch retries were removed.
    """

    assert "deduplicated_dispatch_count" in notebook_vars

    expected = ground_truth["deduplicated_dispatch_count"]
    actual = notebook_vars["deduplicated_dispatch_count"]

    assert actual == expected, (
        f"Expected deduplicated_dispatch_count={expected}, got {actual}. "
        "Check dispatch retry deduplication logic."
    )



def test_negative_duration_trip_count(notebook_vars, ground_truth):
    """
    Verify timezone normalization fixed impossible trip durations.
    """

    assert "negative_duration_trip_count" in notebook_vars

    expected = ground_truth["negative_duration_trip_count"]
    actual = notebook_vars["negative_duration_trip_count"]

    assert actual == expected, (
        f"Expected negative_duration_trip_count={expected}, got {actual}. "
        "Timezone normalization likely incorrect."
    )



def test_airport_exemption_count(notebook_vars, ground_truth):
    """
    Verify airport queue exemptions were correctly identified.
    """

    assert "airport_exemption_count" in notebook_vars

    expected = ground_truth["airport_exemption_count"]
    actual = notebook_vars["airport_exemption_count"]

    assert actual == expected, (
        f"Expected airport_exemption_count={expected}, got {actual}."
    )


# ============================================================
# Summary CSV Structure Tests
# ============================================================



def test_summary_file_exists():
    """Verify output CSV exists."""

    assert SUMMARY_PATH.exists(), (
        f"Missing output file: {SUMMARY_PATH}"
    )



def test_summary_columns(summary_df):
    """Verify required columns exist."""

    required = [
        "reposition_from_zone",
        "total_trips",
        "avg_idle_minutes",
        "avg_reposition_km",
        "inefficient_repositions",
        "inefficiency_rate",
    ]

    for col in required:
        assert col in summary_df.columns, (
            f"Missing required column: {col}"
        )



def test_summary_not_empty(summary_df):
    """Ensure summary contains operational rows."""

    assert len(summary_df) > 10


# ============================================================
# Summary Value Tests
# ============================================================



def test_unknown_zone_assignment(summary_df):
    """
    Verify that repositioning chains with missing origin zones are
    assigned to zone code 0 in the summary.

    Some trips lack a recorded dropoff zone (GPS dropout).  The instruction
    specifies that chains originating from such trips must appear in the
    output under zone code 0 rather than being silently omitted.
    """

    assert 0 in summary_df["reposition_from_zone"].values, (
        "Zone 0 not found in zone_repositioning_summary.csv. "
        "Chains with missing dropoff zones must be assigned to zone code 0."
    )



def test_top_inefficient_zone(summary_df, ground_truth):
    """
    Verify the highest-inefficiency zone matches reference output.
    """

    expected = (
        ground_truth["summary"]
        .sort_values(
            ["inefficiency_rate", "total_trips"],
            ascending=[False, False],
        )
        .iloc[0]["reposition_from_zone"]
    )

    actual = (
        summary_df
        .sort_values(
            ["inefficiency_rate", "total_trips"],
            ascending=[False, False],
        )
        .iloc[0]["reposition_from_zone"]
    )

    assert actual == expected, (
        f"Expected top inefficient zone {expected}, got {actual}."
    )



def test_avg_idle_minutes(summary_df, ground_truth):
    """
    Verify average idle times are approximately correct.
    """

    expected = (
        ground_truth["summary"]
        .set_index("reposition_from_zone")
    )

    actual = (
        summary_df
        .set_index("reposition_from_zone")
    )

    overlap = expected.index.intersection(actual.index)

    for zone in overlap:
        exp_val = expected.loc[zone, "avg_idle_minutes"]
        act_val = actual.loc[zone, "avg_idle_minutes"]

        assert abs(exp_val - act_val) < 2.5, (
            f"avg_idle_minutes mismatch for zone {zone}: "
            f"expected ~{exp_val:.2f}, got {act_val:.2f}"
        )



def test_inefficiency_rate(summary_df, ground_truth):
    """
    Validate inefficiency rates.
    """

    expected = (
        ground_truth["summary"]
        .set_index("reposition_from_zone")
    )

    actual = (
        summary_df
        .set_index("reposition_from_zone")
    )

    overlap = expected.index.intersection(actual.index)

    for zone in overlap:
        exp_val = expected.loc[zone, "inefficiency_rate"]
        act_val = actual.loc[zone, "inefficiency_rate"]

        assert abs(exp_val - act_val) < 0.03, (
            f"inefficiency_rate mismatch for zone {zone}: "
            f"expected ~{exp_val:.4f}, got {act_val:.4f}"
        )



def test_reposition_distance_logic(summary_df, ground_truth):
    """
    Verify reposition distance inference is correct across all zones.

    The zone_adjacency reference table must be deduplicated on
    (from_zone_id, to_zone_id) before joining.  Duplicate entries
    caused by secondary data source ingestion carry inflated distance
    values; failing to remove them causes avg_reposition_km to be
    overstated for affected zones.
    """

    expected = (
        ground_truth["summary"]
        .set_index("reposition_from_zone")
    )

    actual = (
        summary_df
        .set_index("reposition_from_zone")
    )

    overlap = expected.index.intersection(actual.index)

    for zone in overlap:
        exp_val = expected.loc[zone, "avg_reposition_km"]
        act_val = actual.loc[zone, "avg_reposition_km"]

        assert abs(exp_val - act_val) < 0.5, (
            f"avg_reposition_km mismatch for zone {zone}: "
            f"expected ~{exp_val:.2f}, got {act_val:.2f}"
        )



def test_no_negative_idle_times(summary_df):
    """
    Negative idle times indicate sequencing or timezone bugs.
    """

    assert (
        summary_df["avg_idle_minutes"] >= 0
    ).all(), (
        "Negative idle times detected. "
        "Check trip sequencing and timezone normalization."
    )



def test_airport_zones_not_extreme(summary_df):
    """
    Airport queue exemptions should prevent airports from appearing
    as extreme inefficiency outliers.

    JFK = zone_id 132, LGA = zone_id 138.
    """

    summary_df = summary_df.sort_values(
        "inefficiency_rate",
        ascending=False,
    )

    top5 = set(summary_df.head(5)["reposition_from_zone"])

    AIRPORT_ZONE_IDS = {132, 138}  # JFK=132, LGA=138

    assert not (AIRPORT_ZONE_IDS & top5), (
        f"Airport zone(s) {AIRPORT_ZONE_IDS & top5} appear among top "
        "inefficiency zones. Airport queue exemptions likely ignored."
    )



def test_summary_sorted(summary_df):
    """
    Output should be sorted descending by inefficiency_rate.
    """

    vals = summary_df["inefficiency_rate"].values

    assert np.all(vals[:-1] >= vals[1:]), (
        "Summary must be sorted descending by inefficiency_rate"
    )

