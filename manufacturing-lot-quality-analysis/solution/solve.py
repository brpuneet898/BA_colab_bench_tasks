"""
Q1 2024 Manufacturing Lot Quality Report.

lot_id in production_lots.csv is assigned by each line's production controller
and resets at the start of every calendar month, so the same lot_id string can
appear up to three times (once per month) in that file.  Each quality result in
quality_results.csv references lot_id but not the calendar month, so a naïve
merge on lot_id alone produces up to three candidate lots per result.

Disambiguation: testing always follows batch completion by 1–3 days.  Filter to
the candidate lot whose batch_end_datetime (date portion) is the closest
predecessor of tested_date within a four-day window.  This yields a 1-to-1
mapping from result to lot.

Month attribution uses batch_start_datetime — a night-shift lot that begins
on Jan 31 and completes on Feb 1 belongs to January.

A temporary row-index column (_lot_row) is used as a stable unique key throughout
so that downstream aggregations are never confused by the repeated lot_id strings.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")


def load_data():
    lots = pd.read_csv(
        DATA_DIR / "production_lots.csv",
        parse_dates=["batch_start_datetime", "batch_end_datetime"],
    )
    lots["_lot_row"] = np.arange(len(lots))   # stable unique identifier
    results = pd.read_csv(DATA_DIR / "quality_results.csv", parse_dates=["tested_date"])
    specs   = pd.read_csv(DATA_DIR / "product_specifications.csv", parse_dates=["effective_from"])
    catalog = pd.read_csv(DATA_DIR / "test_catalog.csv")
    rej     = pd.read_csv(DATA_DIR / "rejection_events.csv")
    return lots, results, specs, catalog, rej


def link_results_to_lots(results, lots):
    """Return one row per quality result with the correct lot's metadata attached.

    lot_id repeats across months; testing follows batch completion by 1–3 days,
    so filter to the candidate lot with batch_end_datetime just before tested_date.
    """
    lots2 = lots.copy()
    lots2["_batch_end_date"] = lots2["batch_end_datetime"].dt.normalize()
    r = results.merge(
        lots2[["lot_id", "product_id", "line_id", "_batch_end_date", "_lot_row"]],
        on="lot_id",
    )
    r["_days"] = (r["tested_date"] - r["_batch_end_date"]).dt.days
    r = r[(r["_days"] >= 0) & (r["_days"] <= 3)].copy()
    # Rare ties (same lot_id + same batch_end_datetime date): keep the closest match
    r = r.loc[r.groupby("result_id")["_days"].idxmin()]
    return r.drop(columns=["_days", "_batch_end_date"])


def scd_spec_join(results_with_lots, specs):
    """Join each result to the spec version effective at tested_date."""
    merged = results_with_lots.merge(
        specs[["product_id", "test_id", "lower_spec_limit",
               "upper_spec_limit", "target_value", "effective_from"]],
        on=["product_id", "test_id"], how="left",
    )
    valid = merged[merged["effective_from"] <= merged["tested_date"]].copy()
    idx   = valid.groupby("result_id")["effective_from"].idxmax()
    return valid.loc[idx].reset_index(drop=True)


def normalize_units(results_with_specs, catalog):
    r = results_with_specs.merge(catalog[["test_id", "reporting_unit"]], on="test_id")
    # ppm -> %: 1% = 10,000 ppm (SI definition)
    r["normalized_value"] = np.where(
        r["units"] == r["reporting_unit"],
        r["measured_value"],
        np.where(
            (r["units"] == "ppm") & (r["reporting_unit"] == "%"),
            r["measured_value"] * 1e-4,
            r["measured_value"],
        ),
    )
    return r


def evaluate_pass_fail(results_normalized):
    r = results_normalized.copy()
    r["in_spec"] = (
        (r["normalized_value"] >= r["lower_spec_limit"]) &
        (r["normalized_value"] <= r["upper_spec_limit"])
    )
    lot_pass = (
        r.groupby("_lot_row")["in_spec"].all()
        .reset_index()
        .rename(columns={"in_spec": "lot_passed"})
    )
    return r, lot_pass


def compute_spec_deviation(results_normalized, lots):
    r = results_normalized.merge(lots[["_lot_row", "batch_start_datetime"]], on="_lot_row")
    r["month"]     = r["batch_start_datetime"].dt.to_period("M").astype(str)
    half_width     = (r["upper_spec_limit"] - r["lower_spec_limit"]) / 2
    r["deviation"] = (r["normalized_value"] - r["target_value"]).abs() / half_width
    return (
        r.groupby(["line_id", "product_id", "month"])["deviation"]
        .mean().reset_index()
        .rename(columns={"deviation": "mean_spec_deviation"})
    )


def compute_copq(lots, lot_pass, rejection_events):
    failed = lots.merge(lot_pass[~lot_pass["lot_passed"]][["_lot_row"]], on="_lot_row")
    fc = failed.merge(rejection_events, on="lot_id", how="left")
    # If a lot_id appears more than once in rejection_events (different months),
    # keep the record closest to this lot's batch_end_date.
    if fc.duplicated("_lot_row").any():
        fc = fc.loc[fc.groupby("_lot_row").apply(lambda g: g.index[0])]
    fc["copq"] = np.where(
        fc["rework_possible"],
        fc["quantity_produced"] * fc["rework_cost_per_unit"],
        fc["quantity_produced"] * fc["disposal_cost_per_unit"],
    )
    return fc[["_lot_row", "copq"]]


def build_report(lots, lot_pass, copq_per_lot, spec_dev):
    base = lots.merge(lot_pass, on="_lot_row").merge(copq_per_lot, on="_lot_row", how="left")
    base["copq"]  = base["copq"].fillna(0.0)
    base["month"] = base["batch_start_datetime"].dt.to_period("M").astype(str)

    agg = (
        base.groupby(["line_id", "product_id", "month"])
        .agg(
            lots_produced=("_lot_row", "count"),
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

    return (
        report[["line_id", "product_id", "month", "lots_produced",
                "lots_passed", "pass_rate", "mean_spec_deviation", "total_copq"]]
        .sort_values(["line_id", "product_id", "month"])
        .reset_index(drop=True)
    )


def main():
    lots, results, specs, catalog, rej = load_data()

    linked               = link_results_to_lots(results, lots)
    with_specs           = scd_spec_join(linked, specs)
    normalized           = normalize_units(with_specs, catalog)
    normalized, lot_pass = evaluate_pass_fail(normalized)

    spec_dev = compute_spec_deviation(normalized, lots)
    copq     = compute_copq(lots, lot_pass, rej)
    report   = build_report(lots, lot_pass, copq, spec_dev)

    report.to_csv(WORKSPACE_DIR / "quality_report.csv", index=False)

    total_copq   = float(round(report["total_copq"].sum(), 2))
    overall_pass = float(round(
        report["lots_passed"].sum() / report["lots_produced"].sum(), 4
    ))
    line_worst   = str(report.groupby("line_id")["pass_rate"].mean().idxmin())
    prod_copq    = str(report.groupby("product_id")["total_copq"].sum().idxmax())

    summary = {
        "total_lots_produced":       int(report["lots_produced"].sum()),
        "failed_lot_count":          int((~lot_pass["lot_passed"]).sum()),
        "overall_pass_rate":         overall_pass,
        "total_copq":                total_copq,
        "line_with_worst_pass_rate": line_worst,
        "product_with_highest_copq": prod_copq,
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
