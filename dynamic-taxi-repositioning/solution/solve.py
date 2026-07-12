"""
Dynamic Taxi Repositioning Efficiency Analysis — reference solution.

Pipeline:
  1. Composite driver key: (driver_id, service_month).
  2. Normalize pickup_datetime (Eastern) and dropoff_datetime (UTC) to UTC;
     DST boundaries handled with ambiguous='NaT', nonexistent='NaT'.
  3. Remove billing reversals (negative fare_amount) and negative-duration trips.
  4. Deduplicate dispatch events by (driver_id, service_month, pickup_zone_id)
     with dropna=False so absent service_month rows form their own group.
  5. Resolve zone adjacency to the minimum distance per pair before merging.
  6. GPS-dropout chains (null dropoff_zone_id) are assigned to zone code 0.
  7. Airport queue periods at JFK (132) and LGA (138) exempt chains from
     the inefficiency flag regardless of idle duration.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

def _resolve_paths():
    candidates = [
        (Path("/workspace/data"),       Path("/workspace")),
        (Path("../environment/data"),   Path("..")),
        (Path("environment/data"),      Path(".")),
        (Path("data"),                  Path(".")),
    ]
    for data, ws in candidates:
        if (data / "trips.csv").exists():
            return data, ws
    return candidates[-1]

DATA_DIR, WORKSPACE = _resolve_paths()

LOCAL_TIMEZONE          = "America/New_York"
IDLE_THRESHOLD_MINUTES  = 30
DUPLICATE_WINDOW_SEC    = 15


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load():
    trips    = pd.read_csv(DATA_DIR / "trips.csv")
    dispatch = pd.read_csv(DATA_DIR / "dispatch_events.csv")
    shared   = pd.read_csv(DATA_DIR / "shared_rides.csv")
    airport  = pd.read_csv(DATA_DIR / "airport_queue_periods.csv")
    adj      = pd.read_csv(DATA_DIR / "zone_adjacency.csv")
    return trips, dispatch, shared, airport, adj


# ---------------------------------------------------------------------------
# Trips — normalization and cleaning
# ---------------------------------------------------------------------------

def normalize_trips(trips):
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


def count_cancelled(trips):
    return int((trips["fare_amount"] < 0).sum())


def count_negative_duration(trips):
    return int((trips["trip_duration_minutes"] < 0).sum())


def filter_valid_trips(trips):
    return trips[
        (trips["fare_amount"] >= 0) & (trips["trip_duration_minutes"] >= 0)
    ].copy()


# ---------------------------------------------------------------------------
# Dispatch deduplication
# ---------------------------------------------------------------------------

def deduplicate_dispatch(dispatch):
    dispatch = dispatch.copy()
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
    ].copy()


# ---------------------------------------------------------------------------
# Session and chain construction
# ---------------------------------------------------------------------------

def build_chains(trips, shared_ids):
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
        .shift(-1)
        .fillna(False)
    )

    trips["idle_minutes"] = (
        trips["next_trip_pickup"] - trips["dropoff_utc"]
    ).dt.total_seconds() / 60

    trips["reposition_from_zone"] = trips["dropoff_zone_id"].fillna(0).astype(int)
    trips["reposition_to_zone"]   = trips["next_pickup_zone"]

    return trips[trips["next_trip_pickup"].notna()].copy()


# ---------------------------------------------------------------------------
# Reposition distance
# ---------------------------------------------------------------------------

def attach_distance(chains, adj):
    adj = adj.groupby(["from_zone_id", "to_zone_id"], as_index=False)["distance_km"].min()
    chains = chains.merge(
        adj,
        left_on=["reposition_from_zone", "reposition_to_zone"],
        right_on=["from_zone_id", "to_zone_id"],
        how="left",
    )
    chains["distance_km"] = chains["distance_km"].fillna(0)
    return chains


def filter_impossible(chains):
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


# ---------------------------------------------------------------------------
# Airport exemptions
# ---------------------------------------------------------------------------

def apply_airport_exemptions(chains, airport):
    airport = airport.copy()
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
            & (check["queue_end_utc"].isna() | (check["dropoff_utc"] <= check["queue_end_utc"])),
            "trip_id",
        ]
    )
    chains = chains.copy()
    chains["airport_exempt"] = chains["trip_id"].isin(exempt_ids)
    return chains


# ---------------------------------------------------------------------------
# KPI summary
# ---------------------------------------------------------------------------

def compute_summary(chains):
    chains = chains.copy()
    chains["inefficient_idle"] = (
        (chains["idle_minutes"] >= IDLE_THRESHOLD_MINUTES)
        & (~chains["airport_exempt"])
    )
    summary = (
        chains.groupby("reposition_from_zone")
        .agg(
            total_trips              = ("trip_id",          "count"),
            avg_idle_minutes         = ("idle_minutes",      "mean"),
            avg_reposition_km        = ("distance_km",       "mean"),
            inefficient_repositions  = ("inefficient_idle",  "sum"),
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
# Main
# ---------------------------------------------------------------------------

def main():
    trips_raw, dispatch_raw, shared, airport, adj = load()

    trip_row_count = len(trips_raw)

    trips = normalize_trips(trips_raw)
    cancelled_trip_count         = count_cancelled(trips)
    negative_duration_trip_count = count_negative_duration(trips)
    trips = filter_valid_trips(trips)

    dispatch_dedup                = deduplicate_dispatch(dispatch_raw)
    deduplicated_dispatch_count   = len(dispatch_dedup)

    shared_ids = set(shared["trip_id"])
    chains = build_chains(trips, shared_ids)
    chains = attach_distance(chains, adj)
    chains = filter_impossible(chains)
    chains = apply_airport_exemptions(chains, airport)

    airport_exemption_count = int(chains["airport_exempt"].sum())

    summary = compute_summary(chains)
    summary.to_csv(WORKSPACE / "zone_repositioning_summary.csv", index=False)

    counts = {
        "trip_row_count":               trip_row_count,
        "cancelled_trip_count":         cancelled_trip_count,
        "negative_duration_trip_count": negative_duration_trip_count,
        "deduplicated_dispatch_count":  deduplicated_dispatch_count,
        "airport_exemption_count":      airport_exemption_count,
    }
    with open(WORKSPACE / "summary.json", "w") as f:
        json.dump(counts, f, indent=2)


if __name__ == "__main__":
    main()
