"""
Tests for the Q1 2024 manufacturing lot quality report.

Contract (instruction.md): the deliverable is
    /workspace/quality_report.csv  — one row per (line_id, product_id, month)
    /workspace/summary.json        — six scalar keys

Reasoning challenges embedded in the data:
  1. lot_id resets monthly per line — the same string appears up to 3× across Q1;
     results must be matched to the correct lot via batch_end_datetime proximity.
  2. Some lots have a retest result for T01 (more recent than the original);
     only the latest result per (lot, test) is used for pass/fail.
  3. product_specifications has two versions per revised product; the version
     effective at tested_date applies, not the version at production start.
  4. Line L03 T01 results are in ppm; spec limits are in % (1% = 10,000 ppm).
  5. Night-shift lots crossing midnight belong to the month of batch_start.
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


def _revised_products():
    """Products that have more than one spec version in product_specifications.csv."""
    specs = pd.read_csv(
        Path(__file__).parent.parent / "environment" / "data" / "product_specifications.csv"
        if not Path("/workspace/data").exists()
        else Path("/workspace/data/product_specifications.csv")
    )
    counts = specs.groupby(["product_id", "test_id"]).size()
    return set(counts[counts > 1].reset_index()["product_id"].unique())

REVISED_PRODUCTS = _revised_products()


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: lot count, non-unique lot_ids, V2 spec count, ppm unit presence,
    rejection event count, retest result presence, cross-month lot count."""
    lots  = pd.read_csv(DATA_DIR / "production_lots.csv")
    specs = pd.read_csv(DATA_DIR / "product_specifications.csv")
    res   = pd.read_csv(DATA_DIR / "quality_results.csv")
    rej   = pd.read_csv(DATA_DIR / "rejection_events.csv")

    assert len(lots) == 5_997, "production_lots.csv row count must not be modified."

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

    assert len(res) == 30_015, \
        (f"quality_results.csv must not be modified (expected 30,015 rows including "
         f"retest results, got {len(res)}).")

    assert len(rej) == 573, \
        f"rejection_events.csv row count must not be modified (expected 573, got {len(rej)})."

    assert "rejected_quantity" in rej.columns, \
        "rejection_events.csv must contain a 'rejected_quantity' column."
    partial_count = (rej["rejected_quantity"] < rej.merge(
        lots[["lot_id", "quantity_produced"]].drop_duplicates("lot_id"),
        on="lot_id", how="left"
    )["quantity_produced"]).sum()
    assert partial_count >= 100, \
        (f"Expected ≥100 partial rejection events where rejected_quantity < quantity_produced, "
         f"got {partial_count}. The rejected_quantity column must not be replaced with quantity_produced.")

    lots2 = pd.read_csv(DATA_DIR / "production_lots.csv",
                        parse_dates=["batch_start_datetime", "batch_end_datetime"])
    cross_month = (
        lots2["batch_start_datetime"].dt.to_period("M") !=
        lots2["batch_end_datetime"].dt.to_period("M")
    ).sum()
    assert cross_month >= 50, \
        f"Expected >=50 lots where start and end fall in different calendar months, got {cross_month}."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "quality_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"line_id", "product_id", "month", "lots_produced", "lots_passed",
                "pass_rate", "mean_spec_deviation", "total_copq"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) >= 100, f"Expected at least 100 rows, got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values(["line_id", "product_id", "month"]).reset_index(drop=True)
    assert list(df["line_id"])    == list(expected["line_id"]) and \
           list(df["product_id"]) == list(expected["product_id"]) and \
           list(df["month"])      == list(expected["month"]), \
        "Report must be sorted by line_id, product_id, then month."


# ── Hard test 1: failed lot count (exact integer) ─────────────────────────────

def test_case_04_failed_lot_count(ground_truth):
    """Exact count of lots failing at least one quality test. Must be an int in summary.json."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert "failed_lot_count" in s, "summary.json must contain 'failed_lot_count'."
    assert isinstance(s["failed_lot_count"], int), \
        "failed_lot_count must be a plain int."
    expected = ground_truth["failed_lot_count"]
    assert s["failed_lot_count"] == expected, \
        (f"failed_lot_count: got {s['failed_lot_count']}, expected {expected}. "
         f"A lot fails if any quality-test result (using the most recently tested "
         f"result per lot-test pair) falls outside the applicable specification limits.")


# ── Output schema ─────────────────────────────────────────────────────────────

def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_lots_produced", "failed_lot_count", "overall_pass_rate",
                "total_copq", "line_with_worst_pass_rate", "product_with_highest_copq"}
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
                          parse_dates=["batch_start_datetime", "batch_end_datetime"])
    results = pd.read_csv(DATA_DIR / "quality_results.csv",
                          parse_dates=["tested_date"])
    specs   = pd.read_csv(DATA_DIR / "product_specifications.csv",
                          parse_dates=["effective_from"])
    catalog = pd.read_csv(DATA_DIR / "test_catalog.csv")
    rej     = pd.read_csv(DATA_DIR / "rejection_events.csv",
                          parse_dates=["rejection_date"])

    # Disambiguate lot_id via date proximity (batch_end_datetime, 5-day window)
    lots["_lot_row"] = np.arange(len(lots))
    lots["_batch_end_date"] = lots["batch_end_datetime"].dt.normalize()
    r_multi = results.merge(
        lots[["lot_id", "product_id", "line_id", "_batch_end_date", "_lot_row"]], on="lot_id"
    )
    r_multi["_days"] = (r_multi["tested_date"] - r_multi["_batch_end_date"]).dt.days
    r_multi = r_multi[(r_multi["_days"] >= 0) & (r_multi["_days"] <= 5)].copy()
    r = r_multi.loc[r_multi.groupby("result_id")["_days"].idxmin()].drop(
        columns=["_days", "_batch_end_date"]
    )

    # Retest supersession: keep only the most recent result per (lot, test)
    r = r.sort_values("tested_date")
    r = r.loc[r.groupby(["_lot_row", "test_id"])["tested_date"].idxmax()]

    # Join each result to its applicable spec version (effective at tested_date)
    merged = r.merge(
        specs[["product_id", "test_id", "lower_spec_limit",
               "upper_spec_limit", "target_value", "effective_from"]],
        on=["product_id", "test_id"], how="left",
    )
    valid = merged[merged["effective_from"] <= merged["tested_date"]].copy()
    idx   = valid.groupby("result_id")["effective_from"].idxmax()
    jn    = valid.loc[idx].merge(
        catalog[["test_id", "reporting_unit", "unit_conversion_factor"]], on="test_id"
    )

    # Normalise measured values to spec reporting units via catalog conversion factor
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
    lot_pass = jn.groupby("_lot_row")["in_spec"].all().reset_index()
    lot_pass.columns = ["_lot_row", "lot_passed"]

    # Month attribution uses batch_start_datetime (production start date)
    jn2 = jn.merge(lots[["_lot_row", "batch_start_datetime"]], on="_lot_row")
    jn2["month"]     = jn2["batch_start_datetime"].dt.to_period("M").astype(str)
    half_width       = (jn2["upper_spec_limit"] - jn2["lower_spec_limit"]) / 2
    jn2["deviation"] = (jn2["normalized_value"] - jn2["target_value"]).abs() / half_width
    spec_dev = (
        jn2.groupby(["line_id", "product_id", "month"])["deviation"].mean()
        .reset_index().rename(columns={"deviation": "mean_spec_deviation"})
    )

    # COPQ — match rejection events via rejection_date proximity to batch_end_datetime
    failed = lots.merge(lot_pass[~lot_pass["lot_passed"]][["_lot_row"]], on="_lot_row")
    failed["_bed"] = failed["batch_end_datetime"].dt.normalize()
    fc = failed.merge(rej, on="lot_id", how="left")
    fc["_rej_days"] = (fc["rejection_date"] - fc["_bed"]).dt.days
    fc_valid = fc[fc["_rej_days"] >= 0].copy()
    if len(fc_valid) > 0 and fc_valid.duplicated("_lot_row").any():
        best = fc_valid.loc[fc_valid.groupby("_lot_row")["_rej_days"].idxmin()]
    else:
        best = fc_valid
    fc_clean = failed[["_lot_row"]].merge(
        best[["_lot_row", "rework_possible", "rejected_quantity",
              "rework_cost_per_unit", "disposal_cost_per_unit"]],
        on="_lot_row", how="left",
    )
    fc_clean["copq"] = np.where(
        fc_clean["rework_possible"].notna(),
        np.where(fc_clean["rework_possible"],
                 fc_clean["rejected_quantity"] * fc_clean["rework_cost_per_unit"],
                 fc_clean["rejected_quantity"] * fc_clean["disposal_cost_per_unit"]),
        0.0,
    )
    copq_lot = fc_clean[["_lot_row", "copq"]]

    # Report — month by batch_start_datetime
    base = lots.merge(lot_pass, on="_lot_row").merge(copq_lot, on="_lot_row", how="left")
    base["copq"]  = base["copq"].fillna(0.0)
    base["month"] = base["batch_start_datetime"].dt.to_period("M").astype(str)
    agg = (
        base.groupby(["line_id", "product_id", "month"])
        .agg(lots_produced=("_lot_row", "count"),
             lots_passed=("lot_passed", "sum"),
             total_copq=("copq", "sum"))
        .reset_index()
    )
    agg["pass_rate"] = (agg["lots_passed"] / agg["lots_produced"]).round(4)
    report = agg.merge(spec_dev, on=["line_id", "product_id", "month"])

    # Summary scalars
    total_copq   = float(round(report["total_copq"].sum(), 2))
    overall_pass = float(round(
        report["lots_passed"].sum() / report["lots_produced"].sum(), 4
    ))
    line_worst   = str(report.groupby("line_id")["pass_rate"].mean().idxmin())
    prod_copq    = str(report.groupby("product_id")["total_copq"].sum().idxmax())

    return {
        "report":                    report,
        "failed_lot_count":          int((~lot_pass["lot_passed"]).sum()),
        "total_copq":                total_copq,
        "overall_pass_rate":         overall_pass,
        "line_with_worst_pass_rate": line_worst,
        "product_with_highest_copq": prod_copq,
        "lot_pass":                  lot_pass,
        "_lots":                     lots,
    }


# ── Hard test 2: production month attribution ─────────────────────────────────

def test_case_06_production_month_attribution(ground_truth):
    """Monthly lots_produced counts must match ground truth within ±10 per month."""
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["report"]
    agg_gt  = gt.groupby("month")["lots_produced"].sum()
    agg_rep = report.groupby("month")["lots_produced"].sum()
    for month, exp in agg_gt.items():
        got = int(agg_rep.get(month, 0))
        assert abs(got - int(exp)) <= 10, (
            f"lots_produced for {month}: got {got}, expected {int(exp)} (±10). "
            f"Each lot belongs to the calendar month of its production start."
        )


# ── Hard test 3: total COPQ ───────────────────────────────────────────────────

def test_case_07_total_copq(ground_truth):
    """Total COPQ must be within ±2% of ground truth."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    rel_err = abs(s["total_copq"] - ground_truth["total_copq"]) / ground_truth["total_copq"]
    assert rel_err <= 0.02, \
        (f"total_copq: got {s['total_copq']}, "
         f"expected {ground_truth['total_copq']} (±2%)")


# ── Hard test 4: SCD — January pass rates for spec-revised products ───────────

def test_case_08_revised_product_january_pass_rate(ground_truth):
    """≥90% of January rows for products with multiple spec versions must be within ±0.02 of ground truth."""
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["report"]
    jan_rev_gt = gt[
        (gt["product_id"].isin(REVISED_PRODUCTS)) & (gt["month"] == "2024-01")
    ].set_index(["line_id", "product_id"])

    jan_rev_got = report[
        (report["product_id"].isin(REVISED_PRODUCTS)) & (report["month"] == "2024-01")
    ].set_index(["line_id", "product_id"])

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
         f"of ground truth — retest supersession or spec-version lookup may be incorrect.")


# ── Hard test 5: unit conversion — L03 mean spec deviation ───────────────────

def test_case_09_l03_mean_spec_deviation(ground_truth):
    """≥90% of L03 rows must have mean_spec_deviation within ±2% of ground truth."""
    report = pd.read_csv(REPORT_PATH)
    gt     = ground_truth["report"]
    l03_gt  = gt[gt["line_id"] == "L03"].set_index(["product_id", "month"])
    l03_got = report[report["line_id"] == "L03"].set_index(["product_id", "month"])

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
