"""
Tests for the Q1 2024 Supplier Performance Scorecard task.

Ground truth is recomputed from the canonical data files baked into the image.
Three data characteristics are each isolated by at least one test:

  Trap 1 — (warehouse_id, po_id) composite key.
            Joining on po_id alone inflates every supplier's PO count ~3x,
            scrambling all metrics.

  Trap 2 — Return rows carry negative quantity_received.
            Filtering to quantity_received > 0 overstates net fill quantities
            and fill rates for affected suppliers.

  Trap 3 — Effective-dated contracts (SCD).
            Ten suppliers renegotiated in Feb; their January POs must use the
            old contract's fill_rate_sla_threshold and penalty_rate_pct.
            Using the latest contract for all Q1 POs misclassifies some Jan
            POs and inflates SLA breach counts for these suppliers.
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
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )
    on_time_date = (
        deliveries[deliveries["quantity_received"] > 0]
        .groupby(["warehouse_id", "po_id"], as_index=False)["received_date"]
        .max()
        .rename(columns={"received_date": "latest_received_date"})
    )
    po = pos_with_contracts.merge(net_qty,      on=["warehouse_id", "po_id"], how="left")
    po = po.merge(on_time_date, on=["warehouse_id", "po_id"], how="left")
    po["net_fill_rate_po"] = po["net_qty_received"] / po["ordered_quantity"]
    po["on_time"]          = po["latest_received_date"] <= po["promised_delivery_date"]
    po["sla_breach"]       = (po["net_fill_rate_po"] < po["fill_rate_sla_threshold"]) | ~po["on_time"]
    po["penalty_po"]       = po.apply(
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

    return_rows = (deliveries["quantity_received"] < 0).sum()
    assert return_rows == 213, \
        f"delivery_records.csv must contain exactly 213 return rows (quantity_received < 0), got {return_rows}"

    dual_contract_count = (contracts.groupby("supplier_id").size() > 1).sum()
    assert dual_contract_count == 10, \
        f"supplier_contracts.csv must contain exactly 10 suppliers with 2 contract rows, got {dual_contract_count}"


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

def test_case_03_po_count_per_supplier(agent_scorecard, expected, raw_data, q1_pos):
    """
    Trap 1 — Joining delivery_records to purchase_orders on po_id alone
    inflates every supplier's PO count ~3x. The composite key is (warehouse_id, po_id).

    Spot-checks total_pos for 5 suppliers whose po_id strings all appear in
    multiple warehouses (which is every supplier in this dataset).
    """
    exp_df, _, _ = expected
    _, _, pos, _ = raw_data

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
# Test 04 — Net fill rate for high-return suppliers  (Trap 2)
# ---------------------------------------------------------------------------

def test_case_04_net_fill_rate_returns(agent_scorecard, expected, raw_data):
    """
    Trap 2 — delivery_records contains return events with negative quantity_received.
    Net fill rate must sum ALL quantity_received values (including negatives).
    Filtering to quantity_received > 0 overstates fill quantities.

    Spot-checks net_fill_rate for the 5 suppliers with the largest total return volume.
    """
    exp_df, po_lvl, _ = expected
    _, _, _, deliveries = raw_data

    # Identify suppliers with highest total return volume
    return_vol = (
        deliveries[deliveries["quantity_received"] < 0]
        .groupby("supplier_id")["quantity_received"]
        .sum()
        .abs()
        .sort_values(ascending=False)
    )
    top_return_sups = return_vol.index[:5].tolist()

    failures = []
    for sid in top_return_sups:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue
        exp_nfr = float(exp_row["net_fill_rate"].iloc[0])
        act_nfr = float(act_row["net_fill_rate"].iloc[0])
        if abs(act_nfr - exp_nfr) > 0.005:
            failures.append(
                f"{sid}: net_fill_rate {act_nfr:.4f} != expected {exp_nfr:.4f} "
                f"(diff {act_nfr - exp_nfr:+.4f}). "
                "All quantity_received values must be summed algebraically, including returns."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — SLA breach count for dual-contract suppliers  (Trap 3)
# ---------------------------------------------------------------------------

def test_case_05_sla_breach_count_renegotiated_suppliers(agent_scorecard, expected, raw_data):
    """
    Trap 3 — Ten suppliers renegotiated their contracts effective 2024-02-01.
    The applicable fill_rate_sla_threshold and penalty_rate_pct for a PO is
    determined by the contract whose effective dates bracket the order_date.
    Using the latest contract for all Q1 POs misclassifies January POs for
    these ten suppliers, inflating their sla_breach_count.

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
                "Contract SLA terms must be applied as of the purchase order date, "
                "not from the most recent contract."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — Aggregate total penalty  (all traps)
# ---------------------------------------------------------------------------

def test_case_06_total_penalty_assessed(agent_summary, expected):
    """
    Aggregate total_penalty_assessed_usd in summary.json is sensitive to all
    three traps simultaneously. A model failing any one of them produces a value
    outside the 0.5% tolerance.
    """
    exp_df, _, _ = expected
    exp_total = round(float(exp_df["total_penalty_usd"].sum()), 2)
    act_total = float(agent_summary["total_penalty_assessed_usd"])
    assert math.isclose(act_total, exp_total, rel_tol=0.005), \
        f"total_penalty_assessed_usd: expected {exp_total:,.2f}, got {act_total:,.2f}"


# ---------------------------------------------------------------------------
# Test 07 — Row-level scorecard accuracy  (all traps)
# ---------------------------------------------------------------------------

def test_case_07_row_level_accuracy(agent_scorecard, expected):
    """
    Spot-checks all key metrics for 10 suppliers spanning different penalty
    tiers, return volumes, and contract types. Compound errors from any trap
    will surface here.
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
    Verifies worst_on_time_supplier_id, worst_fill_rate_supplier_id, and
    total_sla_breach_count in summary.json.
    """
    exp_df, _, _ = expected
    active = exp_df[exp_df["total_pos"] > 0]

    exp_worst_ot  = str(active.loc[active["on_time_delivery_rate"].idxmin(), "supplier_id"])
    exp_worst_nfr = str(active.loc[active["net_fill_rate"].idxmin(), "supplier_id"])
    exp_total_bc  = int(exp_df["sla_breach_count"].sum())

    assert agent_summary["worst_on_time_supplier_id"] == exp_worst_ot, \
        f"worst_on_time_supplier_id: got {agent_summary['worst_on_time_supplier_id']}, expected {exp_worst_ot}"

    assert agent_summary["worst_fill_rate_supplier_id"] == exp_worst_nfr, \
        f"worst_fill_rate_supplier_id: got {agent_summary['worst_fill_rate_supplier_id']}, expected {exp_worst_nfr}"

    assert abs(agent_summary["total_sla_breach_count"] - exp_total_bc) <= 5, \
        f"total_sla_breach_count: got {agent_summary['total_sla_breach_count']}, expected {exp_total_bc}"
