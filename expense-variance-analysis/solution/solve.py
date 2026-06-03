"""
Q1 2024 payroll variance report for the warehouse.

Two data quality issues stood out when I first loaded everything:

shift_end is stored in UTC but shift_start is US Eastern time with no timezone
info. This means you cannot just subtract them — you need to convert shift_end
to ET first. DST starts on 2024-03-10 at 07:00 UTC, so timestamps before that
get a -5h offset (EST) and those after get -4h (EDT). Without this, any shift
that straddles the DST boundary has a wrong duration, and some cross-week shifts
land in the wrong week.

Contractor hourly rates in employees.csv are in US cents, not dollars. I caught
this because the values were 50-100x larger than the full-time rates. Divided by
100, they fall in a reasonable $17-$28/hr range consistent with the FT staff.

Everything else follows the spec: department-specific OT thresholds (D05 at 36h,
D08 at 44h versus the standard 40h), reassignment hours billed to the receiving
department at the employee's base rate, with the home department keeping the OT
premium.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR = Path("/workspace/data")
WORKSPACE_DIR = Path("/workspace")

# BOTTLENECK: DST spring-forward 2024-03-10 02:00 ET -> 03:00 ET equals
# 07:00 UTC. Shifts ending before this moment use EST (-5h); those ending
# after use EDT (-4h). Using a flat 5h or 4h offset for all shifts would
# silently misassign ~1,000 cross-week shifts to the wrong Monday week.
DST_BOUNDARY_UTC = pd.Timestamp("2024-03-10 07:00:00")


def load_data():
    shifts = pd.read_csv(
        DATA_DIR / "shifts.csv", parse_dates=["shift_start", "shift_end"]
    )
    employees = pd.read_csv(DATA_DIR / "employees.csv")
    depts = pd.read_csv(DATA_DIR / "departments.csv")
    ot_policy = pd.read_csv(DATA_DIR / "ot_policy.csv")
    reassignments = pd.read_csv(DATA_DIR / "reassignments.csv")
    budget = pd.read_csv(DATA_DIR / "weekly_budget.csv")
    return shifts, employees, depts, ot_policy, reassignments, budget


def fix_contractor_rates(employees):
    # BOTTLENECK: contractor hourly_rate is stored in US cents by the HR export.
    # Applying it as-is overstates contractor labor cost by a factor of 100.
    emp = employees.copy()
    mask = emp["employee_type"] == "contractor"
    emp.loc[mask, "hourly_rate"] = emp.loc[mask, "hourly_rate"] / 100.0
    return emp


def localize_shift_end(shifts):
    off_hours = np.where(shifts["shift_end"] < DST_BOUNDARY_UTC, 5, 4)
    sh = shifts.copy()
    sh["shift_end_et"] = sh["shift_end"] - pd.to_timedelta(off_hours, unit="h")
    return sh


def week_start_of(ts_series):
    return (ts_series - pd.to_timedelta(ts_series.dt.weekday, unit="D")).dt.normalize()


def split_cross_week_shifts(shifts):
    """Split any shift whose ET end falls in a different Monday-week than its start."""
    start_wk = week_start_of(shifts["shift_start"])
    end_wk = week_start_of(shifts["shift_end_et"])
    crosses = start_wk != end_wk

    normal = shifts[~crosses].copy()
    normal["seg_start"] = normal["shift_start"]
    normal["seg_end"] = normal["shift_end_et"]

    xw = shifts[crosses].copy()
    xw["boundary"] = week_start_of(xw["shift_end_et"])
    seg1 = xw.copy()
    seg1["seg_start"] = xw["shift_start"]
    seg1["seg_end"] = xw["boundary"]
    seg2 = xw.copy()
    seg2["seg_start"] = xw["boundary"]
    seg2["seg_end"] = xw["shift_end_et"]

    segs = pd.concat([normal, seg1, seg2], ignore_index=True)

    # BOTTLENECK: naive ET subtraction overcounts by 1h for any segment that
    # spans the DST spring-forward (2024-03-10 02:00 ET → 03:00 ET). Converting
    # both endpoints to UTC before subtracting gives correct elapsed duration.
    # seg_end is shift_end_et or the Monday boundary — both are ET naive.
    _DST_ET = pd.Timestamp("2024-03-10 02:00:00")
    _start_off = pd.to_timedelta(np.where(segs["seg_start"] < _DST_ET, 5, 4), unit="h")
    _end_off = pd.to_timedelta(np.where(segs["seg_end"] < _DST_ET, 5, 4), unit="h")
    segs["hours"] = (
        (segs["seg_end"] + _end_off) - (segs["seg_start"] + _start_off)
    ).dt.total_seconds() / 3600

    segs["week_start"] = week_start_of(segs["seg_start"]).dt.strftime("%Y-%m-%d")
    return segs


def compute_weekly_costs(segments, employees, ot_policy):
    weekly_hrs = (
        segments.groupby(["employee_id", "week_start"])["hours"]
        .sum()
        .reset_index()
        .rename(columns={"hours": "total_hours"})
    )
    weekly_hrs = weekly_hrs.merge(
        employees[["employee_id", "department_id", "hourly_rate"]], on="employee_id"
    )
    # BOTTLENECK: each department has its own weekly OT threshold. Merging on
    # department_id (not using a global 40h) is required — D05 at 36h and D08
    # at 44h both shift the OT split, changing actual_cost noticeably.
    weekly_hrs = weekly_hrs.merge(ot_policy, on="department_id")
    weekly_hrs["regular_hours"] = np.minimum(
        weekly_hrs["total_hours"], weekly_hrs["weekly_ot_threshold"]
    )
    weekly_hrs["ot_hours"] = np.maximum(
        weekly_hrs["total_hours"] - weekly_hrs["weekly_ot_threshold"], 0.0
    )
    weekly_hrs["actual_cost"] = (
        weekly_hrs["regular_hours"] * weekly_hrs["hourly_rate"]
        + weekly_hrs["ot_hours"] * weekly_hrs["hourly_rate"] * 1.5
    )
    return weekly_hrs


def apply_reassignments(weekly_hrs, reassignments, employees):
    rc = reassignments.merge(employees[["employee_id", "hourly_rate"]], on="employee_id")
    rc["reass_cost"] = rc["reassigned_hours"] * rc["hourly_rate"]

    home_ded = (
        rc.groupby(["employee_id", "week_start"])["reass_cost"]
        .sum()
        .reset_index()
        .rename(columns={"reass_cost": "home_deduction"})
    )
    wh = weekly_hrs.merge(home_ded, on=["employee_id", "week_start"], how="left")
    wh["home_deduction"] = wh["home_deduction"].fillna(0.0)
    wh["home_cost"] = wh["actual_cost"] - wh["home_deduction"]

    home_agg = (
        wh.groupby(["department_id", "week_start"])
        .agg(actual_cost=("home_cost", "sum"), ot_hours=("ot_hours", "sum"))
        .reset_index()
    )
    recv_agg = (
        rc.groupby(["to_dept_id", "week_start"])["reass_cost"]
        .sum()
        .reset_index()
        .rename(columns={"to_dept_id": "department_id", "reass_cost": "recv_cost"})
    )
    dept_week = home_agg.merge(recv_agg, on=["department_id", "week_start"], how="left")
    dept_week["recv_cost"] = dept_week["recv_cost"].fillna(0.0)
    dept_week["actual_cost"] = dept_week["actual_cost"] + dept_week["recv_cost"]
    dept_week = dept_week.drop(columns=["recv_cost"])
    return dept_week


def build_report(dept_week, budget, depts):
    bud = budget.copy()
    bud["budgeted_cost"] = bud["budgeted_hours"] * bud["avg_hourly_rate"]
    report = bud.merge(dept_week, on=["department_id", "week_start"])
    report = report.merge(depts, on="department_id")
    report["variance"] = report["actual_cost"] - report["budgeted_cost"]
    report = (
        report[
            [
                "department_id",
                "department_name",
                "week_start",
                "budgeted_cost",
                "actual_cost",
                "variance",
            ]
        ]
        .sort_values(["department_id", "week_start"])
        .reset_index(drop=True)
    )
    return report


def main():
    shifts, employees, depts, ot_policy, reassignments, budget = load_data()

    employees = fix_contractor_rates(employees)
    shifts = localize_shift_end(shifts)
    segments = split_cross_week_shifts(shifts)
    weekly_hrs = compute_weekly_costs(segments, employees, ot_policy)
    dept_week = apply_reassignments(weekly_hrs, reassignments, employees)
    report = build_report(dept_week, budget, depts)

    report.to_csv(WORKSPACE_DIR / "payroll_variance_report.csv", index=False)

    summary = {
        "total_budgeted_cost": float(round(report["budgeted_cost"].sum(), 2)),
        "total_actual_cost": float(round(report["actual_cost"].sum(), 2)),
        "total_variance": float(
            round(report["actual_cost"].sum() - report["budgeted_cost"].sum(), 2)
        ),
        "total_overtime_hours": float(round(dept_week["ot_hours"].sum(), 2)),
        "over_budget_week_count": int((report["variance"] > 0).sum()),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
