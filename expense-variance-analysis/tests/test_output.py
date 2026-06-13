"""
Tests for expense-variance-analysis.

Reasoning challenges: (1) contractor rates in US cents require /100 normalisation;
(2) rate_history.csv is a slowly-changing-dimension table — employees.csv is the
April-1 snapshot and overstates cost for Jan 1–Feb 4 shifts for 30 employees;
(3) job_code variants (PICKER/picker/Pick-er/pick_er) must be normalised before
joining to job_code_rates.csv, and FLSA blended-rate OT applies when an employee
works multiple job codes in one week; (4) pay_group_id in employees.csv silently
overrides the department's OT threshold for 40 employees with no schema annotation;
(5) work_order_splits.csv is an undocumented bridge table — missing it produces a
plausible total cost but wrong per-department breakdown; (6) call-in premium (1.4×)
fires when shift was assigned within call_in_window_hours of start, which varies by
department; (7) corrections.csv has three distinct correction_type values — treating
all as hour adjustments silently mishandles ~28% of corrections; (8) rolling budget
carryover, premium exclusivity, cross-midnight split, budget amendments, and the
bidirectional reallocation type all interact with the new layers.

Tests verify: output existence (00), input-data sentinels (01, 11), shape/sort (02,
03), OT hours (04), effective-budget totals (05, 09), actual cost (06), variance
(07), over-budget week count (08), row-level accuracy (10).
"""
import pytest
import pandas as pd
import numpy as np
import json
import os
from pathlib import Path

if os.path.exists('/workspace/data'):
    DATA_DIR      = Path('/workspace/data')
    WORKSPACE_DIR = Path('/workspace')
elif os.path.exists('../environment/data'):
    DATA_DIR      = Path('../environment/data')
    WORKSPACE_DIR = Path('..')
else:
    DATA_DIR      = Path('environment/data')
    WORKSPACE_DIR = Path('.')


# --- Test 00: Output Existence ---
def test_case_00_outputs_exist():
    assert (WORKSPACE_DIR / "payroll_variance_report.csv").exists(), \
        "payroll_variance_report.csv not found"
    assert (WORKSPACE_DIR / "summary.json").exists(), \
        "summary.json not found"


@pytest.fixture(scope="module")
def truth():
    # ── 1. LOAD ───────────────────────────────────────────────────────────────
    shifts     = pd.read_csv(DATA_DIR / "shifts.csv")
    corrs      = pd.read_csv(DATA_DIR / "corrections.csv")
    emps       = pd.read_csv(DATA_DIR / "employees.csv")
    depts      = pd.read_csv(DATA_DIR / "departments.csv")
    ot         = pd.read_csv(DATA_DIR / "ot_policy.csv")
    trans      = pd.read_csv(DATA_DIR / "transfers.csv")
    reassigns  = pd.read_csv(DATA_DIR / "reassignments.csv")
    budgets    = pd.read_csv(DATA_DIR / "weekly_budget.csv")
    amendments = pd.read_csv(DATA_DIR / "budget_amendments.csv")
    pay_groups = pd.read_csv(DATA_DIR / "pay_groups.csv")
    jcr        = pd.read_csv(DATA_DIR / "job_code_rates.csv")
    wo_splits  = pd.read_csv(DATA_DIR / "work_order_splits.csv")
    rate_hist  = pd.read_csv(DATA_DIR / "rate_history.csv")

    # ── 2. NORMALIZE UNITS ────────────────────────────────────────────────────
    emps.loc[emps["employee_type"] == "contractor", "hourly_rate"] /= 100
    contractor_ids = set(emps.loc[emps["employee_type"] == "contractor", "employee_id"])
    rate_hist.loc[rate_hist["employee_id"].isin(contractor_ids), "hourly_rate"] /= 100

    # ── 3. NORMALIZE MIXED-FORMAT STRINGS ─────────────────────────────────────
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
    )[["shift_id", "corrected_hours", "correction_type",
       "corrected_rate", "corrected_dept_id"]]

    # ── 5. JOB CODE NORMALISATION → RATE MULTIPLIER ───────────────────────────
    jcr["jc_norm"]    = jcr["job_code"].str.lower()
    shifts["jc_norm"] = (
        shifts["job_code"].str.lower().str.replace(r"[-_\s]", "", regex=True)
    )
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
    # employees.csv is the April-1 snapshot; for each shift, the rate in effect
    # on the shift date (nearest effective_date not after shift_start) overrides
    # the snapshot rate.
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
    rate_mask = (df["correction_type"] == "rate") & df["corrected_rate"].notna()
    df.loc[rate_mask, "hourly_rate"] = df.loc[rate_mask, "corrected_rate"]
    ct_rate   = rate_mask & (df["employee_type"] == "contractor")
    df.loc[ct_rate, "hourly_rate"] /= 100

    # ── 13. APPLY TRANSFERS ───────────────────────────────────────────────────
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

    # ── 15. MERGE OT POLICY ───────────────────────────────────────────────────
    df = pd.merge(df, ot, on="department_id", how="left")

    # ── 16. CALL-IN PREMIUM ───────────────────────────────────────────────────
    # Window varies by dept (6 h for D05/D08, 12 h for others).
    assigned_at = pd.to_datetime(df["assigned_at"])
    lead_h      = (df["shift_start"] - assigned_at).dt.total_seconds() / 3600
    is_callin   = lead_h < df["call_in_window_hours"]
    df["mult"]  = np.where(is_callin, np.maximum(df["mult"], 1.4), df["mult"])

    # ── 17. EFFECTIVE OT THRESHOLD ────────────────────────────────────────────
    # (a) dept threshold
    df["effective_thresh"] = df["weekly_ot_threshold"]
    # (b) pay_group override (replaces dept threshold; no schema annotation)
    pg_thresh = pay_groups.set_index("pay_group_id")["ot_threshold"].to_dict()
    pg_mask   = df["pay_group_id"].notna() & (df["pay_group_id"].astype(str) != "")
    df.loc[pg_mask, "effective_thresh"] = df.loc[pg_mask, "pay_group_id"].map(pg_thresh)
    # (c) cross-dept receiving-dept min
    cross_ot = (
        ot[ot["applies_cross_dept"]]
        [["department_id", "weekly_ot_threshold"]]
        .rename(columns={"department_id": "to_dept_id",
                          "weekly_ot_threshold": "recv_thresh"})
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
    # Multiple job codes in one week → blended_rate = total_earnings / total_hours
    # OT premium = blended_rate × 0.5 × OT_hours  (not per-shift rate × 0.5).
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
    safe_total     = df["total_earnings"].replace(0, np.nan)
    df["ot_alloc"] = (df["straight_time"] / safe_total * df["ot_premium"].fillna(0)).fillna(0)
    df["total_cost"] = df["straight_time"] + df["ot_alloc"]

    # ── 19. WORK ORDER SECONDARY ATTRIBUTION ───────────────────────────────────
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
    budgets["actual_cost"]        = budgets["actual_cost"].fillna(0.0)
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

    return {
        "report":                final_df,
        "total_budgeted_cost":   final_df["effective_budgeted_cost"].sum(),
        "total_actual_cost":     final_df["actual_cost"].sum(),
        "total_variance":        final_df["variance"].sum(),
        "total_overtime_hours":  float(wk["ot_hours"].sum()),
        "over_budget_week_count": int((final_df["variance"] > 0).sum()),
    }


@pytest.fixture
def agent_report():
    p = WORKSPACE_DIR / "payroll_variance_report.csv"
    if not p.exists():
        pytest.skip("payroll_variance_report.csv not found")
    return pd.read_csv(p)


@pytest.fixture
def agent_summary():
    p = WORKSPACE_DIR / "summary.json"
    if not p.exists():
        pytest.skip("summary.json not found")
    with open(p) as f:
        return json.load(f)


# --- Test 01: Input Data Sentinels (Anti-cheat) ---
def test_case_01_sentinels():
    shifts = pd.read_csv(DATA_DIR / "shifts.csv")
    assert len(shifts) == 35432, "shifts.csv row count changed"
    s = shifts.copy()
    s["start"] = pd.to_datetime(s["shift_start"])
    s["end"]   = pd.to_datetime(s["shift_end"])
    total_h    = (s["end"] - s["start"]).dt.total_seconds().sum() / 3600
    assert 285_000 < total_h < 305_000, "shifts.csv duration data corrupted"
    s1 = shifts[shifts["shift_id"] == "S000001"]
    assert len(s1) == 1 and s1.iloc[0]["shift_start"] == "2024-01-01 09:29:00", \
        "shifts.csv content modified"

    corrs = pd.read_csv(DATA_DIR / "corrections.csv")
    assert len(corrs) == 175, "corrections.csv row count changed"
    assert int((corrs["status"].str.lower() == "approved").sum()) >= 50, \
        "corrections.csv approved-status records changed"

    emps = pd.read_csv(DATA_DIR / "employees.csv")
    c90  = emps[emps["employee_id"] == "C0090"]
    assert len(c90) == 1 and c90.iloc[0]["hourly_rate"] > 1000, \
        "employees.csv contractor rate data changed"

    amends = pd.read_csv(DATA_DIR / "budget_amendments.csv")
    assert len(amends) == 9, "budget_amendments.csv row count changed"
    assert int((amends["type"] == "reallocation").sum()) == 1, \
        "budget_amendments.csv reallocation record changed"
    assert int((amends["approved_date"] > "2024-04-05").sum()) == 3, \
        "budget_amendments.csv post-cutoff records changed"

    ot_pol = pd.read_csv(DATA_DIR / "ot_policy.csv")
    assert "applies_cross_dept" in ot_pol.columns
    assert int(ot_pol["applies_cross_dept"].astype(str).str.lower()
               .isin({"true", "yes", "1"}).sum()) == 1, \
        "ot_policy.csv cross-dept flag count changed"
    assert ot_pol.loc[ot_pol["department_id"] == "D05",
                      "weekly_ot_threshold"].values[0] == 36.0, \
        "ot_policy.csv D05 threshold changed"
    assert "call_in_window_hours" in ot_pol.columns, \
        "ot_policy.csv missing call_in_window_hours column"


# --- Test 02: Output Shape ---
def test_case_02_shape(agent_report):
    assert len(agent_report) == 130
    for c in ["department_id", "department_name", "week_start",
              "base_budgeted_cost", "effective_budgeted_cost", "actual_cost", "variance"]:
        assert c in agent_report.columns


# --- Test 03: Sort Order ---
def test_case_03_sort(agent_report):
    expected = agent_report.sort_values(["department_id", "week_start"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(agent_report, expected)


# --- Test 04: Total Overtime Hours ---
def test_case_04_overtime_hours(truth, agent_summary):
    assert abs(agent_summary["total_overtime_hours"] - truth["total_overtime_hours"]) < 1.0


# --- Test 05: Rolling Budget Carryover — Aggregate Effective Budget ---
def test_case_05_total_budgeted_cost(truth, agent_summary):
    assert abs(agent_summary["total_budgeted_cost"] - truth["total_budgeted_cost"]) < 10.0


# --- Test 06: Total Actual Cost ---
def test_case_06_actual_cost(truth, agent_summary):
    assert abs(agent_summary["total_actual_cost"] - truth["total_actual_cost"]) < 10.0


# --- Test 07: Total Variance ---
def test_case_07_variance(truth, agent_summary):
    assert abs(agent_summary["total_variance"] - truth["total_variance"]) < 10.0


# --- Test 08: Over-Budget Department-Week Count ---
def test_case_08_over_budget_weeks(truth, agent_summary):
    assert agent_summary["over_budget_week_count"] == truth["over_budget_week_count"]


# --- Test 09: Specific Department Effective Budget (carryover chain integrity) ---
def test_case_09_specific_effective_budget(truth, agent_report):
    truth_val = truth["report"].loc[
        (truth["report"]["department_id"] == "D02") &
        (truth["report"]["week_start"]    == "2024-02-12"),
        "effective_budgeted_cost"
    ].values[0]
    agent_val = agent_report.loc[
        (agent_report["department_id"] == "D02") &
        (agent_report["week_start"]    == "2024-02-12"),
        "effective_budgeted_cost"
    ].values[0]
    assert abs(agent_val - truth_val) < 2.0


# --- Test 10: Row-Level Accuracy ---
def test_case_10_row_level_accuracy(truth, agent_report):
    merged = pd.merge(
        agent_report, truth["report"],
        on=["department_id", "week_start"], suffixes=("_agent", "_truth")
    )
    max_ac  = (merged["actual_cost_agent"] - merged["actual_cost_truth"]).abs().max()
    assert max_ac < 5.0, f"Max row actual_cost delta: {max_ac:.2f}"
    max_eff = (merged["effective_budgeted_cost_agent"] -
               merged["effective_budgeted_cost_truth"]).abs().max()
    assert max_eff < 5.0, f"Max row effective_budgeted_cost delta: {max_eff:.2f}"


# --- Test 11: New Input File Sentinels (Anti-cheat) ---
def test_case_11_new_file_sentinels():
    rh = pd.read_csv(DATA_DIR / "rate_history.csv")
    assert len(rh) == 60, "rate_history.csv row count changed"
    assert set(rh["effective_date"].unique()) == {"2024-01-01", "2024-02-05"}, \
        "rate_history.csv effective dates changed"

    wos = pd.read_csv(DATA_DIR / "work_order_splits.csv")
    assert len(wos) == 8, "work_order_splits.csv row count changed"

    corrs = pd.read_csv(DATA_DIR / "corrections.csv")
    assert "correction_type" in corrs.columns, \
        "corrections.csv missing correction_type column"
    tc = corrs["correction_type"].value_counts()
    assert tc.get("hours", 0) > 100, "corrections.csv hours-type count changed"
    assert tc.get("rate",  0) > 20,  "corrections.csv rate-type count changed"
    assert tc.get("dept",  0) > 8,   "corrections.csv dept-type count changed"
