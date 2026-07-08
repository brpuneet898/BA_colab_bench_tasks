"""
Tests for the Q1 2024 Supplier Performance Scorecard task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — (warehouse_id, po_id) composite key.
            Joining on po_id alone inflates every supplier's PO count ~3x,
            scrambling all downstream metrics.

  Trap 2 — Open-ended contracts (NaT contract_effective_to).
            Twenty suppliers hold contracts with no expiry date — the
            contract_effective_to field is blank (NaT). A pandas comparison
            of the form `order_date <= NaT` returns False, silently excluding
            all POs for those suppliers from every metric. The correct
            implementation treats NaT as "no expiry" and keeps those rows.

  Trap 3 — Escalating penalty after 5 cumulative SLA breaches.
            Once a supplier's running breach count (in order_date, po_id order)
            exceeds 5, each subsequent breaching PO is assessed at 2x the
            standard penalty_rate_pct. A flat-rate implementation
            significantly underestimates total_penalty_usd for high-breach
            suppliers.

  Trap 4 — Overlapping contract renegotiation.
            Ten suppliers have two overlapping contracts: an original contract
            (lower penalty_rate_pct, Jan 1–Mar 31) written first in the CSV,
            and a renegotiated contract (higher penalty_rate_pct, Feb 1–Dec 31)
            written second. A naive implementation iterating in CSV order finds
            the original first and uses its lower rate for Feb–Mar POs. The
            correct rule — apply the most recently effective contract (highest
            contract_effective_from that covers the PO date) — is established
            by the instruction's 'Use the cap from the supplier's most recently
            effective contract' and applies equally to all contract terms.
"""

import json
import math
import os
import pytest
import pandas as pd
from pathlib import Path

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

SCORECARD_PATH = WORKSPACE_DIR / "supplier_scorecard.csv"
SUMMARY_PATH   = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END   = pd.Timestamp("2024-03-31")

# ---------------------------------------------------------------------------
# Ground-truth helpers  (replicate oracle logic exactly)
# ---------------------------------------------------------------------------

def _get_applicable_contract(pos, contracts):
    """
    Filter merged PO-contract rows to those where the contract was in effect
    on the order_date. NaT in contract_effective_to means open-ended (no expiry);
    such contracts are always in effect after their start date.
    """
    merged = pos.merge(
        contracts[["supplier_id", "contract_effective_from", "contract_effective_to",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap_usd"]],
        on="supplier_id", how="left",
    )
    open_ended = merged["contract_effective_to"].isna()
    applicable = merged[
        (merged["contract_effective_from"] <= merged["order_date"]) &
        (open_ended | (merged["order_date"] <= merged["contract_effective_to"]))
    ].copy()
    applicable = applicable.sort_values("contract_effective_from", ascending=False)
    applicable = applicable.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return applicable


def _compute_po_level(pos_with_contracts, deliveries):
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    d_with_deadline = deliveries.merge(
        pos_with_contracts[["warehouse_id", "po_id", "promised_delivery_date",
                            "ordered_quantity"]].drop_duplicates(),
        on=["warehouse_id", "po_id"], how="inner",
    )
    d_before = d_with_deadline[
        d_with_deadline["received_date"] <= d_with_deadline["promised_delivery_date"]
    ]
    net_by_deadline = (
        d_before
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_by_deadline"})
    )

    po = pos_with_contracts.merge(net_qty,         on=["warehouse_id", "po_id"], how="left")
    po = po.merge(net_by_deadline, on=["warehouse_id", "po_id"], how="left")

    po["net_fill_rate_po"] = po["net_qty_received"] / po["ordered_quantity"]
    po["on_time"] = po["net_qty_by_deadline"].fillna(0) >= po["ordered_quantity"]
    po["sla_breach"] = (
        (po["net_fill_rate_po"] < po["fill_rate_sla_threshold"]) | (~po["on_time"])
    )

    # Escalating penalty: 2x rate for each breach after the 5th (per supplier, order_date order)
    po = po.sort_values(["supplier_id", "order_date", "po_id"]).copy()
    po["breach_rank"] = po.groupby("supplier_id")["sla_breach"].cumsum()
    escalated = po["sla_breach"] & (po["breach_rank"] > 5)
    po["penalty_rate_eff"] = po["penalty_rate_pct"].where(~escalated,
                                                           po["penalty_rate_pct"] * 2)
    po["penalty_po"] = po.apply(
        lambda r: r["order_value_usd"] * r["penalty_rate_eff"] if r["sla_breach"] else 0.0,
        axis=1,
    )
    return po


def _penalty_caps(contracts):
    latest = (
        contracts
        .sort_values("contract_effective_from", ascending=False)
        .drop_duplicates(subset="supplier_id", keep="first")
        [["supplier_id", "max_penalty_cap_usd"]]
    )
    return latest.set_index("supplier_id")["max_penalty_cap_usd"].to_dict()


def _build_expected(suppliers, contracts, q1_pos, deliveries):
    pos_c  = _get_applicable_contract(q1_pos, contracts)
    po_lvl = _compute_po_level(pos_c, deliveries)
    caps   = _penalty_caps(contracts)

    records = []
    for _, sup in suppliers.iterrows():
        sid     = sup["supplier_id"]
        sup_pos = po_lvl[po_lvl["supplier_id"] == sid]
        if sup_pos.empty:
            records.append(dict(supplier_id=sid, supplier_name=sup["supplier_name"],
                total_pos=0, on_time_delivery_rate=None, net_fill_rate=None,
                sla_breach_count=0, total_penalty_usd=0.0, composite_score=None))
            continue
        n          = len(sup_pos)
        otr        = round(sup_pos["on_time"].sum() / n, 4)
        nfr        = round(sup_pos["net_qty_received"].sum() / sup_pos["ordered_quantity"].sum(), 4)
        breach_cnt = int(sup_pos["sla_breach"].sum())
        raw_pen    = sup_pos["penalty_po"].sum()
        cap        = caps.get(sid, float("inf"))
        penalty    = round(min(raw_pen, cap), 2)
        score      = round(otr * 0.6 + nfr * 0.4, 4)
        records.append(dict(supplier_id=sid, supplier_name=sup["supplier_name"],
            total_pos=n, on_time_delivery_rate=otr, net_fill_rate=nfr,
            sla_breach_count=breach_cnt, total_penalty_usd=penalty, composite_score=score))

    df = pd.DataFrame(records)
    df = df.sort_values(["composite_score", "supplier_id"],
                        ascending=[True, True], na_position="last").reset_index(drop=True)
    return df, po_lvl, caps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_data():
    suppliers  = pd.read_csv(DATA_DIR / "suppliers.csv")
    contracts  = pd.read_csv(DATA_DIR / "supplier_contracts.csv",
                             parse_dates=["contract_effective_from", "contract_effective_to"])
    pos        = pd.read_csv(DATA_DIR / "purchase_orders.csv",
                             parse_dates=["order_date", "promised_delivery_date"])
    deliveries = pd.read_csv(DATA_DIR / "delivery_records.csv",
                             parse_dates=["received_date"])
    return suppliers, contracts, pos, deliveries


@pytest.fixture(scope="module")
def q1_pos(raw_data):
    _, _, pos, _ = raw_data
    return pos[(pos["order_date"] >= Q1_START) & (pos["order_date"] <= Q1_END)].copy()


@pytest.fixture(scope="module")
def expected(raw_data, q1_pos):
    suppliers, contracts, _, deliveries = raw_data
    df, po_lvl, caps = _build_expected(suppliers, contracts, q1_pos, deliveries)
    return df, po_lvl, caps


@pytest.fixture(scope="module")
def agent_scorecard():
    return pd.read_csv(SCORECARD_PATH) if SCORECARD_PATH.exists() else None


@pytest.fixture(scope="module")
def agent_summary():
    if not SUMMARY_PATH.exists():
        return None
    with open(SUMMARY_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 01 — Sentinel: canonical data fingerprint
# ---------------------------------------------------------------------------

def test_case_01_input_sentinels(raw_data):
    """Verify input files were not tampered with."""
    suppliers, contracts, pos, deliveries = raw_data

    assert len(suppliers) == 60, \
        f"suppliers.csv must not be modified (expected 60 rows, got {len(suppliers)})"

    assert len(pos) == 3_000, \
        f"purchase_orders.csv must not be modified (expected 3000 rows, got {len(pos)})"

    unique_po_ids = pos["po_id"].nunique()
    assert unique_po_ids == 1_000, \
        (f"po_id must not be globally unique — each warehouse uses its own sequence "
         f"(expected 1000 unique strings across 3000 rows, got {unique_po_ids})")

    # Structural sentinel: 20 suppliers must have open-ended contracts (NaT effective_to)
    nat_count = contracts["contract_effective_to"].isna().sum()
    assert nat_count == 20, \
        (f"supplier_contracts.csv must contain exactly 20 open-ended contracts "
         f"(contract_effective_to is blank), got {nat_count}")

    # Ten suppliers have overlapping renegotiated contracts (dual-contract renegotiation trap)
    dual_count = (contracts.groupby("supplier_id").size() > 1).sum()
    assert dual_count == 10, \
        f"supplier_contracts.csv must contain exactly 10 dual-contract suppliers, got {dual_count}"


# ---------------------------------------------------------------------------
# Test 02 — Output structure
# ---------------------------------------------------------------------------

def test_case_02_output_structure(agent_scorecard, agent_summary):
    """Both output files exist and have the required shape and columns."""
    assert SCORECARD_PATH.exists(), "supplier_scorecard.csv not found in /workspace"
    assert SUMMARY_PATH.exists(),   "summary.json not found in /workspace"
    assert agent_scorecard is not None

    required_cols = ["supplier_id", "supplier_name", "total_pos",
                     "on_time_delivery_rate", "net_fill_rate",
                     "sla_breach_count", "total_penalty_usd", "composite_score"]
    for col in required_cols:
        assert col in agent_scorecard.columns, f"Missing column in scorecard: {col}"

    assert len(agent_scorecard) == 60, \
        f"supplier_scorecard.csv must have 60 rows (one per supplier), got {len(agent_scorecard)}"

    required_keys = ["total_penalty_assessed_usd", "suppliers_meeting_all_sla",
                     "worst_on_time_supplier_id", "worst_fill_rate_supplier_id",
                     "total_sla_breach_count"]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — PO count accuracy  (Trap 1: composite key)
# ---------------------------------------------------------------------------

def test_case_03_po_count_per_supplier(agent_scorecard, expected, raw_data):
    """
    Trap 1 — Joining delivery_records to purchase_orders on po_id alone
    inflates every supplier's PO count ~3x. The correct join key is
    (warehouse_id, po_id).

    Spot-checks total_pos for the 5 suppliers with the highest PO count.
    """
    exp_df, _, _ = expected

    spot = exp_df.sort_values("total_pos", ascending=False)["supplier_id"].iloc[:5].tolist()

    failures = []
    for sid in spot:
        exp_count = int(exp_df.loc[exp_df["supplier_id"] == sid, "total_pos"].iloc[0])
        act_row   = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if act_row.empty:
            failures.append(f"{sid}: not found in agent scorecard")
            continue
        act_count = int(act_row["total_pos"].iloc[0])
        if act_count != exp_count:
            failures.append(
                f"{sid}: total_pos {act_count} != expected {exp_count}. "
                "Join must use (warehouse_id, po_id) — po_id is not globally unique."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — Open-ended contract suppliers have correct metrics  (Trap 2: NaT)
# ---------------------------------------------------------------------------

def test_case_04_open_ended_contract_suppliers(agent_scorecard, expected, raw_data):
    """
    Trap 2 — Twenty suppliers have open-ended contracts with no expiry date
    (contract_effective_to is NaT). A naive pandas date filter of the form
    `order_date <= NaT` returns False, silently dropping all POs for these
    suppliers. A correct implementation treats NaT as 'no expiry'.

    Verifies that NaT-contract suppliers appear with valid (non-zero, non-null)
    metrics in the agent scorecard, and spot-checks their on_time_delivery_rate.
    """
    _, contracts, _, _ = raw_data
    exp_df, _, _ = expected

    nat_suppliers = contracts[contracts["contract_effective_to"].isna()]["supplier_id"].tolist()
    assert len(nat_suppliers) == 20

    spot = sorted(nat_suppliers)[:5]

    failures = []
    for sid in spot:
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        if act_row.empty or exp_row.empty:
            continue

        act_total = int(act_row["total_pos"].iloc[0])
        exp_total = int(exp_row["total_pos"].iloc[0])

        if act_total == 0:
            failures.append(
                f"{sid}: total_pos=0 but expected {exp_total}. "
                "Open-ended contracts (NaT effective_to) must not be excluded. "
                "The comparison `order_date <= NaT` returns False in pandas — "
                "NaT must be handled as 'no expiry date'."
            )
            continue

        exp_otr = float(exp_row["on_time_delivery_rate"].iloc[0])
        act_otr_raw = act_row["on_time_delivery_rate"].iloc[0]
        if pd.isna(act_otr_raw):
            failures.append(f"{sid}: on_time_delivery_rate is null, expected {exp_otr:.4f}")
            continue
        act_otr = float(act_otr_raw)
        if abs(act_otr - exp_otr) > 0.005:
            failures.append(
                f"{sid}: on_time_delivery_rate {act_otr:.4f} != expected {exp_otr:.4f}"
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — Escalating penalty for high-breach suppliers  (Trap 3: step-up)
# ---------------------------------------------------------------------------

def test_case_05_escalating_penalty_high_breach_suppliers(agent_scorecard, expected, raw_data):
    """
    Trap 3 — After a supplier's 5th SLA breach (evaluated in order_date order),
    every further breaching PO is assessed at twice the standard penalty_rate_pct.
    A flat-rate implementation underestimates total_penalty_usd for suppliers
    with many breaches.

    Spot-checks total_penalty_usd for the 5 non-NaT suppliers with the most
    SLA breaches (where the escalation effect is largest and the NaT trap
    cannot confound the result).
    """
    _, contracts, _, _ = raw_data
    exp_df, po_lvl, _ = expected

    nat_suppliers = set(contracts[contracts["contract_effective_to"].isna()]["supplier_id"])

    breach_counts = (
        po_lvl[~po_lvl["supplier_id"].isin(nat_suppliers)]
        .groupby("supplier_id")["sla_breach"]
        .sum()
        .sort_values(ascending=False)
    )
    spot = breach_counts.index[:5].tolist()

    failures = []
    for sid in spot:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue

        exp_pen = float(exp_row["total_penalty_usd"].iloc[0])
        act_pen = float(act_row["total_penalty_usd"].iloc[0])
        breach_n = int(breach_counts[sid])

        if not math.isclose(act_pen, exp_pen, abs_tol=500):
            failures.append(
                f"{sid}: total_penalty_usd {act_pen:,.2f} != expected {exp_pen:,.2f} "
                f"(diff {act_pen - exp_pen:+,.2f}, {breach_n} SLA breaches). "
                "Each breaching PO from the 6th onwards (by order_date) must be "
                "assessed at twice the standard penalty_rate_pct."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — Aggregate total penalty and breach count  (all traps)
# ---------------------------------------------------------------------------

def test_case_06_aggregate_breach_and_penalty(agent_summary, expected):
    """
    Aggregate total_sla_breach_count and total_penalty_assessed_usd.
    Both are sensitive to composite-key correctness, NaT contract exclusion,
    and escalating penalty calculation.
    """
    exp_df, _, _ = expected
    exp_total_pen = round(float(exp_df["total_penalty_usd"].sum()), 2)
    act_total_pen = float(agent_summary["total_penalty_assessed_usd"])
    assert math.isclose(act_total_pen, exp_total_pen, rel_tol=0.005), \
        f"total_penalty_assessed_usd: expected {exp_total_pen:,.2f}, got {act_total_pen:,.2f}"

    exp_bc = int(exp_df["sla_breach_count"].sum())
    act_bc = int(agent_summary["total_sla_breach_count"])
    assert abs(act_bc - exp_bc) <= 3, \
        f"total_sla_breach_count: expected {exp_bc}, got {act_bc} (diff {act_bc - exp_bc:+d})"


# ---------------------------------------------------------------------------
# Test 07 — Row-level scorecard accuracy  (all traps)
# ---------------------------------------------------------------------------

def test_case_07_row_level_accuracy(agent_scorecard, expected):
    """
    Spot-checks all key metrics for 10 suppliers with the highest PO counts.
    Compound errors from any trap will surface here.
    """
    exp_df, _, _ = expected

    spot = exp_df.sort_values("total_pos", ascending=False)["supplier_id"].iloc[:10].tolist()

    merged = agent_scorecard.merge(
        exp_df[["supplier_id", "on_time_delivery_rate", "net_fill_rate",
                "total_penalty_usd", "composite_score"]],
        on="supplier_id", suffixes=("_act", "_exp"),
    )
    merged = merged[merged["supplier_id"].isin(spot)]

    failures = []
    for _, row in merged.iterrows():
        sid = row["supplier_id"]

        otr_diff = abs(row["on_time_delivery_rate_act"] - row["on_time_delivery_rate_exp"])
        if otr_diff > 0.005:
            failures.append(f"{sid}: on_time_delivery_rate {row['on_time_delivery_rate_act']:.4f} "
                            f"!= {row['on_time_delivery_rate_exp']:.4f}")

        nfr_diff = abs(row["net_fill_rate_act"] - row["net_fill_rate_exp"])
        if nfr_diff > 0.005:
            failures.append(f"{sid}: net_fill_rate {row['net_fill_rate_act']:.4f} "
                            f"!= {row['net_fill_rate_exp']:.4f}")

        pen_diff = abs(row["total_penalty_usd_act"] - row["total_penalty_usd_exp"])
        if pen_diff > 500:
            failures.append(f"{sid}: total_penalty_usd {row['total_penalty_usd_act']:,.2f} "
                            f"!= {row['total_penalty_usd_exp']:,.2f}")

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — Composite score ranking  (compound)
# ---------------------------------------------------------------------------

def test_case_08_composite_score_ranking(agent_scorecard, expected):
    """
    Bottom-3 and top-3 suppliers by composite_score must match ground truth.
    Errors from any trap shuffle the ranking.
    """
    exp_df, _, _ = expected
    exp_active = exp_df[exp_df["total_pos"] > 0].dropna(subset=["composite_score"])
    act_active  = agent_scorecard[agent_scorecard["total_pos"] > 0].dropna(subset=["composite_score"])

    exp_bottom3 = exp_active.nsmallest(3, "composite_score")["supplier_id"].tolist()
    act_bottom3 = act_active.nsmallest(3, "composite_score")["supplier_id"].tolist()
    assert set(act_bottom3) == set(exp_bottom3), \
        f"Bottom-3 by composite_score: got {act_bottom3}, expected {exp_bottom3}"

    exp_top3 = exp_active.nlargest(3, "composite_score")["supplier_id"].tolist()
    act_top3 = act_active.nlargest(3, "composite_score")["supplier_id"].tolist()
    assert set(act_top3) == set(exp_top3), \
        f"Top-3 by composite_score: got {act_top3}, expected {exp_top3}"


# ---------------------------------------------------------------------------
# Test 09 — Summary JSON scalars
# ---------------------------------------------------------------------------

def test_case_09_summary_scalars(agent_summary, expected):
    """
    Verifies worst_on_time_supplier_id and worst_fill_rate_supplier_id.
    These shift when on-time or fill rate calculations are wrong.
    """
    exp_df, _, _ = expected
    active = exp_df[exp_df["total_pos"] > 0]

    exp_worst_ot  = str(active.loc[active["on_time_delivery_rate"].idxmin(), "supplier_id"])
    exp_worst_nfr = str(active.loc[active["net_fill_rate"].idxmin(), "supplier_id"])

    assert agent_summary["worst_on_time_supplier_id"] == exp_worst_ot, \
        f"worst_on_time_supplier_id: got {agent_summary['worst_on_time_supplier_id']}, expected {exp_worst_ot}"

    assert agent_summary["worst_fill_rate_supplier_id"] == exp_worst_nfr, \
        f"worst_fill_rate_supplier_id: got {agent_summary['worst_fill_rate_supplier_id']}, expected {exp_worst_nfr}"

# ---------------------------------------------------------------------------
# Test 10 — Overlapping contract renegotiation  (Trap 4)
# ---------------------------------------------------------------------------

def test_case_10_contract_renegotiation_penalty(agent_scorecard, expected, raw_data):
    """
    Trap 4 — Ten suppliers had their contracts renegotiated mid-quarter.
    Each has two overlapping contract rows in supplier_contracts.csv:

      OLD  contract_effective_from=2024-01-01, effective_to=2024-03-31, lower penalty_rate
      NEW  contract_effective_from=2024-02-01, effective_to=2024-12-31, higher penalty_rate

    The OLD contract is written first in the CSV. A naive implementation that
    iterates contract rows in file order finds the OLD contract first for Feb–Mar
    POs (both contracts are 'in effect') and uses the lower penalty rate, causing
    total_penalty_usd to be underestimated for affected suppliers.

    The correct rule: where multiple contracts cover a PO date, apply the one
    with the most recent contract_effective_from. The instruction establishes
    this principle explicitly for cap selection ('Use the cap from the supplier\'s
    most recently effective contract') — the same precedence applies to all
    contract terms.
    """
    _, contracts, _, _ = raw_data
    exp_df, _, _ = expected

    dual_suppliers = (
        contracts.groupby("supplier_id")
        .size()
        .loc[lambda s: s > 1]
        .index.tolist()
    )
    assert len(dual_suppliers) == 10, \
        f"Expected 10 dual-contract suppliers, got {len(dual_suppliers)}"

    spot = sorted(dual_suppliers)[:5]

    failures = []
    for sid in spot:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue
        exp_pen = float(exp_row["total_penalty_usd"].iloc[0])
        act_pen = float(act_row["total_penalty_usd"].iloc[0])
        if not math.isclose(act_pen, exp_pen, abs_tol=500):
            failures.append(
                f"{sid}: total_penalty_usd {act_pen:,.2f} != expected {exp_pen:,.2f} "
                f"(diff {act_pen - exp_pen:+,.2f}). "
                "When two contracts overlap, apply the one with the most recent "
                "contract_effective_from — the renegotiated terms supersede the original."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 11 — Scorecard sort order
# ---------------------------------------------------------------------------

def test_case_11_scorecard_sort_order(agent_scorecard):
    """
    The instruction mandates supplier_scorecard.csv be sorted by composite_score
    ascending (nulls last), with supplier_id ascending as a tiebreaker within
    the same score. This test reads the CSV in physical row order and verifies
    the sort — value-correctness tests do not check row order.
    """
    assert agent_scorecard is not None
    df = agent_scorecard.reset_index(drop=True)

    null_indices     = df.index[df["composite_score"].isna()].tolist()
    non_null_indices = df.index[df["composite_score"].notna()].tolist()

    if null_indices and non_null_indices:
        assert min(null_indices) > max(non_null_indices), (
            "Rows with null composite_score must appear after all non-null rows "
            "(nulls last). Found a null-score row before a non-null-score row."
        )

    non_null = df[df["composite_score"].notna()].reset_index(drop=True)
    actual_order   = non_null["supplier_id"].tolist()
    expected_order = (
        non_null
        .sort_values(["composite_score", "supplier_id"], ascending=[True, True])
        ["supplier_id"]
        .tolist()
    )

    assert actual_order == expected_order, (
        "supplier_scorecard.csv is not sorted by composite_score ascending "
        "(supplier_id ascending as tiebreaker). "
        "The procurement team's worst-suppliers-first review depends on correct row order."
    )
