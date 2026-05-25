"""
Unit tests for Warehouse Picker Productivity Analysis benchmark.

Seven traps embedded in the data:

  T1 – Midnight shift boundary
       NIGHT shift (22:00–06:00) picks logged after midnight carry the next
       calendar day's date. Naive groupby(pick_timestamp.date()) assigns them
       to the wrong shift. Correct: match each pick to the shift_assignment
       record whose clock window contains the pick_timestamp.

  T2 – Zone reassignment mid-shift (picks)
       picks.zone_id = worker's scanner home zone. When a worker is
       temporarily reassigned, their picks during that window belong to the
       reassigned zone. Correct: temporal join with zone_assignments.csv.

  T3 – Training shift contamination
       Trainee picks are logged under trainer's worker_id. Trainer appears
       ~1.4x more productive on training days. Correct: exclude all picks
       whose matched shift_assignment has is_training_shift = True.

  T4 – Break time deduction
       labor_hours = clock_duration - break_duration_min / 60.
       Naive: uses raw clock_out - clock_in.

  T5 – Out-of-window picks
       ~1.5% of picks have pick_timestamp outside the worker's shift window
       (scanner bleed-over from previous shift). Correct: exclude picks
       where pick_timestamp < clock_in or > clock_out_eff.

  T6 – Labor hour zone split
       Reassigned workers' effective hours must be split proportionally
       between home zone and reassigned zone. Naive: all hours go to home
       zone, leaving zero labor hours for the reassigned zone → division by
       zero or inflated ratios.

  T7 – Orphaned picks (ghost workers)
       5 worker IDs (W151–W155) appear in picks.csv but not in workers.csv
       or shift_assignments.csv. Including them inflates pick counts.
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── Paths ──────────────────────────────────────────────────────────────
if Path("/workspace/data").exists():
    DATA_DIR  = Path("/workspace/data")
    WORKSPACE = Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR  = Path("../environment/data")
    WORKSPACE = Path("..")
elif Path("environment/data").exists():
    DATA_DIR  = Path("environment/data")
    WORKSPACE = Path(".")
else:
    DATA_DIR  = Path("data")
    WORKSPACE = Path(".")

VARIABLES_PATH = Path(os.environ.get("VARIABLES_PATH", "/logs/verifier/notebook_variables.json"))
REPORT_PATH    = WORKSPACE / "productivity_report.csv"


# ── Reference Pipeline ─────────────────────────────────────────────────
def _compute_ground_truth():
    picks_raw = pd.read_csv(DATA_DIR / "picks.csv")
    workers   = pd.read_csv(DATA_DIR / "workers.csv")
    shifts_df = pd.read_csv(DATA_DIR / "shifts.csv")
    sa        = pd.read_csv(DATA_DIR / "shift_assignments.csv")
    zones     = pd.read_csv(DATA_DIR / "zones.csv")
    za        = pd.read_csv(DATA_DIR / "zone_assignments.csv")

    total_raw_picks = int(len(picks_raw))

    # Dedup
    picks = picks_raw.drop_duplicates().reset_index(drop=True)
    duplicate_picks_removed = int(total_raw_picks - len(picks))

    # T7: orphaned picks
    valid_wids = set(workers["worker_id"]) | set(sa["worker_id"])
    orphaned_mask = ~picks["worker_id"].isin(valid_wids)
    orphaned_picks_removed = int(orphaned_mask.sum())
    picks = picks[~orphaned_mask].reset_index(drop=True)

    # Prepare SA
    sa["clock_in"] = pd.to_datetime(sa["clock_in_time"])
    null_clockout_count = int(sa["clock_out_time"].isna().sum())

    def sched_end(row):
        d = pd.to_datetime(row["date"])
        if row["shift_id"] == "S1":
            return d.replace(hour=14, minute=0, second=0, microsecond=0)
        elif row["shift_id"] == "S2":
            return d.replace(hour=22, minute=0, second=0, microsecond=0)
        else:
            return (d + pd.Timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)

    sa["sched_end"] = sa.apply(sched_end, axis=1)
    sa["clock_out_eff"] = sa.apply(
        lambda r: pd.to_datetime(r["clock_out_time"]) if pd.notna(r["clock_out_time"]) else r["sched_end"],
        axis=1,
    )

    # T1 + T5: midnight-aware shift attribution → out-of-window removal
    picks["pick_ts"]        = pd.to_datetime(picks["pick_timestamp"])
    picks["pick_date"]      = picks["pick_ts"].dt.strftime("%Y-%m-%d")
    picks["pick_date_prev"] = (picks["pick_ts"] - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")

    sa_cols = ["worker_id", "date", "assignment_id", "shift_id",
               "clock_in", "clock_out_eff", "is_training_shift", "break_duration_min"]
    p_cols  = ["pick_id", "worker_id", "pick_ts"]

    m_direct = picks[p_cols + ["pick_date"]].merge(
        sa[sa_cols], left_on=["worker_id", "pick_date"], right_on=["worker_id", "date"], how="inner"
    )
    m_direct = m_direct[
        (m_direct["pick_ts"] >= m_direct["clock_in"]) &
        (m_direct["pick_ts"] <= m_direct["clock_out_eff"])
    ]

    sa_night = sa[sa["shift_id"] == "S3"][sa_cols]
    m_night  = picks[p_cols + ["pick_date_prev"]].merge(
        sa_night, left_on=["worker_id", "pick_date_prev"], right_on=["worker_id", "date"], how="inner"
    )
    m_night = m_night[
        (m_night["pick_ts"] >= m_night["clock_in"]) &
        (m_night["pick_ts"] <= m_night["clock_out_eff"])
    ]

    out_cols = ["pick_id", "assignment_id", "shift_id", "is_training_shift", "break_duration_min"]
    matched  = (
        pd.concat([m_direct[out_cols], m_night[out_cols]], ignore_index=True)
        .drop_duplicates("pick_id")
    )
    out_of_window_picks_removed = int(len(picks) - len(matched))
    picks = picks[picks["pick_id"].isin(matched["pick_id"])].merge(matched, on="pick_id", how="left")

    # T3: training picks
    training_mask = picks["is_training_shift"].astype(bool)
    training_picks_removed = int(training_mask.sum())
    picks = picks[~training_mask].reset_index(drop=True)

    # T2: zone reassignment for picks
    za["start_dt"] = pd.to_datetime(za["start_datetime"])
    za["end_dt"]   = pd.to_datetime(za["end_datetime"])

    picks_za = picks[["pick_id", "worker_id", "pick_ts", "zone_id"]].merge(
        za[["worker_id", "zone_id", "start_dt", "end_dt"]].rename(columns={"zone_id": "correct_zone"}),
        on="worker_id", how="left"
    )
    in_ra = (picks_za["pick_ts"] >= picks_za["start_dt"]) & (picks_za["pick_ts"] <= picks_za["end_dt"])
    correct_zones = picks_za[in_ra][["pick_id", "correct_zone"]].drop_duplicates("pick_id")
    picks = picks.merge(correct_zones, on="pick_id", how="left")
    picks["zone_id_final"] = picks["correct_zone"].where(picks["correct_zone"].notna(), picks["zone_id"])
    picks = picks[picks["zone_id_final"].notna()].reset_index(drop=True)

    # T4 + T6: labor hours with break deduction and zone split
    sa["effective_hours"] = (
        (sa["clock_out_eff"] - sa["clock_in"]).dt.total_seconds() / 3600
        - sa["break_duration_min"] / 60
    ).clip(lower=0)

    sa_za = sa[["assignment_id", "worker_id", "shift_id",
                "clock_in", "clock_out_eff", "effective_hours"]].merge(
        za[["worker_id", "zone_id", "start_dt", "end_dt"]], on="worker_id", how="left"
    )
    sa_za["za_s"] = sa_za[["start_dt", "clock_in"]].max(axis=1)
    sa_za["za_e"] = sa_za[["end_dt",   "clock_out_eff"]].min(axis=1)
    sa_za["za_h"] = ((sa_za["za_e"] - sa_za["za_s"]).dt.total_seconds() / 3600).clip(lower=0)
    sa_za_valid = sa_za[sa_za["za_h"] > 0].copy()

    ra_per_sa = (
        sa_za_valid.groupby("assignment_id")["za_h"].sum()
        .reset_index().rename(columns={"za_h": "total_ra_h"})
    )
    sa = sa.merge(ra_per_sa, on="assignment_id", how="left")
    sa["total_ra_h"] = sa["total_ra_h"].fillna(0)
    sa["home_h"]     = (sa["effective_hours"] - sa["total_ra_h"]).clip(lower=0)
    sa = sa.merge(workers[["worker_id", "home_zone_id"]], on="worker_id", how="left")

    home_labor = sa[["shift_id", "home_zone_id", "home_h"]].copy()
    home_labor.columns = ["shift_id", "zone_id", "labor_hours"]
    ra_labor   = sa_za_valid[["shift_id", "zone_id", "za_h"]].copy()
    ra_labor.columns = ["shift_id", "zone_id", "labor_hours"]

    all_labor = pd.concat([home_labor, ra_labor], ignore_index=True)
    all_labor = all_labor.merge(shifts_df[["shift_id", "shift_name"]], on="shift_id", how="left")
    labor_agg = (
        all_labor.groupby(["zone_id", "shift_name"])["labor_hours"]
        .sum().reset_index()
        .rename(columns={"labor_hours": "total_labor_hours"})
    )
    valid_labor_hours_total = round(float(labor_agg["total_labor_hours"].sum()), 2)

    # Picks aggregation
    picks = picks.merge(shifts_df[["shift_id", "shift_name"]], on="shift_id", how="left")
    picks_agg = (
        picks.groupby(["zone_id_final", "shift_name"]).size()
        .reset_index(name="total_picks")
        .rename(columns={"zone_id_final": "zone_id"})
    )

    # Productivity report
    report = picks_agg.merge(labor_agg, on=["zone_id", "shift_name"], how="outer")
    report["total_picks"]       = report["total_picks"].fillna(0).astype(int)
    report["total_labor_hours"] = report["total_labor_hours"].fillna(0).round(2)
    report = report[(report["total_picks"] > 0) & (report["total_labor_hours"] > 0)].copy()
    report["picks_per_labor_hour"] = (report["total_picks"] / report["total_labor_hours"]).round(2)
    report = report.merge(zones[["zone_id", "zone_name"]], on="zone_id", how="left")
    report = report[["zone_id", "zone_name", "shift_name", "total_picks",
                     "total_labor_hours", "picks_per_labor_hour"]]
    report = report.sort_values("picks_per_labor_hour", ascending=False).reset_index(drop=True)

    return {
        "total_raw_picks":            total_raw_picks,
        "duplicate_picks_removed":    duplicate_picks_removed,
        "orphaned_picks_removed":     orphaned_picks_removed,
        "out_of_window_picks_removed": out_of_window_picks_removed,
        "training_picks_removed":     training_picks_removed,
        "null_clockout_count":        null_clockout_count,
        "valid_labor_hours_total":    valid_labor_hours_total,
        "report":                     report,
    }


# ── Fixtures ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ground_truth():
    return _compute_ground_truth()


@pytest.fixture(scope="module")
def notebook_vars():
    if not VARIABLES_PATH.exists():
        pytest.skip("notebook_variables.json not found")
    with open(VARIABLES_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def report_df():
    if not REPORT_PATH.exists():
        pytest.fail(f"productivity_report.csv not found at {REPORT_PATH}")
    return pd.read_csv(REPORT_PATH)


# ── Structure tests ────────────────────────────────────────────────────
def test_output_file_exists():
    assert REPORT_PATH.exists(), f"productivity_report.csv not found at {REPORT_PATH}"


def test_report_required_columns(report_df):
    required = ["zone_id", "zone_name", "shift_name", "total_picks",
                "total_labor_hours", "picks_per_labor_hour"]
    for col in required:
        assert col in report_df.columns, f"Missing column: {col}"


def test_report_sorted_descending(report_df):
    vals = report_df["picks_per_labor_hour"].values
    assert np.all(vals[:-1] >= vals[1:]), \
        "productivity_report.csv must be sorted descending by picks_per_labor_hour"


def test_report_no_zero_labor_hours(report_df):
    assert (report_df["total_labor_hours"] > 0).all(), \
        "All rows must have total_labor_hours > 0 (T6: labor hour zone split required)"


def test_report_valid_ratios(report_df):
    computed = (report_df["total_picks"] / report_df["total_labor_hours"]).round(2)
    assert np.allclose(computed.values, report_df["picks_per_labor_hour"].values, atol=0.02), \
        "picks_per_labor_hour does not match total_picks / total_labor_hours"


# ── Notebook variable tests ────────────────────────────────────────────
def test_total_raw_picks(notebook_vars, ground_truth):
    assert "total_raw_picks" in notebook_vars
    assert notebook_vars["total_raw_picks"] == ground_truth["total_raw_picks"], \
        f"Expected total_raw_picks={ground_truth['total_raw_picks']}"


def test_duplicate_picks_removed(notebook_vars, ground_truth):
    assert "duplicate_picks_removed" in notebook_vars
    expected = ground_truth["duplicate_picks_removed"]
    actual   = notebook_vars["duplicate_picks_removed"]
    assert actual == expected, \
        f"Expected duplicate_picks_removed={expected}, got {actual}. " \
        "Remove exact duplicate rows before any other filtering."


def test_orphaned_picks_removed(notebook_vars, ground_truth):
    assert "orphaned_picks_removed" in notebook_vars, \
        "Variable 'orphaned_picks_removed' not found. " \
        "Exclude picks whose worker_id does not appear in workers.csv or shift_assignments.csv."
    expected = ground_truth["orphaned_picks_removed"]
    actual   = notebook_vars["orphaned_picks_removed"]
    assert actual == expected, \
        f"Expected orphaned_picks_removed={expected}, got {actual}. " \
        "Five ghost worker IDs (W151–W155) appear in picks.csv but not in any reference table."


def test_null_clockout_count(notebook_vars, ground_truth):
    assert "null_clockout_count" in notebook_vars
    expected = ground_truth["null_clockout_count"]
    actual   = notebook_vars["null_clockout_count"]
    assert actual == expected, \
        f"Expected null_clockout_count={expected}, got {actual}."


def test_out_of_window_picks_removed(notebook_vars, ground_truth):
    """
    This test catches two compounding traps:
      T5 – out-of-window picks (scanner bleed-over),
      T1 – NIGHT shift midnight boundary (picks after 00:00 carry next-day date;
           naive shift matching assigns them to the wrong shift or drops them).
    Correct solution: match each pick to the SA window that contains pick_timestamp,
    trying both pick_timestamp.date() and pick_timestamp.date()-1 for NIGHT shift.
    """
    assert "out_of_window_picks_removed" in notebook_vars, \
        "Variable 'out_of_window_picks_removed' not found. " \
        "Exclude picks that fall outside the worker's clock_in / clock_out window."
    expected = ground_truth["out_of_window_picks_removed"]
    actual   = notebook_vars["out_of_window_picks_removed"]
    assert abs(actual - expected) <= 50, \
        f"Expected out_of_window_picks_removed≈{expected}, got {actual}. " \
        "A large discrepancy usually means NIGHT shift picks after midnight were not " \
        "matched to the previous calendar day's shift assignment (T1 midnight boundary)."


def test_training_picks_removed(notebook_vars, ground_truth):
    assert "training_picks_removed" in notebook_vars
    expected = ground_truth["training_picks_removed"]
    actual   = notebook_vars["training_picks_removed"]
    assert abs(actual - expected) <= 200, \
        f"Expected training_picks_removed≈{expected}, got {actual}. " \
        "Join picks to shift_assignments and exclude rows where is_training_shift=True. " \
        "Note: correct shift matching (T1) is required before training detection."


def test_valid_labor_hours_total(notebook_vars, ground_truth):
    """
    Catches T4 (break deduction) and T6 (zone split):
      T4 – naive model uses raw clock duration, not clock_duration - break_duration_min.
      T6 – naive model assigns all labor hours to home zone; reassigned zones get zero
           hours, causing division-by-zero or inflated picks_per_labor_hour.
    """
    assert "valid_labor_hours_total" in notebook_vars, \
        "Variable 'valid_labor_hours_total' not found."
    expected = ground_truth["valid_labor_hours_total"]
    actual   = float(notebook_vars["valid_labor_hours_total"])
    rel_err  = abs(actual - expected) / max(expected, 1)
    assert rel_err < 0.02, \
        f"Expected valid_labor_hours_total≈{expected:.2f}, got {actual:.2f} " \
        f"(relative error {rel_err:.1%}). " \
        "Check: (1) break_duration_min is subtracted from each shift's hours (T4); " \
        "(2) scheduled shift end is used when clock_out_time is null."


# ── Productivity accuracy tests ────────────────────────────────────────
def test_top_zone_shift_identity(report_df, ground_truth):
    """Top row must match the reference on zone_id and shift_name."""
    ref    = ground_truth["report"].iloc[0]
    actual = report_df.sort_values("picks_per_labor_hour", ascending=False).iloc[0]
    assert actual["zone_id"] == ref["zone_id"], \
        f"Top zone: expected '{ref['zone_id']}', got '{actual['zone_id']}'. " \
        "Ensure zone reassignment (T2), training exclusion (T3), and break deduction (T4) " \
        "are all applied before computing productivity."
    assert actual["shift_name"] == ref["shift_name"], \
        f"Top shift: expected '{ref['shift_name']}', got '{actual['shift_name']}'."


def test_top_zone_shift_ratio(report_df, ground_truth):
    """Top row picks_per_labor_hour must be within 5% of reference."""
    ref_ratio    = float(ground_truth["report"].iloc[0]["picks_per_labor_hour"])
    actual_ratio = float(report_df.sort_values("picks_per_labor_hour", ascending=False).iloc[0]["picks_per_labor_hour"])
    assert abs(actual_ratio - ref_ratio) / max(ref_ratio, 1) < 0.05, \
        f"Top picks_per_labor_hour: expected≈{ref_ratio:.2f}, got {actual_ratio:.2f}. " \
        "Inflated ratios indicate labor hours were not split for reassigned zones (T6)."


def test_night_shift_pick_attribution(report_df, ground_truth):
    """
    NIGHT shift must have picks. If zero NIGHT picks appear in the report,
    picks after midnight were not matched to the previous day's NIGHT shift (T1).
    """
    ref_night = ground_truth["report"][ground_truth["report"]["shift_name"] == "NIGHT"]
    if ref_night.empty:
        pytest.skip("No NIGHT shift rows in reference report")
    ref_night_picks = int(ref_night["total_picks"].sum())

    night_rows = report_df[report_df["shift_name"] == "NIGHT"]
    actual_night_picks = int(night_rows["total_picks"].sum()) if not night_rows.empty else 0

    assert abs(actual_night_picks - ref_night_picks) / max(ref_night_picks, 1) < 0.10, \
        f"NIGHT shift total_picks: expected≈{ref_night_picks}, got {actual_night_picks}. " \
        "Picks logged after midnight belong to the NIGHT shift that started the previous day. " \
        "Naive groupby(pick_timestamp.date()) loses ~15% of NIGHT picks to DAY shift (T1)."


def test_zone_reassignment_reduces_home_zone_labor(ground_truth):
    """
    Zone-split labor hours (T6): total labor hours in any zone that received
    reassigned workers must be > 0. If it is 0, all hours stayed in home zone.
    """
    ref = ground_truth["report"]
    za  = pd.read_csv(DATA_DIR / "zone_assignments.csv")
    reassigned_zones = set(za["zone_id"].unique())

    for zone in reassigned_zones:
        zone_rows = ref[ref["zone_id"] == zone]
        if zone_rows.empty:
            continue
        assert zone_rows["total_labor_hours"].sum() > 0, \
            f"Zone {zone} received reassigned workers but has zero labor hours in the report. " \
            "Split worker labor hours proportionally between home and reassigned zones (T6)."


# ── Data integrity (anti-cheat sentinels) ─────────────────────────────
def test_input_picks_row_count():
    """picks.csv must not be truncated."""
    picks_raw = pd.read_csv(DATA_DIR / "picks.csv")
    assert len(picks_raw) == 196_418, \
        f"picks.csv has {len(picks_raw)} rows; expected 196,418. " \
        "Input data must not be modified."


def test_night_shift_assignments_present():
    """shift_assignments.csv must retain all NIGHT shift (S3) records."""
    sa = pd.read_csv(DATA_DIR / "shift_assignments.csv")
    s3_count = int((sa["shift_id"] == "S3").sum())
    assert s3_count >= 5_000, \
        f"Only {s3_count} NIGHT shift assignments found; expected ≥5,000. " \
        "Do not remove or filter shift_assignments records."


def test_zone_assignments_not_removed():
    """zone_assignments.csv must retain all mid-shift reassignment records."""
    za = pd.read_csv(DATA_DIR / "zone_assignments.csv")
    assert len(za) >= 1_200, \
        f"Only {len(za)} zone assignment records found; expected ≥1,200. " \
        "Do not remove or filter zone_assignments records."


def test_ghost_workers_in_picks():
    """picks.csv must contain picks from all five ghost worker IDs (T7)."""
    picks_raw = pd.read_csv(DATA_DIR / "picks.csv")
    ghost_ids = {"W151", "W152", "W153", "W154", "W155"}
    found = ghost_ids & set(picks_raw["worker_id"].unique())
    assert found == ghost_ids, \
        f"Expected all of W151–W155 in picks.csv; found only {found}. " \
        "Do not remove picks attributed to ghost worker IDs."
