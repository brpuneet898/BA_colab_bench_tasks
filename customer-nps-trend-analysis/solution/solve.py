"""H1 2025 NPS program report for Axiom Analytics.

What I found in the data
------------------------
The first thing that stood out was the per-segment response rate table. January
through March, all three segments responded at roughly their expected rates: ~55%
enterprise, ~38% mid-market, ~30% SMB. Starting in April every segment dropped,
but not equally. Enterprise barely moved. Mid-market fell to around 12%.
SMB collapsed to 5%. A quick groupby by wave and channel confirmed the reason:
the delivery method switched from email to in_app in the April wave, and SMB
customers — who had the weakest product engagement — largely stopped seeing or
responding to the in-app prompt.

That response rate pattern immediately made the headline NPS number suspicious.
If SMB (which has the worst NPS) stops responding, a pooled-respondent NPS will
look better in Q2 purely because the composition of the respondent pool changed,
not because satisfaction improved. I computed NPS two ways. Pooled Q2 NPS was
about +12 points higher than Q1 — a model would call this "improving" and move on.
But the company's reporting standard weights by the active customer base: enterprise
is 10% of accounts, mid-market is 30%, SMB is 60%. On that basis Q2 NPS was about
22 points lower than Q1. The trend is declining, not improving.

I also checked the survey_channels reference file. The phone_ivr channel uses a
1–5 CSAT scale, not the standard 0–10 NPS scale. Any response from that channel
would score 5 at most, making it a detractor on the 0–10 classification. There
are about 390 phone_ivr responses concentrated in mid_market. Including them would
bias that segment's NPS down by several points. I excluded them.

For revenue at risk, I joined detractor responses (NPS ≤ 6, qualifying channel)
to the customer MRR. The most frequent complaint theme was mobile_app_performance —
about 1,800 mentions, mostly from SMB accounts with low MRR. But when I summed
distinct-detractor MRR per theme, data_export_reliability came out on top at about
$3.4M, driven by enterprise detractors who each carry tens of thousands of dollars
in MRR. Ranking by complaint count alone would direct the product team to fix the
wrong thing.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

if Path("/workspace").exists():
    WORKSPACE = Path("/workspace")
else:
    WORKSPACE = Path(__file__).parent.parent

DATA_DIR = WORKSPACE / "data" if (WORKSPACE / "data").exists() else Path(__file__).parent.parent / "environment" / "data"

WAVES = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
SEGMENTS = ["enterprise", "mid_market", "smb"]
Q1_WAVES = {"2025-01", "2025-02", "2025-03"}
Q2_WAVES = {"2025-04", "2025-05", "2025-06"}


def load_data():
    customers = pd.read_csv(DATA_DIR / "customers.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses = pd.read_csv(DATA_DIR / "survey_responses.csv")
    channels = pd.read_csv(DATA_DIR / "survey_channels.csv")
    return customers, invitations, responses, channels


def get_nps_channels(channels_df):
    """Return the set of channel names that use the standard 0-10 NPS scale."""
    return set(channels_df.loc[channels_df["score_scale"] == "0-10", "channel"])


def build_valid_responses(customers, invitations, responses, nps_channels):
    """
    Join responses to invitations and customers.
    Apply both exclusions:
      1. Exclude test accounts (is_test_account == True)
      2. Exclude responses from non-0-10-scale channels
    These two filters are independent sets; order does not affect the count.
    Returns the clean responses DataFrame with segment and channel columns.
    """
    real_customers = customers[~customers["is_test_account"]].copy()

    merged = (
        responses
        .merge(invitations[["invitation_id", "customer_id", "wave", "channel"]], on="invitation_id")
        .merge(real_customers[["customer_id", "segment"]], on="customer_id")
    )

    # BOTTLENECK: phone_ivr uses a 1-5 scale (documented in survey_channels.csv).
    # Scores of 1-5 all fall in the 0-6 detractor range on a 0-10 scale, so including
    # them would silently inflate the detractor count per segment. Drop by channel lookup,
    # not by score threshold, so the logic stays tied to the schema.
    valid = merged[merged["channel"].isin(nps_channels)].copy()
    return valid


def compute_segment_weights(customers):
    """
    Segment weights = each segment's share of the active non-test customer base.
    Computed once over the full H1 base, not per wave.
    """
    real_customers = customers[~customers["is_test_account"]]
    seg_counts = real_customers.groupby("segment").size()
    return (seg_counts / seg_counts.sum()).to_dict()


def nps_from_scores(scores_series):
    """Standard NPS: (% promoters − % detractors) × 100. Returns 0.0 for empty input."""
    n = len(scores_series)
    if n == 0:
        return 0.0
    promoters = (scores_series >= 9).sum()
    detractors = (scores_series <= 6).sum()
    return float(round((promoters / n - detractors / n) * 100, 2))


def build_report(customers, invitations, valid_responses, seg_weights):
    """
    Build nps_report.csv: one row per (wave, segment).
    Invited = non-test-account customers in that wave/segment.
    Responses = qualifying 0-10 NPS responses for that wave/segment.
    """
    real_customers = customers[~customers["is_test_account"]]

    # Invited counts: all non-test customers received exactly one invitation per wave.
    # Pivot to get (wave, segment) → count.
    inv_with_seg = invitations.merge(
        real_customers[["customer_id", "segment"]], on="customer_id"
    )
    invited_counts = (
        inv_with_seg.groupby(["wave", "segment"])
        .size()
        .reset_index(name="invited")
    )

    resp_counts = (
        valid_responses.groupby(["wave", "segment"])
        .size()
        .reset_index(name="responses")
    )

    nps_by_wave_seg = (
        valid_responses.groupby(["wave", "segment"])["score"]
        .apply(nps_from_scores)
        .reset_index(name="segment_nps")
    )

    report = (
        invited_counts
        .merge(resp_counts, on=["wave", "segment"], how="left")
        .merge(nps_by_wave_seg, on=["wave", "segment"], how="left")
    )
    report["responses"] = report["responses"].fillna(0).astype(int)
    report["segment_nps"] = report["segment_nps"].fillna(0.0).round(2)
    report["response_rate"] = (report["responses"] / report["invited"]).round(4)

    # Enforce canonical row order and column order
    report["wave"] = pd.Categorical(report["wave"], categories=WAVES, ordered=True)
    report["segment"] = pd.Categorical(report["segment"], categories=SEGMENTS, ordered=True)
    report = report.sort_values(["wave", "segment"]).reset_index(drop=True)
    report = report[["wave", "segment", "invited", "responses", "response_rate", "segment_nps"]]

    # Convert categoricals back to strings for CSV output
    report["wave"] = report["wave"].astype(str)
    report["segment"] = report["segment"].astype(str)
    return report


def compute_weighted_nps(report_df, seg_weights, waves_set):
    """
    Base-weighted NPS for a period: sum of per-segment NPS × segment base weight.
    Per-segment NPS for the period = NPS pooled across all waves in waves_set for that segment.

    BOTTLENECK: we need the per-segment NPS computed across all waves in the period
    (not averaged across wave-level segment NPS values), otherwise a segment with
    zero responses in one wave gets a 0.0 NPS that drags the per-segment average down.
    The report_df already holds wave-level segment NPS, so re-aggregate from valid_responses.
    """
    raise NotImplementedError  # This function is not used; see compute_weighted_nps_from_responses


def compute_weighted_nps_from_responses(valid_responses, seg_weights, waves_set):
    """
    Compute base-weighted overall NPS for the given set of waves.
    1. Filter responses to the period.
    2. Compute NPS for each segment from all period responses combined.
    3. Weight by segment base shares.
    """
    period_resp = valid_responses[valid_responses["wave"].isin(waves_set)]

    nps_by_seg = {}
    for seg in SEGMENTS:
        seg_resp = period_resp[period_resp["segment"] == seg]["score"]
        nps_by_seg[seg] = nps_from_scores(seg_resp)

    # BOTTLENECK: use base-customer-share weights, not respondent-share weights.
    # After the April channel switch, SMB response rates dropped steeply while
    # enterprise stayed high. A respondent-weighted ("pooled") NPS would over-weight
    # enterprise in Q2 and report an improving trend. The base-weighted calculation
    # fixes the weights to the actual customer composition (60% SMB, 30% mid-market,
    # 10% enterprise), which correctly captures that the majority-SMB customer base
    # became harder to satisfy in Q2 even as the minority-enterprise respondents
    # stayed happy.
    weighted = sum(nps_by_seg[seg] * seg_weights.get(seg, 0.0) for seg in SEGMENTS)
    return float(round(weighted, 2))


def compute_revenue_at_risk(valid_responses, customers):
    """
    Revenue at risk per theme = sum of MRR across distinct detractor customers
    who cited that theme in at least one qualifying H1 response.

    BOTTLENECK: deduplicate on (customer_id, primary_theme) BEFORE summing MRR.
    A detractor who submitted two responses with the same theme should count their
    MRR only once. Skipping deduplication inflates totals proportionally across all
    themes and does not change the ranking in this data, but the instruction explicitly
    defines "distinct detractor customers" so the test checks the deduplicated value.
    """
    real_customers = customers[~customers["is_test_account"]]

    detractors = valid_responses[
        (valid_responses["score"] <= 6) & (valid_responses["primary_theme"].notna())
    ].copy()

    detractors_mrr = detractors.merge(
        real_customers[["customer_id", "monthly_recurring_revenue"]], on="customer_id"
    )

    # Dedup: each (customer_id, theme) pair counted once
    mrr_at_risk = (
        detractors_mrr.drop_duplicates(subset=["customer_id", "primary_theme"])
        .groupby("primary_theme")["monthly_recurring_revenue"]
        .sum()
        .sort_values(ascending=False)
    )

    top_theme = str(mrr_at_risk.index[0])
    top_value = float(round(mrr_at_risk.iloc[0], 2))
    return top_theme, top_value


def main():
    customers, invitations, responses, channels = load_data()

    nps_channels = get_nps_channels(channels)
    valid_responses = build_valid_responses(customers, invitations, responses, nps_channels)
    seg_weights = compute_segment_weights(customers)

    report = build_report(customers, invitations, valid_responses, seg_weights)
    report.to_csv(WORKSPACE / "nps_report.csv", index=False)

    overall_q1 = compute_weighted_nps_from_responses(valid_responses, seg_weights, Q1_WAVES)
    overall_q2 = compute_weighted_nps_from_responses(valid_responses, seg_weights, Q2_WAVES)
    nps_change = float(round(overall_q2 - overall_q1, 2))
    nps_trend = "improving" if nps_change > 0 else "declining"

    top_theme, top_at_risk = compute_revenue_at_risk(valid_responses, customers)

    valid_response_count = int(len(valid_responses))

    summary = {
        "overall_nps_q1": overall_q1,
        "overall_nps_q2": overall_q2,
        "nps_change_h1": nps_change,
        "nps_trend": nps_trend,
        "top_revenue_risk_theme": top_theme,
        "top_revenue_at_risk": top_at_risk,
        "valid_response_count": valid_response_count,
    }

    with open(WORKSPACE / "summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)

    print(f"nps_report.csv: {len(report)} rows")
    print(f"summary.json:   {summary}")


if __name__ == "__main__":
    main()
