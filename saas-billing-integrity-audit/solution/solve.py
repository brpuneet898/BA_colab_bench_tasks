"""
H1 2025 billing integrity audit for a B2B SaaS company.

After exploring the data:
- amendments.csv has a credit_method column.  The billing system ALWAYS computes
  credited_amount using monthly proration (credited_periods × reduction, no partial
  month).  Amendments flagged credit_method='daily_prorate' are therefore
  systematically under-credited: the partial-month fraction of the reduction is
  missing.  The gap per amendment is reduction × (remaining_days / days_in_month).

- usage_records.csv has billing_period_id encoding the anniversary date (contract
  start day-of-month), not the calendar 1st.  The column cumulative_units_ytd is
  a YTD running total from Jan 1 — a red herring for per-period overage.  Correct
  overage requires grouping by billing_period_id.

- accounts.csv marks reseller accounts with is_reseller=True and reseller_margin_pct.
  invoices.csv records gross amounts for every account; recognised revenue for
  resellers is gross × margin_pct, so summing all invoices overstates reseller
  revenue by a factor of 3–5×.

- sla_records.csv has one row per (account, period, metric).  When multiple metrics
  breach in the same period, only the highest-severity credit applies — stacking
  all rows overcounts.

- Data cleaning required:
    • invoice_line_items.unit_price: some manually-entered rows use "$X,XXX.XX"
      or European "X.XXX,XX" notation; pd.to_numeric(errors='coerce') silently
      converts these to NaN.
    • invoices.void_reason is populated on some non-void invoices (UI voided,
      status field not synced); those invoices must be excluded.
    • Some invoices have parent_invoice_id set (consolidated billing children);
      including them with the parent double-counts the same period.
    • is_test_account=True accounts must be excluded throughout.
    • usage_records.is_test_usage=True rows must be excluded.
"""

import json
from calendar import monthrange
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path("/workspace")
DATA = WORKSPACE / "data"


def load_data():
    return {
        "accounts":     pd.read_csv(DATA / "accounts.csv"),
        "contracts":    pd.read_csv(DATA / "contracts.csv"),
        "amendments":   pd.read_csv(DATA / "amendments.csv"),
        "usage":        pd.read_csv(DATA / "usage_records.csv"),
        "invoices":     pd.read_csv(DATA / "invoices.csv"),
        "line_items":   pd.read_csv(DATA / "invoice_line_items.csv"),
        "payments":     pd.read_csv(DATA / "payments.csv"),
        "credit_memos": pd.read_csv(DATA / "credit_memos.csv"),
        "sla":          pd.read_csv(DATA / "sla_records.csv"),
        "products":     pd.read_csv(DATA / "products.csv"),
        "fx":           pd.read_csv(DATA / "fx_rates.csv"),
    }


def get_test_account_ids(accounts_df):
    return set(accounts_df.loc[accounts_df["is_test_account"] == True, "account_id"])


def valid_invoices(inv_df, test_ids):
    """
    Return invoices that count toward recognised revenue.

    Exclusions:
      1. Test accounts
      2. status = 'void'
      3. Ghost-voided: void_reason populated but status != 'void' (UI void, field unsync'd)
      4. Consolidated children: parent_invoice_id populated (double-counts against parent)
    """
    df = inv_df[~inv_df["account_id"].isin(test_ids)].copy()
    df = df[df["status"] != "void"]
    # BOTTLENECK: ghost-voided invoices have a non-null void_reason even though
    # status is 'paid' / 'sent' / 'overdue'.  Excluding on status='void' alone
    # leaves these in, inflating valid revenue.
    df = df[df["void_reason"].isna()]
    # BOTTLENECK: consolidated children carry parent_invoice_id; summing them
    # alongside the parent invoice counts the same billing period twice.
    df = df[df["parent_invoice_id"].isna()]
    return df


def trap1_amendment_credit_gap(amds_df, test_ids):
    """
    daily_prorate amendments: credited_amount uses monthly proration (no partial month).
    The gap is the missing partial-month fraction.
    """
    daily_red = amds_df[
        (amds_df["amendment_type"] == "reduction") &
        (amds_df["credit_method"] == "daily_prorate") &
        (~amds_df["account_id"].isin(test_ids))
    ].copy()

    rows = []
    for _, amd in daily_red.iterrows():
        eff = date.fromisoformat(str(amd["effective_date"]))
        reduction = float(amd["old_mrr"]) - float(amd["new_mrr"])
        period_days = monthrange(eff.year, eff.month)[1]
        # BOTTLENECK: remaining_days includes the effective date itself.
        # The billing system credits zero for the partial month (monthly_prorate
        # only counts whole months remaining); daily_prorate should credit the
        # partial fraction reduction × (remaining_days / period_days).
        remaining_days = (date(eff.year, eff.month, period_days) - eff).days + 1
        gap = round(reduction * (remaining_days / period_days), 2)

        rows.append({
            "discrepancy_type":   "amendment_undercredit",
            "account_id":         amd["account_id"],
            "contract_id":        amd["contract_id"],
            "reference_id":       amd["amendment_id"],
            "period":             f"{eff.year}-{eff.month:02d}",
            "billed_amount_usd":  round(float(amd["credited_amount"]), 2),
            "correct_amount_usd": round(float(amd["credited_amount"]) + gap, 2),
            "discrepancy_usd":    gap,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["discrepancy_type","account_id","contract_id","reference_id",
                 "period","billed_amount_usd","correct_amount_usd","discrepancy_usd"]
    )


def trap2_unbilled_overage(usage_df, prods_df, test_ids):
    """
    Compute unbilled API / storage overage using anniversary billing periods.
    billing_period_id is already anniversary-aligned in the data.
    """
    df = usage_df[
        (usage_df["is_test_usage"] == False) &
        (~usage_df["account_id"].isin(test_ids))
    ].copy()

    overage_rates = prods_df.set_index("sku")["overage_rate"].to_dict()

    # BOTTLENECK: grouping by record_date.dt.to_period('M') uses calendar months;
    # billing_period_id encodes the anniversary window (contract start day-of-month).
    # For contracts starting mid-month, calendar grouping splits anniversary periods
    # across two months, changing which periods hit the included-unit ceiling.
    grouped = (
        df.groupby(["contract_id", "sku", "billing_period_id"])
        .agg(
            total_units=("units_consumed", "sum"),
            included_units=("units_included_monthly", "first"),
            account_id=("account_id", "first"),
        )
        .reset_index()
    )

    grouped["overage_units"] = (grouped["total_units"] - grouped["included_units"]).clip(lower=0)
    grouped["rate"] = grouped["sku"].map(overage_rates).fillna(0.0)
    grouped["overage_usd"] = (grouped["overage_units"] * grouped["rate"]).round(2)

    has_overage = grouped[grouped["overage_usd"] > 0].copy()

    rows = []
    for _, r in has_overage.iterrows():
        rows.append({
            "discrepancy_type":   "unbilled_overage",
            "account_id":         r["account_id"],
            "contract_id":        r["contract_id"],
            "reference_id":       f"{r['contract_id']}_{r['sku']}_{r['billing_period_id']}",
            "period":             r["billing_period_id"],
            "billed_amount_usd":  0.0,
            "correct_amount_usd": round(float(r["overage_usd"]), 2),
            "discrepancy_usd":    round(float(r["overage_usd"]), 2),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["discrepancy_type","account_id","contract_id","reference_id",
                 "period","billed_amount_usd","correct_amount_usd","discrepancy_usd"]
    )


def trap3_reseller_overstatement(inv_valid, accounts_df):
    """
    Reseller recognised revenue = gross × reseller_margin_pct.
    invoices.csv records gross; the overstatement is gross × (1 - margin_pct).
    """
    resellers = accounts_df[accounts_df["is_reseller"] == True][
        ["account_id", "reseller_margin_pct"]
    ].copy()
    resellers["reseller_margin_pct"] = pd.to_numeric(
        resellers["reseller_margin_pct"], errors="coerce"
    )

    # BOTTLENECK: must use valid invoices only (ghost-voided and children excluded)
    # before computing gross revenue; including voided children inflates gross.
    res_invs = inv_valid[inv_valid["account_id"].isin(resellers["account_id"])]
    gross_by_acct = (
        res_invs.groupby("account_id")["usd_equivalent"]
        .sum()
        .reset_index()
        .rename(columns={"usd_equivalent": "gross_usd"})
    )
    gross_by_acct = gross_by_acct.merge(resellers, on="account_id")
    gross_by_acct["net_usd"] = (
        gross_by_acct["gross_usd"] * gross_by_acct["reseller_margin_pct"]
    ).round(2)
    gross_by_acct["overstatement"] = (
        gross_by_acct["gross_usd"] - gross_by_acct["net_usd"]
    ).round(2)

    rows = []
    for _, r in gross_by_acct.iterrows():
        rows.append({
            "discrepancy_type":   "reseller_overstatement",
            "account_id":         r["account_id"],
            "contract_id":        "",
            "reference_id":       r["account_id"],
            "period":             "H1-2025",
            "billed_amount_usd":  round(float(r["gross_usd"]), 2),
            "correct_amount_usd": round(float(r["net_usd"]), 2),
            "discrepancy_usd":    round(float(r["overstatement"]), 2),
        })

    total_gross = float(gross_by_acct["gross_usd"].sum())
    total_net = float(gross_by_acct["net_usd"].sum())
    return (
        pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["discrepancy_type","account_id","contract_id","reference_id",
                     "period","billed_amount_usd","correct_amount_usd","discrepancy_usd"]
        ),
        total_gross,
        total_net,
    )


def trap4_sla_credit_stacking(sla_df, test_ids):
    """
    Only the highest-severity credit per (account, period) applies.
    Rows with n_breaches >= 2 have been over-credited by sum - max.
    """
    df = sla_df[~sla_df["account_id"].isin(test_ids)].copy()

    grouped = (
        df.groupby(["account_id", "period_start"])
        .agg(
            total_credit=("credit_calculated", "sum"),
            max_credit=("credit_calculated", "max"),
            n_breaches=("credit_calculated", "count"),
            contract_id=("contract_id", "first"),
        )
        .reset_index()
    )

    # BOTTLENECK: must group by BOTH account_id AND period_start.  Grouping by
    # period_start alone merges different accounts' credits; by account_id alone
    # sums across months.  Only (account, period) uniquely identifies one billing cycle.
    multi = grouped[grouped["n_breaches"] >= 2].copy()
    multi["overcount"] = (multi["total_credit"] - multi["max_credit"]).round(2)

    rows = []
    for _, r in multi.iterrows():
        rows.append({
            "discrepancy_type":   "sla_credit_overcount",
            "account_id":         r["account_id"],
            "contract_id":        r["contract_id"],
            "reference_id":       f"{r['account_id']}_{r['period_start']}",
            "period":             str(r["period_start"])[:7],
            "billed_amount_usd":  round(float(r["total_credit"]), 2),
            "correct_amount_usd": round(float(r["max_credit"]), 2),
            "discrepancy_usd":    round(float(r["overcount"]), 2),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["discrepancy_type","account_id","contract_id","reference_id",
                 "period","billed_amount_usd","correct_amount_usd","discrepancy_usd"]
    )


def main():
    d = load_data()
    test_ids = get_test_account_ids(d["accounts"])

    inv_valid = valid_invoices(d["invoices"], test_ids)
    valid_count = int(len(inv_valid))

    amd_rows = trap1_amendment_credit_gap(d["amendments"], test_ids)
    use_rows = trap2_unbilled_overage(d["usage"], d["products"], test_ids)
    res_rows, gross_rev, net_rev = trap3_reseller_overstatement(inv_valid, d["accounts"])
    sla_rows = trap4_sla_credit_stacking(d["sla"], test_ids)

    audit = pd.concat([amd_rows, use_rows, res_rows, sla_rows], ignore_index=True)
    for col in ("billed_amount_usd", "correct_amount_usd", "discrepancy_usd"):
        audit[col] = audit[col].round(2)

    audit.to_csv(WORKSPACE / "billing_audit.csv", index=False)

    amd_gap      = float(round(amd_rows["discrepancy_usd"].sum(), 2))
    sla_over     = float(round(sla_rows["discrepancy_usd"].sum(), 2))
    res_over     = float(round(res_rows["discrepancy_usd"].sum(), 2))
    use_over     = float(round(use_rows["discrepancy_usd"].sum(), 2))
    total        = float(round(amd_gap + sla_over + res_over + use_over, 2))
    accts_w_disc = int(audit["account_id"].nunique())

    # Canonical bucket order: amendment, usage, reseller, sla
    summary = {
        "discrepancy_bucket_1_usd": float(round(amd_gap, 2)),
        "discrepancy_bucket_2_usd": float(round(use_over, 2)),
        "discrepancy_bucket_3_usd": float(round(res_over, 2)),
        "discrepancy_bucket_4_usd": float(round(sla_over, 2)),
        "total_discrepancy_usd":    float(round(total, 2)),
        "accounts_with_discrepancies": int(accts_w_disc),
        "valid_invoice_count":         int(valid_count),
    }

    with open(WORKSPACE / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"billing_audit.csv : {len(audit)} rows")
    print(f"summary.json      : {summary}")


if __name__ == "__main__":
    main()
