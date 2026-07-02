"""
Tests for the FY2024 ESG performance tracking report.

Contract (instruction.md): the deliverable is
    /workspace/esg_report.csv   — one row per (business_unit_id, facility_id, quarter)
    /workspace/esg_summary.json — four scalar keys
"""

import json
import math
import pandas as pd
import pytest
from pathlib import Path

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
           else Path(__file__).parent.parent / "environment" / "data"
REPORT_PATH  = WORKSPACE_DIR / "esg_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "esg_summary.json"


def _collision_facility_ids():
    """facility_id values reused across more than one business_unit_id."""
    facilities = pd.read_csv(DATA_DIR / "facilities.csv")
    counts = facilities.groupby("facility_id")["business_unit_id"].nunique()
    return set(counts[counts > 1].index)


COLLISION_FACILITY_IDS = _collision_facility_ids()


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: exact row counts, NaT ownership_end count, facility_id
    collision count, and offset/REC sign convention."""
    bu  = pd.read_csv(DATA_DIR / "business_units.csv")
    fac = pd.read_csv(DATA_DIR / "facilities.csv")
    led = pd.read_csv(DATA_DIR / "emissions_ledger.csv")

    assert len(bu) == 12, "business_units.csv row count must not be modified."
    assert len(fac) == 132, "facilities.csv row count must not be modified."
    assert len(led) == 11_015, "emissions_ledger.csv row count must not be modified."

    nat_count = fac["ownership_end"].isna().sum()
    assert nat_count == 112, \
        f"Expected 112 facilities with open-ended ownership_end (NaT), got {nat_count}."

    collisions = fac.groupby("facility_id")["business_unit_id"].nunique()
    n_collisions = (collisions > 1).sum()
    assert n_collisions == 15, \
        f"Expected 15 facility_id values reused across business units, got {n_collisions}."

    offset_rec = led[led["transaction_type"].isin(
        ["purchased_offset", "renewable_energy_certificate"])]
    assert len(offset_rec) > 400, \
        f"Expected >400 offset/REC ledger rows, got {len(offset_rec)}."
    assert (offset_rec["quantity_tco2e"] < 0).all(), \
        "purchased_offset/renewable_energy_certificate rows must carry negative quantity_tco2e."

    verified = led[led["transaction_type"] == "verified_emission"]
    assert (verified["quantity_tco2e"] > 0).all(), \
        "verified_emission rows must carry positive quantity_tco2e."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "esg_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"business_unit_id", "facility_id", "quarter",
                "gross_scope1_tco2e", "gross_scope2_tco2e", "total_gross_tco2e"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) >= 400, f"Expected at least 400 rows, got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values(["business_unit_id", "facility_id", "quarter"]).reset_index(drop=True)
    assert list(df["business_unit_id"]) == list(expected["business_unit_id"]) and \
           list(df["facility_id"])      == list(expected["facility_id"]) and \
           list(df["quarter"])          == list(expected["quarter"]), \
        "Report must be sorted by business_unit_id, facility_id, then quarter."


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    facilities = pd.read_csv(
        DATA_DIR / "facilities.csv",
        parse_dates=["ownership_start", "ownership_end"],
    )
    ledger = pd.read_csv(DATA_DIR / "emissions_ledger.csv", parse_dates=["reporting_date"])

    # Composite-key join: facility_id alone is not unique across business units.
    merged = ledger.merge(
        facilities[["business_unit_id", "facility_id", "ownership_start", "ownership_end"]],
        on=["business_unit_id", "facility_id"], how="inner",
    )

    # Ownership window: NaT ownership_end means still owned (open-ended).
    in_window = (
        (merged["reporting_date"] >= merged["ownership_start"]) &
        (merged["ownership_end"].isna() | (merged["reporting_date"] <= merged["ownership_end"]))
    )
    scoped = merged[in_window].copy()
    scoped["quarter"] = "2024-Q" + ((scoped["reporting_date"].dt.month - 1) // 3 + 1).astype(str)

    gross = scoped[scoped["transaction_type"] == "verified_emission"]
    pivot = (
        gross.groupby(["business_unit_id", "facility_id", "quarter", "scope"])["quantity_tco2e"]
        .sum().unstack("scope").fillna(0.0)
    )
    pivot = pivot.rename(columns={"Scope 1": "gross_scope1_tco2e", "Scope 2": "gross_scope2_tco2e"})
    for col in ("gross_scope1_tco2e", "gross_scope2_tco2e"):
        if col not in pivot.columns:
            pivot[col] = 0.0
    report = pivot.reset_index()
    report["total_gross_tco2e"] = (report["gross_scope1_tco2e"] + report["gross_scope2_tco2e"]).round(2)

    offsets = scoped[scoped["transaction_type"].isin(
        ["purchased_offset", "renewable_energy_certificate"])]
    offset_total = float(round(offsets["quantity_tco2e"].abs().sum(), 2))

    gross_total = float(round(report["total_gross_tco2e"].sum(), 2))

    facility_count = int(
        scoped[scoped["transaction_type"] == "verified_emission"]
        .drop_duplicates(["business_unit_id", "facility_id"]).shape[0]
    )

    bu_totals = report.groupby("business_unit_id")["total_gross_tco2e"].sum()
    worst_bu = str(bu_totals.idxmax())

    return {
        "report":                              report,
        "total_gross_emissions_tco2e":         gross_total,
        "total_offset_credits_tco2e":          offset_total,
        "facility_count_included":             facility_count,
        "business_unit_with_highest_emissions": worst_bu,
    }


# ── Hard test 1: NaT ownership window — facility_count_included ──────────────

def test_case_04_facility_count_included(ground_truth):
    """Exact count of (business_unit_id, facility_id) pairs owned at some point
    during FY2024. Must be an int in esg_summary.json."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert "facility_count_included" in s, "esg_summary.json must contain 'facility_count_included'."
    assert isinstance(s["facility_count_included"], int), \
        "facility_count_included must be a plain int."
    expected = ground_truth["facility_count_included"]
    assert s["facility_count_included"] == expected, \
        f"facility_count_included: got {s['facility_count_included']}, expected {expected}."


# ── Output schema ─────────────────────────────────────────────────────────────

def test_case_05_summary_schema():
    assert SUMMARY_PATH.exists(), "esg_summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_gross_emissions_tco2e", "total_offset_credits_tco2e",
                "facility_count_included", "business_unit_with_highest_emissions"}
    missing = required - set(s.keys())
    assert not missing, f"Missing keys in esg_summary.json: {missing}"
    assert isinstance(s["total_gross_emissions_tco2e"], float)
    assert isinstance(s["total_offset_credits_tco2e"], float)
    assert isinstance(s["facility_count_included"], int)
    assert isinstance(s["business_unit_with_highest_emissions"], str)


# ── Hard test 2: gross emissions must exclude offsets/RECs ───────────────────

def test_case_06_gross_emissions_excludes_offsets(ground_truth):
    """total_gross_emissions_tco2e must be within ±2% of ground truth. Netting
    offset/REC rows into the total (rather than filtering to verified_emission
    only) understates this figure well outside that tolerance."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    expected = ground_truth["total_gross_emissions_tco2e"]
    rel_err = abs(s["total_gross_emissions_tco2e"] - expected) / expected
    assert rel_err <= 0.02, \
        (f"total_gross_emissions_tco2e: got {s['total_gross_emissions_tco2e']}, "
         f"expected {expected} (±2%)")


# ── Hard test 3: offsets/RECs reported as a distinct scalar ──────────────────

def test_case_07_offset_credits_scalar(ground_truth):
    """total_offset_credits_tco2e (sum of purchased_offset + REC magnitudes,
    within their facilities' ownership windows) must be within ±2% of ground truth."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    expected = ground_truth["total_offset_credits_tco2e"]
    rel_err = abs(s["total_offset_credits_tco2e"] - expected) / expected
    assert rel_err <= 0.02, \
        (f"total_offset_credits_tco2e: got {s['total_offset_credits_tco2e']}, "
         f"expected {expected} (±2%)")


# ── Hard test 4: composite key — colliding facility_id rows ──────────────────

def test_case_08_composite_key_facility_rows(ground_truth):
    """≥85% of rows for facility_id values reused across business units must be
    within ±10% of ground truth. A facility_id-only join duplicates these rows
    across the wrong business unit and inflates their totals."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    gt_sub  = gt[gt["facility_id"].isin(COLLISION_FACILITY_IDS)].set_index(
        ["business_unit_id", "facility_id", "quarter"])
    got_sub = report[report["facility_id"].isin(COLLISION_FACILITY_IDS)].set_index(
        ["business_unit_id", "facility_id", "quarter"])

    close, total = 0, 0
    for idx in gt_sub.index:
        if idx not in got_sub.index:
            continue
        total += 1
        exp = float(gt_sub.loc[idx, "total_gross_tco2e"])
        got = float(got_sub.loc[idx, "total_gross_tco2e"])
        if math.isclose(got, exp, rel_tol=0.10):
            close += 1

    assert total > 0, "No colliding-facility rows found in report."
    assert close >= int(total * 0.85), \
        f"Only {close}/{total} colliding-facility rows within ±10% of ground truth."


# ── Hard test 5: business-unit aggregate totals ───────────────────────────────

def test_case_09_business_unit_totals(ground_truth):
    """≥90% of business units' aggregate gross emissions must be within ±2% of
    ground truth. Holistic check that fails if any trap is mishandled."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    gt_bu  = gt.groupby("business_unit_id")["total_gross_tco2e"].sum()
    got_bu = report.groupby("business_unit_id")["total_gross_tco2e"].sum()

    close, total = 0, 0
    for bu, exp in gt_bu.items():
        total += 1
        got = float(got_bu.get(bu, 0.0))
        if math.isclose(got, float(exp), rel_tol=0.02):
            close += 1

    assert close >= int(total * 0.9), \
        f"Only {close}/{total} business units within ±2% of ground truth total emissions."


# ── Final gate: summary scalars ───────────────────────────────────────────────

def test_case_10_summary_scalars(ground_truth):
    """Company-wide gross emissions (±1%) and the highest-emitting business
    unit (exact) must match ground truth."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    expected_total = ground_truth["total_gross_emissions_tco2e"]
    rel_err = abs(s["total_gross_emissions_tco2e"] - expected_total) / expected_total
    assert rel_err <= 0.01, \
        (f"total_gross_emissions_tco2e: got {s['total_gross_emissions_tco2e']}, "
         f"expected {expected_total} (±1%)")

    assert s["business_unit_with_highest_emissions"] == ground_truth["business_unit_with_highest_emissions"], \
        (f"business_unit_with_highest_emissions: got {s['business_unit_with_highest_emissions']!r}, "
         f"expected {ground_truth['business_unit_with_highest_emissions']!r}")
