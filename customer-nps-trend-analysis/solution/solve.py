"""H1 2025 NPS program report for a B2B SaaS company.

What I found in the data
------------------------
The first thing that stood out was the data model: accounts have multiple
contacts, and invitations go to every contact. The instruction is clear that
only the primary contact (is_primary_contact = True) represents the account
for relationship NPS. Secondary contacts respond at lower rates and with
slightly different distributions, so including them would distort segment NPS.

The second thing was segment_history.csv. The accounts table shows
current_segment, but 500 accounts that are currently mid_market spent Q1
as smb. Their wave 2025-01 through 2025-03 invitations were sent while they
were smb, so their Q1 responses belong in the smb segment, not mid_market.
The Jan 1, 2025 account base is also what determines segment weights — not
the current distribution. Using current_segment for Q1 would both misclassify
those responses and use the wrong denominator for the weights.

The response-rate picture explained itself once I looked at channel by wave.
Email in Q1, in_app from April. Low-engagement smb accounts stopped engaging
with the in-app prompt; enterprise primary contacts barely moved. Pooled NPS
rose dramatically from Q1 to Q2, but the base-weighted calculation (anchored
to the 70% smb / 20% mid_market / 10% enterprise Jan 1 split) fell sharply
because the smb segment — which has the worst NPS — lost almost all its
respondents while its weight stayed fixed.

The fx_rates file covers only business days. About a third of response dates
fall on weekends, so a direct date join leaves NaN for those rows. I
forward-filled within each currency so every response date maps to the most
recent available quote, which is what the instruction requires.

The phone_ivr channel was visible in score distributions: grouping scores by
channel showed email and in_app spanning 0–10, while phone_ivr topped out at
5. That is a 1–5 CSAT scale, not the 0–10 NPS question, so those responses
are excluded from all NPS calculations.

Finally, 15% of wave 2025-03 invitations were sent March 27–31. Those
primary contacts responded in early April. Wave membership comes from the
invitation record, not the response date, so those April-dated responses are Q1.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

if Path("/workspace").exists():
    WORKSPACE = Path("/workspace")
else:
    WORKSPACE = Path(__file__).parent.parent

DATA_DIR = (
    WORKSPACE / "data"
    if (WORKSPACE / "data").exists()
    else Path(__file__).parent.parent / "environment" / "data"
)

WAVES    = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
SEGMENTS = ["enterprise", "mid_market", "smb"]
Q1_WAVES = {"2025-01", "2025-02", "2025-03"}
Q2_WAVES = {"2025-04", "2025-05", "2025-06"}


def load_data():
    accounts    = pd.read_csv(DATA_DIR / "accounts.csv")
    contacts    = pd.read_csv(DATA_DIR / "contacts.csv")
    seg_history = pd.read_csv(DATA_DIR / "segment_history.csv")
    invitations = pd.read_csv(DATA_DIR / "survey_invitations.csv")
    responses   = pd.read_csv(DATA_DIR / "survey_responses.csv")
    fx_rates    = pd.read_csv(DATA_DIR / "fx_rates.csv")

    # BOTTLENECK: segment_history is a raw CRM export that uses title-case segment names
    # ("Enterprise", "Mid-Market", "SMB") while accounts.csv uses snake_case lowercase.
    # Normalise before any join or lookup to avoid silent zero-match failures.
    seg_history["segment"] = (
        seg_history["segment"].str.lower().str.replace("-", "_", regex=False)
    )

    # BOTTLENECK: primary_theme has whitespace pseudo-nulls (" ") from a CRM export artifact.
    # .notna() / .dropna() pass single-space strings; strip and replace with NA to exclude
    # them from theme-based revenue calculations.
    responses["primary_theme"] = (
        responses["primary_theme"].str.strip().replace("", pd.NA)
    )

    return accounts, contacts, seg_history, invitations, responses, fx_rates


def build_fx_forward_fill(fx_rates_df):
    """
    Return {(date_str, currency): usd_rate} covering every calendar date.
    fx_rates covers business days only; forward-fill within each currency so
    weekend / holiday response dates map to the most recent available quote.
    """
    all_dates = pd.date_range("2025-01-01", "2025-06-30", freq="D")
    lookup = {}
    for cur in fx_rates_df["currency"].unique():
        sub = fx_rates_df[fx_rates_df["currency"] == cur].copy()
        sub["date"] = pd.to_datetime(sub["date"])
        sub = sub.set_index("date")["usd_rate"].reindex(all_dates).ffill()
        for dt, rate in sub.items():
            lookup[(dt.strftime("%Y-%m-%d"), cur)] = float(rate)
    return lookup


def build_segment_history_lookup(seg_history_df):
    """Pre-index segment history: {account_id: [(valid_from, valid_to, segment)]}"""
    lookup = {}
    for _, r in seg_history_df.iterrows():
        aid = r["account_id"]
        if aid not in lookup:
            lookup[aid] = []
        lookup[aid].append((r["valid_from"], r["valid_to"], r["segment"]))
    return lookup


def resolve_segment(hist_lookup, account_id, date_str):
    for vf, vt, seg in hist_lookup.get(account_id, []):
        if vf <= date_str and (pd.isna(vt) or vt >= date_str):
            return seg
    return None


def build_valid_responses(accounts, contacts, seg_history, invitations, responses):
    """
    Join chain: responses → invitations → contacts → accounts.

    BOTTLENECK: filter to is_primary_contact = True before any NPS calculation.
    Each account's wave NPS comes solely from its primary contact's response.
    Accounts whose primary contact did not respond are excluded — no fallback
    to secondary contacts.

    BOTTLENECK: exclude phone_ivr. Score distribution by channel reveals
    phone_ivr tops out at 5 (1–5 CSAT vs 0–10 NPS). Filter by channel name
    after confirming the score range pattern in EDA.

    BOTTLENECK: wave membership comes from the invitation's wave field via the
    join, not from response_date. Wave 2025-03 invitations sent March 27–31
    produce April response dates that are still classified as Q1.
    """
    real_accounts = accounts[~accounts["is_test_account"]].copy()
    real_acc_ids  = set(real_accounts["account_id"])

    merged = (
        responses
        .merge(
            invitations[["invitation_id", "contact_id", "wave", "channel", "sent_date"]],
            on="invitation_id",
        )
        .merge(
            contacts[["contact_id", "account_id", "is_primary_contact"]],
            on="contact_id",
        )
    )

    merged = merged[merged["account_id"].isin(real_acc_ids)].copy()
    merged = merged[merged["is_primary_contact"]].copy()
    merged = merged[merged["channel"].isin({"email", "in_app"})].copy()

    hist_lookup = build_segment_history_lookup(seg_history)
    merged["hist_segment"] = merged.apply(
        lambda r: resolve_segment(hist_lookup, r["account_id"], r["sent_date"]),
        axis=1,
    )
    return merged


def compute_segment_weights_jan1(accounts, seg_history):
    """
    BOTTLENECK: weights from the Jan 1, 2025 account base, not current.
    500 accounts migrated SMB→mid_market in April; in Jan 1 state they are smb.
    Using current_segment overweights mid_market by ~10 percentage points,
    producing a less-negative overall NPS that still shows the wrong trend.
    """
    real_accounts = accounts[~accounts["is_test_account"]]
    real_acc_ids  = set(real_accounts["account_id"])
    hist_lookup   = build_segment_history_lookup(seg_history)
    counts = {}
    for aid in real_acc_ids:
        seg = resolve_segment(hist_lookup, aid, "2025-01-01")
        if seg:
            counts[seg] = counts.get(seg, 0) + 1
    total = sum(counts.values())
    return {s: counts.get(s, 0) / total for s in SEGMENTS}


def nps_from_scores(scores_series):
    n = len(scores_series)
    if n == 0:
        return 0.0
    p = (scores_series >= 9).sum() / n
    d = (scores_series <= 6).sum() / n
    return float(round((p - d) * 100, 2))


def build_report(accounts, contacts, seg_history, valid_responses):
    real_accounts = accounts[~accounts["is_test_account"]].copy()
    real_acc_ids  = set(real_accounts["account_id"])
    hist_lookup   = build_segment_history_lookup(seg_history)

    primary_contacts = contacts[
        contacts["is_primary_contact"] & contacts["account_id"].isin(real_acc_ids)
    ].copy()

    invited_rows = []
    for wave in WAVES:
        date_ref = pd.Timestamp(wave + "-01").strftime("%Y-%m-%d")
        for _, pc in primary_contacts.iterrows():
            seg = resolve_segment(hist_lookup, pc["account_id"], date_ref)
            if seg:
                invited_rows.append({"wave": wave, "segment": seg})

    invited_counts = (
        pd.DataFrame(invited_rows)
        .groupby(["wave", "segment"])
        .size()
        .reset_index(name="invited")
    )

    resp_counts = (
        valid_responses.groupby(["wave", "hist_segment"])
        .size()
        .reset_index(name="responses")
        .rename(columns={"hist_segment": "segment"})
    )

    nps_by_ws = (
        valid_responses.groupby(["wave", "hist_segment"])["score"]
        .apply(nps_from_scores)
        .reset_index(name="segment_nps")
        .rename(columns={"hist_segment": "segment"})
    )

    report = (
        invited_counts
        .merge(resp_counts, on=["wave", "segment"], how="left")
        .merge(nps_by_ws,  on=["wave", "segment"], how="left")
    )
    report["responses"]     = report["responses"].fillna(0).astype(int)
    report["segment_nps"]   = report["segment_nps"].fillna(0.0).round(2)
    report["response_rate"] = (report["responses"] / report["invited"]).round(4)

    report["wave"]    = pd.Categorical(report["wave"],    categories=WAVES,    ordered=True)
    report["segment"] = pd.Categorical(report["segment"], categories=SEGMENTS, ordered=True)
    report = report.sort_values(["wave", "segment"]).reset_index(drop=True)
    report["wave"]    = report["wave"].astype(str)
    report["segment"] = report["segment"].astype(str)
    return report[["wave", "segment", "invited", "responses", "response_rate", "segment_nps"]]


def compute_weighted_nps(valid_responses, seg_weights, waves_set):
    period = valid_responses[valid_responses["wave"].isin(waves_set)]
    seg_nps = {
        s: nps_from_scores(period[period["hist_segment"] == s]["score"])
        for s in SEGMENTS
    }
    return float(round(sum(seg_nps[s] * seg_weights.get(s, 0.0) for s in SEGMENTS), 2))


def compute_revenue_at_risk(valid_responses, accounts, fx_lookup):
    """
    BOTTLENECK: FX lookup uses forward-filled rates. Direct join on response_date
    returns NaN for weekends (fx_rates is weekday-only). Forward-fill within
    currency means every date gets the last available business-day quote.

    BOTTLENECK: deduplicate on (account_id, primary_theme) keeping the earliest
    qualifying response per account-theme pair. That response date drives the
    FX rate for conversion. Enterprise accounts billing in EUR/GBP carry an
    8–27% premium; skipping conversion undervalues the top theme by ~12%.
    """
    real_accounts = accounts[~accounts["is_test_account"]]

    detractors = valid_responses[
        (valid_responses["score"] <= 6) & (valid_responses["primary_theme"].notna())
    ].copy()

    det_mrr = detractors.merge(
        real_accounts[["account_id", "monthly_recurring_revenue", "billing_currency"]],
        on="account_id",
    )

    det_sorted   = det_mrr.sort_values("response_date")
    first_per_at = det_sorted.drop_duplicates(subset=["account_id", "primary_theme"]).copy()

    first_per_at["usd_rate"] = first_per_at.apply(
        lambda r: fx_lookup.get((r["response_date"], r["billing_currency"]), 1.0),
        axis=1,
    )
    first_per_at["usd_mrr"] = (
        first_per_at["monthly_recurring_revenue"] * first_per_at["usd_rate"]
    )

    mrr_at_risk = (
        first_per_at.groupby("primary_theme")["usd_mrr"]
        .sum()
        .sort_values(ascending=False)
    )
    return str(mrr_at_risk.index[0]), float(round(mrr_at_risk.iloc[0], 2))


def main():
    accounts, contacts, seg_history, invitations, responses, fx_rates = load_data()

    fx_lookup   = build_fx_forward_fill(fx_rates)
    valid       = build_valid_responses(accounts, contacts, seg_history, invitations, responses)
    seg_weights = compute_segment_weights_jan1(accounts, seg_history)

    report = build_report(accounts, contacts, seg_history, valid)
    report.to_csv(WORKSPACE / "nps_report.csv", index=False)

    overall_q1 = compute_weighted_nps(valid, seg_weights, Q1_WAVES)
    overall_q2 = compute_weighted_nps(valid, seg_weights, Q2_WAVES)
    nps_change = float(round(overall_q2 - overall_q1, 2))
    nps_trend  = "improving" if nps_change > 0 else "declining"

    top_theme, top_at_risk = compute_revenue_at_risk(valid, accounts, fx_lookup)

    summary = {
        "overall_nps_q1":         overall_q1,
        "overall_nps_q2":         overall_q2,
        "nps_change_h1":          nps_change,
        "nps_trend":              nps_trend,
        "top_revenue_risk_theme": top_theme,
        "top_revenue_at_risk":    top_at_risk,
        "valid_response_count":   int(len(valid)),
    }

    with open(WORKSPACE / "summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)

    print(f"nps_report.csv: {len(report)} rows")
    print(f"summary.json:   {summary}")


if __name__ == "__main__":
    main()
