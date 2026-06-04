"""
Q1 2024 Manufacturing Lot Quality Report.

Two data issues became clear as soon as I looked at the raw results:

The product_specifications table has two records per product for ten products —
a V1 entry (effective 2023-01-01) and a tighter V2 (effective 2024-02-01). A
plain merge on (product_id, test_id) silently picks up V2 for everything, which
misclassifies January lots that were legitimately within the broader V1 limits.
The correct join is: for each test result, find the spec version whose
effective_from is the most recent date that is still <= tested_date.

The T01 assay column in quality_results has two distinct value ranges when you
look at it by line — roughly 0.8–1.2 for L01/L02 and roughly 8,000–12,000 for
L03. The test_catalog has unit_conversion_factor = 0.0001, and the units column
in quality_results shows "ppm" for L03 and "%" for the others. Multiplying ppm
values by 0.0001 brings them into the same range as the spec limits. The key is
to apply the conversion only when the result's units differ from the reporting_unit;
blindly applying the factor to already-normalized "%" values would make them tiny.

Everything else is straightforward: rework vs disposal COPQ from the flag in
rejection_events, groupby to (line_id, product_id, month) for the report.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR      = Path("/workspace/data")
WORKSPACE_DIR = Path("/workspace")


def load_data():
    lots    = pd.read_csv(DATA_DIR / "production_lots.csv",
                          parse_dates=["batch_start_date", "batch_end_date"])
    results = pd.read_csv(DATA_DIR / "quality_results.csv",
                          parse_dates=["tested_date"])
    specs   = pd.read_csv(DATA_DIR / "product_specifications.csv",
                          parse_dates=["effective_from"])
    catalog = pd.read_csv(DATA_DIR / "test_catalog.csv")
    rej     = pd.read_csv(DATA_DIR / "rejection_events.csv")
    return lots, results, specs, catalog, rej


def scd_spec_join(results, specs):
    """Join each test result to the spec version effective at tested_date."""
    # BOTTLENECK: a plain merge on (product_id, test_id) picks up V2 specs for
    # January lots, misclassifying some that pass V1 but fail the tighter V2.
    # Cross-join, filter to effective_from <= tested_date, keep the most recent.
    merged = results.merge(
        specs[["product_id", "test_id", "lower_spec_limit",
               "upper_spec_limit", "target_value", "effective_from"]],
        on=["product_id", "test_id"],
        how="left",
    )
    valid = merged[merged["effective_from"] <= merged["tested_date"]].copy()
    idx   = valid.groupby("result_id")["effective_from"].idxmax()
    return valid.loc[idx].reset_index(drop=True)


def normalize_units(results_with_specs, catalog):
    """Convert measured_value to reporting_unit before comparing to spec limits."""
    # BOTTLENECK: L03 reports T01 in ppm; spec limits are in %. Apply
    # unit_conversion_factor only when units != reporting_unit — not universally.
    r = results_with_specs.merge(
        catalog[["test_id", "reporting_unit", "unit_conversion_factor"]],
        on="test_id",
    )
    r["normalized_value"] = np.where(
        r["units"] == r["reporting_unit"],
        r["measured_value"],
        r["measured_value"] * r["unit_conversion_factor"],
    )
    return r


def evaluate_pass_fail(results_normalized):
    """Return per-result in_spec flag and per-lot pass/fail."""
    r = results_normalized.copy()
    r["in_spec"] = (
        (r["normalized_value"] >= r["lower_spec_limit"]) &
        (r["normalized_value"] <= r["upper_spec_limit"])
    )
    lot_pass = (
        r.groupby("lot_id")["in_spec"]
        .all()
        .reset_index()
        .rename(columns={"in_spec": "lot_passed"})
    )
    return r, lot_pass


def compute_spec_deviation(results_normalized, lots):
    """Mean normalized deviation from target per (line_id, product_id, month)."""
    # line_id and product_id are already in results_normalized from the earlier merge
    r = results_normalized.merge(
        lots[["lot_id", "batch_end_date"]], on="lot_id"
    )
    r["month"] = r["batch_end_date"].dt.to_period("M").astype(str)
    half_width = (r["upper_spec_limit"] - r["lower_spec_limit"]) / 2
    r["deviation"] = (r["normalized_value"] - r["target_value"]).abs() / half_width
    return (
        r.groupby(["line_id", "product_id", "month"])["deviation"]
        .mean()
        .reset_index()
        .rename(columns={"deviation": "mean_spec_deviation"})
    )


def compute_copq(lots, lot_pass, rejection_events):
    """COPQ per lot: rework or disposal rate × quantity_produced."""
    # BOTTLENECK: rework_cost_per_unit is ~1/3 of disposal_cost_per_unit.
    # Using an average rate or the wrong rate distorts total COPQ significantly.
    failed = lots.merge(
        lot_pass[~lot_pass["lot_passed"]][["lot_id"]], on="lot_id"
    )
    fc = failed.merge(rejection_events, on="lot_id", how="left")
    fc["copq"] = np.where(
        fc["rework_possible"],
        fc["quantity_produced"] * fc["rework_cost_per_unit"],
        fc["quantity_produced"] * fc["disposal_cost_per_unit"],
    )
    return fc[["lot_id", "copq"]]


def build_report(lots, lot_pass, copq_per_lot, spec_dev):
    """Aggregate to (line_id, product_id, month) level."""
    base = lots.merge(lot_pass, on="lot_id").merge(
        copq_per_lot, on="lot_id", how="left"
    )
    base["copq"]  = base["copq"].fillna(0.0)
    base["month"] = base["batch_end_date"].dt.to_period("M").astype(str)

    agg = (
        base.groupby(["line_id", "product_id", "month"])
        .agg(
            lots_produced=("lot_id", "count"),
            lots_passed=("lot_passed", "sum"),
            total_copq=("copq", "sum"),
        )
        .reset_index()
    )
    agg["pass_rate"]   = (agg["lots_passed"] / agg["lots_produced"]).round(4)
    agg["total_copq"]  = agg["total_copq"].round(2)
    agg["lots_passed"] = agg["lots_passed"].astype(int)

    report = agg.merge(spec_dev, on=["line_id", "product_id", "month"])
    report["mean_spec_deviation"] = report["mean_spec_deviation"].round(4)

    report = (
        report[["line_id", "product_id", "month", "lots_produced",
                 "lots_passed", "pass_rate", "mean_spec_deviation", "total_copq"]]
        .sort_values(["line_id", "product_id", "month"])
        .reset_index(drop=True)
    )
    return report


def main():
    lots, results, specs, catalog, rej = load_data()

    results_with_product = results.merge(
        lots[["lot_id", "product_id", "line_id"]], on="lot_id"
    )
    results_with_specs    = scd_spec_join(results_with_product, specs)
    results_normalized    = normalize_units(results_with_specs, catalog)
    results_normalized, lot_pass = evaluate_pass_fail(results_normalized)

    spec_dev  = compute_spec_deviation(results_normalized, lots)
    copq      = compute_copq(lots, lot_pass, rej)
    report    = build_report(lots, lot_pass, copq, spec_dev)

    report.to_csv(WORKSPACE_DIR / "quality_report.csv", index=False)

    total_copq    = float(round(report["total_copq"].sum(), 2))
    overall_pass  = float(round(
        report["lots_passed"].sum() / report["lots_produced"].sum(), 4
    ))
    line_worst    = str(
        report.groupby("line_id")["pass_rate"].mean().idxmin()
    )
    product_copq  = str(
        report.groupby("product_id")["total_copq"].sum().idxmax()
    )

    summary = {
        "total_lots_produced":       int(report["lots_produced"].sum()),
        "failed_lot_count":          int((~lot_pass["lot_passed"]).sum()),
        "overall_pass_rate":         overall_pass,
        "total_copq":                total_copq,
        "line_with_worst_pass_rate": line_worst,
        "product_with_highest_copq": product_copq,
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
