"""
Tests for the H1 2025 NPS program report.

Contract (instruction.md): deliverables are
    /workspace/nps_report.csv  — 18 rows, one per (wave, segment)
    /workspace/summary.json    — 7 scalar keys

Compound traps embedded in the data:

  1. Account-level NPS — primary contacts only (no fallback)
     Invitations go to ALL contacts (primary and secondary). Only the primary
     contact's response represents the account. Including secondary contacts
     inflates enterprise in Q2 (when SMB drops out), amplifying the wrong pooled
     signal. Accounts whose primary contact did not respond are excluded.

  2. Historical segment classification
     accounts.csv shows current_segment. 500 accounts migrated SMB→mid_market on
     2025-04-01. Segment at the time of each invitation comes from segment_history.csv.
     Using current_segment misclassifies 500 accounts in Q1, shifting both per-segment
     NPS and the Jan 1 base weights.

  3. FX forward-fill on business-day gaps
     fx_rates.csv covers Mon–Fri only. ~32% of response dates fall on weekends.
     A direct merge returns NaN for those dates. fillna(1.0) treats EUR/GBP weekend
     responses as USD, undervaluing the top theme by ~12%.

  4. Cross-quarter late responses
     15% of wave 2025-03 invitations sent March 27–31; responses arrive April 1–7.
     Q1 is defined by wave membership (invitation's wave field), not response_date.
     Filtering by response_date < 2025-04-01 silently drops ~220 Q1 responses.

  5. phone_ivr scale mismatch (no channels reference file)
     phone_ivr records 1–5 CSAT, discoverable only from score.describe() by channel.
     Including those responses classifies all of them as detractors on a 0–10 scale.
"""

import json
import math
from pathlib import Path

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

REPORT_PATH  = WORKSPACE_DIR / "nps_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

WAVES    = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
SEGMENTS = ["enterprise", "mid_market", "smb"]
Q1_WAVES = {"2025-01", "2025-02", "2025-03"}
Q2_WAVES = {"2025-04", "2025-05", "2025-06"}


# ---------------------------------------------------------------------------
# Ground-truth fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ground_truth():
    accounts    = pd.read_csv(DATA_DIR / "accounts.csv")
    contacts    = pd.read_csv(DATA_DIR / "contacts.csv")
    seg_history = pd.read_csv(DATA_DIR / "segment_history.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses   = pd.read_csv(DATA_DIR / "survey_responses.csv")
    fx_rates    = pd.read_csv(DATA_DIR / "fx_rates.csv")

    real_accounts = accounts[~accounts["is_test_account"]].copy()
    real_acc_ids  = set(real_accounts["account_id"])

    # Data cleaning (mirrors oracle load_data cleaning steps)
    seg_history["segment"] = (
        seg_history["segment"].str.lower().str.replace("-", "_", regex=False)
    )
    responses["primary_theme"] = responses["primary_theme"].str.strip().replace("", pd.NA)

    # Build segment history lookup
    seg_hist_lookup = {}
    for _, r in seg_history.iterrows():
        aid = r["account_id"]
        seg_hist_lookup.setdefault(aid, []).append(
            (r["valid_from"], r["valid_to"], r["segment"])
        )

    def resolve_seg(account_id, date_str):
        for vf, vt, seg in seg_hist_lookup.get(account_id, []):
            if vf <= date_str and (pd.isna(vt) or vt >= date_str):
                return seg
        return None

    # FX forward-fill (business days only → fill weekends)
    all_dates = pd.date_range("2025-01-01", "2025-06-30", freq="D")
    fx_lookup = {}
    for cur in fx_rates["currency"].unique():
        sub = fx_rates[fx_rates["currency"] == cur].copy()
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub.set_index("date")["usd_rate"].reindex(all_dates).ffill()
        for dt, rate in sub.items():
            fx_lookup[(dt.strftime("%Y-%m-%d"), cur)] = float(rate)

    # Join chain: responses → invitations → contacts, filter primary + NPS channel + real account
    merged = (
        responses
        .merge(invitations[["invitation_id", "contact_id", "wave", "channel", "sent_date"]], on="invitation_id")
        .merge(contacts[["contact_id", "account_id", "is_primary_contact"]], on="contact_id")
    )
    merged = merged[merged["account_id"].isin(real_acc_ids)].copy()

    # Primary contact filter
    primary_only = merged[merged["is_primary_contact"]].copy()

    # NPS channel filter (phone_ivr uses 1-5 CSAT)
    valid = primary_only[primary_only["channel"].isin({"email", "in_app"})].copy()

    # Historical segment per invitation
    valid["hist_segment"] = valid.apply(
        lambda r: resolve_seg(r["account_id"], r["sent_date"]), axis=1
    )

    # Segment weights: Jan 1, 2025 account base
    seg_counts = {}
    for aid in real_acc_ids:
        seg = resolve_seg(aid, "2025-01-01")
        if seg:
            seg_counts[seg] = seg_counts.get(seg, 0) + 1
    total_real = sum(seg_counts.values())
    seg_weights = {s: seg_counts.get(s, 0) / total_real for s in SEGMENTS}

    # Invited counts: all primary contacts per (wave, historical segment)
    primary_contacts = contacts[
        contacts["is_primary_contact"] & contacts["account_id"].isin(real_acc_ids)
    ].copy()
    invited_counts = {}
    for wave in WAVES:
        date_ref = pd.Timestamp(wave + "-01").strftime("%Y-%m-%d")
        for _, pc in primary_contacts.iterrows():
            seg = resolve_seg(pc["account_id"], date_ref)
            if seg:
                invited_counts[(wave, seg)] = invited_counts.get((wave, seg), 0) + 1

    resp_counts  = valid.groupby(["wave", "hist_segment"]).size().to_dict()

    def nps(s):
        s = pd.Series(s)
        n = len(s)
        return 0.0 if n == 0 else float(round(((s >= 9).sum() / n - (s <= 6).sum() / n) * 100, 2))

    wave_seg_nps = {
        (w, seg): nps(grp["score"])
        for (w, seg), grp in valid.groupby(["wave", "hist_segment"])
    }

    def weighted_nps(waves_set):
        p = valid[valid["wave"].isin(waves_set)]
        seg_nps = {s: nps(p[p["hist_segment"] == s]["score"]) for s in SEGMENTS}
        return float(round(sum(seg_nps[s] * seg_weights.get(s, 0) for s in SEGMENTS), 2))

    overall_q1 = weighted_nps(Q1_WAVES)
    overall_q2 = weighted_nps(Q2_WAVES)
    nps_change = float(round(overall_q2 - overall_q1, 2))

    # Revenue at risk with FX forward-fill
    det = valid[(valid["score"] <= 6) & (valid["primary_theme"].notna())].copy()
    det_mrr = det.merge(
        real_accounts[["account_id", "monthly_recurring_revenue", "billing_currency"]],
        on="account_id",
    )
    det_sorted   = det_mrr.sort_values("response_date")
    first_per_at = det_sorted.drop_duplicates(subset=["account_id", "primary_theme"]).copy()
    first_per_at["usd_rate"] = first_per_at.apply(
        lambda r: fx_lookup.get((r["response_date"], r["billing_currency"]), 1.0), axis=1
    )
    first_per_at["usd_mrr"] = first_per_at["monthly_recurring_revenue"] * first_per_at["usd_rate"]
    mrr_at_risk = (
        first_per_at.groupby("primary_theme")["usd_mrr"]
        .sum().sort_values(ascending=False)
    )
    top_theme   = str(mrr_at_risk.index[0])
    top_at_risk = float(round(mrr_at_risk.iloc[0], 2))

    return {
        "seg_weights":          seg_weights,
        "invited_counts":       invited_counts,
        "resp_counts":          resp_counts,
        "wave_seg_nps":         wave_seg_nps,
        "overall_q1":           overall_q1,
        "overall_q2":           overall_q2,
        "nps_change":           nps_change,
        "nps_trend":            "declining" if nps_change < 0 else "improving",
        "top_theme":            top_theme,   # the string we assert equality on
        "top_at_risk":          top_at_risk,
        "valid_count":          int(len(valid)),
        "accounts_row_count":   len(accounts),
        "contacts_row_count":   len(contacts),
        "invitations_row_count": len(invitations),
        "responses_row_count":  len(responses),
        "fx_rates_row_count":   len(fx_rates),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_case_01_anti_cheat_sentinels():
    """Input data must not be modified. Row counts anchor ground truth."""
    accounts    = pd.read_csv(DATA_DIR / "accounts.csv")
    contacts    = pd.read_csv(DATA_DIR / "contacts.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses   = pd.read_csv(DATA_DIR / "survey_responses.csv")
    fx_rates    = pd.read_csv(DATA_DIR / "fx_rates.csv")

    assert len(accounts)    == 5_000,  "accounts.csv must not be modified"
    assert len(contacts)    == 12_000, "contacts.csv must not be modified"
    assert len(invitations) == 71_430, "survey_invitations.csv must not be modified"
    assert len(responses)   == 12_844, "survey_responses.csv must not be modified"
    assert len(fx_rates)    == 387,    "fx_rates.csv must not be modified"

    assert accounts["is_test_account"].sum() >= 35, "Test accounts must be present"

    # FX rates cover business days only (weekdays); no Saturday or Sunday
    fx_rates["dow"] = pd.to_datetime(fx_rates["date"]).dt.dayofweek
    assert (fx_rates["dow"] >= 5).sum() == 0, "fx_rates must contain only weekdays"
    assert set(fx_rates["currency"]) >= {"EUR", "GBP", "USD"}

    # phone_ivr responses exist with 1-5 scores
    inv_ch = invitations[["invitation_id", "channel"]]
    rch = responses.merge(inv_ch, on="invitation_id")
    phone = rch[rch["channel"] == "phone_ivr"]
    assert len(phone) >= 100,          "phone_ivr responses must be present"
    assert phone["score"].max() <= 5,  "phone_ivr scores must not exceed 5"
    assert phone["score"].min() >= 1,  "phone_ivr scores must be at least 1"

    # Late wave-2025-03 responses (April response dates)
    resp_wave = responses.merge(invitations[["invitation_id", "wave"]], on="invitation_id")
    late = resp_wave[(resp_wave["wave"] == "2025-03") & (resp_wave["response_date"] >= "2025-04-01")]
    assert len(late) >= 100, f"Expected >=100 late wave-2025-03 responses, got {len(late)}"

    # segment_history has migrated accounts (two rows for some account_ids)
    seg_hist = pd.read_csv(DATA_DIR / "segment_history.csv")
    multi    = seg_hist.groupby("account_id").size()
    assert (multi >= 2).sum() >= 400, "Expected >=400 accounts with segment migration history"

    # Data quality trick 1: segment_history uses title-case display names, not snake_case
    raw_segs = set(seg_hist["segment"].unique())
    assert "Enterprise" in raw_segs, "segment_history must use title-case segment names"
    assert "enterprise" not in raw_segs, "segment_history must not be pre-normalised"

    # Data quality trick 2: some primary_theme values are whitespace pseudo-nulls (" ")
    ws_themes = (responses["primary_theme"].fillna("NA_SENTINEL") == " ").sum()
    assert ws_themes >= 50, f"Expected >=50 whitespace pseudo-null primary_theme values, got {ws_themes}"


def test_case_02_report_structure():
    """nps_report.csv must exist with exactly the right columns and 18 rows."""
    assert REPORT_PATH.exists(), "nps_report.csv not found in /workspace/"
    report = pd.read_csv(REPORT_PATH)
    expected_cols = ["wave", "segment", "invited", "responses", "response_rate", "segment_nps"]
    assert list(report.columns) == expected_cols, f"Wrong columns: {list(report.columns)}"
    assert len(report) == 18, f"Expected 18 rows, got {len(report)}"
    assert set(report["wave"])    == set(WAVES),    "Unexpected wave values"
    assert set(report["segment"]) == set(SEGMENTS), "Unexpected segment values"


def test_case_03_invited_counts(ground_truth):
    """
    Invited counts per (wave, segment) must reflect historical segment classification.

    QC NOTE (trap 2): Models using current_segment misclassify 500 migrated accounts
    in Q1. In Q1 waves those accounts appear in mid_market (wrong) instead of smb
    (correct), causing invited counts to diverge in at least 6 cells.
    """
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        key    = (row["wave"], row["segment"])
        exp_inv = ground_truth["invited_counts"].get(key, 0)
        assert int(row["invited"]) == exp_inv, (
            f"Invited mismatch at {key}: got {row['invited']}, expected {exp_inv}"
        )


def test_case_04_response_counts(ground_truth):
    """
    Response counts per (wave, segment) must reflect primary-contact-only NPS
    responses on the correct historical segment.

    QC NOTE (compound trap 1+2+5): Three independent errors change these counts:
    including secondary contacts, using current segment, and including phone_ivr.
    """
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        key      = (row["wave"], row["segment"])
        exp_resp = ground_truth["resp_counts"].get(key, 0)
        assert int(row["responses"]) == exp_resp, (
            f"Response mismatch at {key}: got {row['responses']}, expected {exp_resp}"
        )


def test_case_05_response_rates(ground_truth):
    """Response rates must equal responses / invited, rounded to 4 decimal places."""
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        expected = round(row["responses"] / row["invited"], 4) if row["invited"] > 0 else 0.0
        assert math.isclose(row["response_rate"], expected, abs_tol=0.0001), (
            f"response_rate mismatch at ({row['wave']}, {row['segment']}): "
            f"got {row['response_rate']}, expected {expected}"
        )


def test_case_06_segment_nps_values(ground_truth):
    """
    Segment NPS values must match ground truth within ±0.5.

    QC NOTE (compound trap 1+2+5): Including secondary contacts, using wrong
    segments, or including phone_ivr each shift segment NPS by multiple points.
    The ±0.5 tolerance rejects all three systematic biases while accepting minor
    floating-point differences in correct implementations.
    """
    report = pd.read_csv(REPORT_PATH)
    for _, row in report.iterrows():
        key      = (row["wave"], row["segment"])
        expected = ground_truth["wave_seg_nps"].get(key, 0.0)
        assert math.isclose(float(row["segment_nps"]), expected, abs_tol=0.5), (
            f"segment_nps mismatch at {key}: got {row['segment_nps']}, expected {expected}"
        )


def test_case_07_summary_structure():
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
        f"Wrong keys. Extra: {set(s.keys()) - expected_keys}, Missing: {expected_keys - set(s.keys())}"
    )
    assert isinstance(s["overall_nps_q1"],       float)
    assert isinstance(s["overall_nps_q2"],       float)
    assert isinstance(s["nps_change_h1"],        float)
    assert isinstance(s["nps_trend"],            str)
    assert isinstance(s["top_revenue_risk_theme"], str)
    assert isinstance(s["top_revenue_at_risk"],  float)
    assert isinstance(s["valid_response_count"], int)


def test_case_08_overall_nps_values(ground_truth):
    """
    Q1 and Q2 overall NPS must match base-weighted (Jan 1) ground truth within ±0.5.

    QC NOTE (compound trap 1+2): correct Q1 ~ -7.2, Q2 ~ -24.1. A model using
    pooled NPS sees Q1 ~ +13.7, Q2 ~ +31.1 — both ~20+ pts off. A model that
    includes phone_ivr responses gets Q1 ~ -8.7 — outside ±0.5 of the correct -7.2.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    assert math.isclose(s["overall_nps_q1"], ground_truth["overall_q1"], abs_tol=0.5), (
        f"overall_nps_q1: got {s['overall_nps_q1']}, expected ~{ground_truth['overall_q1']}"
    )
    assert math.isclose(s["overall_nps_q2"], ground_truth["overall_q2"], abs_tol=0.5), (
        f"overall_nps_q2: got {s['overall_nps_q2']}, expected ~{ground_truth['overall_q2']}"
    )


def test_case_09_nps_trend(ground_truth):
    """
    nps_change_h1 and nps_trend must reflect the base-weighted declining result.

    QC NOTE (headline test): Pooled NPS improves ~+17 pts Q1→Q2. Base-weighted
    falls ~-17 pts. Any model using pooled NPS or wrong weights reports
    nps_trend = 'improving', which is wrong. The trend string test is independent
    so it catches the wrong sign even when abs_tol on nps_change would pass.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    assert math.isclose(s["nps_change_h1"], ground_truth["nps_change"], abs_tol=0.5), (
        f"nps_change_h1: got {s['nps_change_h1']}, expected ~{ground_truth['nps_change']}"
    )
    assert s["nps_trend"] == "declining", (
        f"nps_trend should be 'declining' (base-weighted NPS fell ~18 pts), got '{s['nps_trend']}'"
    )


def test_case_10_revenue_and_response_count(ground_truth):
    """
    top_revenue_risk_theme (exact string), top_revenue_at_risk (rel_tol=0.001),
    and valid_response_count (exact).

    QC NOTE (compound trap 3 + tricks 1+2):
    (a) Skipping FX forward-fill: EUR/GBP weekend responses get rate=1.0, undervaluing
        enterprise accounts by ~6%; fails rel_tol=0.001.
    (b) Not normalising segment_history case → segments invisible → wrong account grouping.
    (c) Not replacing empty-string primary_theme → '' wins the revenue ranking with ~$1.7M
        (enterprise MRR concentrated there), displacing the real top theme.
    (d) Filtering by response_date instead of wave: late Q1 responses misclassified.
    Errors (b) and (c) each change top_revenue_risk_theme; (a) changes top_revenue_at_risk.
    """
    with open(SUMMARY_PATH) as fh:
        s = json.load(fh)
    assert s["top_revenue_risk_theme"] == ground_truth["top_theme"], (
        f"top_revenue_risk_theme: got '{s['top_revenue_risk_theme']}', "
        f"expected '{ground_truth['top_theme']}'"
    )
    assert math.isclose(s["top_revenue_at_risk"], ground_truth["top_at_risk"], rel_tol=0.001), (
        f"top_revenue_at_risk: got {s['top_revenue_at_risk']}, expected ~{ground_truth['top_at_risk']}"
    )
    assert s["valid_response_count"] == ground_truth["valid_count"], (
        f"valid_response_count: got {s['valid_response_count']}, expected {ground_truth['valid_count']}"
    )
