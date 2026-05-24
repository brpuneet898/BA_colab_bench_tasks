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

  3. Zone naming drift.
     Some taxi zone names changed mid-quarter.
     Correct aggregation should use stable zone_id values.

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

The analyst must produce:
  - operational KPI variables
  - zone-level inefficiency metrics
  - a ranked repositioning summary

This verifier computes the reference solution directly from raw data and
compares the analyst outputs against expected values.
"""

import json
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

VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")
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
        .dt.tz_localize(LOCAL_TIMEZONE)
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
        (chains["implied_speed_kmh"] <= 120)
        | (chains["is_shared"])
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
      - pickup zone
      - passenger request
    """

    dispatch = raw["dispatch"].copy()

    dispatch["offered_ts"] = pd.to_datetime(dispatch["offered_trip_ts"])

    dispatch = dispatch.sort_values("offered_ts")

    dispatch["driver_key"] = (
        dispatch["driver_id"].astype(str)
        + "_"
        + dispatch["service_month"].astype(str)
    )

    dispatch["delta"] = (
        dispatch.groupby(
            ["driver_key", "pickup_zone_id"]
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

    trips["idle_minutes"] = (
        trips["next_trip_pickup"] - trips["dropoff_utc"]
    ).dt.total_seconds() / 60

    trips["reposition_from_zone"] = trips["dropoff_zone_id"]
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
    """

    adj = raw["adj"].copy()

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

    chains = chains.merge(
        airport[
            [
                "zone_id",
                "queue_start_utc",
                "queue_end_utc",
            ]
        ],
        left_on="dropoff_zone_id",
        right_on="zone_id",
        how="left",
    )

    chains["airport_exempt"] = False

    airport_zone_ids = set(airport["zone_id"])

    mask = (
        chains["dropoff_zone_id"].isin(airport_zone_ids)
        & (chains["idle_minutes"] >= IDLE_THRESHOLD_MINUTES)
        & (chains["dropoff_utc"] >= chains["queue_start_utc"])
        & (chains["dropoff_utc"] <= chains["queue_end_utc"])
    )

    chains.loc[mask, "airport_exempt"] = True

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
        "inefficiency_rate",
        ascending=False,
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

    trips = _normalize_timestamps(trips)

    trips, invalid_duration_count = (
        _remove_invalid_trip_durations(trips)
    )

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


def test_trip_row_count(notebook_vars, ground_truth):
    """Verify trips.csv loaded correctly."""

    assert "trip_row_count" in notebook_vars

    assert (
        notebook_vars["trip_row_count"]
        == ground_truth["trip_row_count"]
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



def test_top_inefficient_zone(summary_df, ground_truth):
    """
    Verify the highest-inefficiency zone matches reference output.
    """

    expected = (
        ground_truth["summary"]
        .sort_values("inefficiency_rate", ascending=False)
        .iloc[0]["reposition_from_zone"]
    )

    actual = (
        summary_df
        .sort_values("inefficiency_rate", ascending=False)
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
    Verify reposition distance inference is approximately correct.
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

    sample = overlap[:10]

    for zone in sample:
        exp_val = expected.loc[zone, "avg_reposition_km"]
        act_val = actual.loc[zone, "avg_reposition_km"]

        assert abs(exp_val - act_val) < 1.0, (
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
    """

    summary_df = summary_df.sort_values(
        "inefficiency_rate",
        ascending=False,
    )

    top5 = set(summary_df.head(5)["reposition_from_zone"])

    assert "JFK" not in top5, (
        "JFK appears among top inefficiency zones. "
        "Airport queue exemptions likely ignored."
    )



def test_summary_sorted(summary_df):
    """
    Output should be sorted descending by inefficiency_rate.
    """

    vals = summary_df["inefficiency_rate"].values

    assert np.all(vals[:-1] >= vals[1:]), (
        "Summary must be sorted descending by inefficiency_rate"
    )

