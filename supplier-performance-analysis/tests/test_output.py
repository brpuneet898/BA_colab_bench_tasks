"""
Tests for the Q1 2024 Supplier Performance Scorecard task.

Ground truth is recomputed from the canonical data files baked into the image.

Traps targeted by these tests:

  Trap 1 — (warehouse_id, po_id) composite key.
            Joining on po_id alone inflates every supplier's PO count ~3x,
            scrambling all downstream metrics.

  Trap 2 — Post-promised return + replacement delivery events.
            A PO is on time if the cumulative net quantity received ON OR
            BEFORE the promised_delivery_date meets or exceeds ordered_quantity.
            Return and replacement events that arrive AFTER that date must not
            influence the on-time determination. Any implementation that uses
            the latest positive delivery date to evaluate on-time will mark
            ~120 correctly-on-time POs as late, inflating penalties.

  Trap 3 — Effective-dated contracts (SCD).
            Ten suppliers renegotiated in Feb; their January POs must use the
            old contract's fill_rate_sla_threshold and penalty_rate_pct.
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
    merged = pos.merge(
        contracts[["supplier_id", "contract_effective_from", "contract_effective_to",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap_usd"]],
        on="supplier_id", how="left",
    )
    applicable = merged[
        (merged["contract_effective_from"] <= merged["order_date"]) &
        (merged["order_date"] <= merged["contract_effective_to"])
    ].copy()
    applicable = applicable.sort_values("contract_effective_from", ascending=False)
    applicable = applicable.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return applicable


def _compute_po_level(pos_with_contracts, deliveries):
    # Net fill rate: algebraic sum of ALL quantity_received regardless of date
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    # On-time: filter delivery events to received_date <= promised_delivery_date,
    # then sum (including negatives). A PO is on time if that filtered net total
    # meets or exceeds ordered_quantity.
    d_with_deadline = deliveries.merge(
        pos_with_contracts[["warehouse_id", "po_id", "promised_delivery_date",
                            "ordered_quantity"]].drop_duplicates(),
        on=["warehouse_id", "po_id"],
        how="inner",
    )
    d_before_deadline = d_with_deadline[
        d_with_deadline["received_date"] <= d_with_deadline["promised_delivery_date"]
    ]
    net_by_deadline = (
        d_before_deadline
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
    po["penalty_po"] = po.apply(
        lambda r: r["order_value_usd"] * r["penalty_rate_pct"] if r["sla_breach"] else 0.0,
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
    pos_c   = _get_applicable_contract(q1_pos, contracts)
    po_lvl  = _compute_po_level(pos_c, deliveries)
    caps    = _penalty_caps(contracts)

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

    # po_id is not globally unique — resets per warehouse
    unique_po_ids = pos["po_id"].nunique()
    assert unique_po_ids == 1_000, \
        (f"po_id must not be globally unique — each warehouse uses its own sequence "
         f"(expected 1000 unique strings across 3000 rows, got {unique_po_ids})")

    dual_contract_count = (contracts.groupby("supplier_id").size() > 1).sum()
    assert dual_contract_count == 10, \
        f"supplier_contracts.csv must contain exactly 10 suppliers with 2 contract rows, got {dual_contract_count}"

    # Replacement rows are a structural property that must not be removed
    replacement_rows = (deliveries["delivery_type"] == "Replacement").sum()
    assert replacement_rows == 120, \
        f"delivery_records.csv must contain exactly 120 Replacement rows, got {replacement_rows}"


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
# Test 03 — PO count accuracy  (Trap 1)
# ---------------------------------------------------------------------------

def test_case_03_po_count_per_supplier(agent_scorecard, expected, raw_data):
    """
    Trap 1 — Joining delivery_records to purchase_orders on po_id alone
    inflates every supplier's PO count ~3x. The composite key is (warehouse_id, po_id).

    Spot-checks total_pos for 5 suppliers with the highest PO count.
    """
    exp_df, _, _ = expected

    spot_suppliers = exp_df.sort_values("total_pos", ascending=False)["supplier_id"].iloc[:5].tolist()

    failures = []
    for sid in spot_suppliers:
        exp_count = int(exp_df.loc[exp_df["supplier_id"] == sid, "total_pos"].iloc[0])
        act_row   = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if act_row.empty:
            failures.append(f"{sid}: not found in agent scorecard")
            continue
        act_count = int(act_row["total_pos"].iloc[0])
        if act_count != exp_count:
            failures.append(
                f"{sid}: total_pos {act_count} != expected {exp_count}. "
                "po_id is not globally unique — join must use (warehouse_id, po_id)."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — On-time accuracy for POs with post-deadline delivery events  (Trap 2)
# ---------------------------------------------------------------------------

def test_case_04_on_time_with_post_deadline_adjustments(agent_scorecard, expected, raw_data):
    """
    Trap 2 — Some POs have returns and replacements that arrive AFTER the
    promised_delivery_date. The initial delivery was complete and on time.
    On-time determination must be based on the cumulative net quantity received
    ON OR BEFORE the promised_delivery_date. Any implementation that uses the
    date of the latest positive delivery event marks these POs as late and
    inflates SLA breach counts and penalties for the affected suppliers.

    Spot-checks on_time_delivery_rate for the 5 suppliers most affected by
    post-deadline replacement events.
    """
    exp_df, po_lvl, _ = expected
    _, _, pos, deliveries = raw_data

    # Identify suppliers with the most Replacement rows
    replacement_deliveries = deliveries[deliveries["delivery_type"] == "Replacement"]
    replacement_po_keys = set(zip(replacement_deliveries["warehouse_id"],
                                  replacement_deliveries["po_id"]))
    po_lvl_copy = po_lvl.copy()
    po_lvl_copy["is_replacement_po"] = po_lvl_copy.apply(
        lambda r: (r["warehouse_id"], r["po_id"]) in replacement_po_keys, axis=1
    )
    rep_counts = (
        po_lvl_copy[po_lvl_copy["is_replacement_po"]]
        .groupby("supplier_id")
        .size()
        .sort_values(ascending=False)
    )
    spot_suppliers = rep_counts.index[:5].tolist()

    failures = []
    for sid in spot_suppliers:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue
        exp_otr = float(exp_row["on_time_delivery_rate"].iloc[0])
        act_otr = float(act_row["on_time_delivery_rate"].iloc[0])
        if abs(act_otr - exp_otr) > 0.005:
            failures.append(
                f"{sid}: on_time_delivery_rate {act_otr:.4f} != expected {exp_otr:.4f} "
                f"(diff {act_otr - exp_otr:+.4f}). "
                "On-time must be evaluated by summing net quantity received on or "
                "before promised_delivery_date, not by checking the latest delivery date."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — SLA breach count for dual-contract suppliers  (Trap 3)
# ---------------------------------------------------------------------------

def test_case_05_sla_breach_count_renegotiated_suppliers(agent_scorecard, expected, raw_data):
    """
    Dual-contract suppliers — Ten suppliers renegotiated their contracts
    effective 2024-02-01. Their SLA breach counts are sensitive to both the
    applicable contract terms and the on-time delivery calculation. Any
    systematic error in either dimension will cause sla_breach_count to
    deviate for these suppliers.

    Spot-checks sla_breach_count for 5 of the 10 renegotiated suppliers.
    """
    exp_df, _, _ = expected
    _, contracts, _, _ = raw_data

    dual_sups = (
        contracts.groupby("supplier_id")
        .filter(lambda x: len(x) > 1)["supplier_id"]
        .unique()
    )
    spot = sorted(dual_sups)[:5]

    failures = []
    for sid in spot:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue
        exp_bc = int(exp_row["sla_breach_count"].iloc[0])
        act_bc = int(act_row["sla_breach_count"].iloc[0])
        if act_bc != exp_bc:
            failures.append(
                f"{sid}: sla_breach_count {act_bc} != expected {exp_bc}. "
                "Contract SLA terms must be applied as of the purchase order date."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — Aggregate total penalty  (all traps)
# ---------------------------------------------------------------------------

def test_case_06_aggregate_breach_and_penalty(agent_summary, expected):
    """
    Aggregate total_sla_breach_count and total_penalty_assessed_usd in
    summary.json. Both are sensitive to on-time calculation accuracy,
    contract term selection, and fill rate netting.

    Note: penalty caps can dampen the effect of some breaches on the total
    penalty, so breach count and penalty are tested independently.
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
    Spot-checks all key metrics for 10 suppliers spanning different penalty
    tiers, replacement event volumes, and contract types. Compound errors from
    any trap will surface here.
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
    The bottom-3 and top-3 suppliers by composite_score must match ground truth.
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
    Verifies worst_on_time_supplier_id and worst_fill_rate_supplier_id in
    summary.json. These rankings shift when on-time or fill rate calculations
    are incorrect.
    """
    exp_df, _, _ = expected
    active = exp_df[exp_df["total_pos"] > 0]

    exp_worst_ot  = str(active.loc[active["on_time_delivery_rate"].idxmin(), "supplier_id"])
    exp_worst_nfr = str(active.loc[active["net_fill_rate"].idxmin(), "supplier_id"])

    assert agent_summary["worst_on_time_supplier_id"] == exp_worst_ot, \
        f"worst_on_time_supplier_id: got {agent_summary['worst_on_time_supplier_id']}, expected {exp_worst_ot}"

    assert agent_summary["worst_fill_rate_supplier_id"] == exp_worst_nfr, \
        f"worst_fill_rate_supplier_id: got {agent_summary['worst_fill_rate_supplier_id']}, expected {exp_worst_nfr}"
