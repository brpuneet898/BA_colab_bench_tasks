"""
Q1 2024 B2B SaaS sales performance report.

Key observations from the data:

1. Deal split credit (deal_splits.csv is authoritative):
   deal_splits.csv contains one or more rows per deal. Solo deals have
   credit_pct = 1.0; co-sold deals have two rows summing to 1.0. An agent
   that uses deals.rep_id directly assigns 100% of deal_value to the primary
   rep and gives co-reps nothing — misattributing ~$3M in revenue.

2. Fiscal quarter boundary (Feb 1, not Jan 1):
   quotas.period_start = 2024-02-01. The reporting period is Feb 1 – Apr 30.
   Deals closed in January belong to the prior fiscal period and must be
   excluded. An agent that uses calendar Q1 (Jan–Mar) inflates attainment by
   pulling in January deals.

3. In-period cancellations:
   ~240 closed-won deals were cancelled within the Feb–Apr period.
   net_revenue = gross_revenue minus these cancellations. An agent that sums
   all closed_won deals without netting cancellations over-counts revenue for
   ~5-8 reps, some of whom incorrectly appear over-quota.

4. Multi-currency FX conversion:
   ~30% of deals are in EUR or GBP. fx_rates.csv provides daily rates.
   USD deals have an implicit rate of 1.0 (not in fx_rates.csv).
   An agent that uses deal_value as USD without checking the currency column
   misprices all non-USD deals.
"""

import json
import os
import pandas as pd
from pathlib import Path

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

PERIOD_START = "2024-02-01"
PERIOD_END   = "2024-04-30"


def load_data():
    deals         = pd.read_csv(DATA_DIR / "deals.csv")
    reps          = pd.read_csv(DATA_DIR / "reps.csv")
    deal_splits   = pd.read_csv(DATA_DIR / "deal_splits.csv")
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv")
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv")
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv")
    return deals, reps, deal_splits, quotas, cancellations, fx_rates


def convert_to_usd(deals, fx_rates):
    """Trap 4: attach correct FX rate for each deal on its close_date.
    
    BOTTLENECK: FX Conversion
    ~30% of deals are in EUR or GBP. If an agent assumes all deal_value is USD,
    or uses an average rate instead of the close_date rate, it will fail
    accuracy checks for EMEA/APAC reps.
    """
    fx = fx_rates.rename(columns={"date": "close_date"})
    # USD deals are not in fx_rates; give them rate = 1.0
    usd_rates = deals[deals["currency"] == "USD"][["deal_id", "close_date"]].copy()
    usd_rates["rate_to_usd"] = 1.0

    non_usd = deals[deals["currency"] != "USD"][["deal_id", "close_date", "currency"]].copy()
    non_usd = non_usd.merge(fx, on=["close_date", "currency"], how="left")

    all_rates = pd.concat([
        usd_rates[["deal_id", "rate_to_usd"]],
        non_usd[["deal_id", "rate_to_usd"]],
    ], ignore_index=True)

    deals = deals.merge(all_rates, on="deal_id")
    deals["deal_value_usd"] = deals["deal_value"] * deals["rate_to_usd"]
    return deals


def apply_splits(deals, deal_splits):
    """Trap 1: multiply each deal's USD value by each rep's credit_pct.
    
    BOTTLENECK: Split Attribution
    ~20% of deals are co-sold (two rows in deal_splits). If the agent joins
    deals → rep_id directly, it assigns 100% of the deal value to the primary
    rep, massively inflating some reps' revenue and giving co-reps $0.
    """
    # Drop deals.rep_id — deal_splits is authoritative for attribution
    merged = deals[["deal_id", "deal_value_usd", "close_date"]].merge(
        deal_splits, on="deal_id"
    )
    merged["credited_usd"] = merged["deal_value_usd"] * merged["credit_pct"]
    return merged


def filter_period(df, date_col):
    """Trap 2: keep only deals within the fiscal period (Feb 1 – Apr 30).
    
    BOTTLENECK: Fiscal Quarter Boundary
    quotas.csv specifies Q1 2024 is Feb 1 – Apr 30. If the agent groups by
    calendar quarter (Jan 1 – Mar 31), it includes thousands of prior-period
    deals from January, inflating Q1 revenue.
    """
    return df[(df[date_col] >= PERIOD_START) & (df[date_col] <= PERIOD_END)].copy()


def net_cancellations(credited, cancellations):
    """Trap 3: subtract revenue of deals cancelled within the same period.
    
    BOTTLENECK: In-Period Cancellations
    ~3,000 deals were cancelled in the period. If the agent sums closed_won
    without netting cancellations, revenue is inflated and the 'reps_over_quota'
    metric will be completely wrong.
    """
    in_period_cancels = filter_period(cancellations, "cancelled_date")
    cancelled_ids = set(in_period_cancels["deal_id"])
    credited["is_cancelled"] = credited["deal_id"].isin(cancelled_ids)
    credited["net_credited_usd"] = credited["credited_usd"] * (~credited["is_cancelled"])
    return credited


def build_report(credited_period, reps, quotas):
    period_quotas = quotas[quotas["period_start"] == PERIOD_START].copy()

    agg = credited_period.groupby("rep_id").agg(
        total_deals    =("deal_id",         "nunique"),
        gross_revenue_usd=("credited_usd",  "sum"),
        net_revenue_usd  =("net_credited_usd", "sum"),
    ).reset_index()

    report = reps.merge(agg, on="rep_id", how="left")
    report[["total_deals", "gross_revenue_usd", "net_revenue_usd"]] = (
        report[["total_deals", "gross_revenue_usd", "net_revenue_usd"]].fillna(0)
    )
    report["total_deals"] = report["total_deals"].astype(int)

    report = report.merge(period_quotas[["rep_id", "quota_usd", "period_start"]], on="rep_id")
    report["attainment_pct"] = (
        report["net_revenue_usd"] / report["quota_usd"] * 100
    ).round(2)

    cols = ["rep_id", "rep_name", "region", "period_start",
            "total_deals", "gross_revenue_usd", "net_revenue_usd",
            "quota_usd", "attainment_pct"]
    return report[cols].sort_values("rep_id").reset_index(drop=True)


def main():
    deals, reps, deal_splits, quotas, cancellations, fx_rates = load_data()

    # Trap 4: FX conversion
    deals = convert_to_usd(deals, fx_rates)

    # Trap 1: split credit attribution
    credited = apply_splits(deals, deal_splits)

    # Trap 2: fiscal period filter (Feb 1 – Apr 30 only)
    credited_period = filter_period(credited, "close_date")

    # Trap 3: net out in-period cancellations
    credited_period = net_cancellations(credited_period, cancellations)

    # Build report
    report = build_report(credited_period, reps, quotas)
    report.to_csv(WORKSPACE_DIR / "rep_performance_report.csv", index=False)

    summary = {
        "total_gross_revenue_usd": float(round(report["gross_revenue_usd"].sum(), 2)),
        "total_net_revenue_usd":   float(round(report["net_revenue_usd"].sum(),   2)),
        "total_quota_usd":         float(round(report["quota_usd"].sum(),          2)),
        "overall_attainment_pct":  float(round(
            report["net_revenue_usd"].sum() / report["quota_usd"].sum() * 100, 2
        )),
        "reps_over_quota": int((report["net_revenue_usd"] > report["quota_usd"]).sum()),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
