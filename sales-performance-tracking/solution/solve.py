"""
Q1 2024 B2B SaaS sales performance report.

Key observations and traps in the data:

1. Region-Scoped Primary Keys (Composite Key Trap)
   deal_id resets per region. deal_id=100 exists in NA, EMEA, and APAC.
   An agent that merges deals with deal_splits on just deal_id will trigger
   a massive Cartesian cross-join, duplicating revenue and assigning wrong regions.
   Must merge on ['region', 'deal_id'].

2. Direct vs Indirect FX Quotes (Unit Mismatch)
   fx_rates.csv has quote_convention. EUR is USD_per_Unit (rate = 1.08).
   JPY/CAD are Units_per_USD (rate = 150.5).
   An agent that blindly multiplies everything by rate will inflate JPY by 22,000x.
   Must conditionalize: if USD_per_Unit -> multiply, if Units_per_USD -> divide.

3. ARR vs TCV (Domain Logic)
   deals.csv provides total_contract_value and contract_months.
   Instruction asks for ARR. ARR = TCV / months * 12.
   An agent that blindly sums TCV will overstate multi-year deal values.

4. Mid-Quarter Hire Quota Proration (Algorithmic Depth)
   reps.csv has hire_date. Some reps are hired during the 90-day Q1 period.
   Their quota in quotas.csv must be prorated by active days.
   active_days = min(90, max(0, (period_end - max(hire_date, period_start)).days + 1))
   Prorated quota = base_quota * (active_days / 90).
"""

import json
import os
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

PERIOD_START = pd.Timestamp("2024-02-01")
PERIOD_END   = pd.Timestamp("2024-04-30")
DAYS_IN_QUARTER = 90


def load_data():
    deals         = pd.read_csv(DATA_DIR / "deals.csv", parse_dates=["close_date"])
    reps          = pd.read_csv(DATA_DIR / "reps.csv", parse_dates=["hire_date"])
    deal_splits   = pd.read_csv(DATA_DIR / "deal_splits.csv")
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv", parse_dates=["period_start"])
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv", parse_dates=["cancelled_date"])
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv", parse_dates=["date"])
    return deals, reps, deal_splits, quotas, cancellations, fx_rates


def convert_to_arr_usd(deals, fx_rates):
    # Trap 3: ARR = TCV / months * 12
    deals["arr_local"] = deals["total_contract_value"] / deals["contract_months"] * 12.0

    # Trap 2: Direct vs Indirect FX Quote
    fx = fx_rates.rename(columns={"date": "close_date"})
    
    usd_deals = deals[deals["currency"] == "USD"].copy()
    usd_deals["arr_usd"] = usd_deals["arr_local"]
    
    non_usd = deals[deals["currency"] != "USD"].copy()
    non_usd = non_usd.merge(fx, on=["close_date", "currency"], how="left")
    
    non_usd["arr_usd"] = np.where(
        non_usd["quote_convention"] == "USD_per_Unit",
        non_usd["arr_local"] * non_usd["rate"],
        non_usd["arr_local"] / non_usd["rate"]
    )
    
    all_deals = pd.concat([usd_deals, non_usd], ignore_index=True)
    return all_deals


def apply_splits(deals, deal_splits):
    # Trap 1: Region-scoped primary keys. Must join on ['region', 'deal_id']
    merged = deals[["region", "deal_id", "arr_usd", "close_date"]].merge(
        deal_splits, on=["region", "deal_id"]
    )
    merged["credited_arr"] = merged["arr_usd"] * merged["credit_pct"]
    return merged


def filter_period(df, date_col):
    return df[(df[date_col] >= PERIOD_START) & (df[date_col] <= PERIOD_END)].copy()


def net_cancellations(credited, cancellations):
    in_period_cancels = filter_period(cancellations, "cancelled_date")
    # Must match on region + deal_id!
    cancelled_keys = set(zip(in_period_cancels["region"], in_period_cancels["deal_id"]))
    
    credited["is_cancelled"] = credited.apply(
        lambda r: (r["region"], r["deal_id"]) in cancelled_keys, axis=1
    )
    credited["net_credited_arr"] = credited["credited_arr"] * (~credited["is_cancelled"])
    return credited


def prorate_quotas(reps, quotas):
    # Trap 4: Prorate quota based on hire_date
    q = quotas[quotas["period_start"] == PERIOD_START].copy()
    rq = reps.merge(q[["rep_id", "quota_usd", "period_start"]], on="rep_id")
    
    # Calculate active days
    # If hired before period_start, active_days = 90
    # If hired during period, active_days = (period_end - hire_date).days + 1
    # If hired after period_end, active_days = 0
    
    start_dates = rq[["hire_date"]].assign(period_start=PERIOD_START).max(axis=1)
    days_active = (PERIOD_END - start_dates).dt.days + 1
    days_active = days_active.clip(lower=0, upper=90)
    
    rq["active_quota_usd"] = rq["quota_usd"] * (days_active / 90.0)
    return rq


def build_report(credited_period, reps_quotas):
    agg = credited_period.groupby("rep_id").agg(
        total_deals    =("deal_id",         "nunique"),
        gross_arr_usd=("credited_arr",  "sum"),
        net_arr_usd  =("net_credited_arr", "sum"),
    ).reset_index()

    report = reps_quotas.merge(agg, on="rep_id", how="left")
    report[["total_deals", "gross_arr_usd", "net_arr_usd"]] = (
        report[["total_deals", "gross_arr_usd", "net_arr_usd"]].fillna(0)
    )
    report["total_deals"] = report["total_deals"].astype(int)

    report["attainment_pct"] = np.where(
        report["active_quota_usd"] > 0,
        (report["net_arr_usd"] / report["active_quota_usd"] * 100),
        0.0
    ).round(2)

    # Drop the original quota_usd and rename active_quota_usd to quota_usd
    report = report.drop(columns=["quota_usd"]).rename(columns={"active_quota_usd": "quota_usd"})

    cols = ["rep_id", "rep_name", "region", "period_start",
            "total_deals", "gross_arr_usd", "net_arr_usd",
            "quota_usd", "attainment_pct"]
    
    # Format period_start back to string
    report["period_start"] = report["period_start"].dt.strftime("%Y-%m-%d")
    
    return report[cols].sort_values("rep_id").reset_index(drop=True)


def main():
    deals, reps, deal_splits, quotas, cancellations, fx_rates = load_data()

    deals = convert_to_arr_usd(deals, fx_rates)
    credited = apply_splits(deals, deal_splits)
    credited_period = filter_period(credited, "close_date")
    credited_period = net_cancellations(credited_period, cancellations)
    
    reps_quotas = prorate_quotas(reps, quotas)

    report = build_report(credited_period, reps_quotas)
    report.to_csv(WORKSPACE_DIR / "rep_performance_report.csv", index=False)

    summary = {
        "total_gross_arr_usd": float(round(report["gross_arr_usd"].sum(), 2)),
        "total_net_arr_usd":   float(round(report["net_arr_usd"].sum(),   2)),
        "total_quota_usd":     float(round(report["quota_usd"].sum(),      2)),
        "overall_attainment_pct":  float(round(
            report["net_arr_usd"].sum() / report["quota_usd"].sum() * 100, 2
        )),
        "reps_over_quota": int((report["net_arr_usd"] > report["quota_usd"]).sum()),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
