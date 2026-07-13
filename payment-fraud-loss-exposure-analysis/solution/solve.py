"""
Oracle solution for the Q1 2024 Fraud Loss Exposure Report.

What the data actually required, once explored:
- fraud_disputes.csv only records the transaction a cardholder reported when a
  case was opened. The real scope of a case (sometimes several transactions on
  a compromised card at the same merchant) lives in case_transactions.csv —
  grouping that file by case_id shows a meaningful minority of cases have more
  than one linked transaction.
- dispute_resolutions.csv can carry more than one row per case_id. Sorting by
  resolution_date shows some cases flip outcome on a later record; only the
  latest resolution is the case's actual liability determination.
- Joining case_transactions.csv to transactions.csv on transaction_id alone
  produces duplicate/wrong matches for a subset of ids that recur under more
  than one gateway_id — the join must use (gateway_id, transaction_id).
- merchant_risk_tiers.csv has a real minority of merchants with a null
  tier_effective_to (open-ended assignment, no scheduled review). A plain
  `date <= tier_effective_to` comparison drops those merchants entirely, so
  the open-ended case needs an explicit isna() branch.
"""

import json
import os
from pathlib import Path

import pandas as pd

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(WORKSPACE_DIR / "data")))

REPORT_PATH = WORKSPACE_DIR / "fraud_loss_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END = pd.Timestamp("2024-03-31")

FRAUD_REASONS = {"fraud_card_not_present", "fraud_account_takeover", "fraud_lost_stolen_card"}


def load_data():
    merchants = pd.read_csv(DATA_DIR / "merchants.csv")
    tiers = pd.read_csv(
        DATA_DIR / "merchant_risk_tiers.csv",
        parse_dates=["tier_effective_from", "tier_effective_to"],
    )
    transactions = pd.read_csv(DATA_DIR / "transactions.csv", parse_dates=["transaction_date"])
    disputes = pd.read_csv(DATA_DIR / "fraud_disputes.csv", parse_dates=["filed_date"])
    resolutions = pd.read_csv(DATA_DIR / "dispute_resolutions.csv", parse_dates=["resolution_date"])
    case_transactions = pd.read_csv(DATA_DIR / "case_transactions.csv")
    return merchants, tiers, transactions, disputes, resolutions, case_transactions


def assign_risk_tier(transactions, tiers):
    """
    Attach the risk tier in effect for each transaction's merchant on its
    transaction_date. tier_effective_to is blank for open-ended assignments —
    treat blank as "still in effect" rather than letting the comparison
    against a null silently exclude the row.
    """
    merged = transactions.merge(tiers, on="merchant_id", how="left")
    open_ended = merged["tier_effective_to"].isna()
    in_effect = (
        (merged["tier_effective_from"] <= merged["transaction_date"]) &
        (open_ended | (merged["transaction_date"] <= merged["tier_effective_to"]))
    )
    matched = merged[in_effect].copy()
    matched["month"] = matched["transaction_date"].dt.strftime("%Y-%m")
    return matched[["gateway_id", "transaction_id", "merchant_id", "transaction_date",
                     "amount_usd", "risk_tier", "month"]]


def latest_resolution(resolutions):
    """A case may carry more than one resolution record; only the most
    recently recorded one is authoritative."""
    return (
        resolutions.sort_values("resolution_date")
        .groupby("case_id", as_index=False)
        .last()
    )


def confirmed_fraud_cases(disputes, resolutions):
    """
    A case is confirmed fraud loss when its reason_code describes an
    unauthorized transaction AND its most recent resolution held the
    merchant liable.
    """
    latest = latest_resolution(resolutions)
    joined = disputes.merge(latest, on="case_id", how="left")
    is_fraud_reason = joined["reason_code"].isin(FRAUD_REASONS)
    is_liable = joined["resolution_status"] == "lost"
    return joined.loc[is_fraud_reason & is_liable, "case_id"]


def confirmed_fraud_transactions(case_transactions, confirmed_cases, txns_with_tier):
    """
    Expand each confirmed-fraud case to every transaction associated with it
    in case_transactions.csv (not just the one reported at intake), joining
    on the composite (gateway_id, transaction_id) key — transaction_id alone
    is not unique across gateways.
    """
    confirmed_links = case_transactions[case_transactions["case_id"].isin(confirmed_cases)]
    return confirmed_links.merge(txns_with_tier, on=["gateway_id", "transaction_id"], how="inner")


def build_report(txns_with_tier, confirmed_txns):
    volume = txns_with_tier.groupby(["risk_tier", "month"]).agg(
        total_transaction_volume_usd=("amount_usd", "sum"),
        transaction_count=("amount_usd", "size"),
    ).reset_index()

    fraud = confirmed_txns.groupby(["risk_tier", "month"]).agg(
        confirmed_fraud_loss_usd=("amount_usd", "sum"),
        confirmed_fraud_count=("amount_usd", "size"),
    ).reset_index()

    report = volume.merge(fraud, on=["risk_tier", "month"], how="left")
    report["confirmed_fraud_loss_usd"] = report["confirmed_fraud_loss_usd"].fillna(0.0)
    report["confirmed_fraud_count"] = report["confirmed_fraud_count"].fillna(0).astype(int)

    report["total_transaction_volume_usd"] = report["total_transaction_volume_usd"].round(2)
    report["confirmed_fraud_loss_usd"] = report["confirmed_fraud_loss_usd"].round(2)
    report["fraud_loss_rate_bps"] = (
        report["confirmed_fraud_loss_usd"] / report["total_transaction_volume_usd"] * 10000
    ).round(2)

    report = report.sort_values(["risk_tier", "month"]).reset_index(drop=True)
    return report[["risk_tier", "month", "total_transaction_volume_usd", "transaction_count",
                    "confirmed_fraud_loss_usd", "confirmed_fraud_count", "fraud_loss_rate_bps"]]


def build_summary(report):
    total_volume = float(report["total_transaction_volume_usd"].sum())
    total_fraud = float(report["confirmed_fraud_loss_usd"].sum())

    by_tier = report.groupby("risk_tier").agg(
        total_transaction_volume_usd=("total_transaction_volume_usd", "sum"),
        confirmed_fraud_loss_usd=("confirmed_fraud_loss_usd", "sum"),
    )
    by_tier["rate_bps"] = by_tier["confirmed_fraud_loss_usd"] / by_tier["total_transaction_volume_usd"] * 10000
    highest_tier = by_tier["rate_bps"].idxmax()

    return {
        "total_transaction_volume_usd": round(total_volume, 2),
        "total_confirmed_fraud_loss_usd": round(total_fraud, 2),
        "total_confirmed_fraud_count": int(report["confirmed_fraud_count"].sum()),
        "overall_fraud_loss_rate_bps": round(total_fraud / total_volume * 10000, 2),
        "highest_loss_rate_tier": str(highest_tier),
    }


def main():
    merchants, tiers, transactions, disputes, resolutions, case_transactions = load_data()

    q1_txns = transactions[
        (transactions["transaction_date"] >= Q1_START) & (transactions["transaction_date"] <= Q1_END)
    ].copy()

    txns_with_tier = assign_risk_tier(q1_txns, tiers)
    confirmed = confirmed_fraud_cases(disputes, resolutions)
    confirmed_txns = confirmed_fraud_transactions(case_transactions, confirmed, txns_with_tier)

    report = build_report(txns_with_tier, confirmed_txns)
    summary = build_summary(report)

    report.to_csv(REPORT_PATH, index=False)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {REPORT_PATH} ({len(report)} rows)")
    print(f"Wrote {SUMMARY_PATH}")
    print(f"  total_confirmed_fraud_loss_usd : {summary['total_confirmed_fraud_loss_usd']:,.2f}")
    print(f"  overall_fraud_loss_rate_bps    : {summary['overall_fraud_loss_rate_bps']}")
    print(f"  highest_loss_rate_tier         : {summary['highest_loss_rate_tier']}")


if __name__ == "__main__":
    main()
