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


def _scope_ledger_to_ownership():
    """Shared composite-key join + NaT-safe ownership scoping, reused by the
    helper functions below."""
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
    return scoped


def _restatement_affected_keys():
    """(business_unit_id, facility_id, quarter) triples containing at least
    one Scope 1 emission_restatement row, scoped to ownership windows.
    Restricted to Scope 1 (restatements occur on both scopes) so this test
    is comparable on gross_scope1_tco2e alone, independent of the Scope 2
    dual-method question."""
    scoped = _scope_ledger_to_ownership()
    restated = scoped[(scoped["transaction_type"] == "emission_restatement") &
                       (scoped["scope"] == "Scope 1")]
    return set(zip(restated["business_unit_id"], restated["facility_id"], restated["quarter"]))


def _chain_affected_keys():
    """(business_unit_id, facility_id, quarter) triples where a Scope 1
    restatement was itself later superseded by a further restatement (chain
    depth >= 2), scoped to ownership windows. Restricted to Scope 1 for the
    same column-isolation reason as _restatement_affected_keys."""
    scoped = _scope_ledger_to_ownership()
    restatements = scoped[scoped["transaction_type"] == "emission_restatement"]
    restatement_entry_ids = set(restatements["entry_id"])
    chained = restatements[(restatements["original_entry_id"].isin(restatement_entry_ids)) &
                           (restatements["scope"] == "Scope 1")]
    return set(zip(chained["business_unit_id"], chained["facility_id"], chained["quarter"]))


def _offset_affected_keys():
    """(business_unit_id, facility_id, quarter) triples containing a
    purchased_offset or renewable_energy_certificate row, scoped to
    ownership windows."""
    scoped = _scope_ledger_to_ownership()
    offsets = scoped[scoped["transaction_type"].isin(
        ["purchased_offset", "renewable_energy_certificate"])]
    return set(zip(offsets["business_unit_id"], offsets["facility_id"], offsets["quarter"]))


def _single_method_facility_keys():
    """(business_unit_id, facility_id, quarter) triples for facilities whose
    Scope 2 verified_emission/emission_restatement entries carry no method
    value at all (no dual-method reporting), scoped to ownership windows.
    Restricted to those two transaction types because purchased_offset/
    renewable_energy_certificate rows always carry method=None regardless of
    a facility's dual/single-method status and would otherwise falsely mark
    dual-method facilities as single-method just for having an offset that
    quarter."""
    scoped = _scope_ledger_to_ownership()
    scope2 = scoped[(scoped["scope"] == "Scope 2") &
                     (scoped["transaction_type"].isin(["verified_emission", "emission_restatement"]))]
    no_method = scope2[scope2["method"].isna()]
    return set(zip(no_method["business_unit_id"], no_method["facility_id"], no_method["quarter"]))


COLLISION_FACILITY_IDS = _collision_facility_ids()
REACQUIRED_PAIRS = _reacquired_pairs()
RESTATEMENT_AFFECTED_KEYS = _restatement_affected_keys()
CHAIN_AFFECTED_KEYS = _chain_affected_keys()
SINGLE_METHOD_FACILITY_KEYS = _single_method_facility_keys()
OFFSET_AFFECTED_KEYS = _offset_affected_keys()


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: exact row counts, NaT ownership_end count, facility_id
    collision count, reacquired-pair count, restatement linkage integrity
    (including chained restatements), offset/REC/restatement sign
    conventions, and Scope 2 dual-method row symmetry."""
    bu  = pd.read_csv(DATA_DIR / "business_units.csv")
    fac = pd.read_csv(DATA_DIR / "facilities.csv")
    led = pd.read_csv(DATA_DIR / "emissions_ledger.csv")

    assert len(bu) == 12, "business_units.csv row count must not be modified."
    assert len(fac) == 144, "facilities.csv row count must not be modified."
    assert len(led) == 15_646, "emissions_ledger.csv row count must not be modified."

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
    assert len(offset_rec) == 501, \
        f"Expected 501 offset/REC ledger rows, got {len(offset_rec)}."
    assert (offset_rec["quantity_tco2e"] < 0).all(), \
        "purchased_offset/renewable_energy_certificate rows must carry negative quantity_tco2e."
    assert offset_rec["method"].isna().all(), \
        "purchased_offset/renewable_energy_certificate rows must not carry a method value."

    verified = led[led["transaction_type"] == "verified_emission"]
    assert (verified["quantity_tco2e"] > 0).all(), \
        "verified_emission rows must carry positive quantity_tco2e."

    restatements = led[led["transaction_type"] == "emission_restatement"]
    assert len(restatements) == 1_009, \
        f"Expected 1009 emission_restatement ledger rows, got {len(restatements)}."
    assert (restatements["quantity_tco2e"] > 0).all(), \
        "emission_restatement rows must carry positive quantity_tco2e."
    assert restatements["original_entry_id"].notna().all(), \
        "Every emission_restatement row must reference an original_entry_id."
    non_restatements = led[led["transaction_type"] != "emission_restatement"]
    assert non_restatements["original_entry_id"].isna().all(), \
        "Only emission_restatement rows may carry a non-null original_entry_id."
    assert set(restatements["original_entry_id"]).issubset(set(led["entry_id"])), \
        "Every emission_restatement.original_entry_id must reference a real ledger entry_id."

    restatement_entry_ids = set(restatements["entry_id"])
    n_chained = restatements["original_entry_id"].isin(restatement_entry_ids).sum()
    assert n_chained == 153, \
        f"Expected 153 second-level (chained) restatements, got {n_chained}."
    assert (restatements["method"] != "market_based").all(), \
        "No emission_restatement row may carry method == 'market_based'."

    scope1 = led[led["scope"] == "Scope 1"]
    assert scope1["method"].isna().all(), \
        "Scope 1 ledger rows must not carry a method value."

    verified_scope2 = verified[verified["scope"] == "Scope 2"]
    n_location = (verified_scope2["method"] == "location_based").sum()
    n_market = (verified_scope2["method"] == "market_based").sum()
    n_no_method = verified_scope2["method"].isna().sum()
    assert n_location == 3636 and n_market == 3636 and n_location == n_market, \
        (f"Expected equal location_based/market_based verified Scope 2 rows "
         f"(3636 each), got {n_location} location_based, {n_market} market_based.")
    assert n_no_method == 864, \
        (f"Expected 864 verified Scope 2 rows with no method value (single-method "
         f"facilities), got {n_no_method}.")

    single_method_keys = verified_scope2.loc[
        verified_scope2["method"].isna(), ["business_unit_id", "facility_id"]
    ].drop_duplicates()
    assert len(single_method_keys) == 24, \
        (f"Expected 24 facilities with no Scope 2 dual-method reporting at all, "
         f"got {len(single_method_keys)}.")


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_and_summary_structure():
    """Structural contract: esg_report.csv has the required columns, a
    plausible row count, and is sorted by business_unit_id/facility_id/
    quarter; esg_summary.json has the required keys with the right types."""
    assert REPORT_PATH.exists(), "esg_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required_cols = {"business_unit_id", "facility_id", "quarter",
                     "gross_scope1_tco2e", "gross_scope2_tco2e", "total_gross_tco2e"}
    missing_cols = required_cols - set(df.columns)
    assert not missing_cols, f"Missing columns in esg_report.csv: {missing_cols}"
    assert len(df) >= 350, f"Expected at least 350 rows in esg_report.csv, got {len(df)}."

    expected_order = df.sort_values(["business_unit_id", "facility_id", "quarter"]).reset_index(drop=True)
    assert list(df["business_unit_id"]) == list(expected_order["business_unit_id"]) and \
           list(df["facility_id"])      == list(expected_order["facility_id"]) and \
           list(df["quarter"])          == list(expected_order["quarter"]), \
        "esg_report.csv must be sorted by business_unit_id, facility_id, then quarter."

    assert SUMMARY_PATH.exists(), "esg_summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required_keys = {"total_gross_emissions_tco2e", "total_offset_credits_tco2e",
                     "facility_count_included", "business_unit_with_highest_emissions"}
    missing_keys = required_keys - set(s.keys())
    assert not missing_keys, f"Missing keys in esg_summary.json: {missing_keys}"
    assert isinstance(s["total_gross_emissions_tco2e"], float)
    assert isinstance(s["total_offset_credits_tco2e"], float)
    assert isinstance(s["facility_count_included"], int)
    assert isinstance(s["business_unit_with_highest_emissions"], str)


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

    # Restated entries: the correction supersedes the stale original. A
    # restatement can itself be superseded by a further restatement, so
    # exclusion is not limited to verified_emission rows — drop ANY row
    # whose entry_id appears as someone else's original_entry_id.
    superseded_ids = set(
        scoped.loc[scoped["transaction_type"] == "emission_restatement", "original_entry_id"].dropna()
    )
    is_verified = scoped["transaction_type"] == "verified_emission"
    is_restatement = scoped["transaction_type"] == "emission_restatement"
    gross_candidates = scoped[(is_verified | is_restatement) & ~scoped["entry_id"].isin(superseded_ids)]

    # Scope 2 dual-method reporting: only location-based (or Scope 1's null
    # method) counts toward gross; market-based is a separate accounting of
    # the same physical activity.
    is_gross_method = gross_candidates["method"].isna() | (gross_candidates["method"] == "location_based")
    gross = gross_candidates[is_gross_method]

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

def test_case_03_facility_count_included(ground_truth):
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


# ── Hard test 2: offsets/RECs excluded from gross, reported as a distinct scalar

def test_case_04_offset_exclusion_and_scalar(ground_truth):
    """total_gross_emissions_tco2e must be within ±2% of ground truth — netting
    offset/REC rows into the total (rather than filtering to verified_emission/
    emission_restatement only) understates this figure well outside that
    tolerance. total_offset_credits_tco2e (sum of purchased_offset + REC
    magnitudes, within their facilities' ownership windows) must also be
    within ±2% of ground truth."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)

    expected_gross = ground_truth["total_gross_emissions_tco2e"]
    rel_err_gross = abs(s["total_gross_emissions_tco2e"] - expected_gross) / expected_gross
    assert rel_err_gross <= 0.02, \
        (f"total_gross_emissions_tco2e: got {s['total_gross_emissions_tco2e']}, "
         f"expected {expected_gross} (±2%)")

    expected_offset = ground_truth["total_offset_credits_tco2e"]
    rel_err_offset = abs(s["total_offset_credits_tco2e"] - expected_offset) / expected_offset
    assert rel_err_offset <= 0.02, \
        (f"total_offset_credits_tco2e: got {s['total_offset_credits_tco2e']}, "
         f"expected {expected_offset} (±2%)")


# ── Hard test 4: composite key — colliding facility_id rows ──────────────────

def test_case_05_composite_key_facility_rows(ground_truth):
    """≥85% of rows for facility_id values reused across business units must be
    within ±10% of ground truth on gross_scope1_tco2e. A facility_id-only join
    duplicates these rows across the wrong business unit and inflates their
    totals. Compared on Scope 1 specifically, which has no method distinction,
    so this test is not affected by Scope 2 dual-method handling."""
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
        exp = float(gt_sub.loc[idx, "gross_scope1_tco2e"])
        got = float(got_sub.loc[idx, "gross_scope1_tco2e"])
        if math.isclose(got, exp, rel_tol=0.10):
            close += 1

    assert total > 0, "No colliding-facility rows found in report."
    assert close >= int(total * 0.85), \
        f"Only {close}/{total} colliding-facility rows within ±10% of ground truth."


# ── Hard test 5: multi-interval ownership (divested then reacquired) ─────────

def test_case_06_multi_interval_ownership(ground_truth):
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

def test_case_07_restatement_netting(ground_truth):
    """Facility-quarters containing a restated Scope 1 verified_emission entry
    must reflect the corrected figure, not the stale original (and not both
    summed together). ≥70% of affected (business_unit_id, facility_id,
    quarter) rows must be within ±3% of ground truth on gross_scope1_tco2e.
    Restricted to Scope 1 (restatements occur on both scopes) so this test is
    not affected by Scope 2 dual-method handling."""
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
        exp = float(gt_sub.loc[key, "gross_scope1_tco2e"])
        got = float(got_sub.loc[key, "gross_scope1_tco2e"]) if key in got_sub.index else 0.0
        if math.isclose(got, exp, rel_tol=0.03):
            close += 1

    assert close >= int(total * 0.7), \
        f"Only {close}/{total} restatement-affected facility-quarter rows within ±3% of ground truth."


# ── Hard test 7: chained restatements (restatement of a restatement) ─────────

def test_case_08_chained_restatement_netting(ground_truth):
    """A subset of Scope 1 restatements are themselves later superseded by a
    second restatement, whose original_entry_id points at the FIRST
    restatement's entry_id rather than at the original verified_emission
    entry. Resolving only one level of supersession leaves the superseded
    intermediate restatement double-counted alongside its own successor.
    ≥70% of chain-affected (business_unit_id, facility_id, quarter) rows
    must be within ±3% of ground truth on gross_scope1_tco2e."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    assert len(CHAIN_AFFECTED_KEYS) > 0, "No chained-restatement-affected facility-quarter rows found."

    gt_sub = gt.set_index(["business_unit_id", "facility_id", "quarter"])
    got_sub = report.set_index(["business_unit_id", "facility_id", "quarter"])

    close, total = 0, 0
    for key in CHAIN_AFFECTED_KEYS:
        if key not in gt_sub.index:
            continue
        total += 1
        exp = float(gt_sub.loc[key, "gross_scope1_tco2e"])
        got = float(got_sub.loc[key, "gross_scope1_tco2e"]) if key in got_sub.index else 0.0
        if math.isclose(got, exp, rel_tol=0.03):
            close += 1

    assert close >= int(total * 0.7), \
        f"Only {close}/{total} chained-restatement-affected facility-quarter rows within ±3% of ground truth."


# ── Hard test 8: Scope 2 dual-method reporting ────────────────────────────────

def test_case_09_scope2_method_netting(ground_truth):
    """Every dual-method Scope 2 verified_emission entry has a location_based
    and a market_based companion row for the same facility/month/source — two
    GHG Protocol accountings of the same physical activity, not two additive
    quantities. Summing both roughly doubles gross_scope2_tco2e. ≥85% of
    report rows with nonzero Scope 2 activity must be within ±10% of ground
    truth on that column. Restricted to facility-quarters that are not
    themselves affected by the composite-key collision, offset/REC, or
    single-method traps, so this test isolates the double-counting failure
    mode specifically."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    gt_sub = gt.set_index(["business_unit_id", "facility_id", "quarter"])
    got_sub = report.set_index(["business_unit_id", "facility_id", "quarter"])

    close, total = 0, 0
    for key, exp in gt_sub["gross_scope2_tco2e"].items():
        bu, fac, quarter = key
        if fac in COLLISION_FACILITY_IDS or key in OFFSET_AFFECTED_KEYS or key in SINGLE_METHOD_FACILITY_KEYS:
            continue
        exp = float(exp)
        if exp == 0.0:
            continue
        total += 1
        got = float(got_sub.loc[key, "gross_scope2_tco2e"]) if key in got_sub.index else 0.0
        if math.isclose(got, exp, rel_tol=0.10):
            close += 1

    assert total > 0, "No report rows with nonzero Scope 2 activity found in ground truth."
    assert close >= int(total * 0.85), \
        f"Only {close}/{total} rows had gross_scope2_tco2e within ±10% of ground truth."


# ── Hard test 9b: Scope 2 single-method facilities (no dual reporting) ───────

def test_case_10_single_method_facility_inclusion(ground_truth):
    """A subset of facilities do not do Scope 2 dual-method reporting at all —
    their Scope 2 entries carry no method value, same null convention as
    Scope 1. instruction.md states that only entries specifying the
    location-based method count toward gross Scope 2, without saying what to
    do with entries that specify no method at all. A literal
    `method == "location_based"` filter (rather than
    `method.isna() | (method == "location_based")`) silently drops these
    facilities' entire Scope 2 figure. ≥85% of these facility-quarter rows
    must be within ±10% of ground truth on gross_scope2_tco2e. Excludes
    facility-quarters also affected by the composite-key collision or
    offset/REC traps, so this test isolates the null-handling question
    specifically."""
    report = pd.read_csv(REPORT_PATH)
    gt = ground_truth["report"]

    assert len(SINGLE_METHOD_FACILITY_KEYS) > 0, "No single-method facility-quarter rows found."

    gt_sub = gt.set_index(["business_unit_id", "facility_id", "quarter"])
    got_sub = report.set_index(["business_unit_id", "facility_id", "quarter"])

    close, total = 0, 0
    for key in SINGLE_METHOD_FACILITY_KEYS:
        bu, fac, quarter = key
        if fac in COLLISION_FACILITY_IDS or key in OFFSET_AFFECTED_KEYS:
            continue
        if key not in gt_sub.index:
            continue
        exp = float(gt_sub.loc[key, "gross_scope2_tco2e"])
        if exp == 0.0:
            continue
        total += 1
        got = float(got_sub.loc[key, "gross_scope2_tco2e"]) if key in got_sub.index else 0.0
        if math.isclose(got, exp, rel_tol=0.10):
            close += 1

    assert total > 0, "No nonzero single-method facility-quarter rows found in ground truth."
    assert close >= int(total * 0.85), \
        f"Only {close}/{total} single-method facility-quarter rows had gross_scope2_tco2e within ±10% of ground truth."


# ── Hard test 9: business-unit aggregate totals ───────────────────────────────

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
