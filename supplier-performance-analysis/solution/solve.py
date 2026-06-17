"""Oracle solution for the Q1 2024 Supplier Performance Scorecard task."""

import json
import pandas as pd
from pathlib import Path

DATA_DIR      = Path("/workspace/data")
WORKSPACE_DIR = Path("/workspace")

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
    """Return pos with the contract row in effect on each PO's order_date merged in."""
    merged = pos.merge(
        contracts[["supplier_id", "contract_effective_from", "contract_effective_to",
                   "fill_rate_sla_threshold", "penalty_rate_pct", "max_penalty_cap_usd"]],
        on="supplier_id",
        how="left",
    )
    applicable = merged[
        (merged["contract_effective_from"] <= merged["order_date"]) &
        (merged["order_date"] <= merged["contract_effective_to"])
    ].copy()
    applicable = applicable.sort_values("contract_effective_from", ascending=False)
    applicable = applicable.drop_duplicates(subset=["warehouse_id", "po_id"], keep="first")
    return applicable


def compute_po_level_metrics(pos_with_contracts, deliveries):
    """Compute net fill rate, on-time flag, SLA breach, and penalty per PO."""
    net_qty = (
        deliveries
        .groupby(["warehouse_id", "po_id"], as_index=False)["quantity_received"]
        .sum()
        .rename(columns={"quantity_received": "net_qty_received"})
    )

    # On-time date: latest received_date among positive-quantity delivery rows
    on_time_date = (
        deliveries[deliveries["quantity_received"] > 0]
        .groupby(["warehouse_id", "po_id"], as_index=False)["received_date"]
        .max()
        .rename(columns={"received_date": "latest_received_date"})
    )

    po = pos_with_contracts.merge(net_qty,     on=["warehouse_id", "po_id"], how="left")
    po = po.merge(on_time_date, on=["warehouse_id", "po_id"], how="left")

    po["net_fill_rate_po"] = po["net_qty_received"] / po["ordered_quantity"]
    po["on_time"]          = po["latest_received_date"] <= po["promised_delivery_date"]

    fill_breach   = po["net_fill_rate_po"] < po["fill_rate_sla_threshold"]
    ontime_breach = ~po["on_time"]
    po["sla_breach"] = fill_breach | ontime_breach
    po["penalty_po"]  = po.apply(
        lambda r: r["order_value_usd"] * r["penalty_rate_pct"] if r["sla_breach"] else 0.0,
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
