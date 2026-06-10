"""
Tests for expense-variance-analysis.

Reasoning challenges: (1) budget amendments require a post-cutoff date filter
(parallel to corrections cutoff) and a bidirectional reallocation type where one
row adjusts two departments; (2) the reallocation deduction from the source
department cascades through that department's carryover chain for all subsequent
weeks; (3) contractor rates in cents, premium exclusivity, non-uniform OT thresholds,
and correction cutoff rules must all interact correctly with the amended base budgets.

Tests verify: output existence and shape (00–03), overtime hours (04), effective
budget totals and row-level accuracy (05, 09, 10), actual cost (06), variance (07),
over-budget week count (08), and anti-cheat sentinels anchoring input data integrity (01).
"""
import pytest
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

# --- Test 00: Output Existence (Discriminability guard) ---
def test_case_00_outputs_exist():
    assert (WORKSPACE_DIR / "payroll_variance_report.csv").exists(), \
        "payroll_variance_report.csv not found — agent produced no output"
    assert (WORKSPACE_DIR / "summary.json").exists(), \
        "summary.json not found — agent produced no output"


@pytest.fixture(scope="module")
def truth():
    # 1. Load Data
    shifts = pd.read_csv(DATA_DIR / "shifts.csv")
    corrs = pd.read_csv(DATA_DIR / "corrections.csv")
    emps = pd.read_csv(DATA_DIR / "employees.csv")
    depts = pd.read_csv(DATA_DIR / "departments.csv")
    ot = pd.read_csv(DATA_DIR / "ot_policy.csv")
    trans = pd.read_csv(DATA_DIR / "transfers.csv")
    reassigns = pd.read_csv(DATA_DIR / "reassignments.csv")
    budgets = pd.read_csv(DATA_DIR / "weekly_budget.csv")

    # 2. Contractor rates
    emps.loc[emps["employee_type"] == "contractor", "hourly_rate"] /= 100

    # 3. Corrections (Trap III)
    corrs = corrs[corrs["status"] == "approved"].copy()
    PAYROLL_CUTOFF = "2024-04-05"
    corrs_pre = corrs[corrs["correction_date"] <= PAYROLL_CUTOFF].copy()
    corrs_latest = corrs_pre.sort_values("correction_date").groupby("shift_id").last().reset_index()

    # 4. Outliers & Exclusions
    shifts["shift_start"] = pd.to_datetime(shifts["shift_start"])
    shifts["shift_end"] = pd.to_datetime(shifts["shift_end"])
    shifts["duration_h"] = (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    
    # AT Bug 2 Fix: drop initial outliers BEFORE applying corrections
    shifts = shifts[(shifts["duration_h"] >= 6.0) & (shifts["duration_h"] <= 14.0)].copy()
    
    shifts = pd.merge(shifts, corrs_latest[["shift_id", "corrected_hours"]], on="shift_id", how="left")
    # Store ratio for boundary splitting (AT Bug 1 Fix)
    shifts["ratio"] = np.where(shifts["corrected_hours"].notna(), shifts["corrected_hours"] / shifts["duration_h"], 1.0)
    
    shifts["duration_h"] = np.where(shifts["corrected_hours"].notna(), shifts["corrected_hours"], shifts["duration_h"])
    
    # Rule 4: Exclude if corrected duration < 6
    shifts = shifts[shifts["duration_h"] >= 6.0].copy()

    # 5. Split across midnight (Sunday/Monday boundary)
    split_shifts = []
    for _, row in shifts.iterrows():
        st = row["shift_start"]
        en = row["shift_end"]
        if st.weekday() == 6 and en.weekday() == 0:
            mid = en.normalize()
            raw_durA = (mid - st).total_seconds() / 3600
            raw_durB = (en - mid).total_seconds() / 3600
            
            # AT Bug 1 Fix: Scale by ratio to preserve corrected hours
            durA = raw_durA * row["ratio"]
            durB = raw_durB * row["ratio"]
            
            rowA = row.copy()
            rowA["duration_h"] = durA
            rowA["week_start"] = (st - pd.Timedelta(days=st.weekday())).normalize()
            
            rowB = row.copy()
            rowB["duration_h"] = durB
            rowB["week_start"] = (en - pd.Timedelta(days=en.weekday())).normalize()
            
            split_shifts.extend([rowA, rowB])
        else:
            rowA = row.copy()
            rowA["week_start"] = (st - pd.Timedelta(days=st.weekday())).normalize()
            split_shifts.append(rowA)
            
    df = pd.DataFrame(split_shifts)

    # 6. Premium Exclusivity (Trap II)
    HOLIDAYS = ["2024-01-15", "2024-02-19", "2024-03-10"]
    df["date_str"] = df["shift_start"].dt.strftime("%Y-%m-%d")
    df["is_holiday"] = df["date_str"].isin(HOLIDAYS)
    df["is_weekend"] = df["shift_start"].dt.weekday.isin([5, 6])
    df["is_night"] = df["shift_start"].dt.hour >= 21
    
    df["mult"] = 1.0
    df["mult"] = np.where(df["is_night"], 1.3, df["mult"])
    df["mult"] = np.where(df["is_weekend"], np.maximum(df["mult"], 1.15), df["mult"])
    df["mult"] = np.where(df["is_holiday"], np.maximum(df["mult"], 1.5), df["mult"])

    # 7. Department History
    trans["eff_date"] = pd.to_datetime(trans["effective_date"])
    df = pd.merge(df, emps[["employee_id", "department_id", "hourly_rate"]], on="employee_id", how="left")
    
    for _, t in trans.iterrows():
        mask = (df["employee_id"] == t["employee_id"]) & (df["shift_start"] < t["eff_date"])
        df.loc[mask, "department_id"] = t["from_dept_id"]

    # 8. Base Cost and OT
    df = pd.merge(df, ot, on="department_id", how="left")

    # Trap IV: applies_cross_dept flag drives receiving-dept threshold override
    cross_ot = ot[ot["applies_cross_dept"] == True][["department_id", "weekly_ot_threshold"]].copy()
    cross_ot = cross_ot.rename(columns={"department_id": "to_dept_id", "weekly_ot_threshold": "recv_thresh"})
    ot_recv = reassigns[["employee_id", "week_start", "to_dept_id"]].drop_duplicates().copy()
    ot_recv = pd.merge(ot_recv, cross_ot, on="to_dept_id", how="inner")
    ot_recv_min = ot_recv.groupby(["employee_id", "week_start"])["recv_thresh"].min().reset_index()
    ot_recv_min.rename(columns={"week_start": "week_start_str"}, inplace=True)
    df["week_start_str"] = df["week_start"].dt.strftime("%Y-%m-%d")
    df = pd.merge(df, ot_recv_min, on=["employee_id", "week_start_str"], how="left")
    df["weekly_ot_threshold"] = np.where(
        df["recv_thresh"].notna(),
        np.minimum(df["weekly_ot_threshold"], df["recv_thresh"]),
        df["weekly_ot_threshold"]
    )
    df.drop(columns=["recv_thresh", "week_start_str"], inplace=True)

    df["base_cost"] = df["duration_h"] * df["hourly_rate"] * df["mult"]
    
    df = df.sort_values(["employee_id", "week_start", "shift_start"])
    df["cum_hrs"] = df.groupby(["employee_id", "week_start"])["duration_h"].cumsum()
    df["prev_cum"] = df["cum_hrs"] - df["duration_h"]
    
    def calc_ot(row):
        thresh = row["weekly_ot_threshold"]
        if row["cum_hrs"] <= thresh:
            return 0.0
        elif row["prev_cum"] >= thresh:
            return row["duration_h"] * row["hourly_rate"] * row["mult"] * 0.5
        else:
            ot_hrs = row["cum_hrs"] - thresh
            return ot_hrs * row["hourly_rate"] * row["mult"] * 0.5
            
    df["ot_premium"] = df.apply(calc_ot, axis=1)
    df["total_cost"] = df["base_cost"] + df["ot_premium"]
    
    dept_costs = df.groupby(["department_id", "week_start"])["total_cost"].sum().reset_index()
    dept_costs.rename(columns={"total_cost": "actual_cost"}, inplace=True)
    dept_costs["week_start"] = dept_costs["week_start"].dt.strftime("%Y-%m-%d")

    # 9. Reassignments
    reassigns["week_start_dt"] = pd.to_datetime(reassigns["week_start"])
    reassigns = pd.merge(reassigns, emps[["employee_id", "department_id", "hourly_rate"]], on="employee_id", how="left")
    # Use historical home dept at time of reassignment week (same pattern as shift attribution)
    for _, t in trans.iterrows():
        mask = (reassigns["employee_id"] == t["employee_id"]) & (reassigns["week_start_dt"] < t["eff_date"])
        reassigns.loc[mask, "department_id"] = t["from_dept_id"]
    reassigns["transfer_cost"] = reassigns["reassigned_hours"] * reassigns["hourly_rate"]

    home_deduct = reassigns.groupby(["department_id", "week_start"])["transfer_cost"].sum().reset_index()
    home_deduct.rename(columns={"department_id": "home_dept", "transfer_cost": "deduction"}, inplace=True)
    
    recv_add = reassigns.groupby(["to_dept_id", "week_start"])["transfer_cost"].sum().reset_index()
    recv_add.rename(columns={"to_dept_id": "department_id", "transfer_cost": "addition"}, inplace=True)
    
    actuals = pd.merge(dept_costs, home_deduct, left_on=["department_id", "week_start"], right_on=["home_dept", "week_start"], how="left")
    actuals["deduction"] = actuals["deduction"].fillna(0.0)
    actuals = pd.merge(actuals, recv_add, on=["department_id", "week_start"], how="left")
    actuals["addition"] = actuals["addition"].fillna(0.0)
    
    actuals["actual_cost"] = actuals["actual_cost"] - actuals["deduction"] + actuals["addition"]
    
    # 10. Rolling Budget Carryover (Trap I)
    budgets = budgets.sort_values(["department_id", "week_start"])
    budgets = pd.merge(budgets, actuals[["department_id", "week_start", "actual_cost"]], on=["department_id", "week_start"], how="left")
    budgets["actual_cost"] = budgets["actual_cost"].fillna(0.0)
    budgets["base_budgeted_cost"] = budgets["budgeted_hours"] * budgets["avg_hourly_rate"]

    # Budget amendments
    amendments = pd.read_csv(DATA_DIR / "budget_amendments.csv")
    AMEND_CUTOFF = "2024-04-05"
    amendments = amendments[amendments["approved_date"] <= AMEND_CUTOFF].copy()
    for _, a in amendments.iterrows():
        mask = (budgets["department_id"] == a["department_id"]) & (budgets["week_start"] == a["week_start"])
        budgets.loc[mask, "base_budgeted_cost"] += a["amount"]
        if a["type"] == "reallocation":
            src_mask = (budgets["department_id"] == a["from_dept_id"]) & (budgets["week_start"] == a["week_start"])
            budgets.loc[src_mask, "base_budgeted_cost"] -= a["amount"]

    effective_budgets = []
    for dept, grp in budgets.groupby("department_id"):
        carryover = 0.0
        for _, row in grp.iterrows():
            base = row["base_budgeted_cost"]
            added = min(carryover, base * 0.12)
            eff = base + added
            surplus = eff - row["actual_cost"]
            if surplus > 0:
                carryover = surplus * 0.8
            else:
                carryover = 0.0
            
            row_out = row.copy()
            row_out["effective_budgeted_cost"] = eff
            row_out["variance"] = row["actual_cost"] - eff
            effective_budgets.append(row_out)
            
    final_df = pd.DataFrame(effective_budgets)
    final_df = pd.merge(final_df, depts, on="department_id", how="left")
    
    cols = ["department_id", "department_name", "week_start", "base_budgeted_cost", "effective_budgeted_cost", "actual_cost", "variance"]
    final_df = final_df[cols].sort_values(["department_id", "week_start"]).reset_index(drop=True)
    
    df["ot_hrs"] = df.apply(lambda x: min(max(0, x['cum_hrs'] - x['weekly_ot_threshold']), x['duration_h']), axis=1)

    return {
        "report": final_df,
        "total_budgeted_cost": final_df["effective_budgeted_cost"].sum(),
        "total_actual_cost": final_df["actual_cost"].sum(),
        "total_variance": final_df["variance"].sum(),
        "total_overtime_hours": float(df["ot_hrs"].sum()),
        "over_budget_week_count": int((final_df["variance"] > 0).sum())
    }

@pytest.fixture
def agent_report():
    report_path = WORKSPACE_DIR / "payroll_variance_report.csv"
    if not report_path.exists():
        pytest.skip("payroll_variance_report.csv not found")
    return pd.read_csv(report_path)

@pytest.fixture
def agent_summary():
    summary_path = WORKSPACE_DIR / "summary.json"
    if not summary_path.exists():
        pytest.skip("summary.json not found")
    with open(summary_path) as f:
        return json.load(f)

# --- Test 01: Sentinels (Anti-cheat) ---
def test_case_01_sentinels():
    shifts = pd.read_csv(DATA_DIR / "shifts.csv")
    assert len(shifts) == 35432, "shifts.csv row count changed"
    # Content integrity: total duration must be >300k hours (zeroing shift_end breaks this)
    s = shifts.copy()
    s["start"] = pd.to_datetime(s["shift_start"])
    s["end"] = pd.to_datetime(s["shift_end"])
    total_h = (s["end"] - s["start"]).dt.total_seconds().sum() / 3600
    assert 285_000 < total_h < 305_000, "shifts.csv duration data appears corrupted"
    # Spot-check a specific record to catch value substitution attacks
    s1 = shifts[shifts["shift_id"] == "S000001"]
    assert len(s1) == 1 and s1.iloc[0]["shift_start"] == "2024-01-01 09:29:00", \
        "shifts.csv content modified"

    corrs = pd.read_csv(DATA_DIR / "corrections.csv")
    assert len(corrs) == 175, "corrections.csv row count changed"
    assert int((corrs["status"] == "approved").sum()) >= 50, \
        "corrections.csv approved-status records changed"

    emps = pd.read_csv(DATA_DIR / "employees.csv")
    # Contractor rates are stored in cents — C0090's raw rate must be >1000
    c90 = emps[emps["employee_id"] == "C0090"]
    assert len(c90) == 1 and c90.iloc[0]["hourly_rate"] > 1000, \
        "employees.csv contractor rate data changed"

    amends = pd.read_csv(DATA_DIR / "budget_amendments.csv")
    assert len(amends) == 9, "budget_amendments.csv row count changed"
    assert int((amends["type"] == "reallocation").sum()) == 1, \
        "budget_amendments.csv reallocation record changed"
    assert int((amends["approved_date"] > "2024-04-05").sum()) == 3, \
        "budget_amendments.csv post-cutoff records changed"

    ot_pol = pd.read_csv(DATA_DIR / "ot_policy.csv")
    assert "applies_cross_dept" in ot_pol.columns, "ot_policy.csv missing applies_cross_dept column"
    assert int(ot_pol["applies_cross_dept"].astype(str).str.lower().eq("true").sum()) == 1, \
        "ot_policy.csv cross-dept flag count changed"
    assert ot_pol.loc[ot_pol["department_id"] == "D05", "weekly_ot_threshold"].values[0] == 36.0, \
        "ot_policy.csv D05 threshold changed"

# --- Test 02: Output Shape ---
def test_case_02_shape(agent_report):
    assert len(agent_report) == 130
    cols = ["department_id", "department_name", "week_start", "base_budgeted_cost", "effective_budgeted_cost", "actual_cost", "variance"]
    for c in cols:
        assert c in agent_report.columns

# --- Test 03: Sort Order ---
def test_case_03_sort(agent_report):
    expected_order = agent_report.sort_values(["department_id", "week_start"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(agent_report, expected_order)

# --- Test 04: Non-uniform OT Thresholds (D05=36h, D08=44h) ---
def test_case_04_overtime_hours(truth, agent_summary):
    assert abs(agent_summary["total_overtime_hours"] - truth["total_overtime_hours"]) < 1.0

# --- Test 05: Trap I — Rolling Budget Carryover (aggregate effective budget) ---
def test_case_05_total_budgeted_cost(truth, agent_summary):
    assert abs(agent_summary["total_budgeted_cost"] - truth["total_budgeted_cost"]) < 10.0

# --- Test 06: Total Actual Cost (corrections, premiums, contractor rates) ---
def test_case_06_actual_cost(truth, agent_summary):
    assert abs(agent_summary["total_actual_cost"] - truth["total_actual_cost"]) < 10.0

# --- Test 07: Trap II - Premium Exclusivity ---
def test_case_07_variance(truth, agent_summary):
    assert abs(agent_summary["total_variance"] - truth["total_variance"]) < 10.0

# --- Test 08: Trap II - Over Budget Weeks ---
def test_case_08_over_budget_weeks(truth, agent_summary):
    assert agent_summary["over_budget_week_count"] == truth["over_budget_week_count"]

# --- Test 09: Trap I - Specific Department Effective Budget ---
def test_case_09_specific_effective_budget(truth, agent_report):
    # Check D02 Week 7
    truth_val = truth["report"].loc[(truth["report"]["department_id"] == "D02") & (truth["report"]["week_start"] == "2024-02-12"), "effective_budgeted_cost"].values[0]
    agent_val = agent_report.loc[(agent_report["department_id"] == "D02") & (agent_report["week_start"] == "2024-02-12"), "effective_budgeted_cost"].values[0]
    assert abs(agent_val - truth_val) < 2.0

# --- Test 10: Row-Level Accuracy ---
def test_case_10_row_level_accuracy(truth, agent_report):
    merged = pd.merge(agent_report, truth["report"], on=["department_id", "week_start"], suffixes=("_agent", "_truth"))
    max_diff = (merged["actual_cost_agent"] - merged["actual_cost_truth"]).abs().max()
    assert max_diff < 5.0, f"Max row difference in actual_cost is {max_diff}"
    max_diff_eff = (merged["effective_budgeted_cost_agent"] - merged["effective_budgeted_cost_truth"]).abs().max()
    assert max_diff_eff < 5.0, f"Max row difference in effective_budgeted_cost is {max_diff_eff}"
