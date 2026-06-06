"""
Tests for the Q1 2024 warehouse payroll variance report.

What makes this task genuinely hard:

1. Night shift differential: shifts starting at or after 21:00 are paid at 1.3×
   the employee's base rate. The shift_start hour is in the data; an agent that
   computes cost as hours × rate without checking it misses ~$90k in premiums.
   Night OT is 1.3 × 1.5 = 1.95× base — overtime stacks on the shift rate.

2. Cross-midnight week boundary: night shifts that start Sunday night cross into
   Monday. Hours must be split at the Monday 00:00 boundary and attributed to the
   correct week, or weekly aggregations silently undercount one week and overcount
   the next.

3. Contractor rate units: hourly_rate for contractors is stored in US cents
   (1713–2796), roughly 100× the full-time range ($17–$28). Visible as a
   distributional outlier in a basic EDA.

4. Per-department OT thresholds: D05 uses 36h/week, D08 uses 44h/week (from
   ot_policy.csv). A flat 40h threshold produces wrong costs for both.

5. Duration outliers: ~20 shifts have durations of 26–34 h due to clock-out
   date entry errors. They appear as clear outliers in .describe() on computed
   shift hours. An agent that includes them inflates total hours and cost.

6. Mid-quarter transfers: 10 employees moved departments on 2024-02-05.
   employees.csv shows their post-transfer department. Shifts before the
   effective date must be attributed to from_dept_id in transfers.csv.
   An agent that uses employees.csv as a static lookup misattributes five
   weeks of pre-transfer labor to the wrong department.

Contract (instruction.md): the agent writes
    /workspace/payroll_variance_report.csv  (130 rows, 6 columns)
    /workspace/summary.json                 (5 scalar keys)
"""

import json
import os
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

_ws_env = os.environ.get("WORKSPACE_DIR")
if _ws_env:
    WORKSPACE_DIR = Path(_ws_env)
elif Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

_data_env = os.environ.get("DATA_DIR")
if _data_env:
    DATA_DIR = Path(_data_env)
elif (WORKSPACE_DIR / "data").exists():
    DATA_DIR = WORKSPACE_DIR / "data"
else:
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

REPORT_PATH  = WORKSPACE_DIR / "payroll_variance_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

NIGHT_START_HOUR = 21
NIGHT_RATE_MULT  = 1.3
OT_MULT          = 1.5
MAX_SHIFT_HOURS  = 16.0   # shifts longer than this are data-entry errors


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    shifts = pd.read_csv(DATA_DIR / "shifts.csv", parse_dates=["shift_start", "shift_end"])
    assert len(shifts) == 35_780, "shifts.csv row count must not be modified."

    durations = (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    assert (durations > 16).sum() >= 20, \
        "At least 20 duration-outlier shifts (> 16 h) must be present."

    night = (shifts["shift_start"].dt.hour >= NIGHT_START_HOUR).sum()
    assert night >= 1_500, f"At least 1,500 night shifts must be present (found {night})."

    employees = pd.read_csv(DATA_DIR / "employees.csv")
    ct_max = employees.loc[employees["employee_type"] == "contractor", "hourly_rate"].max()
    ft_max = employees.loc[employees["employee_type"] == "full_time",  "hourly_rate"].max()
    assert ct_max > ft_max * 50, "Contractor rates must be in US cents."

    ot = pd.read_csv(DATA_DIR / "ot_policy.csv").set_index("department_id")["weekly_ot_threshold"]
    assert ot["D05"] == 36.0, "D05 must have a 36h OT threshold."
    assert ot["D08"] == 44.0, "D08 must have a 44h OT threshold."

    transfers = pd.read_csv(DATA_DIR / "transfers.csv")
    assert len(transfers) >= 10, "At least 10 department transfer records must be present."
    assert {"employee_id", "from_dept_id", "to_dept_id", "effective_date"} <= set(transfers.columns)


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "payroll_variance_report.csv not found."
    df = pd.read_csv(REPORT_PATH)
    missing = {"department_id","department_name","week_start",
               "budgeted_cost","actual_cost","variance"} - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 130, f"Expected 130 rows, got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    exp = df.sort_values(["department_id","week_start"]).reset_index(drop=True)
    assert list(df["department_id"]) == list(exp["department_id"]) and \
           list(df["week_start"])    == list(exp["week_start"]), \
        "Report must be sorted by department_id then week_start."


def test_case_04_variance_formula():
    df = pd.read_csv(REPORT_PATH)
    for _, row in df.iterrows():
        exp = round(float(row["actual_cost"]) - float(row["budgeted_cost"]), 2)
        assert abs(float(row["variance"]) - exp) < 0.02, \
            f"variance != actual_cost - budgeted_cost for {row['department_id']}/{row['week_start']}."


def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_budgeted_cost","total_actual_cost","total_variance",
                "total_overtime_hours","over_budget_week_count"}
    assert not (required - set(s.keys())), f"Missing keys: {required - set(s.keys())}"
    for k in required - {"over_budget_week_count"}:
        assert isinstance(s[k], float), f"{k} must be a float."
    assert isinstance(s["over_budget_week_count"], int) and \
           not isinstance(s["over_budget_week_count"], bool)


# ── Ground-truth fixture ──────────────────────────────────────────────────────

def _week_start_of(ts):
    return (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.normalize()


@pytest.fixture(scope="module")
def ground_truth():
    shifts       = pd.read_csv(DATA_DIR / "shifts.csv",
                               parse_dates=["shift_start", "shift_end"])
    employees    = pd.read_csv(DATA_DIR / "employees.csv")
    ot_policy    = pd.read_csv(DATA_DIR / "ot_policy.csv")
    reassignments= pd.read_csv(DATA_DIR / "reassignments.csv")
    budget       = pd.read_csv(DATA_DIR / "weekly_budget.csv")
    transfers    = pd.read_csv(DATA_DIR / "transfers.csv",
                               parse_dates=["effective_date"])

    # Trap 3: contractor rates in cents
    emp = employees.copy()
    emp.loc[emp["employee_type"] == "contractor", "hourly_rate"] /= 100.0

    # Trap 5: exclude duration outliers (> 16 h are data-entry errors)
    sh = shifts.copy()
    sh["_dur_h"] = (sh["shift_end"] - sh["shift_start"]).dt.total_seconds() / 3600
    sh = sh[sh["_dur_h"] <= MAX_SHIFT_HOURS].copy()

    # Trap 1: night shift flag from shift_start hour
    sh["is_night"] = sh["shift_start"].dt.hour >= NIGHT_START_HOUR

    # Trap 2: split cross-midnight shifts at Monday boundary
    crosses = _week_start_of(sh["shift_start"]) != _week_start_of(sh["shift_end"])

    normal = sh[~crosses].copy()
    normal["seg_start"] = normal["shift_start"]
    normal["seg_end"]   = normal["shift_end"]

    xw = sh[crosses].copy()
    xw["boundary"] = _week_start_of(xw["shift_end"])
    s1 = xw.copy(); s1["seg_start"] = xw["shift_start"]; s1["seg_end"] = xw["boundary"]
    s2 = xw.copy(); s2["seg_start"] = xw["boundary"];    s2["seg_end"] = xw["shift_end"]

    cols = ["employee_id", "is_night", "seg_start", "seg_end"]
    segs = pd.concat([normal[cols], s1[cols], s2[cols]], ignore_index=True)
    segs["seg_hours"] = (segs["seg_end"] - segs["seg_start"]).dt.total_seconds() / 3600
    segs["week_start"] = _week_start_of(segs["seg_start"]).dt.strftime("%Y-%m-%d")

    # Merge employees — gives post-transfer department for transferred employees
    segs = segs.merge(emp[["employee_id", "department_id", "hourly_rate"]], on="employee_id")

    # Trap 6: correct department attribution for pre-transfer segments
    for _, tr in transfers.iterrows():
        mask = (segs["employee_id"] == tr["employee_id"]) & \
               (segs["seg_start"] < tr["effective_date"])
        segs.loc[mask, "department_id"] = tr["from_dept_id"]

    segs = segs.merge(ot_policy, on="department_id")

    # Trap 1: effective rate per segment (1.3× for night shifts)
    segs["eff_rate"] = segs["hourly_rate"] * np.where(segs["is_night"], NIGHT_RATE_MULT, 1.0)

    # Chronological OT allocation within each employee-week
    segs = segs.sort_values(["employee_id", "week_start", "seg_start"]).reset_index(drop=True)
    segs["cum_before"] = (segs.groupby(["employee_id", "week_start"])["seg_hours"]
                             .cumsum() - segs["seg_hours"])
    thr = segs["weekly_ot_threshold"]
    segs["regular"] = np.minimum(segs["seg_hours"], np.maximum(0.0, thr - segs["cum_before"]))
    segs["ot_seg"]  = segs["seg_hours"] - segs["regular"]
    segs["seg_cost"] = segs["regular"] * segs["eff_rate"] + \
                       segs["ot_seg"]  * segs["eff_rate"] * OT_MULT

    emp_wk = (segs.groupby(["employee_id", "department_id", "week_start"])
              .agg(actual_cost=("seg_cost", "sum"), ot_hours=("ot_seg", "sum"))
              .reset_index())

    # Reassignment: deduct base-rate hours from home, credit to receiving
    rc = reassignments.merge(emp[["employee_id", "hourly_rate"]], on="employee_id")
    rc["reass_cost"] = rc["reassigned_hours"] * rc["hourly_rate"]

    home_ded = (rc.groupby(["employee_id", "week_start"])["reass_cost"]
                  .sum().reset_index().rename(columns={"reass_cost": "home_deduction"}))
    emp_wk = emp_wk.merge(home_ded, on=["employee_id", "week_start"], how="left")
    emp_wk["home_deduction"] = emp_wk["home_deduction"].fillna(0.0)
    emp_wk["home_cost"] = emp_wk["actual_cost"] - emp_wk["home_deduction"]

    home_agg = (emp_wk.groupby(["department_id", "week_start"])
                .agg(actual_cost=("home_cost", "sum"), ot_hours=("ot_hours", "sum"))
                .reset_index())
    recv_agg = (rc.groupby(["to_dept_id", "week_start"])["reass_cost"]
                  .sum().reset_index()
                  .rename(columns={"to_dept_id": "department_id", "reass_cost": "recv_cost"}))

    dw = home_agg.merge(recv_agg, on=["department_id", "week_start"], how="left")
    dw["recv_cost"]   = dw["recv_cost"].fillna(0.0)
    dw["actual_cost"] = dw["actual_cost"] + dw["recv_cost"]
    dw = dw.drop(columns=["recv_cost"])

    bud = budget.copy()
    bud["budgeted_cost"] = bud["budgeted_hours"] * bud["avg_hourly_rate"]
    report = bud.merge(dw, on=["department_id", "week_start"])

    return {
        "total_budgeted_cost":    float(round(report["budgeted_cost"].sum(), 2)),
        "total_actual_cost":      float(round(report["actual_cost"].sum(),   2)),
        "total_variance":         float(round(
            report["actual_cost"].sum() - report["budgeted_cost"].sum(), 2)),
        "total_overtime_hours":   float(round(dw["ot_hours"].sum(), 2)),
        "over_budget_week_count": int((report["actual_cost"] > report["budgeted_cost"]).sum()),
        "_report": report,
        "_dw":     dw,
    }


# ── Scalar value correctness ──────────────────────────────────────────────────

def test_case_06_total_overtime_hours(ground_truth):
    """Wrong if night shift rate is not applied before computing OT premium,
    or if duration-outlier shifts are included (they generate spurious OT hours)."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["total_overtime_hours"] - ground_truth["total_overtime_hours"]) <= 1.0, \
        (f"total_overtime_hours: got {s['total_overtime_hours']}, "
         f"expected {ground_truth['total_overtime_hours']} (±1.0)")


def test_case_07_total_actual_cost(ground_truth):
    """Wrong if night shift premium, contractor cents, or duration outliers are missed."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["total_actual_cost"] - ground_truth["total_actual_cost"]) <= 1.0, \
        (f"total_actual_cost: got {s['total_actual_cost']}, "
         f"expected {ground_truth['total_actual_cost']} (±1.0)")


# ── Row-level correctness ─────────────────────────────────────────────────────

def test_case_08_night_shift_premium_magnitude(ground_truth):
    """Night premium must add meaningfully to actual costs vs a flat-rate calculation."""
    shifts    = pd.read_csv(DATA_DIR / "shifts.csv", parse_dates=["shift_start", "shift_end"])
    employees = pd.read_csv(DATA_DIR / "employees.csv")
    emp = employees.copy()
    emp.loc[emp["employee_type"] == "contractor", "hourly_rate"] /= 100.0

    # Only consider valid shifts
    durations = (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    valid_shifts = shifts[durations <= MAX_SHIFT_HOURS]

    total_night_hours = 0.0
    night_shifts = valid_shifts[valid_shifts["shift_start"].dt.hour >= NIGHT_START_HOUR]
    for _, row in night_shifts.iterrows():
        h = (row["shift_end"] - row["shift_start"]).total_seconds() / 3600
        total_night_hours += h

    avg_rate = emp["hourly_rate"].mean()
    min_expected_premium = total_night_hours * avg_rate * (NIGHT_RATE_MULT - 1.0) * 0.5

    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["total_actual_cost"] >= ground_truth["total_actual_cost"] - min_expected_premium, \
        ("total_actual_cost is too low — night shift premium (1.3× for shifts starting "
         f"at or after {NIGHT_START_HOUR}:00) appears to be missing or underapplied.")


def test_case_09_d05_d08_ot_thresholds(ground_truth):
    """D05 (36h) and D08 (44h) costs must reflect their non-standard OT thresholds."""
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["_report"]
    for dept in ["D05", "D08"]:
        for ws in gt["week_start"].unique():
            gt_row  = gt  [(gt  ["department_id"] == dept) & (gt  ["week_start"] == ws)]
            rep_row = report[(report["department_id"] == dept) & (report["week_start"] == ws)]
            if gt_row.empty or rep_row.empty:
                continue
            exp = float(round(gt_row["actual_cost"].iloc[0], 2))
            got = float(rep_row["actual_cost"].iloc[0])
            assert abs(got - exp) <= 1.0, \
                f"{dept} actual_cost wrong for {ws}: got {got:.2f}, expected {exp:.2f} (±1.0)"


def test_case_10_reassignment_and_row_accuracy(ground_truth):
    """At least 120 of 130 department-week rows must match ground truth within ±1.0."""
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["_report"]
    merged = report.merge(
        gt[["department_id", "week_start", "actual_cost"]],
        on=["department_id", "week_start"], suffixes=("_got", "_exp"),
    )
    close = (abs(merged["actual_cost_got"] - merged["actual_cost_exp"]) <= 1.0).sum()
    assert close >= 120, \
        f"Only {close}/130 rows match ground truth within ±1.0."


def test_case_11_pre_transfer_department_attribution(ground_truth):
    """Shifts before an employee's transfer date must be attributed to from_dept_id.

    employees.csv reflects each employee's current (post-transfer) department.
    transfers.csv records the effective date and the department they came from.
    Using employees.csv as a static department lookup misattributes five weeks
    of pre-transfer labor to the wrong department.
    """
    transfers = pd.read_csv(DATA_DIR / "transfers.csv", parse_dates=["effective_date"])
    report    = pd.read_csv(REPORT_PATH)
    gt        = ground_truth["_report"]

    # Check from_dept rows in the five pre-transfer weeks (2024-01-01 through 2024-01-29)
    pre_transfer_weeks = pd.date_range("2024-01-01", periods=5, freq="7D") \
                           .strftime("%Y-%m-%d").tolist()
    from_depts = transfers["from_dept_id"].unique()

    errors = []
    for dept in from_depts:
        for ws in pre_transfer_weeks:
            gt_row  = gt  [(gt  ["department_id"] == dept) & (gt  ["week_start"] == ws)]
            rep_row = report[(report["department_id"] == dept) & (report["week_start"] == ws)]
            if gt_row.empty or rep_row.empty:
                continue
            exp = float(round(gt_row["actual_cost"].iloc[0], 2))
            got = float(rep_row["actual_cost"].iloc[0])
            if abs(got - exp) > 5.0:
                errors.append(f"  {dept}/{ws}: got {got:.2f}, expected {exp:.2f}")

    assert not errors, (
        f"{len(errors)} from_dept rows have incorrect costs in pre-transfer weeks. "
        f"Shifts before the effective_date in transfers.csv must be charged to from_dept_id:\n"
        + "\n".join(errors[:5])
    )
