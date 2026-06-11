"""
Tests for the H1 2025 NPS program report.

Contract (instruction.md): deliverables are
    /workspace/nps_report.csv  — 18 rows, one per (wave, segment)
    /workspace/summary.json    — 7 scalar keys

Reasoning challenges embedded in the data:
  1. Response/selection bias → sign flip: after the April channel switch, SMB
     response rates drop steeply while enterprise stays high. Pooled respondent
     NPS rises Q1→Q2; base-weighted NPS (required by reporting standard) falls.
     A model that defaults to pooled NPS reports the wrong trend direction.
  2. Revenue-at-risk ranking flip: mobile_app_performance has the most detractor
     complaints; data_export_reliability has fewer but enterprise-heavy MRR →
     highest revenue at risk. Count-based ranking gives the wrong theme.
  3. Legacy scale mismatch: phone_ivr channel records 1–5 CSAT, not 0–10 NPS.
     Including those responses misclassifies every phone_ivr response as a detractor.
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = (
    WORKSPACE_DIR / "data"
    if (WORKSPACE_DIR / "data").exists()
    else Path(__file__).parent.parent / "environment" / "data"
)

REPORT_PATH = WORKSPACE_DIR / "nps_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

WAVES = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
SEGMENTS = ["enterprise", "mid_market", "smb"]
Q1_WAVES = {"2025-01", "2025-02", "2025-03"}
Q2_WAVES = {"2025-04", "2025-05", "2025-06"}


# ---------------------------------------------------------------------------
# Shared ground-truth fixture (computed once per session)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ground_truth():
    customers = pd.read_csv(DATA_DIR / "customers.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses = pd.read_csv(DATA_DIR / "survey_responses.csv")
    channels = pd.read_csv(DATA_DIR / "survey_channels.csv")

    real_cust = customers[~customers["is_test_account"]].copy()

    nps_channel_set = set(channels.loc[channels["score_scale"] == "0-10", "channel"])

    merged = (
        responses
        .merge(invitations[["invitation_id", "customer_id", "wave", "channel"]], on="invitation_id")
        .merge(real_cust[["customer_id", "segment"]], on="customer_id")
    )
    valid = merged[merged["channel"].isin(nps_channel_set)].copy()

    seg_counts = real_cust.groupby("segment").size()
    seg_weights = (seg_counts / seg_counts.sum()).to_dict()

    inv_with_seg = invitations.merge(real_cust[["customer_id", "segment"]], on="customer_id")
    invited_counts = inv_with_seg.groupby(["wave", "segment"]).size().to_dict()

    def nps_from_scores(s):
        n = len(s)
        if n == 0:
            return 0.0
        return float(round((((s >= 9).sum() / n) - ((s <= 6).sum() / n)) * 100, 2))

    def weighted_nps(waves_set):
        period = valid[valid["wave"].isin(waves_set)]
        nps_seg = {seg: nps_from_scores(period[period["segment"] == seg]["score"]) for seg in SEGMENTS}
        return float(round(sum(nps_seg[s] * seg_weights.get(s, 0) for s in SEGMENTS), 2))

    overall_q1 = weighted_nps(Q1_WAVES)
    overall_q2 = weighted_nps(Q2_WAVES)
    nps_change = float(round(overall_q2 - overall_q1, 2))

    # Revenue at risk
    dets = valid[(valid["score"] <= 6) & (valid["primary_theme"].notna())]
    dets_mrr = dets.merge(real_cust[["customer_id", "monthly_recurring_revenue"]], on="customer_id")
    mrr_at_risk = (
        dets_mrr.drop_duplicates(subset=["customer_id", "primary_theme"])
        .groupby("primary_theme")["monthly_recurring_revenue"]
        .sum()
        .sort_values(ascending=False)
    )
    top_theme = str(mrr_at_risk.index[0])
    top_at_risk = float(round(mrr_at_risk.iloc[0], 2))

    # Per-wave per-segment counts
    resp_counts = valid.groupby(["wave", "segment"]).size().to_dict()

    wave_seg_nps = {}
    for (wave, seg), grp in valid.groupby(["wave", "segment"]):
        wave_seg_nps[(wave, seg)] = nps_from_scores(grp["score"])

    return {
        "seg_weights": seg_weights,
        "invited_counts": invited_counts,
        "resp_counts": resp_counts,
        "wave_seg_nps": wave_seg_nps,
        "overall_q1": overall_q1,
        "overall_q2": overall_q2,
        "nps_change": nps_change,
        "nps_trend": "declining" if nps_change < 0 else "improving",
        "top_theme": top_theme,
        "top_at_risk": top_at_risk,
        "valid_response_count": int(len(valid)),
        "customers_row_count": len(customers),
        "invitations_row_count": len(invitations),
        "responses_row_count": len(responses),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_case_01_anti_cheat_sentinels():
    """Input data must not have been modified. Row count sentinels anchor ground truth."""
    customers = pd.read_csv(DATA_DIR / "customers.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses = pd.read_csv(DATA_DIR / "survey_responses.csv")

    assert len(customers) == 12_000, "customers.csv must not be modified"
    assert len(invitations) == 71_730, "survey_invitations.csv must not be modified"
    assert len(responses) == 17_061, "survey_responses.csv must not be modified"

    # Test accounts must be present
    assert customers["is_test_account"].sum() >= 40, "Test accounts must be present"

    # phone_ivr responses must be present and all scores in 1-5
    channels = pd.read_csv(DATA_DIR / "survey_channels.csv")
    phone_ivr_channel = channels.loc[channels["score_scale"] == "1-5", "channel"].values
    assert len(phone_ivr_channel) >= 1, "A non-0-10 channel must exist in survey_channels.csv"

    inv_ch = invitations.merge(responses[["invitation_id", "score"]], on="invitation_id")
    phone_inv = inv_ch[inv_ch["channel"] == "phone_ivr"]
    assert len(phone_inv) >= 100, "phone_ivr responses must be present"
    assert phone_inv["score"].max() <= 5, "phone_ivr scores must not exceed 5"
    assert phone_inv["score"].min() >= 1, "phone_ivr scores must be at least 1"


def test_case_02_report_structure():
    """nps_report.csv must exist with exactly the right columns and 18 rows."""
    assert REPORT_PATH.exists(), "nps_report.csv not found in /workspace/"
    report = pd.read_csv(REPORT_PATH)
    expected_cols = ["wave", "segment", "invited", "responses", "response_rate", "segment_nps"]
    assert list(report.columns) == expected_cols, f"Wrong columns: {list(report.columns)}"
    assert len(report) == 18, f"Expected 18 rows (6 waves × 3 segments), got {len(report)}"
    assert set(report["wave"]) == set(WAVES), "Wave values do not match expected 2025-01..2025-06"
    assert set(report["segment"]) == set(SEGMENTS), "Segment values do not match expected set"


def test_case_03_invited_and_response_counts(ground_truth):
    """Invited and response counts per (wave, segment) must match ground truth exactly."""
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        key = (row["wave"], row["segment"])
        expected_invited = ground_truth["invited_counts"].get(key, 0)
        expected_resp = ground_truth["resp_counts"].get(key, 0)
        assert int(row["invited"]) == expected_invited, (
            f"Invited count mismatch for {key}: got {row['invited']}, expected {expected_invited}"
        )
        assert int(row["responses"]) == expected_resp, (
            f"Response count mismatch for {key}: got {row['responses']}, expected {expected_resp}"
        )


def test_case_04_response_rates(ground_truth):
    """Response rates must equal responses / invited, rounded to 4 decimal places."""
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        expected = round(row["responses"] / row["invited"], 4) if row["invited"] > 0 else 0.0
        assert math.isclose(row["response_rate"], expected, abs_tol=0.0001), (
            f"response_rate mismatch for ({row['wave']}, {row['segment']}): "
            f"got {row['response_rate']}, expected {expected}"
        )


def test_case_05_segment_nps_values(ground_truth):
    """
    Segment NPS values must match ground truth within ±0.5 NPS points.

    QC NOTE: This test catches both Trap 3 (phone_ivr scale mismatch) and
    straightforward NPS calculation errors. The abs_tol of 0.5 accommodates
    minor floating-point rounding differences while rejecting the ~3-6 point
    systematic bias introduced by including phone_ivr responses as 0-10 scores.
    The tolerance is NOT generous enough to pass an implementation that includes
    phone_ivr (which shifts mid_market NPS by several points).
    """
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        key = (row["wave"], row["segment"])
        expected_nps = ground_truth["wave_seg_nps"].get(key, 0.0)
        assert math.isclose(float(row["segment_nps"]), expected_nps, abs_tol=0.5), (
            f"segment_nps mismatch for {key}: got {row['segment_nps']}, expected {expected_nps}"
        )


def test_case_06_summary_structure():
    """summary.json must exist with exactly the right keys and scalar types."""
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace/"
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    expected_keys = {
        "overall_nps_q1", "overall_nps_q2", "nps_change_h1",
        "nps_trend", "top_revenue_risk_theme", "top_revenue_at_risk",
        "valid_response_count",
    }
    assert set(s.keys()) == expected_keys, (
        f"Wrong keys in summary.json. Extra: {set(s.keys()) - expected_keys}, "
        f"Missing: {expected_keys - set(s.keys())}"
    )
    assert isinstance(s["overall_nps_q1"], float), "overall_nps_q1 must be float"
    assert isinstance(s["overall_nps_q2"], float), "overall_nps_q2 must be float"
    assert isinstance(s["nps_change_h1"], float), "nps_change_h1 must be float"
    assert isinstance(s["nps_trend"], str), "nps_trend must be str"
    assert isinstance(s["top_revenue_risk_theme"], str), "top_revenue_risk_theme must be str"
    assert isinstance(s["top_revenue_at_risk"], float), "top_revenue_at_risk must be float"
    assert isinstance(s["valid_response_count"], int), "valid_response_count must be int"


def test_case_07_overall_nps_values(ground_truth):
    """
    Q1 and Q2 overall NPS must match the base-weighted ground truth within ±0.5.

    QC NOTE (trap 1): This test catches the pooled-vs-weighted computation error.
    The base-weighted Q1 NPS is ~3.8 and Q2 is ~-18.5. A model that computes pooled
    respondent NPS (the natural default) will report ~8.8 for Q1 and ~20.8 for Q2 —
    both wrong and in the wrong direction for Q2. The abs_tol of 0.5 is tight enough
    to reject pooled NPS (which is ~22 points off for Q2) while allowing minor
    rounding differences in correct implementations.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    gt_q1 = ground_truth["overall_q1"]
    gt_q2 = ground_truth["overall_q2"]
    assert math.isclose(s["overall_nps_q1"], gt_q1, abs_tol=0.5), (
        f"overall_nps_q1: got {s['overall_nps_q1']}, expected ~{gt_q1}"
    )
    assert math.isclose(s["overall_nps_q2"], gt_q2, abs_tol=0.5), (
        f"overall_nps_q2: got {s['overall_nps_q2']}, expected ~{gt_q2}"
    )


def test_case_08_nps_trend_and_change(ground_truth):
    """
    nps_change_h1 and nps_trend must reflect the base-weighted declining trend.

    QC NOTE (trap 1 — headline generalization test): Pooled NPS improves by ~+12
    points Q1→Q2. Base-weighted NPS falls by ~-22 points. A model defaulting to
    pooled NPS will set nps_trend = "improving", which is wrong. This test
    independently verifies the sign of the change and the trend string, so it
    will fail even if nps_change is wrong by less than the abs_tol in test_case_07.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    gt_change = ground_truth["nps_change"]
    assert math.isclose(s["nps_change_h1"], gt_change, abs_tol=0.5), (
        f"nps_change_h1: got {s['nps_change_h1']}, expected ~{gt_change}"
    )
    assert s["nps_trend"] == "declining", (
        f"nps_trend should be 'declining' (base-weighted NPS fell), got '{s['nps_trend']}'"
    )


def test_case_09_top_revenue_risk_theme(ground_truth):
    """
    top_revenue_risk_theme must be the theme with the highest distinct-detractor MRR.

    QC NOTE (trap 2): mobile_app_performance has ~1,800 detractor mentions (SMB-heavy),
    but data_export_reliability has ~$3.4M in distinct-detractor MRR (enterprise-heavy).
    An agent that ranks by complaint count will return mobile_app_performance. The
    correct answer is data_export_reliability. The test checks the theme string exactly.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    assert s["top_revenue_risk_theme"] == ground_truth["top_theme"], (
        f"top_revenue_risk_theme: got '{s['top_revenue_risk_theme']}', "
        f"expected '{ground_truth['top_theme']}'"
    )


def test_case_10_revenue_at_risk_and_response_count(ground_truth):
    """
    top_revenue_at_risk and valid_response_count must match ground truth.
    The two exclusion filters (test accounts, non-NPS channels) are independent
    sets, so valid_response_count is order-independent and is checked exactly.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    gt_at_risk = ground_truth["top_at_risk"]
    assert math.isclose(s["top_revenue_at_risk"], gt_at_risk, rel_tol=0.001), (
        f"top_revenue_at_risk: got {s['top_revenue_at_risk']}, expected ~{gt_at_risk}"
    )
    assert s["valid_response_count"] == ground_truth["valid_response_count"], (
        f"valid_response_count: got {s['valid_response_count']}, "
        f"expected {ground_truth['valid_response_count']}"
    )
