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
    # 1. Load Data
    shifts = pd.read_csv(DATA_DIR / "shifts.csv")
    corrs = pd.read_csv(DATA_DIR / "corrections.csv")
    emps = pd.read_csv(DATA_DIR / "employees.csv")
    depts = pd.read_csv(DATA_DIR / "departments.csv")
    ot = pd.read_csv(DATA_DIR / "ot_policy.csv")
    trans = pd.read_csv(DATA_DIR / "transfers.csv")
    reassigns = pd.read_csv(DATA_DIR / "reassignments.csv")
    budgets = pd.read_csv(DATA_DIR / "weekly_budget.csv")

    emps.loc[emps["employee_type"] == "contractor", "hourly_rate"] /= 100

    corrs = corrs[corrs["status"] == "approved"].copy()
    PAYROLL_CUTOFF = "2024-04-05"
    corrs_pre = corrs[corrs["correction_date"] <= PAYROLL_CUTOFF].copy()
    corrs_latest = corrs_pre.sort_values("correction_date").groupby("shift_id").last().reset_index()

    shifts["shift_start"] = pd.to_datetime(shifts["shift_start"])
    shifts["shift_end"] = pd.to_datetime(shifts["shift_end"])
    shifts["duration_h"] = (shifts["shift_end"] - shifts["shift_start"]).dt.total_seconds() / 3600
    
    shifts = shifts[(shifts["duration_h"] >= 6.0) & (shifts["duration_h"] <= 14.0)].copy()
    
    shifts = pd.merge(shifts, corrs_latest[["shift_id", "corrected_hours"]], on="shift_id", how="left")
    shifts["ratio"] = np.where(shifts["corrected_hours"].notna(), shifts["corrected_hours"] / shifts["duration_h"], 1.0)
    shifts["duration_h"] = np.where(shifts["corrected_hours"].notna(), shifts["corrected_hours"], shifts["duration_h"])
    
    shifts = shifts[shifts["duration_h"] >= 6.0].copy()

    split_shifts = []
    for _, row in shifts.iterrows():
        st = row["shift_start"]
        en = row["shift_end"]
        if st.weekday() == 6 and en.weekday() == 0:
            mid = en.normalize()
            raw_durA = (mid - st).total_seconds() / 3600
            raw_durB = (en - mid).total_seconds() / 3600
            
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

    HOLIDAYS = ["2024-01-15", "2024-02-19", "2024-03-10"]
    df["date_str"] = df["shift_start"].dt.strftime("%Y-%m-%d")
    df["is_holiday"] = df["date_str"].isin(HOLIDAYS)
    df["is_weekend"] = df["shift_start"].dt.weekday.isin([5, 6])
    df["is_night"] = df["shift_start"].dt.hour >= 21
    
    df["mult"] = 1.0
    df["mult"] = np.where(df["is_night"], 1.3, df["mult"])
    df["mult"] = np.where(df["is_weekend"], np.maximum(df["mult"], 1.15), df["mult"])
    df["mult"] = np.where(df["is_holiday"], np.maximum(df["mult"], 1.5), df["mult"])

    trans["eff_date"] = pd.to_datetime(trans["effective_date"])
    df = pd.merge(df, emps[["employee_id", "department_id", "hourly_rate"]], on="employee_id", how="left")
    
    for _, t in trans.iterrows():
        mask = (df["employee_id"] == t["employee_id"]) & (df["shift_start"] < t["eff_date"])
        df.loc[mask, "department_id"] = t["from_dept_id"]

    df = pd.merge(df, ot, on="department_id", how="left")
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

    reassigns = pd.merge(reassigns, emps[["employee_id", "department_id", "hourly_rate"]], on="employee_id", how="left")
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
    
    budgets = budgets.sort_values(["department_id", "week_start"])
    budgets = pd.merge(budgets, actuals[["department_id", "week_start", "actual_cost"]], on=["department_id", "week_start"], how="left")
    budgets["actual_cost"] = budgets["actual_cost"].fillna(0.0)
    budgets["base_budgeted_cost"] = budgets["budgeted_hours"] * budgets["avg_hourly_rate"]
    
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

    final_df.to_csv(WORKSPACE_DIR / "payroll_variance_report.csv", index=False)
    
    summary = {
        "total_budgeted_cost": final_df["effective_budgeted_cost"].sum(),
        "total_actual_cost": final_df["actual_cost"].sum(),
        "total_variance": final_df["variance"].sum(),
        "total_overtime_hours": round(float(df["ot_hrs"].sum()), 2),
        "over_budget_week_count": int((final_df["variance"] > 0).sum())
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=4)

if __name__ == "__main__":
    main()
