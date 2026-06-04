"""
Tests for the Q1 2024 manufacturing lot quality report.

Three traps are embedded in the data:

1. Slowly-changing specification limits — product_specifications.csv contains
   multiple spec versions per product-test pair, each with an effective_from date.
   The version applicable to a test result is determined by the result's tested_date.
   Using the latest version for all results misclassifies January lots evaluated
   against tighter V2 limits that were not yet in effect.

2. Unit mismatch — Line L03 reports the T01 assay in ppm; specification limits are
   in %. test_catalog.csv supplies reporting_unit and unit_conversion_factor.
   Skipping unit normalisation makes all L03 T01 values appear wildly out of spec,
   inflating failed_lot_count and collapsing the L03 pass rate.

3. Rework vs disposal cost — each failed lot has a rework_possible flag that
   determines which per-unit cost applies. Using an average or the wrong rate
   distorts COPQ for individual lots.

Contract (instruction.md): the deliverable is
    /workspace/quality_report.csv  — one row per (line_id, product_id, month)
    /workspace/summary.json        — six scalar keys
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
    """Sentinel: lot count, non-unique lot_ids, V2 spec count, ppm unit presence, rejection event count."""
    lots  = pd.read_csv(DATA_DIR / "production_lots.csv")
    specs = pd.read_csv(DATA_DIR / "product_specifications.csv")
    res   = pd.read_csv(DATA_DIR / "quality_results.csv")
    rej   = pd.read_csv(DATA_DIR / "rejection_events.csv")

    assert len(lots) == 5_992, "production_lots.csv row count must not be modified."

    unique_ids = lots["lot_id"].nunique()
    assert unique_ids < 3_000, \
        (f"lot_id must not be globally unique — the production controller resets "
         f"numbering each month (expected <3000 unique IDs, got {unique_ids}).")

    v2_count = (specs["effective_from"] == "2024-02-01").sum()
    assert v2_count == 10, \
        f"Expected 10 V2 spec records (one per revised product), got {v2_count}."

    ppm_count = (res["units"] == "ppm").sum()
    assert ppm_count > 1_000, \
        f"Expected >1000 ppm results from L03 legacy instrument, got {ppm_count}."

    assert len(rej) == 606, \
        f"rejection_events.csv row count must not be modified (expected 606, got {len(rej)})."


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


# ── Hard test 1: failed lot count (exact integer) ─────────────────────────────

def test_case_04_failed_lot_count(ground_truth):
    """Exact count of lots failing at least one quality test.

    Catches both unit-conversion errors (L03 ppm values inflate failures by ~3×
    when not converted) and spec-version errors (using V2 for January adds ~100
    false failures). Must be an int in summary.json.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert "failed_lot_count" in s, "summary.json must contain 'failed_lot_count'."
    assert isinstance(s["failed_lot_count"], int), \
        "failed_lot_count must be a plain int."
    expected = ground_truth["failed_lot_count"]
    assert s["failed_lot_count"] == expected, \
        (f"failed_lot_count: got {s['failed_lot_count']}, expected {expected}. "
         f"A lot fails if any quality-test result falls outside the applicable "
         f"specification limits.")


# ── Output schema ─────────────────────────────────────────────────────────────

def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_lots_produced","failed_lot_count","overall_pass_rate",
                "total_copq","line_with_worst_pass_rate","product_with_highest_copq"}
    missing = required - set(s.keys())
    assert not missing, f"Missing keys in summary.json: {missing}"
    assert isinstance(s["total_lots_produced"], int)
    assert isinstance(s["failed_lot_count"], int)
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

    # lot_id resets monthly per line — disambiguate via date proximity
    lots["_lot_row"] = np.arange(len(lots))
    r_multi = results.merge(
        lots[["lot_id","product_id","line_id","batch_end_date","_lot_row"]], on="lot_id"
    )
    r_multi["_days"] = (r_multi["tested_date"] - r_multi["batch_end_date"]).dt.days
    r_multi = r_multi[(r_multi["_days"] >= 0) & (r_multi["_days"] <= 3)].copy()
    r = r_multi.loc[r_multi.groupby("result_id")["_days"].idxmin()].drop(
        columns=["_days", "batch_end_date"]
    )

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

    # Unit normalisation
    jn["normalized_value"] = np.where(
        jn["units"] == jn["reporting_unit"],
        jn["measured_value"],
        jn["measured_value"] * jn["unit_conversion_factor"],
    )
    jn["in_spec"] = (
        (jn["normalized_value"] >= jn["lower_spec_limit"]) &
        (jn["normalized_value"] <= jn["upper_spec_limit"])
    )

    # Lot-level pass/fail — group by _lot_row (unique per lot instance)
    lot_pass = jn.groupby("_lot_row")["in_spec"].all().reset_index()
    lot_pass.columns = ["_lot_row","lot_passed"]

    # Spec deviation per (line_id, product_id, month)
    jn2 = jn.merge(lots[["_lot_row","batch_end_date"]], on="_lot_row")
    jn2["month"]     = jn2["batch_end_date"].dt.to_period("M").astype(str)
    half_width       = (jn2["upper_spec_limit"] - jn2["lower_spec_limit"]) / 2
    jn2["deviation"] = (jn2["normalized_value"] - jn2["target_value"]).abs() / half_width
    spec_dev = (
        jn2.groupby(["line_id","product_id","month"])["deviation"].mean()
        .reset_index().rename(columns={"deviation":"mean_spec_deviation"})
    )

    # COPQ — only for test-failed lots
    failed = lots.merge(lot_pass[~lot_pass["lot_passed"]][["_lot_row"]], on="_lot_row")
    fc = failed.merge(rej, on="lot_id", how="left")
    if fc.duplicated("_lot_row").any():
        fc = fc.loc[fc.groupby("_lot_row").apply(lambda g: g.index[0])]
    fc["copq"] = np.where(
        fc["rework_possible"],
        fc["quantity_produced"] * fc["rework_cost_per_unit"],
        fc["quantity_produced"] * fc["disposal_cost_per_unit"],
    )
    copq_lot = fc[["_lot_row","copq"]]

    # Report
    base = lots.merge(lot_pass, on="_lot_row").merge(copq_lot, on="_lot_row", how="left")
    base["copq"]  = base["copq"].fillna(0.0)
    base["month"] = base["batch_end_date"].dt.to_period("M").astype(str)
    agg = (
        base.groupby(["line_id","product_id","month"])
        .agg(lots_produced=("_lot_row","count"),
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
        "report":                    report,
        "failed_lot_count":          int((~lot_pass["lot_passed"]).sum()),  # 606
        "total_copq":                total_copq,
        "overall_pass_rate":         overall_pass,
        "line_with_worst_pass_rate": line_worst,
        "product_with_highest_copq": prod_copq,
        "lot_pass":                  lot_pass,
        "_lots":                     lots,
    }


# ── Hard test 2: overall pass rate (tight tolerance) ─────────────────────────

def test_case_06_overall_pass_rate(ground_truth):
    """Overall pass rate must be within ±0.002 of ground truth.

    Using the latest spec version for all lots (ignoring effective_from) shifts
    ~100 January lots from pass to fail, a delta of ~0.017 — well outside ±0.002.
    Skipping unit conversion for L03 shifts ~1800 lots, a delta of ~0.30.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert abs(s["overall_pass_rate"] - ground_truth["overall_pass_rate"]) <= 0.002, \
        (f"overall_pass_rate: got {s['overall_pass_rate']}, "
         f"expected {ground_truth['overall_pass_rate']} (±0.002)")


# ── Hard test 3: total COPQ ───────────────────────────────────────────────────

def test_case_07_total_copq(ground_truth):
    """Total COPQ must be within ±2% of ground truth.

    Each failed lot carries a rework_possible flag; the wrong per-unit cost
    (rework_cost_per_unit vs disposal_cost_per_unit) distorts total COPQ.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    rel_err = abs(s["total_copq"] - ground_truth["total_copq"]) / ground_truth["total_copq"]
    assert rel_err <= 0.02, \
        (f"total_copq: got {s['total_copq']}, "
         f"expected {ground_truth['total_copq']} (±2%)")


# ── Hard test 4: SCD — January pass rates for spec-revised products ───────────

def test_case_08_revised_product_january_pass_rate(ground_truth):
    """≥90% of January rows for spec-revised products must be within ±0.02 of ground truth.

    V2 limits for ten products took effect 2024-02-01. January lots tested before
    that date must be evaluated against V1. Using V2 for January tightens limits
    and inflates failures; using V1 for February/March is the symmetric error.
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
        if abs(got - exp) <= 0.02:
            close += 1

    assert total > 0, "No revised-product January rows found in report."
    assert close >= int(total * 0.9), \
        (f"Only {close}/{total} revised-product January pass rates within ±0.02 "
         f"of ground truth — spec-version lookup by tested_date likely incorrect.")


# ── Hard test 5: unit conversion — L03 mean spec deviation ───────────────────

def test_case_09_l03_mean_spec_deviation(ground_truth):
    """≥90% of L03 rows must have mean_spec_deviation within ±2% of ground truth.

    L03 reports the T01 assay in ppm; specification limits are in %.
    Without applying the unit_conversion_factor from test_catalog.csv the
    deviations are off by a factor of ~10 000.
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
        if math.isclose(got, exp, rel_tol=0.02):
            close += 1

    assert total > 0, "No L03 rows found in report."
    assert close >= int(total * 0.9), \
        (f"Only {close}/{total} L03 mean_spec_deviation values within ±2% of "
         f"ground truth — unit conversion from ppm to % likely not applied.")


# ── Medium test: summary scalar identifiers ───────────────────────────────────

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
