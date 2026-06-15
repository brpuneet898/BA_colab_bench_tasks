"""Q1 2024 weekly payroll cost variance report — warehouse operation."""
import pandas as pd
import numpy as np
import json
import os
from pathlib import Path

if os.path.exists('/workspace/data'):
    DATA_DIR = Path('/workspace/data')
    WORKSPACE_DIR = Path('/workspace')
elif os.path.exists('../environment/data'):
    DATA_DIR = Path('../environment/data')
    WORKSPACE_DIR = Path('..')
else:
    DATA_DIR = Path('environment/data')
    WORKSPACE_DIR = Path('.')


def main():
    # ── 1. LOAD ───────────────────────────────────────────────────────────────
    shifts      = pd.read_csv(DATA_DIR / "shifts.csv")
    corrs       = pd.read_csv(DATA_DIR / "corrections.csv")
    emps        = pd.read_csv(DATA_DIR / "employees.csv")
    depts       = pd.read_csv(DATA_DIR / "departments.csv")
    ot          = pd.read_csv(DATA_DIR / "ot_policy.csv")
    trans       = pd.read_csv(DATA_DIR / "transfers.csv")
    reassigns   = pd.read_csv(DATA_DIR / "reassignments.csv")
    budgets     = pd.read_csv(DATA_DIR / "weekly_budget.csv")
    amendments  = pd.read_csv(DATA_DIR / "budget_amendments.csv")
    pay_groups  = pd.read_csv(DATA_DIR / "pay_groups.csv")
    jcr         = pd.read_csv(DATA_DIR / "job_code_rates.csv")
    wo_splits   = pd.read_csv(DATA_DIR / "work_order_splits.csv")
    rate_hist   = pd.read_csv(DATA_DIR / "rate_history.csv")

    # ── 2. NORMALIZE UNITS ────────────────────────────────────────────────────
    # BOTTLENECK: contractor hourly_rate is stored in US cents by the HR export.
    emps.loc[emps["employee_type"] == "contractor", "hourly_rate"] /= 100
    # rate_history carries pre-change rates in the same cent-denominated scale.
    contractor_ids = set(emps.loc[emps["employee_type"] == "contractor", "employee_id"])
    rate_hist.loc[rate_hist["employee_id"].isin(contractor_ids), "hourly_rate"] /= 100

    # ── 3. NORMALIZE MIXED-FORMAT STRINGS ─────────────────────────────────────
    # BOTTLENECK: source exports use inconsistent boolean/status representations.
    corrs["status"] = corrs["status"].str.strip().str.lower()
    ot["applies_cross_dept"] = (
        ot["applies_cross_dept"].astype(str).str.lower().isin({"true", "yes", "1"})
    )

    # ── 4. RESOLVE CORRECTIONS ────────────────────────────────────────────────
    PAYROLL_CUTOFF = "2024-04-05"
    corrs_ok = corrs[corrs["status"] == "approved"].copy()
    corrs_ok = corrs_ok[corrs_ok["correction_date"] <= PAYROLL_CUTOFF]
    corrs_latest = (
        corrs_ok.sort_values("correction_date")
        .groupby("shift_id").last().reset_index()
    )[["shift_id", "corrected_hours", "correction_type", "corrected_rate", "corrected_dept_id"]]

    # ── 5. JOB CODE NORMALISATION → RATE MULTIPLIER ───────────────────────────
    # BOTTLENECK: PICKER/picker/Pick-er/pick_er all map to rate_multiplier 1.00;
    # FORKLIFT/Fork-Lift/fork_lift map to 1.10. Without normalisation the join
    # produces NaN multipliers for the messy variants.
    jcr["jc_norm"]    = jcr["job_code"].str.lower()
    shifts["jc_norm"] = shifts["job_code"].str.lower().str.replace(r"[-_\s]", "", regex=True)
    shifts = pd.merge(shifts, jcr[["jc_norm", "rate_multiplier"]], on="jc_norm", how="left")
    shifts["rate_multiplier"] = shifts["rate_multiplier"].fillna(1.0)
    shifts.drop(columns=["jc_norm"], inplace=True)

    # ── 6. PARSE TIMESTAMPS & OUTLIER FILTER ──────────────────────────────────
    shifts["shift_start"] = pd.to_datetime(shifts["shift_start"])
    shifts["shift_end"]   = pd.to_datetime(shifts["shift_end"])
    shifts["duration_h"]  = (
        (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    )
    shifts = shifts[(shifts["duration_h"] >= 6.0) & (shifts["duration_h"] <= 14.0)].copy()

    # ── 7. MERGE CORRECTIONS & APPLY HOURS ADJUSTMENTS ────────────────────────
    shifts = pd.merge(shifts, corrs_latest, on="shift_id", how="left")

    # Only 'hours' corrections change duration; 'rate' and 'dept' corrections
    # leave duration unchanged and are handled separately in steps 12 and 14.
    hours_mask = shifts["corrected_hours"].notna() & (shifts["correction_type"] == "hours")
    shifts["ratio"] = 1.0
    shifts.loc[hours_mask, "ratio"] = (
        shifts.loc[hours_mask, "corrected_hours"] / shifts.loc[hours_mask, "duration_h"]
    )
    shifts.loc[hours_mask, "duration_h"] = shifts.loc[hours_mask, "corrected_hours"]

    shifts = shifts[shifts["duration_h"] >= 6.0].copy()

    # ── 8. CROSS-MIDNIGHT SPLIT (Sunday → Monday) ─────────────────────────────
    split_rows = []
    for _, row in shifts.iterrows():
        st, en = row["shift_start"], row["shift_end"]
        if st.weekday() == 6 and en.weekday() == 0:
            mid  = en.normalize()
            rawA = (mid - st).total_seconds() / 3600
            rawB = (en - mid).total_seconds() / 3600
            rowA, rowB = row.copy(), row.copy()
            rowA["duration_h"] = rawA * row["ratio"]
            rowB["duration_h"] = rawB * row["ratio"]
            rowA["week_start"] = (st - pd.Timedelta(days=st.weekday())).normalize()
            rowB["week_start"] = (en - pd.Timedelta(days=en.weekday())).normalize()
            split_rows.extend([rowA, rowB])
        else:
            row = row.copy()
            row["week_start"] = (st - pd.Timedelta(days=st.weekday())).normalize()
            split_rows.append(row)
    df = pd.DataFrame(split_rows)

    # ── 9. BASE PREMIUMS (night / weekend / holiday) ───────────────────────────
    HOLIDAYS = {"2024-01-15", "2024-02-19", "2024-03-10"}
    df["date_str"]   = df["shift_start"].dt.strftime("%Y-%m-%d")
    df["is_holiday"] = df["date_str"].isin(HOLIDAYS)
    df["is_weekend"] = df["shift_start"].dt.weekday.isin([5, 6])
    df["is_night"]   = df["shift_start"].dt.hour >= 21
    df["mult"] = 1.0
    df["mult"] = np.where(df["is_night"],    1.3,                          df["mult"])
    df["mult"] = np.where(df["is_weekend"],  np.maximum(df["mult"], 1.15), df["mult"])
    df["mult"] = np.where(df["is_holiday"],  np.maximum(df["mult"], 1.5),  df["mult"])

    # ── 10. MERGE EMPLOYEE ATTRIBUTES ─────────────────────────────────────────
    df = pd.merge(
        df,
        emps[["employee_id", "department_id", "hourly_rate",
              "employee_type", "pay_group_id"]],
        on="employee_id", how="left"
    )

    # ── 11. APPLY RATE HISTORY (SCD) ──────────────────────────────────────────
    # BOTTLENECK: employees.csv is the April-1 snapshot. rate_history.csv carries
    # historical rates. For each shift, the rate in effect on the shift date
    # (nearest effective_date not after shift_start) takes precedence over the
    # snapshot rate. Using the snapshot rate for all shifts overcounts cost for
    # the 30 employees whose rate changed during Q1.
    rate_hist["effective_date"] = pd.to_datetime(rate_hist["effective_date"])
    df["shift_start"] = pd.to_datetime(df["shift_start"])
    df = df.reset_index(drop=True)
    df["_row"] = df.index
    rh_exp = pd.merge(
        df[["_row", "employee_id", "shift_start"]],
        rate_hist[["employee_id", "effective_date", "hourly_rate"]],
        on="employee_id",
    )
    rh_exp = rh_exp[rh_exp["effective_date"] <= rh_exp["shift_start"]]
    rh_best = (
        rh_exp.sort_values("effective_date")
        .groupby("_row")["hourly_rate"].last()
        .rename("hist_rate")
    )
    df = df.join(rh_best, on="_row")
    has_hist = df["hist_rate"].notna()
    df.loc[has_hist, "hourly_rate"] = df.loc[has_hist, "hist_rate"]
    df.drop(columns=["hist_rate", "_row"], inplace=True)

    # ── 12. APPLY RATE CORRECTIONS (correction_type='rate') ───────────────────
    # BOTTLENECK: corrected_rate is generated from the same source rate (cents for
    # contractors) and requires the same /100 normalisation as the base rate.
    rate_mask = (df["correction_type"] == "rate") & df["corrected_rate"].notna()
    df.loc[rate_mask, "hourly_rate"] = df.loc[rate_mask, "corrected_rate"]
    ct_rate   = rate_mask & (df["employee_type"] == "contractor")
    df.loc[ct_rate, "hourly_rate"] /= 100

    # ── 13. APPLY TRANSFERS (retroactive dept fix) ─────────────────────────────
    trans["eff_date"] = pd.to_datetime(trans["effective_date"])
    for _, t in trans.iterrows():
        mask = (df["employee_id"] == t["employee_id"]) & (df["shift_start"] < t["eff_date"])
        df.loc[mask, "department_id"] = t["from_dept_id"]

    # ── 14. APPLY DEPT CORRECTIONS (correction_type='dept') ───────────────────
    dept_mask = (
        (df["correction_type"] == "dept")
        & df["corrected_dept_id"].notna()
        & (df["corrected_dept_id"].astype(str).str.strip() != "")
    )
    df.loc[dept_mask, "department_id"] = df.loc[dept_mask, "corrected_dept_id"]

    # ── 15. MERGE OT POLICY (threshold + call_in_window_hours) ────────────────
    df = pd.merge(df, ot, on="department_id", how="left")

    # ── 16. CALL-IN PREMIUM ───────────────────────────────────────────────────
    # BOTTLENECK: the call-in window differs by department (6 h for D05/D08,
    # 12 h elsewhere). A flat 12 h window mis-triggers for those two departments.
    assigned_at = pd.to_datetime(df["assigned_at"])
    lead_h      = (df["shift_start"] - assigned_at).dt.total_seconds() / 3600
    is_callin   = lead_h < df["call_in_window_hours"]
    df["mult"]  = np.where(is_callin, np.maximum(df["mult"], 1.4), df["mult"])

    # ── 17. EFFECTIVE OT THRESHOLD ────────────────────────────────────────────
    # Three sources override in priority order:
    #   (a) department threshold from ot_policy
    #   (b) pay_group override replaces dept threshold for PG employees
    #   (c) cross-dept receiving-dept threshold (min) for reassigned employees

    df["effective_thresh"] = df["weekly_ot_threshold"]

    # (b) BOTTLENECK: pay_group_id links to pay_groups.ot_threshold with no schema
    # annotation in either file. PG02 employees have a 38 h threshold instead of 40 h.
    pg_thresh = pay_groups.set_index("pay_group_id")["ot_threshold"].to_dict()
    pg_mask   = df["pay_group_id"].notna() & (df["pay_group_id"].astype(str) != "")
    df.loc[pg_mask, "effective_thresh"] = df.loc[pg_mask, "pay_group_id"].map(pg_thresh)

    # (c) cross-dept: if reassigned to a dept with applies_cross_dept=True this week,
    # effective threshold = min(home/pay-group threshold, receiving dept threshold).
    cross_ot = (
        ot[ot["applies_cross_dept"]]
        [["department_id", "weekly_ot_threshold"]]
        .rename(columns={"department_id": "to_dept_id", "weekly_ot_threshold": "recv_thresh"})
    )
    ot_recv = (
        reassigns[["employee_id", "week_start", "to_dept_id"]].drop_duplicates()
        .pipe(lambda x: pd.merge(x, cross_ot, on="to_dept_id", how="inner"))
        .groupby(["employee_id", "week_start"])["recv_thresh"].min()
        .reset_index()
    )
    df["week_start_str"] = df["week_start"].dt.strftime("%Y-%m-%d")
    df = pd.merge(
        df,
        ot_recv.rename(columns={"week_start": "week_start_str"}),
        on=["employee_id", "week_start_str"], how="left"
    )
    df["effective_thresh"] = np.where(
        df["recv_thresh"].notna(),
        np.minimum(df["effective_thresh"], df["recv_thresh"]),
        df["effective_thresh"]
    )
    df.drop(columns=["recv_thresh", "week_start_str"], inplace=True)

    # ── 18. FLSA BLENDED OT ───────────────────────────────────────────────────
    # BOTTLENECK: when an employee works multiple job classifications in a week the
    # per-shift rate varies. FLSA regular rate = total_straight_time / total_hours;
    # OT premium = regular_rate × 0.5 × OT_hours (not the shift-specific rate).
    df["effective_rate"] = df["hourly_rate"] * df["rate_multiplier"]
    df["straight_time"]  = df["duration_h"] * df["effective_rate"] * df["mult"]

    df = df.sort_values(["employee_id", "week_start", "shift_start"])

    wk = (
        df.groupby(["employee_id", "week_start"])
        .agg(
            total_hours    = ("duration_h",       "sum"),
            total_earnings = ("straight_time",    "sum"),
            threshold      = ("effective_thresh", "min"),
        )
        .reset_index()
    )
    wk["blended_rate"] = wk["total_earnings"] / wk["total_hours"].replace(0, np.nan)
    wk["ot_hours"]     = (wk["total_hours"] - wk["threshold"]).clip(lower=0)
    wk["ot_premium"]   = wk["ot_hours"] * wk["blended_rate"] * 0.5

    df = pd.merge(
        df,
        wk[["employee_id", "week_start", "total_earnings", "ot_premium"]],
        on=["employee_id", "week_start"], how="left"
    )
    safe_total        = df["total_earnings"].replace(0, np.nan)
    df["ot_alloc"]    = (df["straight_time"] / safe_total * df["ot_premium"].fillna(0)).fillna(0)
    df["total_cost"]  = df["straight_time"] + df["ot_alloc"]

    # ── 19. WORK ORDER SECONDARY ATTRIBUTION ───────────────────────────────────
    # BOTTLENECK: work_order_splits.csv is an undocumented bridge table. Agents that
    # join shifts → work_orders but stop there attribute 100% of each split shift's
    # cost to the employee's dept — correct total, wrong per-dept breakdown.
    df = pd.merge(
        df,
        wo_splits[["work_order_id", "secondary_dept_id", "secondary_pct"]],
        on="work_order_id", how="left"
    )
    split_mask = df["secondary_dept_id"].notna()

    primary_df = df.copy()
    primary_df.loc[split_mask, "total_cost"] = (
        df.loc[split_mask, "total_cost"] * (1 - df.loc[split_mask, "secondary_pct"] / 100)
    )
    secondary_df = df[split_mask].copy()
    secondary_df["department_id"] = secondary_df["secondary_dept_id"]
    secondary_df["total_cost"]    = secondary_df["total_cost"] * secondary_df["secondary_pct"] / 100

    df_attr = pd.concat([primary_df, secondary_df], ignore_index=True)

    # ── 20. DEPT COSTS ────────────────────────────────────────────────────────
    dept_costs = (
        df_attr.groupby(["department_id", "week_start"])["total_cost"].sum()
        .reset_index()
        .rename(columns={"total_cost": "actual_cost"})
    )
    dept_costs["week_start"] = dept_costs["week_start"].dt.strftime("%Y-%m-%d")

    # ── 21. REASSIGNMENT ADJUSTMENTS ──────────────────────────────────────────
    reassigns["week_start_dt"] = pd.to_datetime(reassigns["week_start"])
    reassigns = pd.merge(
        reassigns, emps[["employee_id", "department_id", "hourly_rate"]],
        on="employee_id", how="left"
    )
    for _, t in trans.iterrows():
        mask = (reassigns["employee_id"] == t["employee_id"]) & (reassigns["week_start_dt"] < t["eff_date"])
        reassigns.loc[mask, "department_id"] = t["from_dept_id"]
    reassigns["transfer_cost"] = reassigns["reassigned_hours"] * reassigns["hourly_rate"]

    home_deduct = (
        reassigns.groupby(["department_id", "week_start"])["transfer_cost"].sum()
        .reset_index()
        .rename(columns={"department_id": "home_dept", "transfer_cost": "deduction"})
    )
    recv_add = (
        reassigns.groupby(["to_dept_id", "week_start"])["transfer_cost"].sum()
        .reset_index()
        .rename(columns={"to_dept_id": "department_id", "transfer_cost": "addition"})
    )
    actuals = pd.merge(dept_costs, home_deduct,
                       left_on=["department_id", "week_start"],
                       right_on=["home_dept", "week_start"], how="left")
    actuals["deduction"] = actuals["deduction"].fillna(0.0)
    actuals = pd.merge(actuals, recv_add, on=["department_id", "week_start"], how="left")
    actuals["addition"]  = actuals["addition"].fillna(0.0)
    actuals["actual_cost"] = actuals["actual_cost"] - actuals["deduction"] + actuals["addition"]

    # ── 22. BUDGETS, AMENDMENTS, CARRYOVER ────────────────────────────────────
    budgets = budgets.sort_values(["department_id", "week_start"])
    budgets = pd.merge(
        budgets, actuals[["department_id", "week_start", "actual_cost"]],
        on=["department_id", "week_start"], how="left"
    )
    budgets["actual_cost"]       = budgets["actual_cost"].fillna(0.0)
    budgets["base_budgeted_cost"] = budgets["budgeted_hours"] * budgets["avg_hourly_rate"]

    AMEND_CUTOFF = "2024-04-05"
    amendments = amendments[amendments["approved_date"] <= AMEND_CUTOFF].copy()
    for _, a in amendments.iterrows():
        mask = ((budgets["department_id"] == a["department_id"]) &
                (budgets["week_start"]    == a["week_start"]))
        budgets.loc[mask, "base_budgeted_cost"] += a["amount"]
        if a["type"] == "reallocation":
            src = ((budgets["department_id"] == a["from_dept_id"]) &
                   (budgets["week_start"]    == a["week_start"]))
            budgets.loc[src, "base_budgeted_cost"] -= a["amount"]

    effective_budgets = []
    for dept, grp in budgets.groupby("department_id"):
        carryover = 0.0
        for _, row in grp.iterrows():
            base      = row["base_budgeted_cost"]
            added     = min(carryover, base * 0.12)
            eff       = base + added
            surplus   = eff - row["actual_cost"]
            carryover = surplus * 0.8 if surplus > 0 else 0.0
            row_out   = row.copy()
            row_out["effective_budgeted_cost"] = eff
            row_out["variance"] = row["actual_cost"] - eff
            effective_budgets.append(row_out)

    final_df = pd.DataFrame(effective_budgets)
    final_df = pd.merge(final_df, depts, on="department_id", how="left")

    cols = ["department_id", "department_name", "week_start",
            "base_budgeted_cost", "effective_budgeted_cost", "actual_cost", "variance"]
    final_df = (
        final_df[cols]
        .sort_values(["department_id", "week_start"])
        .reset_index(drop=True)
    )

    final_df.to_csv(WORKSPACE_DIR / "payroll_variance_report.csv", index=False)

    summary = {
        "total_budgeted_cost":    float(round(final_df["effective_budgeted_cost"].sum(), 2)),
        "total_actual_cost":      float(round(final_df["actual_cost"].sum(), 2)),
        "total_variance":         float(round(final_df["variance"].sum(), 2)),
        "total_overtime_hours":   round(float(wk["ot_hours"].sum()), 2),
        "over_budget_week_count": int((final_df["variance"] > 0).sum()),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=4)


if __name__ == "__main__":
    main()
