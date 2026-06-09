import json
import os
import pandas as pd
import pytest
from pathlib import Path

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
elif Path("/workspace/data").exists():
    DATA_DIR = Path("/workspace/data")
else:
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

REPORT_PATH  = WORKSPACE_DIR / "rep_performance_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"


@pytest.fixture(scope="module")
def ground_truth():
    deals         = pd.read_csv(DATA_DIR / "deals.csv", parse_dates=["close_date"])
    reps          = pd.read_csv(DATA_DIR / "reps.csv", parse_dates=["hire_date"])
    deal_splits   = pd.read_csv(DATA_DIR / "deal_splits.csv")
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv", parse_dates=["period_start"])
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv", parse_dates=["cancelled_date"])
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv", parse_dates=["date"])

    PERIOD_START = pd.Timestamp("2024-02-01")
    PERIOD_END   = pd.Timestamp("2024-04-30")

    # Trap 5: filter to closed_won only before any revenue calculation
    deals = deals[deals["stage"] == "closed_won"].copy()

    # convert_to_arr_usd
    deals["arr_local"] = deals["total_contract_value"] / deals["contract_months"] * 12.0
    fx = fx_rates.rename(columns={"date": "close_date"})
    usd_deals = deals[deals["currency"] == "USD"].copy()
    usd_deals["arr_usd"] = usd_deals["arr_local"]
    non_usd = deals[deals["currency"] != "USD"].copy()
    non_usd = non_usd.merge(fx, on=["close_date", "currency"], how="left")
    import numpy as np
    non_usd["arr_usd"] = np.where(
        non_usd["quote_convention"] == "USD_per_Unit",
        non_usd["arr_local"] * non_usd["rate"],
        non_usd["arr_local"] / non_usd["rate"]
    )
    all_deals = pd.concat([usd_deals, non_usd], ignore_index=True)

    # apply_splits
    credited = all_deals[["region", "deal_id", "arr_usd", "close_date"]].merge(
        deal_splits, on=["region", "deal_id"]
    )
    credited["credited_arr"] = credited["arr_usd"] * credited["credit_pct"]

    # filter_period
    credited_period = credited[(credited["close_date"] >= PERIOD_START) & (credited["close_date"] <= PERIOD_END)].copy()

    # net_cancellations
    in_period_cancels = cancellations[(cancellations["cancelled_date"] >= PERIOD_START) & (cancellations["cancelled_date"] <= PERIOD_END)].copy()
    cancelled_keys = set(zip(in_period_cancels["region"], in_period_cancels["deal_id"]))
    credited_period["is_cancelled"] = credited_period.apply(
        lambda r: (r["region"], r["deal_id"]) in cancelled_keys, axis=1
    )
    credited_period["net_credited_arr"] = credited_period["credited_arr"] * (~credited_period["is_cancelled"])

    # prorate_quotas
    q = quotas[quotas["period_start"] == PERIOD_START].copy()
    rq = reps.merge(q[["rep_id", "quota_usd", "period_start"]], on="rep_id")
    start_dates = rq[["hire_date"]].assign(period_start=PERIOD_START).max(axis=1)
    days_active = (PERIOD_END - start_dates).dt.days + 1
    days_active = days_active.clip(lower=0, upper=90)
    rq["active_quota_usd"] = rq["quota_usd"] * (days_active / 90.0)

    # build_report
    agg = credited_period.groupby("rep_id").agg(
        total_deals    =("deal_id",         "nunique"),
        gross_arr_usd=("credited_arr",  "sum"),
        net_arr_usd  =("net_credited_arr", "sum"),
    ).reset_index()

    report = rq.merge(agg, on="rep_id", how="left")
    report[["total_deals", "gross_arr_usd", "net_arr_usd"]] = (
        report[["total_deals", "gross_arr_usd", "net_arr_usd"]].fillna(0)
    )
    report["total_deals"] = report["total_deals"].astype(int)

    report["attainment_pct"] = np.where(
        report["active_quota_usd"] > 0,
        (report["net_arr_usd"] / report["active_quota_usd"] * 100),
        0.0
    ).round(2)

    report = report.drop(columns=["quota_usd"]).rename(columns={"active_quota_usd": "quota_usd"})
    cols = ["rep_id", "rep_name", "region", "period_start",
            "total_deals", "gross_arr_usd", "net_arr_usd",
            "quota_usd", "attainment_pct"]
    report["period_start"] = report["period_start"].dt.strftime("%Y-%m-%d")
    report = report[cols].sort_values("rep_id").reset_index(drop=True)

    return {
        "report": report,
        "reps_quotas": rq
    }


def test_case_01_input_data_not_tampered():
    deals = pd.read_csv(DATA_DIR / "deals.csv")
    fx_rates = pd.read_csv(DATA_DIR / "fx_rates.csv")
    reps = pd.read_csv(DATA_DIR / "reps.csv")
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv")
    deal_splits = pd.read_csv(DATA_DIR / "deal_splits.csv")
    quotas = pd.read_csv(DATA_DIR / "quotas.csv")
    
    assert len(deals) == 50000, "deals.csv has been modified"
    assert len(reps) == 200, "reps.csv has been modified"
    assert len(cancellations) == 3000, "cancellations.csv has been modified"
    assert len(deal_splits) == 59954, "deal_splits.csv has been modified"
    assert len(quotas) == 800, "quotas.csv has been modified"
    
    assert "quote_convention" in fx_rates.columns, "fx_rates.csv has been modified"
    assert "total_contract_value" in deals.columns, "deals.csv has been modified"
    # Trap 5 sentinel: deals.csv must contain non-closed_won rows
    assert set(deals["stage"].unique()) > {"closed_won"}, "stage column must have multiple values"


def test_case_02_output_schema_and_shape():
    assert REPORT_PATH.exists(), f"{REPORT_PATH.name} is missing."
    assert SUMMARY_PATH.exists(), f"{SUMMARY_PATH.name} is missing."

    report = pd.read_csv(REPORT_PATH)
    assert len(report) == 200, f"Expected 200 reps, got {len(report)}"
    
    expected_cols = [
        "rep_id", "rep_name", "region", "period_start",
        "total_deals", "gross_arr_usd", "net_arr_usd",
        "quota_usd", "attainment_pct"
    ]
    for col in expected_cols:
        assert col in report.columns, f"Missing column: {col}"


def test_case_03_trap_region_deal_id_explosion(ground_truth):
    """
    Trap 1: Region-Scoped Primary Keys
    If the agent merges deals and deal_splits on deal_id only, the deal count and revenue
    will explode across regions.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    got_total_deals = report_got["total_deals"].sum()
    gt_total_deals  = gt_report["total_deals"].sum()

    assert abs(got_total_deals - gt_total_deals) < 10, (
        f"Total deals mismatch. Expected ~{gt_total_deals}, got {got_total_deals}. "
        "Did you merge on region + deal_id?"
    )


def test_case_04_trap_indirect_fx_quotes(ground_truth):
    """
    Trap 2: Direct vs Indirect FX Quotes
    If the agent multiplies JPY/CAD deals instead of dividing based on quote_convention,
    APAC and NA revenues will be massively inflated.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    apac_got = report_got[report_got["region"] == "APAC"]["gross_arr_usd"].sum()
    apac_gt  = gt_report[gt_report["region"] == "APAC"]["gross_arr_usd"].sum()

    assert abs(apac_got - apac_gt) < 10000, (
        "APAC revenue is incorrect. Check how Units_per_USD quotes are handled for JPY."
    )


def test_case_05_trap_arr_vs_tcv(ground_truth):
    """
    Trap 3: ARR vs TCV
    If the agent sums TCV instead of prorating to ARR (TCV / months * 12),
    gross_arr_usd will be incorrect for almost everyone.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    total_gross_got = report_got["gross_arr_usd"].sum()
    total_gross_gt  = gt_report["gross_arr_usd"].sum()

    # The difference between TCV and ARR sums will be tens of millions
    assert abs(total_gross_got - total_gross_gt) < 1000, (
        f"Gross ARR mismatch. Expected {total_gross_gt:,.2f}, got {total_gross_got:,.2f}. "
        "Did you convert TCV to ARR using contract_months?"
    )


def test_case_06_trap_quota_proration(ground_truth):
    """
    Trap 4: Mid-Quarter Hire Quota Proration
    If the agent uses the flat quota for reps hired mid-quarter, their quota_usd
    will be too high, and their attainment_pct will be too low.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]
    rq         = ground_truth["reps_quotas"]

    # Find reps hired in Q1 2024 who should be prorated
    mid_quarter_reps = rq[rq["active_quota_usd"] < rq["quota_usd"]]["rep_id"]
    
    assert len(mid_quarter_reps) > 0, "Test data generation error: no mid-quarter hires."

    for rep in mid_quarter_reps[:5]:
        got_q = report_got[report_got["rep_id"] == rep]["quota_usd"].iloc[0]
        gt_q  = gt_report[gt_report["rep_id"] == rep]["quota_usd"].iloc[0]
        assert abs(got_q - gt_q) < 0.1, (
            f"Quota for mid-quarter hire {rep} not prorated correctly. "
            f"Expected {gt_q:,.2f}, got {got_q:,.2f}."
        )


def test_case_07_gross_and_net_revenue_accuracy(ground_truth):
    """General accuracy check for gross and net ARR per rep."""
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    merged = report_got.merge(gt_report, on="rep_id", suffixes=("_got", "_gt"))
    
    gross_diff = (merged["gross_arr_usd_got"] - merged["gross_arr_usd_gt"]).abs()
    net_diff   = (merged["net_arr_usd_got"] - merged["net_arr_usd_gt"]).abs()

    # Allow tiny float precision gaps
    assert (gross_diff < 1.0).all(), "Some reps have incorrect gross_arr_usd"
    assert (net_diff < 1.0).all(), "Some reps have incorrect net_arr_usd (check cancellations)"


def test_case_08_reps_over_quota_count(ground_truth):
    """Check summary metric."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    report_got = pd.read_csv(REPORT_PATH)
    # Re-calculate over-quota reps from their report output
    reps_over_quota = (report_got["net_arr_usd"] > report_got["quota_usd"]).sum()
    
    assert int(s["reps_over_quota"]) == reps_over_quota, (
        "summary.json reps_over_quota does not match report output."
    )


def test_case_09_stage_filter_deal_count(ground_truth):
    """
    Trap 5 (part A): Stage filter — total_deals count.
    deals.csv contains closed_lost and prospecting rows that also appear in
    deal_splits.csv. An agent that does not filter by stage will count
    non-won deals, inflating total_deals for every rep.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    merged = report_got.merge(gt_report, on="rep_id", suffixes=("_got", "_gt"))
    deal_diff = (merged["total_deals_got"] - merged["total_deals_gt"]).abs()

    assert deal_diff.sum() == 0, (
        f"{(deal_diff > 0).sum()} reps have wrong total_deals count. "
        "Ensure only closed_won deals are counted."
    )


def test_case_10_stage_filter_revenue(ground_truth):
    """
    Trap 5 (part B): Stage filter — gross ARR inflation.
    Non-closed_won deals carry non-trivial revenue. An agent that includes
    them will overstate gross_arr_usd by ~28% across the team.
    """
    report_got = pd.read_csv(REPORT_PATH)
    gt_report  = ground_truth["report"]

    total_gross_got = report_got["gross_arr_usd"].sum()
    total_gross_gt  = gt_report["gross_arr_usd"].sum()

    # If stage is not filtered, the inflated sum will differ by millions
    assert abs(total_gross_got - total_gross_gt) < 500, (
        f"Total gross ARR differs by {abs(total_gross_got - total_gross_gt):,.0f}. "
        "Non-closed_won deals are likely included in revenue."
    )
