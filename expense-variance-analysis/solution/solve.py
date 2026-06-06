"""
Q1 2024 warehouse payroll variance report.

Key observations from the data:

1. Night shifts (shift_start hour >= 21:00) are paid at 1.3× base rate.
   Overtime on night shifts is 1.3 × 1.5 = 1.95× base. An agent that skips
   the shift_start hour check undercounts actual cost by ~$90k+.

2. Some night shifts start on Sunday night and end Monday morning, crossing
   the weekly boundary. Hours must be split at Monday 00:00 to be attributed
   to the correct week.

3. Contractor hourly_rate values are in US cents (1713–2796 vs $17–$28 for
   full-time). Divide by 100 before any cost calculation.

4. D05 uses a 36h/week OT threshold; D08 uses 44h/week (from ot_policy.csv).

5. shifts.csv contains ~20 records with duration > 16 h (clock-out date entry
   errors). These must be excluded before computing costs.

6. transfers.csv records employees who moved departments on 2024-02-05.
   employees.csv shows their current (post-transfer) department. Shifts before
   the effective date must be attributed to from_dept_id.
"""

import json
import os
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

NIGHT_START_HOUR = 21
NIGHT_RATE_MULT  = 1.3
OT_MULT          = 1.5
MAX_SHIFT_HOURS  = 16.0   # shifts longer than this are data-entry errors


def load_data():
    return (
        pd.read_csv(DATA_DIR / "shifts.csv",       parse_dates=["shift_start", "shift_end"]),
        pd.read_csv(DATA_DIR / "employees.csv"),
        pd.read_csv(DATA_DIR / "departments.csv"),
        pd.read_csv(DATA_DIR / "ot_policy.csv"),
        pd.read_csv(DATA_DIR / "reassignments.csv"),
        pd.read_csv(DATA_DIR / "weekly_budget.csv"),
        pd.read_csv(DATA_DIR / "transfers.csv",    parse_dates=["effective_date"]),
    )


def fix_contractor_rates(employees):
    emp  = employees.copy()
    mask = emp["employee_type"] == "contractor"
    emp.loc[mask, "hourly_rate"] /= 100.0
    return emp


def filter_duration_outliers(shifts):
    hours = (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    return shifts[hours <= MAX_SHIFT_HOURS].copy()


def week_start_of(ts):
    return (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.normalize()


def build_segments(shifts):
    """Tag night shifts, split cross-Monday shifts, return per-segment rows."""
    sh = shifts.copy()
    sh["is_night"] = sh["shift_start"].dt.hour >= NIGHT_START_HOUR

    crosses = week_start_of(sh["shift_start"]) != week_start_of(sh["shift_end"])
    normal  = sh[~crosses].copy()
    normal["seg_start"] = normal["shift_start"]
    normal["seg_end"]   = normal["shift_end"]

    xw = sh[crosses].copy()
    xw["boundary"] = week_start_of(xw["shift_end"])
    s1 = xw.copy(); s1["seg_start"] = xw["shift_start"]; s1["seg_end"] = xw["boundary"]
    s2 = xw.copy(); s2["seg_start"] = xw["boundary"];    s2["seg_end"] = xw["shift_end"]

    cols = ["employee_id", "is_night", "seg_start", "seg_end"]
    segs = pd.concat([normal[cols], s1[cols], s2[cols]], ignore_index=True)
    segs["seg_hours"] = (segs["seg_end"] - segs["seg_start"]).dt.total_seconds() / 3600
    segs["week_start"] = week_start_of(segs["seg_start"]).dt.strftime("%Y-%m-%d")
    return segs


def apply_transfer_corrections(segs, transfers):
    """Override department_id for segments that predate each employee's transfer."""
    for _, tr in transfers.iterrows():
        mask = (segs["employee_id"] == tr["employee_id"]) & \
               (segs["seg_start"] < tr["effective_date"])
        segs.loc[mask, "department_id"] = tr["from_dept_id"]
    return segs


def compute_weekly_costs(segs, employees, ot_policy, transfers):
    """Per employee-week cost with night-shift rate and chronological OT allocation."""
    segs = segs.merge(employees[["employee_id", "department_id", "hourly_rate"]],
                      on="employee_id")
    segs = apply_transfer_corrections(segs, transfers)
    segs = segs.merge(ot_policy, on="department_id")

    segs["eff_rate"] = segs["hourly_rate"] * np.where(segs["is_night"], NIGHT_RATE_MULT, 1.0)

    segs = segs.sort_values(["employee_id", "week_start", "seg_start"]).reset_index(drop=True)
    segs["cum_before"] = (segs.groupby(["employee_id", "week_start"])["seg_hours"]
                             .cumsum() - segs["seg_hours"])
    thr = segs["weekly_ot_threshold"]
    segs["regular"]  = np.minimum(segs["seg_hours"], np.maximum(0.0, thr - segs["cum_before"]))
    segs["ot_seg"]   = segs["seg_hours"] - segs["regular"]
    segs["seg_cost"] = (segs["regular"] * segs["eff_rate"] +
                        segs["ot_seg"]  * segs["eff_rate"] * OT_MULT)

    return (segs.groupby(["employee_id", "department_id", "week_start"])
            .agg(actual_cost=("seg_cost", "sum"), ot_hours=("ot_seg", "sum"))
            .reset_index())


def apply_reassignments(emp_wk, employees, reassignments):
    """Deduct reassigned hours (base rate) from home dept; credit to receiving dept."""
    rc = reassignments.merge(employees[["employee_id", "hourly_rate"]], on="employee_id")
    rc["reass_cost"] = rc["reassigned_hours"] * rc["hourly_rate"]

    home_ded = (rc.groupby(["employee_id", "week_start"])["reass_cost"]
                  .sum().reset_index().rename(columns={"reass_cost": "home_deduction"}))
    wh = emp_wk.merge(home_ded, on=["employee_id", "week_start"], how="left")
    wh["home_deduction"] = wh["home_deduction"].fillna(0.0)
    wh["home_cost"] = wh["actual_cost"] - wh["home_deduction"]

    home_agg = (wh.groupby(["department_id", "week_start"])
                .agg(actual_cost=("home_cost", "sum"), ot_hours=("ot_hours", "sum"))
                .reset_index())
    recv_agg = (rc.groupby(["to_dept_id", "week_start"])["reass_cost"]
                  .sum().reset_index()
                  .rename(columns={"to_dept_id": "department_id", "reass_cost": "recv_cost"}))

    dw = home_agg.merge(recv_agg, on=["department_id", "week_start"], how="left")
    dw["recv_cost"]   = dw["recv_cost"].fillna(0.0)
    dw["actual_cost"] = dw["actual_cost"] + dw["recv_cost"]
    return dw.drop(columns=["recv_cost"])


def build_report(dept_week, budget, depts):
    bud = budget.copy()
    bud["budgeted_cost"] = bud["budgeted_hours"] * bud["avg_hourly_rate"]
    report = bud.merge(dept_week, on=["department_id", "week_start"])
    report = report.merge(depts, on="department_id")
    report["variance"] = report["actual_cost"] - report["budgeted_cost"]
    return (report[["department_id", "department_name", "week_start",
                    "budgeted_cost", "actual_cost", "variance"]]
            .sort_values(["department_id", "week_start"])
            .reset_index(drop=True))


def main():
    shifts, employees, depts, ot_policy, reassignments, budget, transfers = load_data()
    employees = fix_contractor_rates(employees)
    shifts    = filter_duration_outliers(shifts)
    segs      = build_segments(shifts)
    emp_wk    = compute_weekly_costs(segs, employees, ot_policy, transfers)
    dept_week = apply_reassignments(emp_wk, employees, reassignments)
    report    = build_report(dept_week, budget, depts)

    report.to_csv(WORKSPACE_DIR / "payroll_variance_report.csv", index=False)

    summary = {
        "total_budgeted_cost":    float(round(report["budgeted_cost"].sum(), 2)),
        "total_actual_cost":      float(round(report["actual_cost"].sum(),   2)),
        "total_variance":         float(round(
            report["actual_cost"].sum() - report["budgeted_cost"].sum(), 2)),
        "total_overtime_hours":   float(round(dept_week["ot_hours"].sum(), 2)),
        "over_budget_week_count": int((report["variance"] > 0).sum()),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
