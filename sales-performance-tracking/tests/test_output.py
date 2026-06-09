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
    import sys
    sys.path.append(str(Path(__file__).parent.parent / "solution"))
    try:
        import solve
        deals, reps, deal_splits, quotas, cancellations, fx_rates = solve.load_data()
        deals = solve.convert_to_arr_usd(deals, fx_rates)
        credited = solve.apply_splits(deals, deal_splits)
        credited_period = solve.filter_period(credited, "close_date")
        credited_period = solve.net_cancellations(credited_period, cancellations)
        reps_quotas = solve.prorate_quotas(reps, quotas)
        report = solve.build_report(credited_period, reps_quotas)
        return {
            "report": report,
            "reps_quotas": reps_quotas
        }
    except ImportError:
        pytest.skip("Oracle solution script not found; cannot generate ground truth.")


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
    assert len(deal_splits) == 60066, "deal_splits.csv has been modified"
    assert len(quotas) == 800, "quotas.csv has been modified"
    
    assert "quote_convention" in fx_rates.columns, "fx_rates.csv has been modified"
    assert "total_contract_value" in deals.columns, "deals.csv has been modified"


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
