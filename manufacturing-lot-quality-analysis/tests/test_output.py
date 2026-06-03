"""
Tests for the Q1 2024 manufacturing lot quality report.

The task requires computing pass rates, mean specification deviation, and cost of
poor quality for a contract manufacturer's three production lines across Q1 2024.

Several implementation details affect correctness:

1. product_specifications.csv contains multiple versions of specification limits
   for some products, each with an effective_from date. The version applicable to
   a given test result is determined by when the lot was tested, not the current
   or latest version.

2. quality_results.csv contains measurements in different units for the same test
   depending on which line performed the analysis. test_catalog.csv provides the
   canonical reporting unit and a unit_conversion_factor; conversion applies only
   when the result's units differ from the reporting unit.

3. rejection_events.csv distinguishes reworkable from non-reworkable lots, each
   with a different per-unit remediation cost. Correct COPQ requires selecting the
   applicable rate for each lot individually.

Contract (instruction.md): the deliverable is
    /workspace/quality_report.csv  — one row per (line_id, product_id, month)
    /workspace/summary.json        — five scalar keys
"""

import json
import math
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR    = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
              else Path(__file__).parent.parent / "environment" / "data"
REPORT_PATH  = WORKSPACE_DIR / "quality_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

REVISED_PRODUCTS = {
    "P003","P006","P009","P012","P015","P018","P020","P022","P023","P024"
}


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: lot count, V2 spec count, ppm unit presence."""
    lots  = pd.read_csv(DATA_DIR / "production_lots.csv")
    specs = pd.read_csv(DATA_DIR / "product_specifications.csv")
    res   = pd.read_csv(DATA_DIR / "quality_results.csv")

    assert len(lots) == 5_992, "production_lots.csv row count must not be modified."

    v2_count = (specs["effective_from"] == "2024-02-01").sum()
    assert v2_count == 10, \
        f"Expected 10 V2 spec records (one per revised product), got {v2_count}."

    ppm_count = (res["units"] == "ppm").sum()
    assert ppm_count > 1_000, \
        f"Expected >1000 ppm results from L03 legacy instrument, got {ppm_count}."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "quality_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"line_id","product_id","month","lots_produced","lots_passed",
                "pass_rate","mean_spec_deviation","total_copq"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) >= 100, f"Expected at least 100 rows, got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values(["line_id","product_id","month"]).reset_index(drop=True)
    assert list(df["line_id"])    == list(expected["line_id"]) and \
           list(df["product_id"]) == list(expected["product_id"]) and \
           list(df["month"])      == list(expected["month"]), \
        "Report must be sorted by line_id, product_id, then month."


def test_case_04_pass_rate_formula():
    df = pd.read_csv(REPORT_PATH)
    for _, row in df.iterrows():
        expected = round(row["lots_passed"] / row["lots_produced"], 4)
        assert abs(row["pass_rate"] - expected) < 1e-3, \
            f"pass_rate != lots_passed/lots_produced for {row['line_id']}/{row['product_id']}/{row['month']}."


def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_lots_produced","overall_pass_rate","total_copq",
                "line_with_worst_pass_rate","product_with_highest_copq"}
    missing = required - set(s.keys())
    assert not missing, f"Missing keys in summary.json: {missing}"
    assert isinstance(s["total_lots_produced"], int)
    assert isinstance(s["overall_pass_rate"], float)
    assert isinstance(s["total_copq"], float)
    assert isinstance(s["line_with_worst_pass_rate"], str)
    assert isinstance(s["product_with_highest_copq"], str)


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    lots    = pd.read_csv(DATA_DIR / "production_lots.csv",
                          parse_dates=["batch_start_date","batch_end_date"])
    results = pd.read_csv(DATA_DIR / "quality_results.csv",
                          parse_dates=["tested_date"])
    specs   = pd.read_csv(DATA_DIR / "product_specifications.csv",
                          parse_dates=["effective_from"])
    catalog = pd.read_csv(DATA_DIR / "test_catalog.csv")
    rej     = pd.read_csv(DATA_DIR / "rejection_events.csv")

    # Attach product_id and line_id to results
    r = results.merge(lots[["lot_id","product_id","line_id"]], on="lot_id")

    # SCD join: cross-merge, filter to effective_from <= tested_date, keep latest
    merged = r.merge(
        specs[["product_id","test_id","lower_spec_limit",
               "upper_spec_limit","target_value","effective_from"]],
        on=["product_id","test_id"], how="left",
    )
    valid = merged[merged["effective_from"] <= merged["tested_date"]].copy()
    idx   = valid.groupby("result_id")["effective_from"].idxmax()
    jn    = valid.loc[idx].merge(
        catalog[["test_id","reporting_unit","unit_conversion_factor"]], on="test_id"
    )

    # Unit normalization
    jn["normalized_value"] = np.where(
        jn["units"] == jn["reporting_unit"],
        jn["measured_value"],
        jn["measured_value"] * jn["unit_conversion_factor"],
    )
    jn["in_spec"] = (
        (jn["normalized_value"] >= jn["lower_spec_limit"]) &
        (jn["normalized_value"] <= jn["upper_spec_limit"])
    )

    # Lot-level pass/fail
    lot_pass = jn.groupby("lot_id")["in_spec"].all().reset_index()
    lot_pass.columns = ["lot_id","lot_passed"]

    # Spec deviation per (line_id, product_id, month)
    jn2 = jn.merge(lots[["lot_id","batch_end_date"]], on="lot_id")
    jn2["month"]     = jn2["batch_end_date"].dt.to_period("M").astype(str)
    half_width       = (jn2["upper_spec_limit"] - jn2["lower_spec_limit"]) / 2
    jn2["deviation"] = (jn2["normalized_value"] - jn2["target_value"]).abs() / half_width
    spec_dev = (
        jn2.groupby(["line_id","product_id","month"])["deviation"].mean()
        .reset_index().rename(columns={"deviation":"mean_spec_deviation"})
    )

    # COPQ
    failed = lots.merge(lot_pass[~lot_pass["lot_passed"]][["lot_id"]], on="lot_id")
    fc = failed.merge(rej, on="lot_id", how="left")
    fc["copq"] = np.where(
        fc["rework_possible"],
        fc["quantity_produced"] * fc["rework_cost_per_unit"],
        fc["quantity_produced"] * fc["disposal_cost_per_unit"],
    )
    copq_lot = fc[["lot_id","copq"]]

    # Report
    base = lots.merge(lot_pass, on="lot_id").merge(copq_lot, on="lot_id", how="left")
    base["copq"]  = base["copq"].fillna(0.0)
    base["month"] = base["batch_end_date"].dt.to_period("M").astype(str)
    agg = (
        base.groupby(["line_id","product_id","month"])
        .agg(lots_produced=("lot_id","count"),
             lots_passed=("lot_passed","sum"),
             total_copq=("copq","sum"))
        .reset_index()
    )
    agg["pass_rate"] = (agg["lots_passed"] / agg["lots_produced"]).round(4)
    report = agg.merge(spec_dev, on=["line_id","product_id","month"])

    # Summary scalars
    total_copq   = float(round(report["total_copq"].sum(), 2))
    overall_pass = float(round(
        report["lots_passed"].sum() / report["lots_produced"].sum(), 4
    ))
    line_worst   = str(report.groupby("line_id")["pass_rate"].mean().idxmin())
    prod_copq    = str(report.groupby("product_id")["total_copq"].sum().idxmax())

    return {
        "report": report,
        "total_copq": total_copq,
        "overall_pass_rate": overall_pass,
        "line_with_worst_pass_rate": line_worst,
        "product_with_highest_copq": prod_copq,
        "lot_pass": lot_pass,
        "_lots": lots,
    }


# ── Scalar correctness ────────────────────────────────────────────────────────

def test_case_06_overall_pass_rate(ground_truth):
    """Primary accuracy check — wrong if SCD or unit conversion is mishandled."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["overall_pass_rate"] - ground_truth["overall_pass_rate"]) <= 0.005, \
        (f"overall_pass_rate: got {s['overall_pass_rate']}, "
         f"expected {ground_truth['overall_pass_rate']} (±0.005)")


def test_case_07_total_copq(ground_truth):
    """COPQ: rework and disposal lots carry different per-unit costs and must be computed separately."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    rel_err = abs(s["total_copq"] - ground_truth["total_copq"]) / ground_truth["total_copq"]
    assert rel_err <= 0.02, \
        (f"total_copq: got {s['total_copq']}, "
         f"expected {ground_truth['total_copq']} (±2%)")


# ── Trap-specific row-level checks ────────────────────────────────────────────

def test_case_08_revised_product_january_pass_rate(ground_truth):
    """January pass rates for spec-revised products must use the pre-revision (V1) limits.

    Ten products had their assay limits tightened on 2024-02-01. January lots must be
    evaluated against the V1 specification that was in effect when they were tested.
    """
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["report"]
    jan_rev_gt = gt[
        (gt["product_id"].isin(REVISED_PRODUCTS)) & (gt["month"] == "2024-01")
    ].set_index(["line_id","product_id"])

    jan_rev_got = report[
        (report["product_id"].isin(REVISED_PRODUCTS)) & (report["month"] == "2024-01")
    ].set_index(["line_id","product_id"])

    close = 0
    total = 0
    for idx in jan_rev_gt.index:
        if idx not in jan_rev_got.index:
            continue
        total += 1
        exp = float(jan_rev_gt.loc[idx, "pass_rate"])
        got = float(jan_rev_got.loc[idx, "pass_rate"])
        if abs(got - exp) <= 0.05:
            close += 1

    assert total > 0, "No revised product January rows found in report."
    assert close >= int(total * 0.8), \
        (f"Only {close}/{total} revised-product January pass rates within ±0.05 "
         f"of ground truth — SCD spec lookup likely incorrect.")


def test_case_09_l03_mean_spec_deviation(ground_truth):
    """L03 mean spec deviations must be computed after converting measurements to the reporting unit.

    L03 reports the T01 assay in ppm; spec limits are in %. The conversion factor in
    test_catalog.csv must be applied before comparing values to specification limits.
    """
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["report"]
    l03_gt  = gt[gt["line_id"] == "L03"].set_index(["product_id","month"])
    l03_got = report[report["line_id"] == "L03"].set_index(["product_id","month"])

    close = 0
    total = 0
    for idx in l03_gt.index:
        if idx not in l03_got.index:
            continue
        total += 1
        exp = float(l03_gt.loc[idx, "mean_spec_deviation"])
        got = float(l03_got.loc[idx, "mean_spec_deviation"])
        if math.isclose(got, exp, rel_tol=0.05):
            close += 1

    assert total > 0, "No L03 rows found in report."
    assert close >= int(total * 0.8), \
        (f"Only {close}/{total} L03 mean_spec_deviation values within ±5% of "
         f"ground truth — unit conversion (ppm→%) likely not applied for L03.")


def test_case_10_summary_scalars(ground_truth):
    """Line and product identifiers in summary must match ground truth exactly."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["line_with_worst_pass_rate"] == ground_truth["line_with_worst_pass_rate"], \
        (f"line_with_worst_pass_rate: got {s['line_with_worst_pass_rate']!r}, "
         f"expected {ground_truth['line_with_worst_pass_rate']!r}")
    assert s["product_with_highest_copq"] == ground_truth["product_with_highest_copq"], \
        (f"product_with_highest_copq: got {s['product_with_highest_copq']!r}, "
         f"expected {ground_truth['product_with_highest_copq']!r}")
