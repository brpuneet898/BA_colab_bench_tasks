"""Oracle solution for the Q1 2024 Supplier Performance Scorecard task."""

import json
import numpy as np
import pandas as pd
from pathlib import Path

import os as _os
WORKSPACE_DIR = Path(_os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR      = Path(_os.environ.get("DATA_DIR", str(WORKSPACE_DIR / "data")))

SCORECARD_PATH = WORKSPACE_DIR / "supplier_scorecard.csv"
SUMMARY_PATH   = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END   = pd.Timestamp("2024-03-31")

_BUYER_PICKUP_INCOTERMS = {"FOB", "EXW", "CIF"}


def load_data():
    suppliers  = pd.read_csv(DATA_DIR / "suppliers.csv")
    contracts  = pd.read_csv(DATA_DIR / "supplier_contracts.csv",
                             parse_dates=["contract_effective_from",
                                          "contract_effective_to",
                                          "contract_superseded_by"])
    pos        = pd.read_csv(DATA_DIR / "purchase_orders.csv",
                             parse_dates=["order_date", "promised_delivery_date",
                                          "amendment_date"])
    deliveries = pd.read_csv(DATA_DIR / "delivery_records.csv",
                             parse_dates=["received_date", "ship_date"])
    regional_rates   = pd.read_csv(DATA_DIR / "regional_penalty_rates.csv")
    uom_ref          = pd.read_csv(DATA_DIR / "product_uom_reference.csv")
    escalation_rules = pd.read_csv(DATA_DIR / "escalation_rules.csv")
    fx_rates         = pd.read_csv(DATA_DIR / "fx_rates.csv")
    hierarchy        = pd.read_csv(DATA_DIR / "supplier_hierarchy.csv")
    return (suppliers, contracts, pos, deliveries, regional_rates, uom_ref,
            escalation_rules, fx_rates, hierarchy)


def convert_po_quantities(pos, uom_ref):
    """Convert ordered_quantity to EA for Chemicals POs stored in MT."""
    if "quantity_uom" not in pos.columns:
        return pos
    uom_map = uom_ref.set_index("quantity_uom")["units_per_ea_equivalent"].to_dict()
    pos = pos.copy()
    pos["ordered_quantity"] = pos.apply(
        lambda r: round(r["ordered_quantity"] * uom_map.get(r["quantity_uom"], 1)),
        axis=1,
    )
    return pos


def resolve_amendments(pos):
    """Keep only the most recently amended row for each (warehouse_id, po_id)."""
    pos = pos.sort_values(
        ["warehouse_id", "po_id", "amendment_date"],
        ascending=[True, True, False],
    )
    return pos.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first").copy()


def sign_returns(deliveries):
    """
    Retain accepted Primary rows and Rejection returns only.
    Negate Rejection return quantities for downstream netting.
    Rework events and Operational returns are excluded.
    """
    keep = (
        (deliveries["delivery_type"] == "Primary") &
        (deliveries["receipt_status"] == "accepted")
    ) | (
        (deliveries["delivery_type"] == "Return") &
        (deliveries["return_basis"] == "Rejection")
    )
    deliveries = deliveries[keep].copy()
    deliveries.loc[deliveries["delivery_type"] == "Return", "quantity_received"] *= -1
    return deliveries


def get_applicable_contract(pos, contracts):
    """
    For each PO, find the contract effective on order_date that has not been
    superseded. NaT in effective_to means open-ended; NaT in superseded_by
    means still active. Keep the most recently effective qualifying contract.
    """
    merged = pos.merge(
        contracts[["supplier_id", "contract_effective_from", "contract_effective_to",
                   "contract_superseded_by", "contract_tier",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap",
                   "currency", "grace_period_days"]],
        on="supplier_id",
        how="left",
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


def compute_po_level_metrics(pos_with_contracts, deliveries, regional_rates, escalation_rules):
    """
    Compute net fill rate, on-time flag, SLA breach, and raw penalty per PO.
    deliveries must already have Rejection return quantities negated.

    Escalation thresholds and multipliers come from escalation_rules (keyed by
    contract_tier) rather than a hardcoded value. Penalty caps are NOT applied
    here — they are applied supplier-by-supplier in build_scorecard.
    """
    esc_threshold_map  = escalation_rules.set_index("contract_tier")["breach_threshold"].to_dict()
    esc_multiplier_map = escalation_rules.set_index("contract_tier")["escalation_multiplier"].to_dict()

    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    d_with_deadline = deliveries.merge(
        pos_with_contracts[["warehouse_id", "po_id", "promised_delivery_date",
                            "ordered_quantity", "grace_period_days",
                            "incoterms"]].drop_duplicates(),
        on=["warehouse_id", "po_id"],
        how="inner",
    ).copy()
    grace = d_with_deadline["grace_period_days"].fillna(0).astype(int)
    d_with_deadline["effective_deadline"] = pd.to_datetime([
        np.busday_offset(d.date(), int(n), roll="forward")
        for d, n in zip(d_with_deadline["promised_delivery_date"], grace)
    ])
    buyer_pickup = d_with_deadline["incoterms"].isin(_BUYER_PICKUP_INCOTERMS)
    d_with_deadline["effective_date"] = d_with_deadline["received_date"].copy()
    d_with_deadline.loc[buyer_pickup, "effective_date"] = d_with_deadline.loc[buyer_pickup, "ship_date"]
    d_before = d_with_deadline[
        d_with_deadline["effective_date"] <= d_with_deadline["effective_deadline"]
    ]
    net_by_deadline = (
        d_before
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_by_deadline"})
    )

    po = pos_with_contracts.merge(net_qty,         on=["warehouse_id", "po_id"], how="left")
    po = po.merge(net_by_deadline, on=["warehouse_id", "po_id"], how="left")

    po["net_fill_rate_po"] = po["net_qty_received"].fillna(0) / po["ordered_quantity"]
    po["on_time"]          = po["net_qty_by_deadline"].fillna(0) >= po["ordered_quantity"]

    fill_breach   = po["net_fill_rate_po"] < po["fill_rate_sla_threshold"]
    ontime_breach = ~po["on_time"]
    po["sla_breach"] = fill_breach | ontime_breach

    fill_only = fill_breach & ~ontime_breach
    po["assessment_base"] = po["order_value_usd"]
    mask = po["sla_breach"] & fill_only
    po.loc[mask, "assessment_base"] = (
        po.loc[mask, "order_value_usd"] *
        (1.0 - po.loc[mask, "net_fill_rate_po"].clip(upper=1.0))
    )

    po = po.merge(
        regional_rates[["warehouse_id", "contract_tier", "regional_penalty_multiplier"]],
        on=["warehouse_id", "contract_tier"],
        how="left",
    )
    po["regional_penalty_multiplier"] = po["regional_penalty_multiplier"].fillna(1.0)

    po = po.sort_values(["supplier_id", "order_date", "po_id"]).copy()
    po["breach_rank"] = po.groupby("supplier_id")["sla_breach"].cumsum()

    # Tier-dependent escalation from lookup table
    po["esc_threshold"]  = po["contract_tier"].map(esc_threshold_map).fillna(5).astype(int)
    po["esc_multiplier"] = po["contract_tier"].map(esc_multiplier_map).fillna(2.0)
    escalated  = po["sla_breach"] & (po["breach_rank"] > po["esc_threshold"])
    base_rate  = po["penalty_rate_pct"] * po["regional_penalty_multiplier"]
    po["penalty_rate_eff"] = base_rate.where(~escalated, base_rate * po["esc_multiplier"])
    po["penalty_po"] = po.apply(
        lambda r: r["assessment_base"] * r["penalty_rate_eff"] if r["sla_breach"] else 0.0,
        axis=1,
    )
    return po


def _max_consecutive_streak(breach_series):
    streak = max_s = 0
    for v in breach_series:
        streak = (streak + 1) if v else 0
        if streak > max_s:
            max_s = streak
    return max_s


def build_scorecard(suppliers, po_level, contracts, fx_rates, hierarchy):
    """
    Two-pass cap application:
      Pass 1 — compute raw penalty and all non-penalty metrics per supplier.
      Pass 2 — apply caps:
        * Subsidiaries (in supplier_hierarchy.csv): sum raw penalties by parent,
          apply group_penalty_cap_usd, allocate back proportionally.
        * Independent suppliers: apply max_penalty_cap from most recent contract,
          converting from contract_currency to USD using the March 2024 FX rate.
    """
    # March 2024 FX rates for individual cap conversion
    march_fx = (
        fx_rates[fx_rates["reporting_month"] == "2024-03"]
        .set_index("currency")["usd_per_unit"]
        .to_dict()
    )

    # Group cap structures
    sub_to_parent = hierarchy.set_index("supplier_id")["parent_company_id"].to_dict()
    group_caps    = (
        hierarchy.groupby("parent_company_id")["group_penalty_cap_usd"]
        .first().to_dict()
    )

    # Latest contract per supplier (individual cap + currency)
    latest_ctrs = (
        contracts
        .sort_values("contract_effective_from", ascending=False)
        .drop_duplicates(subset="supplier_id", keep="first")
        .set_index("supplier_id")
    )

    # --- Pass 1: all metrics except total_penalty_usd ---
    raw_penalties = {}
    records       = {}

    for _, sup in suppliers.iterrows():
        sid     = sup["supplier_id"]
        name    = sup["supplier_name"]
        sup_pos = po_level[po_level["supplier_id"] == sid]

        if sup_pos.empty:
            raw_penalties[sid] = 0.0
            records[sid] = dict(
                supplier_id=sid, supplier_name=name,
                total_pos=0, on_time_delivery_rate=None,
                net_fill_rate=None, sla_breach_count=0,
                total_penalty_usd=0.0, composite_score=None,
                max_consecutive_breach_streak=0,
            )
            continue

        total_pos    = len(sup_pos)
        on_time_rate = round(sup_pos["on_time"].sum() / total_pos, 4)
        net_fill     = round(
            sup_pos["net_qty_received"].sum() / sup_pos["ordered_quantity"].sum(), 4
        )
        breach_count = int(sup_pos["sla_breach"].sum())
        raw_pen      = sup_pos["penalty_po"].sum()
        score        = round(on_time_rate * 0.6 + net_fill * 0.4, 4)
        streak       = _max_consecutive_streak(sup_pos["sla_breach"].tolist())

        raw_penalties[sid] = raw_pen
        records[sid] = dict(
            supplier_id=sid, supplier_name=name,
            total_pos=total_pos, on_time_delivery_rate=on_time_rate,
            net_fill_rate=net_fill, sla_breach_count=breach_count,
            total_penalty_usd=None,   # filled in pass 2
            composite_score=score,
            max_consecutive_breach_streak=streak,
        )

    # --- Pass 2a: group cap for subsidiaries (pro-rata) ---
    groups = {}
    for sid, parent in sub_to_parent.items():
        groups.setdefault(parent, []).append(sid)

    for parent, members in groups.items():
        group_raw    = sum(raw_penalties.get(sid, 0.0) for sid in members)
        gcap         = float(group_caps[parent])
        group_capped = min(group_raw, gcap)
        for sid in members:
            raw = raw_penalties.get(sid, 0.0)
            allocated = (group_capped * (raw / group_raw)) if group_raw > 0 else 0.0
            records[sid]["total_penalty_usd"] = round(allocated, 2)

    # --- Pass 2b: individual FX-converted cap for independent suppliers ---
    for sid, rec in records.items():
        if rec["total_penalty_usd"] is not None:
            continue   # subsidiary, already handled
        raw = raw_penalties.get(sid, 0.0)
        if sid in latest_ctrs.index:
            lc       = latest_ctrs.loc[sid]
            cap_val  = lc["max_penalty_cap"]
            currency = lc.get("currency", "USD")
            cap_usd  = float("inf") if pd.isna(cap_val) else float(cap_val) * march_fx.get(str(currency), 1.0)
        else:
            cap_usd = float("inf")
        rec["total_penalty_usd"] = round(min(raw, cap_usd), 2)

    df = pd.DataFrame(list(records.values()))
    df = df.sort_values(
        ["composite_score", "supplier_id"],
        ascending=[True, True],
        na_position="last",
    ).reset_index(drop=True)
    return df


def build_summary(scorecard):
    active = scorecard[scorecard["total_pos"] > 0]
    return {
        "total_penalty_assessed_usd":  round(float(scorecard["total_penalty_usd"].sum()), 2),
        "suppliers_meeting_all_sla":   int((active["sla_breach_count"] == 0).sum()),
        "worst_on_time_supplier_id":   str(active.sort_values(["on_time_delivery_rate", "supplier_id"]).iloc[0]["supplier_id"]),
        "worst_fill_rate_supplier_id": str(active.sort_values(["net_fill_rate", "supplier_id"]).iloc[0]["supplier_id"]),
        "total_sla_breach_count":      int(scorecard["sla_breach_count"].sum()),
    }


def main():
    (suppliers, contracts, pos, deliveries, regional_rates, uom_ref,
     escalation_rules, fx_rates, hierarchy) = load_data()

    pos        = convert_po_quantities(pos, uom_ref)
    pos        = resolve_amendments(pos)
    deliveries = sign_returns(deliveries)

    q1_pos = pos[(pos["order_date"] >= Q1_START) & (pos["order_date"] <= Q1_END)].copy()

    pos_with_contracts = get_applicable_contract(q1_pos, contracts)
    po_level           = compute_po_level_metrics(
        pos_with_contracts, deliveries, regional_rates, escalation_rules
    )
    scorecard = build_scorecard(suppliers, po_level, contracts, fx_rates, hierarchy)
    summary   = build_summary(scorecard)

    scorecard.to_csv(SCORECARD_PATH, index=False)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {SCORECARD_PATH}  ({len(scorecard)} rows)")
    print(f"Wrote {SUMMARY_PATH}")
    print(f"  total_penalty_assessed_usd : {summary['total_penalty_assessed_usd']:,.2f}")
    print(f"  suppliers_meeting_all_sla  : {summary['suppliers_meeting_all_sla']}")
    print(f"  total_sla_breach_count     : {summary['total_sla_breach_count']}")


if __name__ == "__main__":
    main()
