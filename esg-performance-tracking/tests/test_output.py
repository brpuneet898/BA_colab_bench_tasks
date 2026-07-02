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


def _reacquired_pairs():
    """(business_unit_id, facility_id) pairs with two facilities.csv rows —
    divested then reacquired within FY2024."""
    facilities = pd.read_csv(DATA_DIR / "facilities.csv")
    counts = facilities.groupby(["business_unit_id", "facility_id"]).size()
    return set(counts[counts > 1].index)


def _restatement_affected_keys():
    """(business_unit_id, facility_id, quarter) triples containing at least
    one emission_restatement row, scoped to ownership windows."""
    facilities = pd.read_csv(
        DATA_DIR / "facilities.csv",
        parse_dates=["ownership_start", "ownership_end"],
    )
    ledger = pd.read_csv(DATA_DIR / "emissions_ledger.csv", parse_dates=["reporting_date"])
    merged = ledger.merge(
        facilities[["business_unit_id", "facility_id", "ownership_start", "ownership_end"]],
        on=["business_unit_id", "facility_id"], how="inner",
    )
    in_window = (
        (merged["reporting_date"] >= merged["ownership_start"]) &
        (merged["ownership_end"].isna() | (merged["reporting_date"] <= merged["ownership_end"]))
    )
    scoped = merged[in_window].copy()
    scoped["quarter"] = "2024-Q" + ((scoped["reporting_date"].dt.month - 1) // 3 + 1).astype(str)
    restated = scoped[scoped["transaction_type"] == "emission_restatement"]
    return set(zip(restated["business_unit_id"], restated["facility_id"], restated["quarter"]))


COLLISION_FACILITY_IDS = _collision_facility_ids()
REACQUIRED_PAIRS = _reacquired_pairs()
RESTATEMENT_AFFECTED_KEYS = _restatement_affected_keys()


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: exact row counts, NaT ownership_end count, facility_id
    collision count, reacquired-pair count, restatement linkage integrity,
    and offset/REC/restatement sign conventions."""
    bu  = pd.read_csv(DATA_DIR / "business_units.csv")
    fac = pd.read_csv(DATA_DIR / "facilities.csv")
    led = pd.read_csv(DATA_DIR / "emissions_ledger.csv")

    assert len(bu) == 12, "business_units.csv row count must not be modified."
    assert len(fac) == 144, "facilities.csv row count must not be modified."
    assert len(led) == 12_977, "emissions_ledger.csv row count must not be modified."

    nat_count = fac["ownership_end"].isna().sum()
    assert nat_count == 112, \
        f"Expected 112 facilities with open-ended ownership_end (NaT), got {nat_count}."

    collisions = fac.groupby("facility_id")["business_unit_id"].nunique()
    n_collisions = (collisions > 1).sum()
    assert n_collisions == 15, \
        f"Expected 15 facility_id values reused across business units, got {n_collisions}."

    reacquired_counts = fac.groupby(["business_unit_id", "facility_id"]).size()
    n_reacquired = (reacquired_counts > 1).sum()
    assert n_reacquired == 12, \
        f"Expected 12 (business_unit_id, facility_id) pairs with two ownership-window rows, got {n_reacquired}."

    offset_rec = led[led["transaction_type"].isin(
        ["purchased_offset", "renewable_energy_certificate"])]
    assert len(offset_rec) == 533, \
        f"Expected 533 offset/REC ledger rows, got {len(offset_rec)}."
    assert (offset_rec["quantity_tco2e"] < 0).all(), \
        "purchased_offset/renewable_energy_certificate rows must carry negative quantity_tco2e."

    verified = led[led["transaction_type"] == "verified_emission"]
    assert (verified["quantity_tco2e"] > 0).all(), \
        "verified_emission rows must carry positive quantity_tco2e."

    restatements = led[led["transaction_type"] == "emission_restatement"]
    assert len(restatements) == 936, \
        f"Expected 936 emission_restatement ledger rows, got {len(restatements)}."
    assert (restatements["quantity_tco2e"] > 0).all(), \
        "emission_restatement rows must carry positive quantity_tco2e."
    assert restatements["original_entry_id"].notna().all(), \
        "Every emission_restatement row must reference an original_entry_id."
    non_restatements = led[led["transaction_type"] != "emission_restatement"]
    assert non_restatements["original_entry_id"].isna().all(), \
        "Only emission_restatement rows may carry a non-null original_entry_id."
    assert set(restatements["original_entry_id"]).issubset(set(verified["entry_id"])), \
        "Every emission_restatement.original_entry_id must reference a real verified_emission entry_id."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "esg_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"business_unit_id", "facility_id", "quarter",
                "gross_scope1_tco2e", "gross_scope2_tco2e", "total_gross_tco2e"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) >= 350, f"Expected at least 350 rows, got {len(df)}."


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

    # Restated entries: the correction supersedes the stale original.
    restated_ids = set(
        scoped.loc[scoped["transaction_type"] == "emission_restatement", "original_entry_id"].dropna()
    )
    is_verified = scoped["transaction_type"] == "verified_emission"
    is_restatement = scoped["transaction_type"] == "emission_restatement"
    gross = scoped[(is_verified & ~scoped["entry_id"].isin(restated_ids)) | is_restatement]
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

    facility_count = int(report[["business_unit_id", "facility_id"]].drop_duplicates().shape[0])

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


# ── Hard test 5: multi-interval ownership (divested then reacquired) ─────────

def test_case_09_multi_interval_ownership(ground_truth):
    """(business_unit_id, facility_id) pairs divested and later reacquired
    within FY2024 carry two facilities.csv rows sharing the same composite
    key. The quarter(s) in the gap between the two ownership windows must be
    excluded; quarters covered by either window must be included. A naive
    single-row lookup (e.g. deduplicating facilities.csv on the composite key
    before joining) silently drops one window's quarters."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    assert len(REACQUIRED_PAIRS) > 0, "No multi-interval facilities found in facilities.csv."

    close, total = 0, 0
    for bu, fac in REACQUIRED_PAIRS:
        gt_quarters = set(gt[(gt["business_unit_id"] == bu) & (gt["facility_id"] == fac)]["quarter"])
        got_quarters = set(report[(report["business_unit_id"] == bu) & (report["facility_id"] == fac)]["quarter"])
        total += 1
        if got_quarters == gt_quarters:
            close += 1

    assert close >= int(total * 0.7), \
        f"Only {close}/{total} multi-interval facilities had the correct set of quarters included."


# ── Hard test 6: restatement netting ──────────────────────────────────────────

def test_case_10_restatement_netting(ground_truth):
    """Facility-quarters containing a restated verified_emission entry must
    reflect the corrected figure, not the stale original (and not both summed
    together). ≥70% of affected (business_unit_id, facility_id, quarter) rows
    must be within ±3% of ground truth."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    assert len(RESTATEMENT_AFFECTED_KEYS) > 0, "No restatement-affected facility-quarter rows found."

    gt_sub = gt.set_index(["business_unit_id", "facility_id", "quarter"])
    got_sub = report.set_index(["business_unit_id", "facility_id", "quarter"])

    close, total = 0, 0
    for key in RESTATEMENT_AFFECTED_KEYS:
        if key not in gt_sub.index:
            continue
        total += 1
        exp = float(gt_sub.loc[key, "total_gross_tco2e"])
        got = float(got_sub.loc[key, "total_gross_tco2e"]) if key in got_sub.index else 0.0
        if math.isclose(got, exp, rel_tol=0.03):
            close += 1

    assert close >= int(total * 0.7), \
        f"Only {close}/{total} restatement-affected facility-quarter rows within ±3% of ground truth."


# ── Hard test 7: business-unit aggregate totals ───────────────────────────────

def test_case_11_business_unit_totals(ground_truth):
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

def test_case_12_summary_scalars(ground_truth):
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
