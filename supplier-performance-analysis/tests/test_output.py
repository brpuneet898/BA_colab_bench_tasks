"""
Tests for the Q1 2024 Supplier Performance Scorecard task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — (warehouse_id, po_id) composite key.
            Joining on po_id alone inflates every supplier's PO count ~3x,
            scrambling all downstream metrics.

  Trap 2 — Open-ended contracts (NaT contract_effective_to).
            Twenty suppliers hold contracts with no expiry date. A pandas
            comparison `order_date <= NaT` returns False, silently zeroing
            all metrics for those suppliers. Correct implementation treats
            NaT as "no expiry".

  Trap 3 — Unsigned return quantities.
            Return rows in delivery_records.csv carry a positive
            quantity_received. A model that sums all quantity_received
            without negating Return rows inflates net fill quantities and
            on-time counts.

  Trap 4 — Purchase order amendments.
            300 POs have two rows in purchase_orders.csv: original
            (lower ordered_quantity) written first, amendment (higher
            ordered_quantity, later amendment_date) written after. Naive
            drop_duplicates(keep='first') silently uses the original lower
            quantity as the fill-rate denominator, inflating fill rates and
            suppressing SLA breaches.

  Trap 5 — contract_superseded_by NaT.
            All currently active contracts carry NaT in contract_superseded_by.
            A pandas comparison `order_date < NaT` returns False, silently
            excluding every active contract. Correct implementation treats NaT
            as "not yet superseded". Compounds with Trap 2: both NaT-guarded
            columns must be handled correctly.

  Trap 6 — Overlapping contract renegotiation.
            Ten suppliers have two overlapping contracts: old (lower rate,
            written first, contract_superseded_by = 2024-02-01) and new
            (higher rate, written second, contract_superseded_by = NaT).
            Correct rule: apply the non-superseded contract with the most
            recent contract_effective_from.

  Trap 7 — Escalating step-up penalty.
            After a supplier's 5th SLA breach (in order_date, po_id order),
            each further breaching PO is assessed at 2x penalty_rate_pct.

  Trap 8 — Rework delivery events.
            ~80 rows in delivery_records.csv carry delivery_type="Rework" with
            a positive quantity_received. These are internal quality events and
            must not be counted toward net fill rate or on-time quantity. The
            instruction defines the net quantity computation only for Primary
            (increase) and Return (decrease); Rework has no assigned role.
            A model that negates Returns then sums all quantity_received inflates
            per-supplier NFR by 0.5–2% and produces extra phantom "no-breach"
            POs.

  Trap 9 — Pre-deadline Return events.
            100 Return rows have received_date ≤ promised_delivery_date.
            These represent goods that arrived before the deadline and were
            immediately rejected on inspection. Return quantity is sized so
            that net-before-deadline = 0.98 × ordered_quantity (below ordered,
            above all SLA fill-rate thresholds). The PO was previously on-time
            with no breach; the pre-deadline return makes it a pure on-time
            breach. A model that evaluates on-time as "did primary delivery
            arrive by the deadline?" ignores that pre-deadline Returns reduce
            the net quantity before the deadline, producing an inflated
            on_time_delivery_rate and a lower sla_breach_count.
            The instruction states "net quantity received on or before the
            promised_delivery_date" — "net" explicitly includes pre-deadline
            Returns. The NFR definition's "regardless of when those events
            occurred" applies only to fill-rate, not to the date-gated on-time
            check.

  Trap 10 — Post-Q1 Return events.
            ~160 Return rows have received_date in April 2024 (includes both
            dedicated post-Q1 returns and early-return rows for POs with an
            April promised_delivery_date). The instruction says net fill rate
            is computed "regardless of when those events occurred." A model
            that scopes delivery events to Q1 dates before aggregating silently
            omits these April returns, producing inflated net_fill_rate values
            and a lower sla_breach_count. The scope restriction applies to
            order_date only, not to delivery event dates.

  Trap 11 — Return basis discrimination.
            Return rows carry return_basis "Rejection" (supplier quality
            failure) or "Operational" (buyer-initiated). The instruction's
            NFR definition says "Return deliveries arising from non-conforming
            goods decrease it" — Operational returns must be excluded from the
            net quantity computation. A model that negates all Return rows
            incorrectly reduces fill rates, pushing borderline POs across the
            SLA threshold and inflating sla_breach_count.

  Trap 12 — Regional penalty rate composite key.
            regional_penalty_rates.csv has 9 rows indexed by
            (warehouse_id, contract_tier). EMEA multipliers: 1.20–1.30;
            APAC: 1.10; AMER: 1.00. Effective penalty = penalty_rate_pct ×
            regional_penalty_multiplier. Joining on contract_tier alone fans
            out 3× per PO; ignoring the file under-counts EMEA/APAC penalties.

  Trap 13 — Chemicals unit mismatch (MT vs EA).
            purchase_orders.csv stores ordered_quantity in MT for Chemicals
            and in EA for all other categories (quantity_uom column).
            delivery_records.csv always records quantity_received in EA.
            product_uom_reference.csv provides the conversion: 1 MT = 1000 EA.
            A model that skips conversion produces fill rates ~1000× too large
            for Chemicals, suppressing all their SLA breaches.
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
                   "contract_superseded_by", "contract_tier",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap_usd",
                   "grace_period_days"]],
        on="supplier_id", how="left",
    )
    open_ended     = merged["contract_effective_to"].isna()
    not_superseded = merged["contract_superseded_by"].isna()
    applicable = merged[
        (merged["contract_effective_from"] <= merged["order_date"]) &
        (open_ended | (merged["order_date"] <= merged["contract_effective_to"])) &
        (not_superseded | (merged["order_date"] < merged["contract_superseded_by"]))
    ].copy()
    applicable = applicable.sort_values("contract_effective_from", ascending=False)
    applicable = applicable.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return applicable


def _compute_po_level(pos_with_contracts, deliveries, regional_rates):
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    d_with_deadline = deliveries.merge(
        pos_with_contracts[["warehouse_id", "po_id", "promised_delivery_date",
                            "ordered_quantity", "grace_period_days"]].drop_duplicates(),
        on=["warehouse_id", "po_id"], how="inner",
    )
    d_with_deadline = d_with_deadline.copy()
    d_with_deadline["effective_deadline"] = (
        d_with_deadline["promised_delivery_date"] +
        pd.to_timedelta(d_with_deadline["grace_period_days"].fillna(0).astype(int), unit="D")
    )
    d_before = d_with_deadline[
        d_with_deadline["received_date"] <= d_with_deadline["effective_deadline"]
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

    po = po.merge(
        regional_rates[["warehouse_id", "contract_tier", "regional_penalty_multiplier"]],
        on=["warehouse_id", "contract_tier"],
        how="left",
    )
    po["regional_penalty_multiplier"] = po["regional_penalty_multiplier"].fillna(1.0)

    po = po.sort_values(["supplier_id", "order_date", "po_id"]).copy()
    po["breach_rank"] = po.groupby("supplier_id")["sla_breach"].cumsum()
    escalated = po["sla_breach"] & (po["breach_rank"] > 5)
    base_rate = po["penalty_rate_pct"] * po["regional_penalty_multiplier"]
    po["penalty_rate_eff"] = base_rate.where(~escalated, base_rate * 2)
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


def _build_expected(suppliers, contracts, q1_pos, deliveries, regional_rates):
    keep = (deliveries["delivery_type"] == "Primary") | (
        (deliveries["delivery_type"] == "Return") &
        (deliveries["return_basis"] == "Rejection")
    )
    deliveries = deliveries[keep].copy()
    deliveries.loc[deliveries["delivery_type"] == "Return", "quantity_received"] *= -1

    pos_c  = _get_applicable_contract(q1_pos, contracts)
    po_lvl = _compute_po_level(pos_c, deliveries, regional_rates)
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
                             parse_dates=["contract_effective_from",
                                          "contract_effective_to",
                                          "contract_superseded_by"])
    pos        = pd.read_csv(DATA_DIR / "purchase_orders.csv",
                             parse_dates=["order_date", "promised_delivery_date",
                                          "amendment_date"])
    deliveries = pd.read_csv(DATA_DIR / "delivery_records.csv",
                             parse_dates=["received_date"])
    regional_rates = pd.read_csv(DATA_DIR / "regional_penalty_rates.csv")
    uom_ref        = pd.read_csv(DATA_DIR / "product_uom_reference.csv")
    return suppliers, contracts, pos, deliveries, regional_rates, uom_ref


@pytest.fixture(scope="module")
def q1_pos(raw_data):
    _, _, pos, _, _, uom_ref = raw_data
    pos = pos.copy()
    if "quantity_uom" in pos.columns:
        uom_map = uom_ref.set_index("quantity_uom")["units_per_ea_equivalent"].to_dict()
        pos["ordered_quantity"] = pos.apply(
            lambda r: round(r["ordered_quantity"] * uom_map.get(r["quantity_uom"], 1)),
            axis=1,
        )
    pos = pos.sort_values(["warehouse_id", "po_id", "amendment_date"],
                          ascending=[True, True, False])
    pos = pos.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return pos[(pos["order_date"] >= Q1_START) & (pos["order_date"] <= Q1_END)].copy()


@pytest.fixture(scope="module")
def expected(raw_data, q1_pos):
    suppliers, contracts, _, deliveries, regional_rates, _ = raw_data
    df, po_lvl, caps = _build_expected(suppliers, contracts, q1_pos, deliveries, regional_rates)
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
    """
    Verifies canonical data fingerprints: row counts, po_id non-uniqueness across
    warehouses (composite key trap), amendment pair count, open-ended contract count,
    dual-contract count, superseded contract count, unsigned Return quantities,
    presence of Rework events, pre-deadline Return events (early return trap),
    post-Q1 Return events, Chemicals MT unit mismatch, and regional penalty rates.
    """
    suppliers, contracts, pos, deliveries, regional_rates, uom_ref = raw_data

    assert len(suppliers) == 60, \
        f"suppliers.csv must not be modified (expected 60, got {len(suppliers)})"

    assert len(pos) == 3_300, \
        (f"purchase_orders.csv must not be modified "
         f"(expected 3300 rows = 3000 original + 300 amendment rows, got {len(pos)})")

    unique_po_ids = pos["po_id"].nunique()
    assert unique_po_ids == 1_000, \
        (f"po_id must not be globally unique — each warehouse uses its own sequence "
         f"(expected 1000 unique strings across 3300 rows, got {unique_po_ids})")

    amended_pairs = (pos.groupby(["warehouse_id", "po_id"]).size() > 1).sum()
    assert amended_pairs == 300, \
        (f"purchase_orders.csv must contain exactly 300 amended (warehouse_id, po_id) pairs "
         f"(each with an original row and an amendment row), got {amended_pairs}")

    nat_count = contracts["contract_effective_to"].isna().sum()
    assert nat_count == 20, \
        (f"supplier_contracts.csv must contain exactly 20 open-ended contracts "
         f"(contract_effective_to blank), got {nat_count}")

    dual_count = (contracts.groupby("supplier_id").size() > 1).sum()
    assert dual_count == 10, \
        f"supplier_contracts.csv must contain exactly 10 dual-contract suppliers, got {dual_count}"

    superseded_count = contracts["contract_superseded_by"].notna().sum()
    assert superseded_count == 10, \
        (f"supplier_contracts.csv must contain exactly 10 superseded contract rows "
         f"(contract_superseded_by non-blank — the OLD dual contracts only), got {superseded_count}")

    returns = deliveries[deliveries["delivery_type"] == "Return"]
    assert (returns["quantity_received"] > 0).all(), \
        ("All Return rows in delivery_records.csv must have positive quantity_received. "
         "Return quantities are unsigned — delivery_type indicates direction.")

    rework_count = (deliveries["delivery_type"] == "Rework").sum()
    assert rework_count >= 50, \
        (f"delivery_records.csv must contain Rework events (internal quality remediation rows); "
         f"expected at least 50, got {rework_count}")

    # Trap 9: pre-deadline Return events
    # Resolve amendments to get each PO's current promised_delivery_date
    pos_latest = (
        pos.sort_values(["warehouse_id", "po_id", "amendment_date"],
                        ascending=[True, True, False])
        .drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    )
    returns_with_deadline = returns.merge(
        pos_latest[["warehouse_id", "po_id", "promised_delivery_date"]],
        on=["warehouse_id", "po_id"],
        how="left",
    )
    early_return_count = (
        returns_with_deadline["received_date"] <= returns_with_deadline["promised_delivery_date"]
    ).sum()
    assert early_return_count >= 80, \
        (f"delivery_records.csv must contain at least 80 pre-deadline Return events "
         f"(received_date <= promised_delivery_date); got {early_return_count}. "
         f"These are quality-rejection returns that arrive before the deadline and "
         f"reduce net-before-deadline below ordered_quantity.")

    # Trap 10: post-Q1 Return events (received_date in April)
    post_q1_returns = (deliveries["delivery_type"] == "Return") & (deliveries["received_date"] > pd.Timestamp("2024-03-31"))
    assert post_q1_returns.sum() >= 60, \
        (f"delivery_records.csv must contain at least 60 post-Q1 Return events "
         f"(received_date > 2024-03-31); got {post_q1_returns.sum()}. "
         f"These April returns reduce net_fill_rate for Q1 POs per the instruction's "
         f"'regardless of when those events occurred' clause.")

    # Trap 11: return_basis column distinguishes Rejection vs Operational
    assert "return_basis" in deliveries.columns, \
        "delivery_records.csv must have a return_basis column (Rejection / Operational)"
    return_basis_vals = set(deliveries[deliveries["delivery_type"] == "Return"]["return_basis"].dropna().unique())
    assert "Rejection" in return_basis_vals and "Operational" in return_basis_vals, \
        (f"Return rows must have both Rejection and Operational in return_basis; "
         f"got {return_basis_vals}")

    # Trap 12: regional_penalty_rates.csv composite key (warehouse_id, contract_tier)
    assert len(regional_rates) == 9, \
        (f"regional_penalty_rates.csv must have 9 rows (3 warehouses × 3 tiers), "
         f"got {len(regional_rates)}")
    emea_min = regional_rates[regional_rates["warehouse_id"] == "EMEA"]["regional_penalty_multiplier"].min()
    assert emea_min > 1.0, \
        f"EMEA regional_penalty_multiplier must be > 1.0, got {emea_min}"

    # Trap 13: Chemicals ordered_quantity in MT (unit mismatch)
    assert "quantity_uom" in pos.columns, \
        "purchase_orders.csv must have a quantity_uom column (EA or MT)"
    mt_count = (pos["quantity_uom"] == "MT").sum()
    assert mt_count > 0, \
        (f"purchase_orders.csv must have MT-unit rows (Chemicals category); "
         f"got {mt_count} MT rows")


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
        f"supplier_scorecard.csv must have 60 rows, got {len(agent_scorecard)}"

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
    Trap 1 — All three warehouses use the same po_id sequence (PO-00001…PO-01000),
    so po_id is not globally unique. Joining purchase_orders to delivery_records on
    po_id alone creates cross-warehouse phantom matches, inflating delivery quantities
    ~3x. If the agent derives total_pos from a row count in the merged table it also
    appears ~3x inflated; the correct join key is (warehouse_id, po_id).

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
# Test 04 — Open-ended contract suppliers  (Trap 2: NaT effective_to + Trap 5: NaT superseded_by)
# ---------------------------------------------------------------------------

def test_case_04_open_ended_contract_suppliers(agent_scorecard, expected, raw_data):
    """
    Trap 2 — Twenty suppliers have open-ended contracts (contract_effective_to = NaT).
    Trap 5 — All active contracts also carry NaT in contract_superseded_by.
    A naive pandas date filter fails on both NaT columns, silently dropping all
    POs for affected suppliers. Both NaT-guarded conditions must be handled explicitly.

    Spot-checks on_time_delivery_rate for the first 5 open-ended-contract suppliers.
    """
    _, contracts, _, _, _, _ = raw_data
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
                "Open-ended contracts (NaT effective_to) and active contracts "
                "(NaT contract_superseded_by) must both be handled — "
                "the comparison `timestamp <= NaT` returns False in pandas."
            )
            continue

        exp_otr     = float(exp_row["on_time_delivery_rate"].iloc[0])
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
# Test 05 — Escalating penalty  (Trap 7: step-up after 5 breaches)
# ---------------------------------------------------------------------------

def test_case_05_escalating_penalty_high_breach_suppliers(agent_scorecard, expected, raw_data):
    """
    Trap 7 — After a supplier's 5th SLA breach (in order_date, po_id order),
    each further breaching PO is assessed at 2x the standard penalty_rate_pct.

    Spot-checks total_penalty_usd for the 5 non-NaT suppliers with the most
    SLA breaches (where the escalation effect is largest).
    """
    _, contracts, _, _, _, _ = raw_data
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

        exp_pen  = float(exp_row["total_penalty_usd"].iloc[0])
        act_pen  = float(act_row["total_penalty_usd"].iloc[0])
        breach_n = int(breach_counts[sid])

        if not math.isclose(act_pen, exp_pen, abs_tol=500):
            failures.append(
                f"{sid}: total_penalty_usd {act_pen:,.2f} != expected {exp_pen:,.2f} "
                f"(diff {act_pen - exp_pen:+,.2f}, {breach_n} SLA breaches). "
                "Each breaching PO from the 6th onwards must be assessed at 2x rate."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — Aggregate total penalty and breach count  (all traps)
# ---------------------------------------------------------------------------

def test_case_06_aggregate_breach_and_penalty(agent_summary, expected):
    """
    Aggregate total_sla_breach_count and total_penalty_assessed_usd.
    Sensitive to all traps: composite key, NaT handling, unsigned returns,
    PO amendments, superseded contracts, escalating penalty, and Rework
    events (Trap 8 inflates fill rates enough to suppress ~5 breaches,
    pushing the count outside the abs_tol=3 tolerance).
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
    Spot-checks all key metrics for the 10 suppliers with the highest PO count.
    Errors from any trap surface here: wrong join key (inflated delivery qty),
    unhandled NaT (missing POs), unsigned returns not negated (inflated fill rate),
    original instead of amended ordered_quantity (wrong denominator), or Rework
    events included in net fill rate calculations (inflated NFR by 0.5–2%).
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
    Ties broken by lowest supplier_id alphabetically.
    """
    exp_df, _, _ = expected
    active = exp_df[exp_df["total_pos"] > 0]

    exp_worst_ot  = str(active.sort_values(["on_time_delivery_rate",  "supplier_id"]).iloc[0]["supplier_id"])
    exp_worst_nfr = str(active.sort_values(["net_fill_rate", "supplier_id"]).iloc[0]["supplier_id"])

    assert agent_summary["worst_on_time_supplier_id"] == exp_worst_ot, \
        f"worst_on_time_supplier_id: got {agent_summary['worst_on_time_supplier_id']}, expected {exp_worst_ot}"

    assert agent_summary["worst_fill_rate_supplier_id"] == exp_worst_nfr, \
        f"worst_fill_rate_supplier_id: got {agent_summary['worst_fill_rate_supplier_id']}, expected {exp_worst_nfr}"


# ---------------------------------------------------------------------------
# Test 10 — Overlapping contract renegotiation + supersession  (Traps 5 & 6)
# ---------------------------------------------------------------------------

def test_case_10_contract_renegotiation_penalty(agent_scorecard, expected, raw_data):
    """
    Trap 6 — Ten suppliers had contracts renegotiated mid-quarter.
    OLD contract: effective_from=2024-01-01, contract_superseded_by=2024-02-01, lower rate.
    NEW contract: effective_from=2024-02-01, contract_superseded_by=NaT, higher rate.

    Trap 5 — The NEW contract's contract_superseded_by is NaT (still active).
    A naive pandas comparison `order_date < NaT` evaluates to False, incorrectly
    excluding the NEW contract and leaving Feb–Mar POs without applicable terms.

    The correct rule: treat NaT contract_superseded_by as "not yet superseded"
    and apply the non-superseded contract with the most recent effective_from.

    Spot-checks total_penalty_usd for the first 5 dual-contract suppliers.
    """
    _, contracts, _, _, _, _ = raw_data
    exp_df, _, _ = expected

    dual_suppliers = (
        contracts.groupby("supplier_id")
        .size()
        .loc[lambda s: s > 1]
        .index.tolist()
    )
    assert len(dual_suppliers) == 10

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
                "The renegotiated (NEW) contract has NaT in contract_superseded_by — "
                "it must be treated as still active, not excluded."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 11 — Chemicals unit conversion  (Trap 13: MT → EA)
# ---------------------------------------------------------------------------

def test_case_11_chemicals_unit_conversion(agent_scorecard, expected, raw_data):
    """
    Trap 13 — Chemicals ordered_quantity is stored in MT in purchase_orders.csv;
    all other categories use EA. delivery_records.csv always records in EA.
    A model that skips MT→EA conversion (× 1000 per product_uom_reference.csv)
    produces fill rates ~1000× too large for Chemicals, suppressing their SLA breaches.

    Spot-checks net_fill_rate for the 3 suppliers with the most Chemicals POs.
    """
    _, _, pos, _, _, uom_ref = raw_data
    exp_df, _, _ = expected

    uom_map = uom_ref.set_index("quantity_uom")["units_per_ea_equivalent"].to_dict()
    pos_conv = pos.copy()
    if "quantity_uom" in pos_conv.columns:
        pos_conv["ordered_quantity"] = pos_conv.apply(
            lambda r: round(r["ordered_quantity"] * uom_map.get(r["quantity_uom"], 1)), axis=1
        )
    pos_latest = (
        pos_conv
        .sort_values(["warehouse_id", "po_id", "amendment_date"], ascending=[True, True, False])
        .drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    )
    q1 = pos_latest[(pos_latest["order_date"] >= Q1_START) & (pos_latest["order_date"] <= Q1_END)]
    chem_counts = (
        q1[q1["product_category"] == "Chemicals"]
        .groupby("supplier_id").size()
        .sort_values(ascending=False)
    )
    spot = chem_counts.index[:3].tolist()

    failures = []
    for sid in spot:
        exp_row = exp_df[exp_df["supplier_id"] == sid]
        act_row = agent_scorecard[agent_scorecard["supplier_id"] == sid]
        if exp_row.empty or act_row.empty:
            continue
        exp_nfr = float(exp_row["net_fill_rate"].iloc[0])
        act_nfr_raw = act_row["net_fill_rate"].iloc[0]
        if pd.isna(act_nfr_raw):
            continue
        act_nfr = float(act_nfr_raw)
        if abs(act_nfr - exp_nfr) > 0.005:
            failures.append(
                f"{sid}: net_fill_rate {act_nfr:.4f} != expected {exp_nfr:.4f}. "
                f"Chemicals ordered_quantity is in MT — convert via product_uom_reference.csv "
                f"(1 MT = 1000 EA) before computing fill rates."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 12 — Regional penalty multiplier  (Trap 12: composite key)
# ---------------------------------------------------------------------------

def test_case_12_regional_penalty_multiplier(agent_scorecard, expected, raw_data):
    """
    Trap 12 — regional_penalty_rates.csv applies compliance multipliers indexed by
    (warehouse_id, contract_tier): EMEA 1.20–1.30, APAC 1.10, AMER 1.00.
    A model joining on contract_tier alone fans out 3× per PO; one that ignores
    the file uses base penalty_rate_pct without the regional uplift.

    Spot-checks total_penalty_usd for the 5 non-open-ended EMEA/APAC suppliers
    with the highest PO count.
    """
    _, contracts, pos, _, _, uom_ref = raw_data
    exp_df, _, _ = expected

    nat_suppliers = set(contracts[contracts["contract_effective_to"].isna()]["supplier_id"])
    pos_conv = pos.copy()
    if "quantity_uom" in pos_conv.columns:
        uom_map = uom_ref.set_index("quantity_uom")["units_per_ea_equivalent"].to_dict()
        pos_conv["ordered_quantity"] = pos_conv.apply(
            lambda r: round(r["ordered_quantity"] * uom_map.get(r["quantity_uom"], 1)), axis=1
        )
    pos_latest = (
        pos_conv
        .sort_values(["warehouse_id", "po_id", "amendment_date"], ascending=[True, True, False])
        .drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    )
    q1 = pos_latest[(pos_latest["order_date"] >= Q1_START) & (pos_latest["order_date"] <= Q1_END)]
    emea_apac = q1[
        q1["warehouse_id"].isin(["EMEA", "APAC"]) & ~q1["supplier_id"].isin(nat_suppliers)
    ]
    spot = emea_apac.groupby("supplier_id").size().sort_values(ascending=False).index[:5].tolist()

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
                f"Join regional_penalty_rates.csv on (warehouse_id, contract_tier) — "
                f"EMEA 1.20–1.30×, APAC 1.10×, AMER 1.00×."
            )

    assert not failures, "\n".join(failures)
