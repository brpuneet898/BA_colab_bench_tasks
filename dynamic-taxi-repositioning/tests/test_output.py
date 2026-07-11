"""
Tests for the Dynamic Taxi Repositioning Efficiency Analysis benchmark.

Expected outputs in /workspace/:

  zone_repositioning_summary.csv  — one row per origin zone, sorted by
                                    inefficiency_rate descending, ties broken
                                    by total_trips descending.

  summary.json                    — five integer counts from the
                                    reconstruction pipeline.
"""

import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _resolve_paths():
    candidates = [
        (Path("/workspace/data"),                                      Path("/workspace")),
        (Path(__file__).parent.parent / "environment" / "data",       Path(__file__).parent.parent),
    ]
    for data, ws in candidates:
        if (data / "trips.csv").exists():
            return data, ws
    return candidates[-1]

DATA_DIR, WORKSPACE = _resolve_paths()

SUMMARY_PATH = WORKSPACE / "zone_repositioning_summary.csv"
COUNTS_PATH  = WORKSPACE / "summary.json"

LOCAL_TIMEZONE         = "America/New_York"
IDLE_THRESHOLD_MINUTES = 30
DUPLICATE_WINDOW_SEC   = 15


# ---------------------------------------------------------------------------
# Anti-cheat sentinel
# ---------------------------------------------------------------------------

def test_input_data_not_tampered():
    """Verify source files have not been modified by the agent."""
    trips    = pd.read_csv(DATA_DIR / "trips.csv")
    dispatch = pd.read_csv(DATA_DIR / "dispatch_events.csv")
    adj      = pd.read_csv(DATA_DIR / "zone_adjacency.csv")

    assert len(trips) == 300_000, (
        f"trips.csv row count changed (expected 300000, got {len(trips)})."
    )
    assert len(dispatch) == 300_550, (
        f"dispatch_events.csv row count changed (expected 300550, got {len(dispatch)})."
    )
    assert len(adj) == 7_687, (
        f"zone_adjacency.csv row count changed (expected 7687, got {len(adj)})."
    )
    dup_pairs = int(adj.duplicated(subset=["from_zone_id", "to_zone_id"]).sum())
    assert dup_pairs == 48, (
        f"zone_adjacency.csv data integrity check failed (expected 48, got {dup_pairs})."
    )
    nan_dispatch = int(dispatch["service_month"].isna().sum())
    assert nan_dispatch == 50, (
        f"dispatch_events.csv data integrity check failed (expected 50, got {nan_dispatch})."
    )


# ---------------------------------------------------------------------------
# Reference pipeline (ground truth)
# ---------------------------------------------------------------------------

def _load_raw():
    return {
        "trips":    pd.read_csv(DATA_DIR / "trips.csv"),
        "dispatch": pd.read_csv(DATA_DIR / "dispatch_events.csv"),
        "shared":   pd.read_csv(DATA_DIR / "shared_rides.csv"),
        "airport":  pd.read_csv(DATA_DIR / "airport_queue_periods.csv"),
        "adj":      pd.read_csv(DATA_DIR / "zone_adjacency.csv"),
    }


def _normalize_trips(trips):
    trips = trips.copy()
    trips["driver_key"] = (
        trips["driver_id"].astype(str) + "_" + trips["service_month"].astype(str)
    )
    trips["pickup_local"] = pd.to_datetime(trips["pickup_datetime"])
    trips["pickup_utc"] = (
        trips["pickup_local"]
        .dt.tz_localize(LOCAL_TIMEZONE, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert("UTC")
    )
    trips["dropoff_utc"] = pd.to_datetime(trips["dropoff_datetime"], utc=True)
    trips["trip_duration_minutes"] = (
        trips["dropoff_utc"] - trips["pickup_utc"]
    ).dt.total_seconds() / 60
    return trips


def _deduplicate_dispatches(raw):
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
    return dispatch[
        dispatch["delta"].isna() | (dispatch["delta"] > DUPLICATE_WINDOW_SEC)
    ]


def _build_chains(trips, shared_ids):
    trips = trips.copy()
    trips["is_shared"] = trips["trip_id"].isin(shared_ids)
    trips = trips.sort_values(["driver_key", "pickup_utc"])

    trips["prev_dropoff"] = trips.groupby("driver_key")["dropoff_utc"].shift(1)
    trips["session_gap_hours"] = (
        trips["pickup_utc"] - trips["prev_dropoff"]
    ).dt.total_seconds() / 3600
    trips["new_session"] = trips["session_gap_hours"].isna() | (
        trips["session_gap_hours"] > 4
    )
    trips["session_id"] = trips.groupby("driver_key")["new_session"].cumsum()

    trips["next_trip_pickup"] = (
        trips.groupby(["driver_key", "session_id"])["pickup_utc"].shift(-1)
    )
    trips["next_pickup_zone"] = (
        trips.groupby(["driver_key", "session_id"])["pickup_zone_id"].shift(-1)
    )
    trips["next_is_shared"] = (
        trips.groupby(["driver_key", "session_id"])["is_shared"]
        .shift(-1).fillna(False)
    )
    trips["idle_minutes"] = (
        trips["next_trip_pickup"] - trips["dropoff_utc"]
    ).dt.total_seconds() / 60
    trips["reposition_from_zone"] = trips["dropoff_zone_id"].fillna(0).astype(int)
    trips["reposition_to_zone"]   = trips["next_pickup_zone"]
    return trips[trips["next_trip_pickup"].notna()].copy()


def _attach_reposition_distance(raw, chains):
    adj = raw["adj"].groupby(["from_zone_id", "to_zone_id"], as_index=False)["distance_km"].min()
    chains = chains.merge(
        adj,
        left_on=["reposition_from_zone", "reposition_to_zone"],
        right_on=["from_zone_id", "to_zone_id"],
        how="left",
    )
    chains["distance_km"] = chains["distance_km"].fillna(0)
    return chains


def _remove_impossible_repositions(chains):
    chains = chains.copy()
    chains["implied_speed_kmh"] = np.where(
        chains["idle_minutes"] > 0,
        chains["distance_km"] / (chains["idle_minutes"] / 60),
        0,
    )
    return chains[
        ((chains["idle_minutes"] >= 0) & (chains["implied_speed_kmh"] <= 120))
        | chains["is_shared"]
        | chains["next_is_shared"]
    ].copy()


def _apply_airport_exemptions(raw, chains):
    airport = raw["airport"].copy()
    airport["queue_start_utc"] = pd.to_datetime(airport["queue_start"], utc=True)
    airport["queue_end_utc"]   = pd.to_datetime(airport["queue_end"],   utc=True)
    airport_zone_ids = set(airport["zone_id"])

    check = chains[chains["dropoff_zone_id"].isin(airport_zone_ids)][
        ["trip_id", "dropoff_zone_id", "dropoff_utc"]
    ].merge(
        airport[["zone_id", "queue_start_utc", "queue_end_utc"]],
        left_on="dropoff_zone_id", right_on="zone_id",
    )
    exempt_ids = set(
        check.loc[
            (check["dropoff_utc"] >= check["queue_start_utc"])
            & (check["dropoff_utc"] <= check["queue_end_utc"]),
            "trip_id",
        ]
    )
    chains = chains.copy()
    chains["airport_exempt"] = chains["trip_id"].isin(exempt_ids)
    return chains


def _compute_kpis(chains):
    chains = chains.copy()
    chains["inefficient_idle"] = (
        (chains["idle_minutes"] >= IDLE_THRESHOLD_MINUTES)
        & (~chains["airport_exempt"])
    )
    summary = (
        chains.groupby("reposition_from_zone")
        .agg(
            total_trips             = ("trip_id",         "count"),
            avg_idle_minutes        = ("idle_minutes",     "mean"),
            avg_reposition_km       = ("distance_km",      "mean"),
            inefficient_repositions = ("inefficient_idle", "sum"),
        )
        .reset_index()
    )
    summary["inefficiency_rate"] = (
        summary["inefficient_repositions"] / summary["total_trips"]
    )
    return summary.sort_values(
        ["inefficiency_rate", "total_trips"], ascending=[False, False]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ground_truth():
    raw   = _load_raw()
    trips = _normalize_trips(raw["trips"])

    negative_duration_count = int((trips["trip_duration_minutes"] < 0).sum())
    cancelled_count         = int((trips["fare_amount"] < 0).sum())

    trips = trips[
        (trips["fare_amount"] >= 0) & (trips["trip_duration_minutes"] >= 0)
    ].copy()

    dispatch = _deduplicate_dispatches(raw)
    shared_ids = set(raw["shared"]["trip_id"])

    chains = _build_chains(trips, shared_ids)
    chains = _attach_reposition_distance(raw, chains)
    chains = _remove_impossible_repositions(chains)
    chains = _apply_airport_exemptions(raw, chains)

    return {
        "trip_row_count":               len(raw["trips"]),
        "cancelled_trip_count":         cancelled_count,
        "negative_duration_trip_count": negative_duration_count,
        "deduplicated_dispatch_count":  len(dispatch),
        "airport_exemption_count":      int(chains["airport_exempt"].sum()),
        "summary":                      _compute_kpis(chains),
    }


@pytest.fixture(scope="module")
def summary_json():
    if not COUNTS_PATH.exists():
        pytest.fail(f"summary.json not found at {COUNTS_PATH}")
    with open(COUNTS_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def summary_df():
    if not SUMMARY_PATH.exists():
        pytest.fail(f"zone_repositioning_summary.csv not found at {SUMMARY_PATH}")
    return pd.read_csv(SUMMARY_PATH)


# ---------------------------------------------------------------------------
# Count tests (from summary.json)
# ---------------------------------------------------------------------------

def test_pipeline_counts(summary_json, ground_truth):
    """Billing reversals and timezone-induced invalid durations must be correctly counted."""
    for key in ("cancelled_trip_count", "negative_duration_trip_count"):
        assert key in summary_json, f"{key} missing from summary.json."
    assert summary_json["cancelled_trip_count"] == ground_truth["cancelled_trip_count"], (
        f"cancelled_trip_count: expected {ground_truth['cancelled_trip_count']}, "
        f"got {summary_json['cancelled_trip_count']}. "
        "Fare reversal filtering may be missing or applied incorrectly."
    )
    assert summary_json["negative_duration_trip_count"] == ground_truth["negative_duration_trip_count"], (
        f"negative_duration_trip_count: expected {ground_truth['negative_duration_trip_count']}, "
        f"got {summary_json['negative_duration_trip_count']}. "
        "Timezone normalization likely incorrect."
    )


def test_deduplicated_dispatch_count(summary_json, ground_truth):
    """Remaining dispatch events after deduplication must match the reference."""
    assert "deduplicated_dispatch_count" in summary_json, \
        "deduplicated_dispatch_count missing from summary.json."
    expected = ground_truth["deduplicated_dispatch_count"]
    actual   = summary_json["deduplicated_dispatch_count"]
    assert actual == expected, (
        f"Expected deduplicated_dispatch_count={expected}, got {actual}. "
        "Verify the deduplication grouping key is applied consistently across all records."
    )


def test_airport_exemption_count(summary_json, ground_truth):
    """Airport queue chains must be correctly identified regardless of idle duration."""
    assert "airport_exemption_count" in summary_json, \
        "airport_exemption_count missing from summary.json."
    expected = ground_truth["airport_exemption_count"]
    actual   = summary_json["airport_exemption_count"]
    assert actual == expected, (
        f"Expected airport_exemption_count={expected}, got {actual}."
    )


# ---------------------------------------------------------------------------
# Summary CSV structure and content
# ---------------------------------------------------------------------------

def test_output_structure(summary_df):
    """Output file must exist, contain all required columns, and have enough rows."""
    assert SUMMARY_PATH.exists(), f"Missing output file: {SUMMARY_PATH}"
    required = [
        "reposition_from_zone", "total_trips", "avg_idle_minutes",
        "avg_reposition_km", "inefficient_repositions", "inefficiency_rate",
    ]
    for col in required:
        assert col in summary_df.columns, f"Missing required column: {col}"
    assert len(summary_df) > 10, "Summary has too few rows."


def test_unknown_zone_assignment(summary_df):
    """GPS-dropout chains must appear as zone 0, not be silently dropped."""
    assert 0 in summary_df["reposition_from_zone"].values, (
        "Zone 0 not found in zone_repositioning_summary.csv. "
        "Chains with missing dropoff zones must be assigned to zone code 0."
    )


def test_avg_idle_minutes(summary_df, ground_truth):
    """Average idle times must be approximately correct and non-negative for every zone."""
    assert (summary_df["avg_idle_minutes"] >= 0).all(), (
        "Negative avg_idle_minutes detected — check trip sequencing and timezone normalization."
    )
    expected = ground_truth["summary"].set_index("reposition_from_zone")
    actual   = summary_df.set_index("reposition_from_zone")
    for zone in expected.index.intersection(actual.index):
        exp_val = expected.loc[zone, "avg_idle_minutes"]
        act_val = actual.loc[zone, "avg_idle_minutes"]
        assert abs(exp_val - act_val) < 2.5, (
            f"avg_idle_minutes mismatch for zone {zone}: "
            f"expected ~{exp_val:.2f}, got {act_val:.2f}"
        )


def test_inefficiency_rate(summary_df, ground_truth):
    """Inefficiency rates must be approximately correct for every zone."""
    expected = ground_truth["summary"].set_index("reposition_from_zone")
    actual   = summary_df.set_index("reposition_from_zone")
    for zone in expected.index.intersection(actual.index):
        exp_val = expected.loc[zone, "inefficiency_rate"]
        act_val = actual.loc[zone, "inefficiency_rate"]
        assert abs(exp_val - act_val) < 0.03, (
            f"inefficiency_rate mismatch for zone {zone}: "
            f"expected ~{exp_val:.4f}, got {act_val:.4f}"
        )


def test_reposition_distance_logic(summary_df, ground_truth):
    """avg_reposition_km must be correct across all zones."""
    expected = ground_truth["summary"].set_index("reposition_from_zone")
    actual   = summary_df.set_index("reposition_from_zone")
    for zone in expected.index.intersection(actual.index):
        exp_val = expected.loc[zone, "avg_reposition_km"]
        act_val = actual.loc[zone, "avg_reposition_km"]
        assert abs(exp_val - act_val) < 0.5, (
            f"avg_reposition_km mismatch for zone {zone}: "
            f"expected ~{exp_val:.2f}, got {act_val:.2f}"
        )


def test_airport_zones_not_extreme(summary_df):
    """JFK (132) and LGA (138) must not appear in the top 5 inefficiency zones."""
    top5 = set(
        summary_df.sort_values("inefficiency_rate", ascending=False)
        .head(5)["reposition_from_zone"]
    )
    airport_ids = {132, 138}
    assert not (airport_ids & top5), (
        f"Airport zone(s) {airport_ids & top5} appear among top inefficiency zones. "
        "Airport queue exemptions likely ignored."
    )


def test_summary_sorted_and_top_zone(summary_df, ground_truth):
    """Output must be sorted descending by inefficiency_rate and the top zone must be correct."""
    vals = summary_df["inefficiency_rate"].values
    assert np.all(vals[:-1] >= vals[1:]), (
        "Summary must be sorted descending by inefficiency_rate."
    )
    expected_top = (
        ground_truth["summary"]
        .sort_values(["inefficiency_rate", "total_trips"], ascending=[False, False])
        .iloc[0]["reposition_from_zone"]
    )
    actual_top = (
        summary_df
        .sort_values(["inefficiency_rate", "total_trips"], ascending=[False, False])
        .iloc[0]["reposition_from_zone"]
    )
    assert actual_top == expected_top, (
        f"Expected top inefficient zone {expected_top}, got {actual_top}."
    )
