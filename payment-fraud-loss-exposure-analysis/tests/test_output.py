"""
Tests for the Q1 2024 Fraud Loss Exposure Report task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — Case-to-transaction indirection.
            fraud_disputes.csv records only the transaction reported when a
            case was opened. case_transactions.csv is the authoritative set
            of transactions confirmed as part of each case — 33 fraud-reason
            cases are linked to 2-4 transactions rather than just the
            reported one. A model that treats the reported transaction as
            the complete case undercounts confirmed fraud loss for every
            multi-transaction case.

  Trap 2 — Resolution history / stale liability.
            dispute_resolutions.csv can carry more than one row per case_id
            (25 cases do, as representment cycles play out); the earlier
            record is written first. Only the most recently recorded
            resolution is authoritative. A naive keep-first deduplication
            locks in a stale (sometimes opposite) liability outcome.

  Trap 3 — Gateway-scoped transaction_id collision (composite key).
            ALPHA and BRAVO share a 180-value transaction_id pool
            double-assigned during a 2023 platform migration. Joining
            case_transactions.csv to transactions.csv on transaction_id
            alone (ignoring gateway_id) cross-matches rows across gateways.
            Several confirmed multi-transaction cases have an extra linked
            transaction drawn from this pool.

  Trap 4 — NaT risk tier effective dating.
            15 of 50 merchants carry an open-ended current risk tier
            assignment (null tier_effective_to). A pandas comparison
            transaction_date <= tier_effective_to evaluates False against a
            null, silently dropping those merchants' transactions from the
            tier-grouped totals.

  Trap 5 — Velocity-fraud clustering (payment_instrument_id).
            Independent of the dispute-case system: a payment instrument
            used at 3+ distinct merchants within any 48-hour window is a
            velocity-fraud cluster: 12 true clusters exist. 15 decoy
            instruments are also used at 3+ distinct merchants, but spread
            across weeks — ordinary repeat customers, not fraud. A model
            that checks distinct-merchant count without any time window
            wrongly flags the decoys (overcount).

  Trap 6 — Boundary-spanning velocity clusters (pipeline-sequencing).
            transactions.csv is not strictly Q1-bounded (Dec 2023 / Apr 2024
            buffer). 8 instruments have a qualifying 48h window that
            straddles the Q1 boundary — fewer than 3 distinct merchants in
            the Q1-only slice, 3+ once buffer-period transactions are
            included. A pipeline that scopes to Q1 before clustering misses
            these regardless of EDA thoroughness — it's a pipeline-order
            error, not an anomaly-discovery one.

  Trap 7 — writeoff_status false friend.
            dispute_resolutions.csv carries writeoff_status on every "lost"
            row — a GL/accounting-cycle fact (write-off posting batches lag
            actual case resolution) that is never part of the confirmed-
            fraud-loss definition (reason_code + resolution_status only). A
            model that discovers this column and infers "not posted yet
            means not final" excludes the ~30% of confirmed cases still
            pending write-off — a natural administrative-workflow instinct
            that happens to be wrong here.
"""

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
if not WORKSPACE_DIR.exists():
    WORKSPACE_DIR = Path(__file__).parent.parent

_env_data = os.environ.get("DATA_DIR")
if _env_data:
    DATA_DIR = Path(_env_data)
elif (WORKSPACE_DIR / "data").exists():
    DATA_DIR = WORKSPACE_DIR / "data"
else:
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

REPORT_PATH = WORKSPACE_DIR / "fraud_loss_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END = pd.Timestamp("2024-03-31")
Q1_END_EXCLUSIVE = pd.Timestamp("2024-04-01")  # transaction_date carries time-of-day; use a half-open upper bound
FRAUD_REASONS = {"fraud_card_not_present", "fraud_account_takeover", "fraud_lost_stolen_card"}

REQUIRED_COLUMNS = ["risk_tier", "month", "total_transaction_volume_usd", "transaction_count",
                    "confirmed_fraud_loss_usd", "confirmed_fraud_count", "fraud_loss_rate_bps"]

# ---------------------------------------------------------------------------
# Ground-truth helpers (replicate oracle logic independently)
# ---------------------------------------------------------------------------


def _assign_risk_tier(transactions, tiers):
    merged = transactions.merge(tiers, on="merchant_id", how="left")
    open_ended = merged["tier_effective_to"].isna()
    in_effect = (
        (merged["tier_effective_from"] <= merged["transaction_date"]) &
        (open_ended | (merged["transaction_date"] <= merged["tier_effective_to"]))
    )
    matched = merged[in_effect].copy()
    matched["month"] = matched["transaction_date"].dt.strftime("%Y-%m")
    return matched


def _confirmed_fraud_cases(disputes, resolutions):
    latest = resolutions.sort_values("resolution_date").groupby("case_id", as_index=False).last()
    joined = disputes.merge(latest, on="case_id", how="left")
    is_fraud = joined["reason_code"].isin(FRAUD_REASONS)
    is_liable = joined["resolution_status"] == "lost"
    return joined.loc[is_fraud & is_liable, "case_id"]


def _flag_velocity_fraud(all_transactions):
    """
    Trap 5/6 — same two-pointer sliding-window logic as solve.py. Must run on
    ALL transactions, not just the Q1-scoped subset — the file is not
    strictly Q1-bounded, and some instruments' qualifying 48h window
    straddles the Q1 boundary.
    """
    flagged = []
    for _, grp in all_transactions.groupby("payment_instrument_id"):
        if len(grp) < 3:
            continue
        grp = grp.sort_values("transaction_date")
        times = grp["transaction_date"].to_numpy()
        merchants = grp["merchant_id"].to_numpy()
        keys = list(zip(grp["gateway_id"], grp["transaction_id"]))
        window = np.timedelta64(48, "h")
        left = 0
        for right in range(len(grp)):
            while times[right] - times[left] > window:
                left += 1
            if len(set(merchants[left:right + 1])) >= 3:
                flagged.extend(keys[left:right + 1])
    return set(flagged)


def _build_expected(merchants, tiers, transactions, disputes, resolutions, case_transactions):
    # Velocity clustering runs on the FULL file first (Trap 6) — only after
    # flagging do we scope to Q1 for the report.
    velocity_keys = _flag_velocity_fraud(transactions)

    q1_txns = transactions[
        (transactions["transaction_date"] >= Q1_START) & (transactions["transaction_date"] < Q1_END_EXCLUSIVE)
    ].copy()
    txns_with_tier = _assign_risk_tier(q1_txns, tiers)

    confirmed = _confirmed_fraud_cases(disputes, resolutions)
    confirmed_links = case_transactions[case_transactions["case_id"].isin(confirmed)]
    case_confirmed_txns = confirmed_links.merge(
        txns_with_tier, on=["gateway_id", "transaction_id"], how="inner"
    )

    key_cols = ["gateway_id", "transaction_id"]
    velocity_txns = txns_with_tier[txns_with_tier[key_cols].apply(tuple, axis=1).isin(velocity_keys)]

    confirmed_txns = pd.concat([case_confirmed_txns, velocity_txns]).drop_duplicates(subset=key_cols)

    volume = txns_with_tier.groupby(["risk_tier", "month"]).agg(
        total_transaction_volume_usd=("amount_usd", "sum"),
        transaction_count=("amount_usd", "size"),
    ).reset_index()
    fraud = confirmed_txns.groupby(["risk_tier", "month"]).agg(
        confirmed_fraud_loss_usd=("amount_usd", "sum"),
        confirmed_fraud_count=("amount_usd", "size"),
    ).reset_index()

    report = volume.merge(fraud, on=["risk_tier", "month"], how="left")
    report["confirmed_fraud_loss_usd"] = report["confirmed_fraud_loss_usd"].fillna(0.0)
    report["confirmed_fraud_count"] = report["confirmed_fraud_count"].fillna(0).astype(int)
    report["fraud_loss_rate_bps"] = (
        report["confirmed_fraud_loss_usd"] / report["total_transaction_volume_usd"] * 10000
    )
    report = report.sort_values(["risk_tier", "month"]).reset_index(drop=True)

    return report, txns_with_tier, confirmed_txns, confirmed, velocity_keys


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_data():
    merchants = pd.read_csv(DATA_DIR / "merchants.csv")
    tiers = pd.read_csv(DATA_DIR / "merchant_risk_tiers.csv",
                        parse_dates=["tier_effective_from", "tier_effective_to"])
    transactions = pd.read_csv(DATA_DIR / "transactions.csv", parse_dates=["transaction_date"])
    disputes = pd.read_csv(DATA_DIR / "fraud_disputes.csv", parse_dates=["filed_date"])
    resolutions = pd.read_csv(DATA_DIR / "dispute_resolutions.csv", parse_dates=["resolution_date"])
    case_transactions = pd.read_csv(DATA_DIR / "case_transactions.csv")
    return merchants, tiers, transactions, disputes, resolutions, case_transactions


@pytest.fixture(scope="module")
def expected(raw_data):
    return _build_expected(*raw_data)


@pytest.fixture(scope="module")
def agent_report():
    return pd.read_csv(REPORT_PATH) if REPORT_PATH.exists() else None


@pytest.fixture(scope="module")
def agent_summary():
    if not SUMMARY_PATH.exists():
        return None
    with open(SUMMARY_PATH) as f:
        return json.load(f)


def _cell(df, tier, month, col):
    row = df[(df["risk_tier"] == tier) & (df["month"] == month)]
    if row.empty:
        return None
    return row[col].iloc[0]


# ---------------------------------------------------------------------------
# Test 01 — Input sentinels
# ---------------------------------------------------------------------------

def test_case_01_input_sentinels(raw_data):
    """Canonical data fingerprints: row counts and trap-specific structural counts."""
    merchants, tiers, transactions, disputes, resolutions, case_transactions = raw_data

    assert len(merchants) == 50, f"merchants.csv must not be modified (expected 50, got {len(merchants)})"

    null_tier_to = tiers["tier_effective_to"].isna().sum()
    assert null_tier_to == 15, \
        f"merchant_risk_tiers.csv must have exactly 15 open-ended (null tier_effective_to) rows, got {null_tier_to}"

    assert len(disputes) == 220, f"fraud_disputes.csv must not be modified (expected 220, got {len(disputes)})"
    fraud_reason_count = disputes["reason_code"].isin(FRAUD_REASONS).sum()
    assert fraud_reason_count == 99, \
        f"fraud_disputes.csv must have exactly 99 fraud-prefixed-reason cases, got {fraud_reason_count}"

    dual_res_count = (resolutions["case_id"].value_counts() > 1).sum()
    assert dual_res_count == 25, \
        f"dispute_resolutions.csv must have exactly 25 cases with more than one resolution record, got {dual_res_count}"

    multi_txn_count = (case_transactions["case_id"].value_counts() > 1).sum()
    assert multi_txn_count == 33, \
        f"case_transactions.csv must have exactly 33 cases linked to more than one transaction, got {multi_txn_count}"

    collision = transactions.groupby("transaction_id")["gateway_id"].nunique()
    colliding_ids = (collision > 1).sum()
    assert colliding_ids == 180, \
        f"transactions.csv must have exactly 180 transaction_id values shared across gateways, got {colliding_ids}"

    assert "payment_instrument_id" in transactions.columns, \
        "transactions.csv must have a payment_instrument_id column"
    inst_stats = transactions.groupby("payment_instrument_id").agg(
        n_merchants=("merchant_id", "nunique"),
        span_hours=("transaction_date", lambda s: (s.max() - s.min()).total_seconds() / 3600),
    )
    multi_merchant = inst_stats[inst_stats["n_merchants"] >= 3]
    true_clusters = (multi_merchant["span_hours"] < 48).sum()
    decoys = (multi_merchant["span_hours"] >= 48).sum()
    assert true_clusters == 20, \
        f"transactions.csv must have exactly 20 payment_instrument_id values with 3+ merchants within 48h, got {true_clusters}"
    assert decoys == 15, \
        f"transactions.csv must have exactly 15 payment_instrument_id values with 3+ merchants spread beyond 48h, got {decoys}"

    assert transactions["transaction_date"].min() < Q1_START, \
        "transactions.csv must include pre-Q1 buffer activity (file is not strictly Q1-bounded)"
    assert transactions["transaction_date"].max() >= Q1_END_EXCLUSIVE, \
        "transactions.csv must include post-Q1 buffer activity (file is not strictly Q1-bounded)"

    assert "writeoff_status" in resolutions.columns, \
        "dispute_resolutions.csv must have a writeoff_status column"
    lost_writeoffs = resolutions.loc[resolutions["resolution_status"] == "lost", "writeoff_status"]
    pending_writeoffs = (lost_writeoffs == "pending").sum()
    assert pending_writeoffs == 36, \
        f"dispute_resolutions.csv must have exactly 36 'lost' rows with writeoff_status == 'pending', got {pending_writeoffs}"


# ---------------------------------------------------------------------------
# Test 02 — Output structure
# ---------------------------------------------------------------------------

def test_case_02_output_structure(agent_report, agent_summary, expected):
    """Both output files exist and have the required shape and columns."""
    assert REPORT_PATH.exists(), "fraud_loss_report.csv not found in /workspace"
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace"
    assert agent_report is not None

    for col in REQUIRED_COLUMNS:
        assert col in agent_report.columns, f"Missing column in fraud_loss_report.csv: {col}"

    exp_report, _, _, _, _ = expected
    assert len(agent_report) == len(exp_report), \
        f"fraud_loss_report.csv must have {len(exp_report)} rows (one per risk_tier/month with Q1 activity), got {len(agent_report)}"

    required_keys = ["total_transaction_volume_usd", "total_confirmed_fraud_loss_usd",
                     "total_confirmed_fraud_count", "overall_fraud_loss_rate_bps", "highest_loss_rate_tier"]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — Case-to-transaction expansion (Trap 1)
# ---------------------------------------------------------------------------

def test_case_03_case_transaction_expansion(agent_report, expected, raw_data):
    """
    Trap 1 — Cases confirmed as fraud may cover more than one transaction via
    case_transactions.csv. A model that only counts the transaction reported
    in fraud_disputes.csv undercounts confirmed_fraud_loss_usd for tier/month
    cells touched by any of the extra (non-reported) transactions.

    Spot-checks the tier/month cells containing at least one such extra
    transaction.
    """
    _, _, _, disputes, resolutions, case_transactions = raw_data
    exp_report, txns_with_tier, confirmed_txns, confirmed, _ = expected

    reported = disputes.set_index("case_id")[["reported_gateway_id", "reported_transaction_id"]]
    confirmed_links = case_transactions[case_transactions["case_id"].isin(confirmed)]
    is_extra = confirmed_links.apply(
        lambda r: (r["gateway_id"], r["transaction_id"]) !=
                  (reported.loc[r["case_id"], "reported_gateway_id"], reported.loc[r["case_id"], "reported_transaction_id"]),
        axis=1,
    )
    extra_links = confirmed_links[is_extra].merge(txns_with_tier, on=["gateway_id", "transaction_id"], how="inner")
    cells = extra_links[["risk_tier", "month"]].drop_duplicates()
    assert len(cells) > 0, "test setup error: expected at least one tier/month cell touched by a multi-transaction case"

    failures = []
    for _, c in cells.head(5).iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "A confirmed case's loss must include every transaction in case_transactions.csv, "
                "not just the one reported in fraud_disputes.csv."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — Resolution history / stale liability (Trap 2)
# ---------------------------------------------------------------------------

def test_case_04_resolution_history(agent_report, expected, raw_data):
    """
    Trap 2 — 25 cases carry more than one dispute_resolutions.csv record; the
    preliminary outcome is written first. Only the most recently recorded
    resolution is authoritative. A naive keep-first deduplication locks in
    the wrong liability outcome for these cases, shifting confirmed_fraud_loss_usd
    in the tier/month cells their transactions belong to.

    Spot-checks tier/month cells containing a transaction tied to a
    dual-resolution case.
    """
    _, _, _, disputes, resolutions, case_transactions = raw_data
    exp_report, txns_with_tier, _, _, _ = expected

    dual_cases = resolutions["case_id"].value_counts().loc[lambda s: s > 1].index
    dual_links = case_transactions[case_transactions["case_id"].isin(dual_cases)]
    dual_txns = dual_links.merge(txns_with_tier, on=["gateway_id", "transaction_id"], how="inner")
    cells = dual_txns[["risk_tier", "month"]].drop_duplicates()
    assert len(cells) > 0, "test setup error: expected at least one tier/month cell touched by a dual-resolution case"

    failures = []
    for _, c in cells.head(5).iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "Use the most recently recorded resolution per case_id, not the first row encountered."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — Composite key join (Trap 3)
# ---------------------------------------------------------------------------

def test_case_05_composite_key_join(agent_report, expected, raw_data):
    """
    Trap 3 — ALPHA and BRAVO share 180 transaction_id values double-assigned
    during a 2023 migration. Joining case_transactions.csv to transactions.csv
    on transaction_id alone (ignoring gateway_id) attaches the wrong
    merchant's transaction to a case for any row in this pool.

    Spot-checks tier/month cells containing a confirmed case's transaction
    that falls in the colliding id pool.
    """
    _, _, transactions, disputes, resolutions, case_transactions = raw_data
    exp_report, txns_with_tier, confirmed_txns, confirmed, _ = expected

    collision = transactions.groupby("transaction_id")["gateway_id"].nunique()
    colliding_ids = set(collision[collision > 1].index)

    confirmed_links = case_transactions[case_transactions["case_id"].isin(confirmed)]
    in_pool = confirmed_links[confirmed_links["transaction_id"].isin(colliding_ids)]
    pool_txns = in_pool.merge(txns_with_tier, on=["gateway_id", "transaction_id"], how="inner")
    cells = pool_txns[["risk_tier", "month"]].drop_duplicates()
    assert len(cells) > 0, "test setup error: expected at least one confirmed case transaction in the collision pool"

    failures = []
    for _, c in cells.head(5).iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "Join case_transactions.csv to transactions.csv on (gateway_id, transaction_id) — "
                "transaction_id alone is not unique across gateways."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — NaT risk tier effective dating (Trap 4)
# ---------------------------------------------------------------------------

def test_case_06_nat_risk_tier(agent_report, expected, raw_data):
    """
    Trap 4 — 15 of 50 merchants have an open-ended current risk tier
    assignment (null tier_effective_to). A naive `date <= tier_effective_to`
    comparison drops those merchants' transactions from the tier-grouped
    totals entirely.

    Spot-checks total_transaction_volume_usd and transaction_count for the
    tier/month cells where open-ended merchants contribute the largest share
    of volume.
    """
    merchants, tiers, transactions, _, _, _ = raw_data
    exp_report, txns_with_tier, _, _, _ = expected

    open_ended_merchants = set(tiers[tiers["tier_effective_to"].isna()]["merchant_id"])
    assert len(open_ended_merchants) == 15

    oe_txns = txns_with_tier[txns_with_tier["merchant_id"].isin(open_ended_merchants)]
    oe_volume_by_cell = oe_txns.groupby(["risk_tier", "month"])["amount_usd"].sum().sort_values(ascending=False)
    cells = oe_volume_by_cell.index[:5]

    failures = []
    for tier, month in cells:
        exp_vol = _cell(exp_report, tier, month, "total_transaction_volume_usd")
        act_vol = _cell(agent_report, tier, month, "total_transaction_volume_usd")
        exp_cnt = _cell(exp_report, tier, month, "transaction_count")
        act_cnt = _cell(agent_report, tier, month, "transaction_count")
        if act_vol is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_vol), float(exp_vol), rel_tol=0.01):
            failures.append(
                f"{tier}/{month}: total_transaction_volume_usd {act_vol:,.2f} != expected {exp_vol:,.2f}. "
                "A null tier_effective_to means the assignment is still in effect (open-ended), "
                "not that the merchant should be excluded."
            )
        if int(act_cnt) != int(exp_cnt):
            failures.append(f"{tier}/{month}: transaction_count {int(act_cnt)} != expected {int(exp_cnt)}")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 07 — Full row-level accuracy (all traps)
# ---------------------------------------------------------------------------

def test_case_07_full_row_accuracy(agent_report, expected):
    """Every (risk_tier, month) row must match ground truth within tolerance."""
    exp_report, _, _, _, _ = expected
    merged = agent_report.merge(exp_report, on=["risk_tier", "month"], suffixes=("_act", "_exp"))
    assert len(merged) == len(exp_report), "agent report is missing one or more risk_tier/month rows"

    failures = []
    for _, row in merged.iterrows():
        label = f"{row['risk_tier']}/{row['month']}"
        if not math.isclose(row["total_transaction_volume_usd_act"], row["total_transaction_volume_usd_exp"], rel_tol=0.01):
            failures.append(f"{label}: total_transaction_volume_usd {row['total_transaction_volume_usd_act']:,.2f} "
                            f"!= {row['total_transaction_volume_usd_exp']:,.2f}")
        if int(row["transaction_count_act"]) != int(row["transaction_count_exp"]):
            failures.append(f"{label}: transaction_count {int(row['transaction_count_act'])} "
                            f"!= {int(row['transaction_count_exp'])}")
        if not math.isclose(row["confirmed_fraud_loss_usd_act"], row["confirmed_fraud_loss_usd_exp"], rel_tol=0.03, abs_tol=15):
            failures.append(f"{label}: confirmed_fraud_loss_usd {row['confirmed_fraud_loss_usd_act']:,.2f} "
                            f"!= {row['confirmed_fraud_loss_usd_exp']:,.2f}")
        if int(row["confirmed_fraud_count_act"]) != int(row["confirmed_fraud_count_exp"]):
            failures.append(f"{label}: confirmed_fraud_count {int(row['confirmed_fraud_count_act'])} "
                            f"!= {int(row['confirmed_fraud_count_exp'])}")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — Summary.json scalars (all traps)
# ---------------------------------------------------------------------------

def test_case_08_summary_scalars(agent_summary, expected):
    """Portfolio-level totals and the highest-loss-rate tier must match ground truth."""
    exp_report, _, _, _, _ = expected
    exp_volume = float(exp_report["total_transaction_volume_usd"].sum())
    exp_fraud = float(exp_report["confirmed_fraud_loss_usd"].sum())
    exp_count = int(exp_report["confirmed_fraud_count"].sum())
    exp_rate = exp_fraud / exp_volume * 10000

    by_tier = exp_report.groupby("risk_tier").agg(
        total_transaction_volume_usd=("total_transaction_volume_usd", "sum"),
        confirmed_fraud_loss_usd=("confirmed_fraud_loss_usd", "sum"),
    )
    by_tier["rate_bps"] = by_tier["confirmed_fraud_loss_usd"] / by_tier["total_transaction_volume_usd"] * 10000
    exp_highest_tier = str(by_tier["rate_bps"].idxmax())

    assert math.isclose(float(agent_summary["total_transaction_volume_usd"]), exp_volume, rel_tol=0.01), \
        f"total_transaction_volume_usd: expected {exp_volume:,.2f}, got {agent_summary['total_transaction_volume_usd']}"

    assert math.isclose(float(agent_summary["total_confirmed_fraud_loss_usd"]), exp_fraud, rel_tol=0.03, abs_tol=25), \
        f"total_confirmed_fraud_loss_usd: expected {exp_fraud:,.2f}, got {agent_summary['total_confirmed_fraud_loss_usd']}"

    assert abs(int(agent_summary["total_confirmed_fraud_count"]) - exp_count) <= 3, \
        f"total_confirmed_fraud_count: expected {exp_count}, got {agent_summary['total_confirmed_fraud_count']}"

    assert math.isclose(float(agent_summary["overall_fraud_loss_rate_bps"]), exp_rate, rel_tol=0.03, abs_tol=3), \
        f"overall_fraud_loss_rate_bps: expected {exp_rate:.2f}, got {agent_summary['overall_fraud_loss_rate_bps']}"

    assert str(agent_summary["highest_loss_rate_tier"]) == exp_highest_tier, \
        f"highest_loss_rate_tier: expected {exp_highest_tier}, got {agent_summary['highest_loss_rate_tier']}"


# ---------------------------------------------------------------------------
# Test 09 — Transaction count reconciliation (anti-cartesian-blowup check)
# ---------------------------------------------------------------------------

def test_case_09_transaction_count_reconciliation(agent_report, raw_data):
    """
    transaction_count summed across the report must not exceed the number of
    Q1 transactions in transactions.csv. A join that fans out rows (e.g. a
    composite-key mistake elsewhere, or a many-to-many merge) would inflate
    this total even if individual cells look plausible in isolation.
    """
    _, _, transactions, _, _, _ = raw_data
    q1_count = int(((transactions["transaction_date"] >= Q1_START) &
                     (transactions["transaction_date"] < Q1_END_EXCLUSIVE)).sum())
    act_total = int(agent_report["transaction_count"].sum())
    assert act_total <= q1_count, \
        (f"Sum of transaction_count across the report ({act_total}) exceeds the number of "
         f"Q1 transactions in transactions.csv ({q1_count}) — check for a join that fans out rows.")
    assert act_total >= int(q1_count * 0.95), \
        (f"Sum of transaction_count across the report ({act_total}) is well below the number of "
         f"Q1 transactions in transactions.csv ({q1_count}) — check for transactions silently "
         f"dropped during risk tier assignment.")


# ---------------------------------------------------------------------------
# Test 10 — Velocity-fraud cluster detection + decoy exclusion (Trap 5)
# ---------------------------------------------------------------------------

def test_case_10_velocity_cluster_detection(agent_report, expected, raw_data):
    """
    Trap 5 — A payment instrument used at 3+ distinct merchants within any
    48-hour window is a velocity-fraud cluster and counts as confirmed fraud
    loss independent of any dispute case. A model that misses this mechanism
    entirely undercounts confirmed_fraud_loss_usd in the tier/month cells
    those clusters' transactions belong to.

    15 separate payment instruments are used at 3+ distinct merchants but
    spread across weeks, not within any 48-hour window (ordinary repeat
    customers). A model that checks distinct-merchant count without any time
    constraint wrongly flags these too, overstating confirmed_fraud_loss_usd
    in the cells their transactions belong to.

    Spot-checks tier/month cells touched by true clusters and by decoys.
    """
    _, _, transactions, _, _, _ = raw_data
    exp_report, txns_with_tier, _, _, velocity_keys = expected
    key_cols = ["gateway_id", "transaction_id"]

    vel_txns = txns_with_tier[txns_with_tier[key_cols].apply(tuple, axis=1).isin(velocity_keys)]
    assert len(vel_txns) > 0, "test setup error: expected at least one velocity-flagged transaction"

    inst_stats = transactions.groupby("payment_instrument_id").agg(
        n_merchants=("merchant_id", "nunique"),
        span_hours=("transaction_date", lambda s: (s.max() - s.min()).total_seconds() / 3600),
    )
    decoy_instruments = inst_stats[(inst_stats["n_merchants"] >= 3) & (inst_stats["span_hours"] >= 48)].index
    decoy_txns = txns_with_tier[txns_with_tier["payment_instrument_id"].isin(decoy_instruments)]
    assert len(decoy_txns) > 0, "test setup error: expected at least one decoy instrument transaction"
    assert not decoy_txns[key_cols].apply(tuple, axis=1).isin(velocity_keys).any(), \
        "test setup error: a decoy instrument's transaction was flagged by the correct sliding-window logic"

    cells = pd.concat([
        vel_txns[["risk_tier", "month"]].drop_duplicates().head(3),
        decoy_txns[["risk_tier", "month"]].drop_duplicates().head(3),
    ]).drop_duplicates()

    failures = []
    for _, c in cells.iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "A payment instrument used at 3+ distinct merchants within any 48-hour window counts "
                "as confirmed fraud loss independent of any dispute case; one spread across weeks does not."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 11 — Boundary-spanning velocity clusters (Trap 6)
# ---------------------------------------------------------------------------

def test_case_11_boundary_spanning_clusters(agent_report, expected, raw_data):
    """
    Trap 6 — transactions.csv is not strictly Q1-bounded (it carries a
    Dec 2023 / Apr 2024 buffer). 8 payment instruments have a qualifying
    48-hour, 3+-merchant window that straddles the Q1 boundary: fewer than 3
    distinct merchants appear in the Q1-only slice, but 3+ appear once the
    buffer-period transactions are included. A pipeline that scopes to Q1
    BEFORE running the velocity clustering check silently sees only the
    smaller Q1-side view and misses these — the correct order clusters on
    the full file first, then scopes to Q1 for the report.

    Spot-checks tier/month cells containing a boundary-cluster's Q1-side
    transaction.
    """
    _, _, transactions, _, _, _ = raw_data
    exp_report, txns_with_tier, _, _, velocity_keys = expected

    boundary_instruments = transactions.loc[
        transactions["payment_instrument_id"].str.startswith("PMT-BND", na=False), "payment_instrument_id"
    ].unique()
    assert len(boundary_instruments) == 8, \
        f"transactions.csv must have exactly 8 boundary-spanning payment instruments, got {len(boundary_instruments)}"

    key_cols = ["gateway_id", "transaction_id"]
    boundary_txns = txns_with_tier[txns_with_tier["payment_instrument_id"].isin(boundary_instruments)]
    assert len(boundary_txns) > 0, "test setup error: expected Q1-side transactions for boundary instruments"
    assert boundary_txns[key_cols].apply(tuple, axis=1).isin(velocity_keys).all(), \
        "test setup error: a boundary instrument's Q1-side transaction was not flagged by the correct full-file check"

    cells = boundary_txns[["risk_tier", "month"]].drop_duplicates()

    failures = []
    for _, c in cells.head(5).iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "The velocity-cluster check must run on the full transactions.csv file, not a "
                "Q1-scoped subset — some qualifying windows straddle the Q1 boundary."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 12 — Writeoff-status false friend (Trap 7)
# ---------------------------------------------------------------------------

def test_case_12_writeoff_status_not_a_confirmation_gate(agent_report, agent_summary, expected, raw_data):
    """
    Trap 7 — writeoff_status records whether a confirmed case's loss has been
    posted to the general ledger yet — a GL/accounting-cycle fact, not part
    of the confirmed-fraud-loss definition (reason_code + resolution_status
    only). ~30% of confirmed cases carry writeoff_status == "pending" on
    their final resolution (write-off posting is batched and lags actual
    case resolution). A model that discovers this column and infers "not
    posted yet means not final" excludes these cases, undercounting
    confirmed_fraud_loss_usd in the tier/month cells they touch and shifting
    highest_loss_rate_tier.

    Spot-checks tier/month cells touched by a pending-writeoff confirmed case,
    and the summary-level highest_loss_rate_tier.
    """
    _, _, _, _, resolutions, case_transactions = raw_data
    exp_report, txns_with_tier, confirmed_txns, confirmed, _ = expected

    latest = resolutions.sort_values("resolution_date").groupby("case_id", as_index=False).last()
    pending_writeoff_cases = set(latest.loc[latest["writeoff_status"] == "pending", "case_id"]) & set(confirmed)
    assert len(pending_writeoff_cases) > 0, "test setup error: expected at least one confirmed case pending write-off"

    pending_links = case_transactions[case_transactions["case_id"].isin(pending_writeoff_cases)]
    key_cols = ["gateway_id", "transaction_id"]
    pending_txns = pending_links.merge(txns_with_tier, on=key_cols, how="inner")
    assert len(pending_txns) > 0, "test setup error: expected at least one pending-writeoff confirmed transaction"

    cells = pending_txns[["risk_tier", "month"]].drop_duplicates()

    failures = []
    for _, c in cells.head(5).iterrows():
        tier, month = c["risk_tier"], c["month"]
        exp_loss = _cell(exp_report, tier, month, "confirmed_fraud_loss_usd")
        act_loss = _cell(agent_report, tier, month, "confirmed_fraud_loss_usd")
        if act_loss is None:
            failures.append(f"{tier}/{month}: row missing from agent report")
            continue
        if not math.isclose(float(act_loss), float(exp_loss), rel_tol=0.03, abs_tol=15):
            failures.append(
                f"{tier}/{month}: confirmed_fraud_loss_usd {act_loss:,.2f} != expected {exp_loss:,.2f}. "
                "writeoff_status records GL posting timing, not fraud confirmation — a case pending "
                "write-off is still confirmed fraud loss if reason_code and resolution_status qualify."
            )

    by_tier = exp_report.groupby("risk_tier").agg(
        total_transaction_volume_usd=("total_transaction_volume_usd", "sum"),
        confirmed_fraud_loss_usd=("confirmed_fraud_loss_usd", "sum"),
    )
    by_tier["rate_bps"] = by_tier["confirmed_fraud_loss_usd"] / by_tier["total_transaction_volume_usd"] * 10000
    exp_highest_tier = str(by_tier["rate_bps"].idxmax())
    if str(agent_summary["highest_loss_rate_tier"]) != exp_highest_tier:
        failures.append(
            f"highest_loss_rate_tier: expected {exp_highest_tier}, got {agent_summary['highest_loss_rate_tier']}. "
            "Excluding pending-writeoff cases shifts which tier has the highest fraud loss rate."
        )

    assert not failures, "\n".join(failures)
