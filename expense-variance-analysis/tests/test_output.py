"""
Tests for the Q1 2024 warehouse payroll variance report (TerminalBench format).

The task asks an analyst to reconcile actual warehouse payroll costs against a
weekly budget across 10 departments for 13 weeks of Q1 2024.

There are four reasoning traps in the data:

1. Timezone mismatch: shift_end is UTC, shift_start is US Eastern (naive). The
   DST spring-forward on 2024-03-10 means a flat offset is wrong for around 1,000
   shifts, and some of those also cross a Monday week boundary.

2. Contractor rate units: hourly_rate for contractors is in US cents, not dollars.
   A solver that misses this inflates contractor labor cost by 100x.

3. Reassignment billing: hours worked in another department are billed to the
   receiving department at base rate; the overtime premium stays with the home
   department. Naive solvers often double-count or attribute to the wrong side.

4. Non-standard OT thresholds: D05 uses 36h/week and D08 uses 44h/week instead
   of the common 40h. A solver using a flat threshold gets those two departments wrong.

Contract (instruction.md): the agent writes
    /workspace/payroll_variance_report.csv  (130 rows, 6 columns)
    /workspace/summary.json                 (5 scalar keys)
"""

import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    # Fallback for local runs outside the container (see local_run.sh)
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
    else Path(__file__).parent.parent / "environment" / "data"
REPORT_PATH = WORKSPACE_DIR / "payroll_variance_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

DST_BOUNDARY_UTC = pd.Timestamp("2024-03-10 07:00:00")


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: verify the input files were not modified to trivialise the task."""
    shifts = pd.read_csv(DATA_DIR / "shifts.csv")
    assert len(shifts) == 35_756, "shifts.csv row count must not be modified."

    employees = pd.read_csv(DATA_DIR / "employees.csv")
    ct_max = employees.loc[employees["employee_type"] == "contractor", "hourly_rate"].max()
    ft_max = employees.loc[employees["employee_type"] == "full_time", "hourly_rate"].max()
    assert ct_max > ft_max * 50, "Contractor rates must be in US cents (much larger than FT rates)."

    ot = pd.read_csv(DATA_DIR / "ot_policy.csv").set_index("department_id")["weekly_ot_threshold"]
    assert ot.get("D05") == 36.0, "D05 must have a 36h OT threshold."
    assert ot.get("D08") == 44.0, "D08 must have a 44h OT threshold."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "payroll_variance_report.csv not found."
    df = pd.read_csv(REPORT_PATH)
    required = {"department_id", "department_name", "week_start",
                "budgeted_cost", "actual_cost", "variance"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 130, f"Expected 130 rows (10 depts x 13 weeks), got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values(["department_id", "week_start"]).reset_index(drop=True)
    assert list(df["department_id"]) == list(expected["department_id"]) and \
           list(df["week_start"]) == list(expected["week_start"]), \
        "Report must be sorted by department_id then week_start."


def test_case_04_variance_formula():
    df = pd.read_csv(REPORT_PATH)
    for _, row in df.iterrows():
        expected = round(float(row["actual_cost"]) - float(row["budgeted_cost"]), 2)
        assert abs(float(row["variance"]) - expected) < 0.02, \
            f"variance != actual_cost - budgeted_cost for {row['department_id']}/{row['week_start']}."


def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required_keys = {"total_budgeted_cost", "total_actual_cost", "total_variance",
                     "total_overtime_hours", "over_budget_week_count"}
    missing = required_keys - set(s.keys())
    assert not missing, f"Missing keys in summary.json: {missing}"
    for k in required_keys - {"over_budget_week_count"}:
        assert isinstance(s[k], float), f"{k} must be a float."
    assert isinstance(s["over_budget_week_count"], int) and \
           not isinstance(s["over_budget_week_count"], bool), \
        "over_budget_week_count must be an int."


# ── Ground-truth fixture ──────────────────────────────────────────────────────

def _week_start_of(ts_series):
    return (ts_series - pd.to_timedelta(ts_series.dt.weekday, unit="D")).dt.normalize()


@pytest.fixture(scope="module")
def ground_truth():
    shifts = pd.read_csv(DATA_DIR / "shifts.csv", parse_dates=["shift_start", "shift_end"])
    employees = pd.read_csv(DATA_DIR / "employees.csv")
    ot_policy = pd.read_csv(DATA_DIR / "ot_policy.csv")
    reassignments = pd.read_csv(DATA_DIR / "reassignments.csv")
    budget = pd.read_csv(DATA_DIR / "weekly_budget.csv")

    emp = employees.copy()
    mask = emp["employee_type"] == "contractor"
    emp.loc[mask, "hourly_rate"] = emp.loc[mask, "hourly_rate"] / 100.0

    sh = shifts.copy()
    off = pd.to_timedelta(np.where(sh["shift_end"] < DST_BOUNDARY_UTC, 5, 4), unit="h")
    sh["shift_end_et"] = sh["shift_end"] - off

    crosses = _week_start_of(sh["shift_start"]) != _week_start_of(sh["shift_end_et"])
    normal = sh[~crosses].copy()
    normal["seg_start"] = normal["shift_start"]
    normal["seg_end"] = normal["shift_end_et"]
    xw = sh[crosses].copy()
    xw["boundary"] = _week_start_of(xw["shift_end_et"])
    s1 = xw.copy(); s1["seg_start"] = xw["shift_start"]; s1["seg_end"] = xw["boundary"]
    s2 = xw.copy(); s2["seg_start"] = xw["boundary"];    s2["seg_end"] = xw["shift_end_et"]
    segs = pd.concat([normal, s1, s2], ignore_index=True)
    segs["hours"] = (segs["seg_end"] - segs["seg_start"]).dt.total_seconds() / 3600
    segs["week_start"] = _week_start_of(segs["seg_start"]).dt.strftime("%Y-%m-%d")

    wh = (segs.groupby(["employee_id", "week_start"])["hours"]
          .sum().reset_index().rename(columns={"hours": "total_hours"}))
    wh = wh.merge(emp[["employee_id", "department_id", "hourly_rate"]], on="employee_id")
    wh = wh.merge(ot_policy, on="department_id")
    wh["regular_hours"] = np.minimum(wh["total_hours"], wh["weekly_ot_threshold"])
    wh["ot_hours"] = np.maximum(wh["total_hours"] - wh["weekly_ot_threshold"], 0.0)
    wh["actual_cost"] = (wh["regular_hours"] * wh["hourly_rate"]
                         + wh["ot_hours"] * wh["hourly_rate"] * 1.5)

    rc = reassignments.merge(emp[["employee_id", "hourly_rate"]], on="employee_id")
    rc["reass_cost"] = rc["reassigned_hours"] * rc["hourly_rate"]
    home_ded = (rc.groupby(["employee_id", "week_start"])["reass_cost"]
                .sum().reset_index().rename(columns={"reass_cost": "home_deduction"}))
    wh = wh.merge(home_ded, on=["employee_id", "week_start"], how="left")
    wh["home_deduction"] = wh["home_deduction"].fillna(0.0)
    wh["home_cost"] = wh["actual_cost"] - wh["home_deduction"]

    home_agg = (wh.groupby(["department_id", "week_start"])
                .agg(actual_cost=("home_cost", "sum"), ot_hours=("ot_hours", "sum"))
                .reset_index())
    recv_agg = (rc.groupby(["to_dept_id", "week_start"])["reass_cost"]
                .sum().reset_index()
                .rename(columns={"to_dept_id": "department_id", "reass_cost": "recv_cost"}))
    dw = home_agg.merge(recv_agg, on=["department_id", "week_start"], how="left")
    dw["recv_cost"] = dw["recv_cost"].fillna(0.0)
    dw["actual_cost"] = dw["actual_cost"] + dw["recv_cost"]
    dw = dw.drop(columns=["recv_cost"])

    bud = budget.copy()
    bud["budgeted_cost"] = bud["budgeted_hours"] * bud["avg_hourly_rate"]
    report = bud.merge(dw, on=["department_id", "week_start"])

    return {
        "total_budgeted_cost":    float(round(report["budgeted_cost"].sum(), 2)),
        "total_actual_cost":      float(round(report["actual_cost"].sum(), 2)),
        "total_variance":         float(round(report["actual_cost"].sum() - report["budgeted_cost"].sum(), 2)),
        "total_overtime_hours":   float(round(dw["ot_hours"].sum(), 2)),
        "over_budget_week_count": int((report["actual_cost"] > report["budgeted_cost"]).sum()),
        "_report": report,
        "_dw": dw,
    }


# ── Scalar value correctness ──────────────────────────────────────────────────

def test_case_06_total_overtime_hours(ground_truth):
    """Primary trap: wrong if timezone not converted before computing hours."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["total_overtime_hours"] - ground_truth["total_overtime_hours"]) <= 1.0, \
        f"total_overtime_hours: got {s['total_overtime_hours']}, expected {ground_truth['total_overtime_hours']} (±1.0)"


def test_case_07_total_actual_cost(ground_truth):
    """Affected by timezone trap and contractor rate units trap."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["total_actual_cost"] - ground_truth["total_actual_cost"]) <= 1.0, \
        f"total_actual_cost: got {s['total_actual_cost']}, expected {ground_truth['total_actual_cost']} (±1.0)"


# ── Row-level correctness ─────────────────────────────────────────────────────

def test_case_08_d05_cost_reflects_36h_threshold(ground_truth):
    """D05 uses a 36h OT threshold; its actual_cost should be higher than with 40h."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["_report"]
    for ws in gt["week_start"].unique():
        gt_row = gt[(gt["department_id"] == "D05") & (gt["week_start"] == ws)]
        rep_row = report[(report["department_id"] == "D05") & (report["week_start"] == ws)]
        if len(gt_row) == 0 or len(rep_row) == 0:
            continue
        exp = float(round(gt_row["actual_cost"].iloc[0], 2))
        got = float(rep_row["actual_cost"].iloc[0])
        assert abs(got - exp) <= 1.0, \
            f"D05 actual_cost wrong for {ws}: got {got}, expected {exp} (±1.0)"


def test_case_09_reassignment_cost_allocation(ground_truth):
    """At least 120 of 130 rows must match ground truth actual_cost within ±1.0."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["_report"]
    merged = report.merge(
        gt[["department_id", "week_start", "actual_cost"]],
        on=["department_id", "week_start"],
        suffixes=("_got", "_exp"),
    )
    close = (abs(merged["actual_cost_got"] - merged["actual_cost_exp"]) <= 1.0).sum()
    assert close >= 120, \
        f"Only {close}/130 rows match ground truth — reassignment cost allocation is likely wrong."


def test_case_10_over_budget_week_count(ground_truth):
    """Affected by all four traps via per-department actual_cost."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["over_budget_week_count"] == ground_truth["over_budget_week_count"], \
        (f"over_budget_week_count: got {s['over_budget_week_count']}, "
         f"expected {ground_truth['over_budget_week_count']}")
