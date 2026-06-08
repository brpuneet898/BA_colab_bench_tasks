"""
Tests for the Q1 2024 B2B SaaS sales performance report.

What makes this task genuinely hard:

1. Deal split credit: deal_splits.csv contains attribution records. Solo deals
   appear with credit_pct = 1.0; co-sold deals have two rows summing to 1.0.
   An agent that joins deals.csv directly to the report assigns 100% of revenue
   to the primary rep — giving co-reps nothing and inflating primary totals.

2. Non-calendar fiscal quarters: the company's fiscal quarter starts Feb 1,
   not Jan 1. quotas.csv has explicit period_start / period_end columns.
   An agent that groups by calendar quarter (dt.quarter or pd.Grouper(freq='QS'))
   pulls January deals into Q1 attainment — they belong to the prior period.

3. In-period cancellations: closed-won deals can be cancelled within the same
   period. net_revenue = gross_revenue minus these cancellations.
   An agent that sums all closed_won deals inflates attainment for several
   reps, flipping some from under-quota to (incorrectly) over-quota.

4. Multi-currency FX: ~30% of deals are in EUR or GBP. fx_rates.csv provides
   daily rates keyed on (date, currency). An agent that treats deal_value
   as USD ignores the currency column entirely.

Contract (instruction.md): the agent writes
    /workspace/rep_performance_report.csv  (200 rows, 9 columns)
    /workspace/summary.json                (5 scalar keys)
"""

import json
import os
import pandas as pd
import pytest
from pathlib import Path

# ── Path resolution (harness + local) ────────────────────────────────────────

_ws_env = os.environ.get("WORKSPACE_DIR")
if _ws_env:
    WORKSPACE_DIR = Path(_ws_env)
elif Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

_data_env = os.environ.get("DATA_DIR")
if _data_env:
    DATA_DIR = Path(_data_env)
elif (WORKSPACE_DIR / "data").exists():
    DATA_DIR = WORKSPACE_DIR / "data"
else:
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

REPORT_PATH  = WORKSPACE_DIR / "rep_performance_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

PERIOD_START = "2024-02-01"
PERIOD_END   = "2024-04-30"


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Verify the six input files are present and have the expected properties."""
    deals = pd.read_csv(DATA_DIR / "deals.csv")
    assert len(deals) == 50000, "deals.csv must have exactly 50000 rows."

    non_usd = (deals["currency"] != "USD").sum()
    assert non_usd >= 10000, (
        f"At least 10000 non-USD deals (EUR/GBP) must be present (found {non_usd})."
    )

    splits = pd.read_csv(DATA_DIR / "deal_splits.csv")
    split_counts = splits.groupby("deal_id").size()
    n_cosold = int((split_counts > 1).sum())
    assert n_cosold >= 8000, (
        f"At least 8000 co-sold deals (2 rows in deal_splits) must be present "
        f"(found {n_cosold})."
    )
    assert set(splits.groupby("deal_id")["credit_pct"].sum().round(4).unique()) == {1.0}, \
        "credit_pct must sum to exactly 1.0 per deal_id."

    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv")
    assert len(cancellations) >= 2000, (
        f"At least 2000 cancellations must be present (found {len(cancellations)})."
    )

    quotas = pd.read_csv(DATA_DIR / "quotas.csv")
    assert quotas["period_start"].min() < PERIOD_START, (
        "quotas must include a period that starts before 2024-02-01 "
        "(prior fiscal period — Jan deals land there)."
    )
    assert PERIOD_START in quotas["period_start"].values, (
        f"quotas must contain a period starting {PERIOD_START}."
    )

    fx = pd.read_csv(DATA_DIR / "fx_rates.csv")
    assert set(fx["currency"].unique()) == {"EUR", "GBP"}, \
        "fx_rates.csv must contain exactly EUR and GBP rows."
    assert fx["date"].min() <= PERIOD_START, \
        "fx_rates.csv must cover dates from at least 2024-02-01."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_output_schema_and_shape():
    assert REPORT_PATH.exists(), "rep_performance_report.csv not found."
    df = pd.read_csv(REPORT_PATH)
    required_cols = {
        "rep_id", "rep_name", "region", "period_start",
        "total_deals", "gross_revenue_usd", "net_revenue_usd",
        "quota_usd", "attainment_pct",
    }
    missing = required_cols - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 200, f"Expected 200 rows (one per rep), got {len(df)}."

    assert SUMMARY_PATH.exists(), "summary.json not found."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required_keys = {
        "total_gross_revenue_usd", "total_net_revenue_usd",
        "total_quota_usd", "overall_attainment_pct", "reps_over_quota",
    }
    missing_keys = required_keys - set(s.keys())
    assert not missing_keys, f"Missing summary.json keys: {missing_keys}"
    assert isinstance(s["reps_over_quota"], int) and not isinstance(s["reps_over_quota"], bool), \
        "reps_over_quota must be an int."
    for k in required_keys - {"reps_over_quota"}:
        assert isinstance(s[k], float), f"{k} must be a float."


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    deals         = pd.read_csv(DATA_DIR / "deals.csv")
    reps          = pd.read_csv(DATA_DIR / "reps.csv")
    deal_splits   = pd.read_csv(DATA_DIR / "deal_splits.csv")
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv")
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv")
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv")

    # Trap 4: FX conversion
    fx_lookup = fx_rates.set_index(["date", "currency"])["rate_to_usd"].to_dict()
    def get_rate(row):
        if row["currency"] == "USD":
            return 1.0
        return fx_lookup.get((row["close_date"], row["currency"]), 1.0)
    deals["rate_to_usd"]    = deals.apply(get_rate, axis=1)
    deals["deal_value_usd"] = deals["deal_value"] * deals["rate_to_usd"]

    # Trap 1: split credit — drop deals.rep_id, use deal_splits
    credited = deals[["deal_id", "deal_value_usd", "close_date"]].merge(
        deal_splits, on="deal_id"
    )
    credited["credited_usd"] = credited["deal_value_usd"] * credited["credit_pct"]

    # Trap 2: fiscal period filter — Feb 1 to Apr 30 only
    in_period = credited[
        (credited["close_date"] >= PERIOD_START) &
        (credited["close_date"] <= PERIOD_END)
    ].copy()

    # Trap 3: net out in-period cancellations
    in_period_cancels = cancellations[
        (cancellations["cancelled_date"] >= PERIOD_START) &
        (cancellations["cancelled_date"] <= PERIOD_END)
    ]
    cancelled_ids = set(in_period_cancels["deal_id"])
    in_period["is_cancelled"]       = in_period["deal_id"].isin(cancelled_ids)
    in_period["net_credited_usd"]   = in_period["credited_usd"] * (~in_period["is_cancelled"])

    # Aggregate per rep
    agg = in_period.groupby("rep_id").agg(
        total_deals       =("deal_id",           "nunique"),
        gross_revenue_usd =("credited_usd",       "sum"),
        net_revenue_usd   =("net_credited_usd",   "sum"),
    ).reset_index()

    period_quotas = quotas[quotas["period_start"] == PERIOD_START].copy()
    report = reps.merge(agg, on="rep_id", how="left")
    report[["total_deals", "gross_revenue_usd", "net_revenue_usd"]] = (
        report[["total_deals", "gross_revenue_usd", "net_revenue_usd"]].fillna(0)
    )
    report["total_deals"] = report["total_deals"].astype(int)
    report = report.merge(period_quotas[["rep_id", "quota_usd"]], on="rep_id")
    report["attainment_pct"] = (report["net_revenue_usd"] / report["quota_usd"] * 100).round(2)
    report["period_start"] = PERIOD_START
    report = report.sort_values("rep_id").reset_index(drop=True)

    return {
        "report":                  report,
        "total_gross_revenue_usd": float(round(report["gross_revenue_usd"].sum(), 2)),
        "total_net_revenue_usd":   float(round(report["net_revenue_usd"].sum(),   2)),
        "total_quota_usd":         float(round(report["quota_usd"].sum(),          2)),
        "overall_attainment_pct":  float(round(
            report["net_revenue_usd"].sum() / report["quota_usd"].sum() * 100, 2
        )),
        "reps_over_quota":         int((report["net_revenue_usd"] > report["quota_usd"]).sum()),
        # For Trap 3 test: gross-based over-quota count
        "reps_over_quota_gross":   int((report["gross_revenue_usd"] > report["quota_usd"]).sum()),
    }


# ── Trap 1: Deal split credit ─────────────────────────────────────────────────

def test_case_03_total_revenue_with_splits(ground_truth):
    """Total gross revenue must equal Σ(deal_value_usd × credit_pct).

    Wrong if deal_splits is ignored and 100% credit given to deals.rep_id —
    that double-counts revenue for co-sold deals (primary rep gets full value
    instead of their share).
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    gt = ground_truth["total_gross_revenue_usd"]
    assert abs(s["total_gross_revenue_usd"] - gt) <= 5.0, (
        f"total_gross_revenue_usd: got {s['total_gross_revenue_usd']:.2f}, "
        f"expected {gt:.2f}. "
        f"Check that deal_splits.csv is used for credit attribution, not deals.rep_id."
    )


def test_case_04_per_rep_revenue_for_split_deals(ground_truth):
    """Reps who have co-sold deals must show correct partial revenue.

    QC NOTE: Deal Splits Tolerance
    We test the 10 reps with the highest co-sold volume. A ±2.0 USD tolerance
    is used to allow for float precision differences in merge/sum orders. If an
    agent misses the deal_splits table, the error will be in the tens of thousands.
    """
    splits     = pd.read_csv(DATA_DIR / "deal_splits.csv")
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    # Find reps with highest co-sold exposure (most revenue from partial-credit deals)
    partial = splits[splits["credit_pct"] < 1.0]
    top_partial_reps = (
        partial.groupby("rep_id")["credit_pct"].count()
        .sort_values(ascending=False)
        .head(10)
        .index.tolist()
    )

    errors = []
    for rep_id in top_partial_reps:
        gt_row  = gt_report[gt_report["rep_id"] == rep_id]
        got_row = report_got[report_got["rep_id"] == rep_id]
        if gt_row.empty or got_row.empty:
            continue
        gt_val  = float(gt_row["gross_revenue_usd"].iloc[0])
        got_val = float(got_row["gross_revenue_usd"].iloc[0])
        if abs(got_val - gt_val) > 2.0:
            errors.append(
                f"  {rep_id}: got {got_val:.2f}, expected {gt_val:.2f}"
            )

    assert not errors, (
        f"{len(errors)} reps with co-sold deals have wrong gross_revenue_usd.\n"
        "deal_splits.credit_pct must be applied; using deals.rep_id gives 100% to "
        "the primary rep:\n" + "\n".join(errors[:5])
    )


# ── Trap 2: Non-calendar fiscal quarter ──────────────────────────────────────

def test_case_05_january_deals_excluded_from_report(ground_truth):
    """January deals (close_date < 2024-02-01) must not count toward Q1 attainment.

    They belong to the prior fiscal period (Nov 1 – Jan 31). An agent using
    calendar Q1 (Jan–Mar) pulls these in, inflating revenue for every rep
    who closed deals in January.
    """
    deals   = pd.read_csv(DATA_DIR / "deals.csv")
    splits  = pd.read_csv(DATA_DIR / "deal_splits.csv")
    fx      = pd.read_csv(DATA_DIR / "fx_rates.csv")
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    # Compute how much each rep would gain by incorrectly including Jan deals
    fx_lookup = fx.set_index(["date", "currency"])["rate_to_usd"].to_dict()
    jan_deals = deals[deals["close_date"] < PERIOD_START].copy()

    def get_rate(row):
        if row["currency"] == "USD":
            return 1.0
        return fx_lookup.get((row["close_date"], row["currency"]), 1.0)

    jan_deals["rate_to_usd"]    = jan_deals.apply(get_rate, axis=1)
    jan_deals["deal_value_usd"] = jan_deals["deal_value"] * jan_deals["rate_to_usd"]
    jan_credited = jan_deals[["deal_id", "deal_value_usd"]].merge(splits, on="deal_id")
    jan_credited["jan_credited_usd"] = jan_credited["deal_value_usd"] * jan_credited["credit_pct"]
    jan_per_rep = jan_credited.groupby("rep_id")["jan_credited_usd"].sum()

    # For reps with non-trivial January revenue, verify the report matches ground truth
    errors = []
    for rep_id, jan_rev in jan_per_rep.items():
        if jan_rev < 10_000:
            continue
        gt_row  = gt_report[gt_report["rep_id"] == rep_id]
        got_row = report_got[report_got["rep_id"] == rep_id]
        if gt_row.empty or got_row.empty:
            continue
        gt_val  = float(gt_row["gross_revenue_usd"].iloc[0])
        got_val = float(got_row["gross_revenue_usd"].iloc[0])
        if abs(got_val - gt_val) > 2.0:
            errors.append(
                f"  {rep_id}: got {got_val:.2f}, expected {gt_val:.2f} "
                f"(Jan revenue that must be excluded: {jan_rev:.2f})"
            )

    assert not errors, (
        f"{len(errors)} reps show wrong revenue — likely because January deals "
        f"(close_date < {PERIOD_START}) were incorrectly included.\n"
        "The fiscal period starts 2024-02-01 (see quotas.period_start), not Jan 1.\n"
        + "\n".join(errors[:5])
    )


def test_case_06_quota_attainment_pct_correctness(ground_truth):
    """attainment_pct must be correct for at least 190/200 reps.

    QC NOTE: Attainment Threshold
    We use a ±0.5 pct point tolerance and require 190/200 reps to pass. This
    allows for minor rounding discrepancies but strictly catches agents that use
    the calendar quarter (which affects ~100+ reps) or gross revenue (affects ~100+).
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    merged = report_got.merge(
        gt_report[["rep_id", "attainment_pct"]],
        on="rep_id",
        suffixes=("_got", "_exp"),
    )
    close = (abs(merged["attainment_pct_got"] - merged["attainment_pct_exp"]) <= 0.5).sum()
    assert close >= 190, (
        f"Only {close}/200 reps have correct attainment_pct (within ±0.5 pct points). "
        f"Check fiscal period boundaries (period_start = {PERIOD_START}, not 2024-01-01) "
        f"and that net_revenue_usd is used, not gross."
    )


# ── Trap 3: In-period cancellations ──────────────────────────────────────────

def test_case_07_gross_and_net_revenue_correctness(ground_truth):
    """Both gross and net revenue totals must match ground truth.

    gross_revenue_usd = Σ(credited_usd) before cancellations.
    net_revenue_usd   = Σ(credited_usd) minus cancelled deals.
    If only one is wrong, it pinpoints whether cancellations were applied.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    gt_gross = ground_truth["total_gross_revenue_usd"]
    gt_net   = ground_truth["total_net_revenue_usd"]

    assert abs(s["total_gross_revenue_usd"] - gt_gross) <= 5.0, (
        f"total_gross_revenue_usd: got {s['total_gross_revenue_usd']:.2f}, "
        f"expected {gt_gross:.2f}."
    )
    assert abs(s["total_net_revenue_usd"] - gt_net) <= 5.0, (
        f"total_net_revenue_usd: got {s['total_net_revenue_usd']:.2f}, "
        f"expected {gt_net:.2f}. "
        f"Cancellations within {PERIOD_START}–{PERIOD_END} must be subtracted from revenue."
    )


def test_case_08_reps_over_quota_count(ground_truth):
    """The count of reps where net_revenue_usd > quota_usd must be correct.

    QC NOTE: Gross vs Net Flips
    We test exact integer match here because generate_data.py specifically targets
    deals of over-quota reps for cancellation. This creates a large enough gap
    between gross reps_over_quota and net reps_over_quota that float precision
    issues will not cause off-by-one errors.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    gt_net   = ground_truth["reps_over_quota"]
    gt_gross = ground_truth["reps_over_quota_gross"]

    if gt_gross != gt_net:
        assert s["reps_over_quota"] == gt_net, (
            f"reps_over_quota: got {s['reps_over_quota']}, expected {gt_net}. "
            f"On gross revenue it would be {gt_gross} — cancellations flip "
            f"{gt_gross - gt_net} rep(s) from over to under quota."
        )
    else:
        assert s["reps_over_quota"] == gt_net, (
            f"reps_over_quota: got {s['reps_over_quota']}, expected {gt_net}."
        )


# ── Trap 4: Multi-currency FX ─────────────────────────────────────────────────

def test_case_09_non_usd_rep_revenue_accuracy(ground_truth):
    """Reps with predominantly non-USD deals must have correct USD revenue.

    An agent that treats deal_value as USD misprices these reps entirely.
    """
    deals      = pd.read_csv(DATA_DIR / "deals.csv")
    splits     = pd.read_csv(DATA_DIR / "deal_splits.csv")
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    non_usd_deals = deals[deals["currency"] != "USD"][["deal_id"]].copy()
    non_usd_credited = non_usd_deals.merge(splits, on="deal_id")
    top_non_usd_reps = (
        non_usd_credited.groupby("rep_id")["deal_id"].count()
        .sort_values(ascending=False)
        .head(10)
        .index.tolist()
    )

    errors = []
    for rep_id in top_non_usd_reps:
        gt_row  = gt_report[gt_report["rep_id"] == rep_id]
        got_row = report_got[report_got["rep_id"] == rep_id]
        if gt_row.empty or got_row.empty:
            continue
        gt_val  = float(gt_row["net_revenue_usd"].iloc[0])
        got_val = float(got_row["net_revenue_usd"].iloc[0])
        if abs(got_val - gt_val) > 2.0:
            errors.append(
                f"  {rep_id}: got {got_val:.2f}, expected {gt_val:.2f}"
            )

    assert not errors, (
        f"{len(errors)} reps with high non-USD deal exposure have wrong net_revenue_usd.\n"
        "EUR/GBP deal_value must be converted using fx_rates.csv on the close_date:\n"
        + "\n".join(errors[:5])
    )


def test_case_10_total_usd_revenue_accuracy(ground_truth):
    """total_net_revenue_usd and overall_attainment_pct must both be correct.

    This is the combined-trap test: wrong if any of FX conversion, deal splits,
    fiscal period, or cancellations is mishandled.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    gt_net  = ground_truth["total_net_revenue_usd"]
    gt_att  = ground_truth["overall_attainment_pct"]

    assert abs(s["total_net_revenue_usd"] - gt_net) <= 5.0, (
        f"total_net_revenue_usd: got {s['total_net_revenue_usd']:.2f}, "
        f"expected {gt_net:.2f}. "
        f"Combined error from FX, splits, period filter, or cancellations."
    )
    assert abs(s["overall_attainment_pct"] - gt_att) <= 0.5, (
        f"overall_attainment_pct: got {s['overall_attainment_pct']:.2f}, "
        f"expected {gt_att:.2f} (±0.5 pct points)."
    )
