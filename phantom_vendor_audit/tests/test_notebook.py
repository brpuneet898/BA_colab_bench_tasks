"""
Unit tests for the Phantom Vendor Weekend audit analytics task.

Ground truth: 6 phantom vendor clusters (14 aliases total) whose invoices are
exclusively submitted on weekends with amounts split into round-$1000 groups.
Bank account tokens reveal intra-BU linkages; name camouflage (homoglyph
swaps, abbreviation, casing) hides cross-BU connections.  One cluster has
aliases in different business units so its bank tokens will NOT match —
it can only be found through name resolution.

Tests verify entity resolution, behavioral pattern detection, and evidence
compilation without executing the agent notebook.
"""

import json
import pytest
import pandas as pd
from pathlib import Path

WORKSPACE = Path("/workspace")
VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")

PHANTOM_CLUSTERS_GROUND_TRUTH = {
    "PHANTOM_001": ["V-1001", "V-1002", "V-1003"],
    "PHANTOM_002": ["V-2001", "V-2002"],
    "PHANTOM_003": ["V-3001", "V-3002", "V-3003"],
    "PHANTOM_004": ["V-4001", "V-4002"],
    "PHANTOM_005": ["V-5001", "V-5002"],
    "PHANTOM_006": ["V-6001", "V-6002"],
}

ALL_PHANTOM_VENDOR_IDS = sorted(
    [vid for vids in PHANTOM_CLUSTERS_GROUND_TRUTH.values() for vid in vids]
)

EXPECTED_TOTAL_VENDORS = 54
EXPECTED_TOTAL_INVOICES = 2157
EXPECTED_TOTAL_PAYMENTS = 1707
EXPECTED_WEEKEND_ONLY_VENDORS = ALL_PHANTOM_VENDOR_IDS
EXPECTED_SHARED_TOKEN_PAIRS = 7


@pytest.fixture(scope="module")
def notebook_variables() -> dict:
    """Load variables extracted from the notebook."""
    assert VARIABLES_PATH.exists(), (
        "notebook_variables.json was not created in /logs/verifier"
    )
    with open(VARIABLES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def vendor_clusters_df() -> pd.DataFrame:
    """Load the vendor_clusters.csv output."""
    path = WORKSPACE / "vendor_clusters.csv"
    assert path.exists(), "vendor_clusters.csv not found at /workspace/"
    return pd.read_csv(path)


@pytest.fixture(scope="module")
def suspicious_vendors_df() -> pd.DataFrame:
    """Load the suspicious_vendors.csv output."""
    path = WORKSPACE / "suspicious_vendors.csv"
    assert path.exists(), "suspicious_vendors.csv not found at /workspace/"
    return pd.read_csv(path)


@pytest.fixture(scope="module")
def investigation_report() -> dict:
    """Load the investigation_report.json output."""
    path = WORKSPACE / "investigation_report.json"
    assert path.exists(), "investigation_report.json not found at /workspace/"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Test 1: Data loading ----------

def test_data_loaded(notebook_variables):
    """Verify the three source datasets were loaded with correct row counts."""
    assert "total_vendors" in notebook_variables, "total_vendors not defined"
    assert "total_invoices" in notebook_variables, "total_invoices not defined"
    assert "total_payments" in notebook_variables, "total_payments not defined"

    assert notebook_variables["total_vendors"] == EXPECTED_TOTAL_VENDORS, (
        f"Expected {EXPECTED_TOTAL_VENDORS} vendors, got {notebook_variables['total_vendors']}"
    )
    assert notebook_variables["total_invoices"] == EXPECTED_TOTAL_INVOICES, (
        f"Expected {EXPECTED_TOTAL_INVOICES} invoices, got {notebook_variables['total_invoices']}"
    )
    assert notebook_variables["total_payments"] == EXPECTED_TOTAL_PAYMENTS, (
        f"Expected {EXPECTED_TOTAL_PAYMENTS} payments, got {notebook_variables['total_payments']}"
    )


# ---------- Test 2: Vendor clusters file structure ----------

def test_vendor_clusters_structure(vendor_clusters_df):
    """Verify vendor_clusters.csv has the required columns, shape, and non-trivial clustering."""
    required_cols = ["vendor_id", "vendor_name", "canonical_id", "cluster_size"]
    for col in required_cols:
        assert col in vendor_clusters_df.columns, (
            f"Missing column '{col}' in vendor_clusters.csv"
        )
    assert len(vendor_clusters_df) >= EXPECTED_TOTAL_VENDORS, (
        f"Expected at least {EXPECTED_TOTAL_VENDORS} rows, got {len(vendor_clusters_df)}"
    )
    has_multi_vendor_cluster = (vendor_clusters_df["cluster_size"] > 1).any()
    assert has_multi_vendor_cluster, (
        "No multi-vendor clusters found — entity resolution must identify "
        "at least one group of vendor IDs belonging to the same entity"
    )


# ---------- Test 3: Phantom cluster detection ----------

def test_phantom_clusters_detected(vendor_clusters_df):
    """
    Verify that the agent correctly grouped phantom vendor aliases.
    At least 5 of the 6 ground-truth clusters must be identified: for each,
    at least 2 of its vendor IDs must share the same canonical_id.
    """
    vid_to_canonical = dict(
        zip(vendor_clusters_df["vendor_id"], vendor_clusters_df["canonical_id"])
    )

    clusters_found = 0
    for cluster_name, vendor_ids in PHANTOM_CLUSTERS_GROUND_TRUTH.items():
        canonical_ids = [
            vid_to_canonical.get(vid) for vid in vendor_ids
            if vid in vid_to_canonical
        ]
        canonical_ids = [c for c in canonical_ids if c is not None]
        if len(canonical_ids) < 2:
            continue
        from collections import Counter
        counts = Counter(canonical_ids)
        most_common_count = counts.most_common(1)[0][1]
        if most_common_count >= 2:
            clusters_found += 1

    assert clusters_found >= 5, (
        f"Expected at least 5 of 6 phantom clusters to be correctly grouped, "
        f"found {clusters_found}"
    )


# ---------- Test 4: Suspicious vendors file structure ----------

def test_suspicious_vendors_structure(suspicious_vendors_df):
    """Verify suspicious_vendors.csv has required columns and at least 5 rows."""
    required_cols = ["rank", "canonical_id", "risk_score", "vendor_ids", "evidence_summary"]
    for col in required_cols:
        assert col in suspicious_vendors_df.columns, (
            f"Missing column '{col}' in suspicious_vendors.csv"
        )
    assert len(suspicious_vendors_df) >= 5, (
        f"Expected at least 5 suspicious vendors, got {len(suspicious_vendors_df)}"
    )


# ---------- Test 5: Top suspicious vendors are phantoms ----------

def test_suspicious_vendors_top5_contain_phantoms(suspicious_vendors_df):
    """
    The top 5 suspicious vendor entries must collectively reference at
    least 10 of the 14 phantom vendor IDs. This allows tolerance for
    imperfect entity resolution while ensuring the core fraud is detected.
    """
    top5 = suspicious_vendors_df.sort_values("rank").head(5)
    found_phantom_ids = set()
    for _, row in top5.iterrows():
        vendor_ids_str = str(row.get("vendor_ids", ""))
        for vid in vendor_ids_str.split("|"):
            vid = vid.strip()
            if vid in ALL_PHANTOM_VENDOR_IDS:
                found_phantom_ids.add(vid)

    assert len(found_phantom_ids) >= 10, (
        f"Top 5 suspicious vendors should reference at least 10 of the 14 phantom "
        f"vendor IDs, but only found {len(found_phantom_ids)}: {sorted(found_phantom_ids)}"
    )


# ---------- Test 6: Weekend-only vendors ----------

def test_weekend_only_vendors(notebook_variables):
    """
    Verify that the agent identified all vendor IDs with 100% weekend
    invoices. This is a direct data observation — the answer is
    deterministic.
    """
    assert "weekend_only_vendors" in notebook_variables, (
        "weekend_only_vendors not defined in notebook variables"
    )
    submitted = sorted(notebook_variables["weekend_only_vendors"])
    expected = EXPECTED_WEEKEND_ONLY_VENDORS

    assert submitted == expected, (
        f"weekend_only_vendors mismatch.\n"
        f"  Expected: {expected}\n"
        f"  Got:      {submitted}"
    )


# ---------- Test 7: Shared bank token pairs ----------

def test_shared_token_pairs(notebook_variables):
    """
    Verify the count of vendor pairs sharing the same bank account token
    within the same business unit. This is a direct data observation.
    """
    assert "num_shared_token_pairs" in notebook_variables, (
        "num_shared_token_pairs not defined in notebook variables"
    )
    actual = notebook_variables["num_shared_token_pairs"]
    assert actual == EXPECTED_SHARED_TOKEN_PAIRS, (
        f"Expected {EXPECTED_SHARED_TOKEN_PAIRS} shared-token pairs, got {actual}"
    )


# ---------- Test 8: Investigation report structure ----------

def test_investigation_report_structure(investigation_report):
    """Verify investigation_report.json has the required top-level keys and types."""
    required_keys = [
        "phantom_vendor_clusters",
        "weekend_pattern",
        "split_invoice_groups",
        "shared_bank_tokens",
    ]
    for key in required_keys:
        assert key in investigation_report, (
            f"Missing key '{key}' in investigation_report.json"
        )

    assert isinstance(investigation_report["phantom_vendor_clusters"], list), (
        "phantom_vendor_clusters must be a list"
    )
    assert len(investigation_report["phantom_vendor_clusters"]) >= 1, (
        "phantom_vendor_clusters must contain at least one identified cluster"
    )
    assert isinstance(investigation_report["weekend_pattern"], dict), (
        "weekend_pattern must be a dict"
    )
    assert isinstance(investigation_report["split_invoice_groups"], int), (
        "split_invoice_groups must be an int"
    )
    assert isinstance(investigation_report["shared_bank_tokens"], list), (
        "shared_bank_tokens must be a list"
    )
    assert len(investigation_report["shared_bank_tokens"]) >= 1, (
        "shared_bank_tokens must contain at least one shared-token entry"
    )


# ---------- Test 9: Investigation report weekend pattern ----------

def test_investigation_report_weekend_content(investigation_report):
    """
    Verify that the weekend_pattern section identifies the correct number
    of weekend-only vendors.
    """
    wp = investigation_report["weekend_pattern"]
    assert "total_weekend_only_vendors" in wp, (
        "weekend_pattern must contain 'total_weekend_only_vendors'"
    )
    assert "vendor_ids" in wp, (
        "weekend_pattern must contain 'vendor_ids'"
    )
    assert wp["total_weekend_only_vendors"] == len(EXPECTED_WEEKEND_ONLY_VENDORS), (
        f"Expected {len(EXPECTED_WEEKEND_ONLY_VENDORS)} weekend-only vendors, "
        f"got {wp['total_weekend_only_vendors']}"
    )


# ---------- Test 10: Invoice-payment linkage file ----------

def test_invoice_payment_linkage_exists():
    """Verify invoice_payment_linkage.csv exists, has required columns, and contains real matches."""
    path = WORKSPACE / "invoice_payment_linkage.csv"
    assert path.exists(), "invoice_payment_linkage.csv not found at /workspace/"
    df = pd.read_csv(path)
    required_cols = ["invoice_id", "payment_id", "vendor_id", "invoice_amount",
                     "payment_amount", "match_type"]
    for col in required_cols:
        assert col in df.columns, (
            f"Missing column '{col}' in invoice_payment_linkage.csv"
        )
    assert len(df) >= 100, (
        f"invoice_payment_linkage.csv has only {len(df)} rows — expected at least "
        f"100 linkage entries across {EXPECTED_TOTAL_INVOICES} invoices"
    )
    valid_match_types = {"exact", "partial", "unmatched"}
    actual_types = set(df["match_type"].dropna().unique())
    assert actual_types.issubset(valid_match_types), (
        f"match_type contains invalid values: {actual_types - valid_match_types}"
    )
    assert (df["match_type"] == "exact").sum() >= 1, (
        "invoice_payment_linkage.csv must contain at least one 'exact' match"
    )
