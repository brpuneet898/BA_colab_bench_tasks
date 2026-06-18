"""Oracle solution for the Q1 2024 Supplier Performance Scorecard task."""

import json
import pandas as pd
from pathlib import Path

import os as _os
WORKSPACE_DIR = Path(_os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR      = Path(_os.environ.get("DATA_DIR", str(WORKSPACE_DIR / "data")))

SCORECARD_PATH = WORKSPACE_DIR / "supplier_scorecard.csv"
SUMMARY_PATH   = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END   = pd.Timestamp("2024-03-31")


def load_data():
    suppliers  = pd.read_csv(DATA_DIR / "suppliers.csv")
    contracts  = pd.read_csv(DATA_DIR / "supplier_contracts.csv",
                             parse_dates=["contract_effective_from", "contract_effective_to"])
    pos        = pd.read_csv(DATA_DIR / "purchase_orders.csv",
                             parse_dates=["order_date", "promised_delivery_date"])
    deliveries = pd.read_csv(DATA_DIR / "delivery_records.csv",
                             parse_dates=["received_date"])
    return suppliers, contracts, pos, deliveries


def get_applicable_contract(pos, contracts):
    """Attach the contract row in effect on each PO's order_date.

    Open-ended contracts have contract_effective_to = NaT (no expiry).
    Pandas evaluates `order_date <= NaT` as False, so NaT must be handled
    explicitly: treat it as always in effect after contract_effective_from.
    """
    merged = pos.merge(
        contracts[["supplier_id", "contract_effective_from", "contract_effective_to",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap_usd"]],
        on="supplier_id",
        how="left",
    )
    open_ended = merged["contract_effective_to"].isna()
    applicable = merged[
        (merged["contract_effective_from"] <= merged["order_date"]) &
        (open_ended | (merged["order_date"] <= merged["contract_effective_to"]))
    ].copy()
    applicable = applicable.sort_values("contract_effective_from", ascending=False)
    applicable = applicable.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return applicable


def compute_po_level_metrics(pos_with_contracts, deliveries):
    """Compute net fill rate, on-time flag, SLA breach, and penalty per PO.

    Penalty uses an escalating rate: once a supplier's running SLA breach
    count (sorted by order_date) exceeds 5, each further breaching PO is
    assessed at 2x the standard penalty_rate_pct.
    """
    # Net fill rate: algebraic sum of ALL delivery events regardless of date
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    # On-time: total received on or before promised_delivery_date >= ordered_quantity
    d_with_deadline = deliveries.merge(
        pos_with_contracts[["warehouse_id", "po_id", "promised_delivery_date",
                            "ordered_quantity"]].drop_duplicates(),
        on=["warehouse_id", "po_id"],
        how="inner",
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

    fill_breach   = po["net_fill_rate_po"] < po["fill_rate_sla_threshold"]
    ontime_breach = ~po["on_time"]
    po["sla_breach"] = fill_breach | ontime_breach

    # Escalating penalty: sort by supplier + order_date + po_id (tiebreaker),
    # compute running breach count per supplier. Apply 2x rate after the 5th.
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


def compute_penalty_cap(contracts):
    """Return max_penalty_cap_usd per supplier from the most recently effective contract."""
    latest = (
        contracts
        .sort_values("contract_effective_from", ascending=False)
        .drop_duplicates(subset="supplier_id", keep="first")
        [["supplier_id", "max_penalty_cap_usd"]]
    )
    return latest.set_index("supplier_id")["max_penalty_cap_usd"].to_dict()


def build_scorecard(suppliers, po_level, penalty_caps):
    records = []
    for _, sup in suppliers.iterrows():
        sid  = sup["supplier_id"]
        name = sup["supplier_name"]
        sup_pos = po_level[po_level["supplier_id"] == sid]

        if sup_pos.empty:
            records.append(dict(
                supplier_id=sid, supplier_name=name,
                total_pos=0, on_time_delivery_rate=None,
                net_fill_rate=None, sla_breach_count=0,
                total_penalty_usd=0.0, composite_score=None,
            ))
            continue

        total_pos    = len(sup_pos)
        on_time_rate = round(sup_pos["on_time"].sum() / total_pos, 4)
        net_fill     = round(
            sup_pos["net_qty_received"].sum() / sup_pos["ordered_quantity"].sum(), 4
        )
        breach_count = int(sup_pos["sla_breach"].sum())
        raw_penalty  = sup_pos["penalty_po"].sum()
        cap          = penalty_caps.get(sid, float("inf"))
        penalty      = round(min(raw_penalty, cap), 2)
        score        = round(on_time_rate * 0.6 + net_fill * 0.4, 4)

        records.append(dict(
            supplier_id=sid, supplier_name=name,
            total_pos=total_pos,
            on_time_delivery_rate=on_time_rate,
            net_fill_rate=net_fill,
            sla_breach_count=breach_count,
            total_penalty_usd=penalty,
            composite_score=score,
        ))

    df = pd.DataFrame(records)
    df = df.sort_values(
        ["composite_score", "supplier_id"],
        ascending=[True, True],
        na_position="last",
    ).reset_index(drop=True)
    return df


def build_summary(scorecard):
    active = scorecard[scorecard["total_pos"] > 0]
    return {
        "total_penalty_assessed_usd": round(float(scorecard["total_penalty_usd"].sum()), 2),
        "suppliers_meeting_all_sla":  int((active["sla_breach_count"] == 0).sum()),
        "worst_on_time_supplier_id":  str(active.loc[active["on_time_delivery_rate"].idxmin(), "supplier_id"]),
        "worst_fill_rate_supplier_id": str(active.loc[active["net_fill_rate"].idxmin(), "supplier_id"]),
        "total_sla_breach_count":     int(scorecard["sla_breach_count"].sum()),
    }


def main():
    suppliers, contracts, pos, deliveries = load_data()

    q1_pos = pos[(pos["order_date"] >= Q1_START) & (pos["order_date"] <= Q1_END)].copy()

    pos_with_contracts = get_applicable_contract(q1_pos, contracts)
    po_level           = compute_po_level_metrics(pos_with_contracts, deliveries)
    penalty_caps       = compute_penalty_cap(contracts)

    scorecard = build_scorecard(suppliers, po_level, penalty_caps)
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
