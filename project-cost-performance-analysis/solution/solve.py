"""
Oracle solution for: Q1 2024 EVM Cost Performance Report

Reads all source files from /workspace/data/, computes EVM metrics for each of
the 200 work packages, and writes:
  /workspace/evm_report.csv   — 200-row work-package detail
  /workspace/summary.json     — 9-key portfolio summary
"""

import json
import os
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR  = Path(os.environ.get("DATA_DIR", WORKSPACE / "data"))

REPORTING_DATE = pd.Timestamp("2024-03-31")

# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------

work_packages = pd.read_csv(DATA_DIR / "work_packages.csv")
baselines     = pd.read_csv(DATA_DIR / "baselines.csv")
actuals       = pd.read_csv(DATA_DIR / "actuals.csv")
progress      = pd.read_csv(DATA_DIR / "progress_entries.csv")
pv_schedule   = pd.read_csv(DATA_DIR / "planned_value_schedule.csv")

# ---------------------------------------------------------------------------
# BAC: select applicable baseline per (project_id, work_package_id)
#
# Rule: most recent baseline_effective_from on or before REPORTING_DATE.
# NaT in baseline_effective_to means the record has no expiry — treat as
# always valid after its effective_from. The pandas expression
#   `REPORTING_DATE <= NaT`  evaluates to False, so NaT must be handled
# explicitly before applying any date filter.
# ---------------------------------------------------------------------------

baselines["baseline_effective_from"] = pd.to_datetime(baselines["baseline_effective_from"])
open_ended = baselines["baseline_effective_to"].isna()
baselines["baseline_effective_to"]   = pd.to_datetime(baselines["baseline_effective_to"])
work_packages["completion_date"]     = pd.to_datetime(work_packages["completion_date"])

valid_baselines = baselines[
    (baselines["baseline_effective_from"] <= REPORTING_DATE) &
    (open_ended | (REPORTING_DATE <= baselines["baseline_effective_to"]))
].copy()

# Where multiple baselines are valid, keep the most recently approved
valid_baselines = valid_baselines.sort_values("baseline_effective_from", ascending=False)
applicable_bac  = valid_baselines.drop_duplicates(
    subset=["project_id", "work_package_id"], keep="first"
)[["project_id", "work_package_id", "bac_usd"]]

# ---------------------------------------------------------------------------
# AC: sum cost_amount_usd per (project_id, work_package_id)
#     where billing_period_date <= REPORTING_DATE and transaction_type != "commitment"
#
# Commitments are purchase orders not yet invoiced — future obligations, not
# incurred costs.  EVM AC excludes them.
# ---------------------------------------------------------------------------

actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
in_period = actuals[
    (actuals["billing_period_date"] <= REPORTING_DATE) &
    (actuals["transaction_type"] != "commitment")
]
ac_df = (
    in_period
    .groupby(["project_id", "work_package_id"], as_index=False)["cost_amount_usd"]
    .sum()
    .rename(columns={"cost_amount_usd": "ac_usd"})
)

# ---------------------------------------------------------------------------
# Progress (percent_complete as of 2024-03)
#
# Some WPs have two entries for "2024-03" — a preliminary estimate filed first
# and the confirmed final report filed later.  Sort by submitted_date descending
# before deduplicating to ensure the confirmed (most recent) value is used.
# ---------------------------------------------------------------------------

mar_progress = (
    progress[progress["reporting_period"] == "2024-03"]
    .sort_values("submitted_date", ascending=False)
    .drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    [["project_id", "work_package_id", "percent_complete"]]
)

# ---------------------------------------------------------------------------
# Planned Value (cumulative PV for 2024-03)
# ---------------------------------------------------------------------------

pv_schedule["schedule_effective_from"] = pd.to_datetime(pv_schedule["schedule_effective_from"])
mar_pv = (
    pv_schedule[pv_schedule["reporting_period"] == "2024-03"]
    .sort_values("schedule_effective_from", ascending=False)
    .drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    [["project_id", "work_package_id", "cumulative_pv_usd"]]
    .rename(columns={"cumulative_pv_usd": "pv_usd"})
)

# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

df = (
    work_packages[["project_id", "work_package_id", "work_package_name",
                   "control_account_id", "ev_technique", "completion_status",
                   "completion_date"]]
    .merge(applicable_bac, on=["project_id", "work_package_id"], how="left")
    .merge(ac_df,          on=["project_id", "work_package_id"], how="left")
    .merge(mar_progress,   on=["project_id", "work_package_id"], how="left")
    .merge(mar_pv,         on=["project_id", "work_package_id"], how="left")
)

df["ac_usd"]           = df["ac_usd"].fillna(0.0)
df["percent_complete"] = df["percent_complete"].fillna(0.0)
df["pv_usd"]           = df["pv_usd"].fillna(0.0)

# ---------------------------------------------------------------------------
# EV
# ---------------------------------------------------------------------------

def compute_ev(row):
    if row["ev_technique"] == "0_100":
        comp_date = row["completion_date"]
        if pd.notna(comp_date) and comp_date <= REPORTING_DATE:
            return float(row["bac_usd"])
        return 0.0
    return (float(row["percent_complete"]) / 100.0) * float(row["bac_usd"])

df["ev_usd"] = df.apply(compute_ev, axis=1)

# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

df["cv_usd"] = df["ev_usd"] - df["ac_usd"]
df["sv_usd"] = df["ev_usd"] - df["pv_usd"]

df["cpi"] = df.apply(
    lambda r: round(r["ev_usd"] / r["ac_usd"], 4) if r["ac_usd"] > 0 else None, axis=1)
df["spi"] = df.apply(
    lambda r: round(r["ev_usd"] / r["pv_usd"], 4) if r["pv_usd"] > 0 else None, axis=1)

df["eac_usd"] = df.apply(
    lambda r: round(r["bac_usd"] / r["cpi"], 2)
    if (r["cpi"] is not None and r["cpi"] > 0) else round(r["bac_usd"], 2), axis=1)

for col in ["bac_usd", "pv_usd", "ev_usd", "ac_usd", "cv_usd", "sv_usd"]:
    df[col] = df[col].round(2)

df["etc_usd"] = (df["eac_usd"] - df["ac_usd"]).round(2)
df["vac_usd"] = (df["bac_usd"] - df["eac_usd"]).round(2)

# ---------------------------------------------------------------------------
# evm_report.csv
# ---------------------------------------------------------------------------

report_cols = [
    "project_id", "work_package_id", "work_package_name", "control_account_id",
    "ev_technique", "completion_status", "percent_complete",
    "bac_usd", "pv_usd", "ev_usd", "ac_usd",
    "cv_usd", "sv_usd", "cpi", "spi",
    "eac_usd", "etc_usd", "vac_usd",
]

df = df.sort_values(["project_id", "work_package_id"]).reset_index(drop=True)
df[report_cols].to_csv(WORKSPACE / "evm_report.csv", index=False)

# ---------------------------------------------------------------------------
# summary.json
# ---------------------------------------------------------------------------

total_bac = round(float(df["bac_usd"].sum()), 2)
total_ev  = round(float(df["ev_usd"].sum()), 2)
total_ac  = round(float(df["ac_usd"].sum()), 2)
total_pv  = round(float(df["pv_usd"].sum()), 2)
total_eac = round(float(df["eac_usd"].sum()), 2)

portfolio_cpi = round(total_ev / total_ac, 4) if total_ac > 0 else 0.0
portfolio_spi = round(total_ev / total_pv, 4) if total_pv > 0 else 0.0

overbudget        = int((df["cv_usd"] < 0).sum())
behind_schedule   = int((df["sv_usd"] < 0).sum())

summary = {
    "reporting_date":                    "2024-03-31",
    "total_bac_usd":                     total_bac,
    "total_ev_usd":                      total_ev,
    "total_ac_usd":                      total_ac,
    "total_pv_usd":                      total_pv,
    "portfolio_cpi":                     portfolio_cpi,
    "portfolio_spi":                     portfolio_spi,
    "total_eac_usd":                     total_eac,
    "overbudget_work_package_count":     overbudget,
    "behind_schedule_work_package_count": behind_schedule,
}

with open(WORKSPACE / "summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print("Done.")
print(f"  evm_report.csv : {len(df)} rows")
print(f"  summary.json   : {list(summary.keys())}")
