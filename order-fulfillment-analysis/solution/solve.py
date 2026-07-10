"""
Q1 2024 Order Fulfillment Rate Report.

line_id in order_lines.csv is a line number that resets to 1 within every
order, so it is not globally unique across the dataset. shipment_line_items
references lines via (order_id, line_id) -- joining on line_id alone would
create a many-to-many match across unrelated orders. The join key used
throughout is always the (order_id, line_id) pair.

Shipment activity is split across two files the way a real WMS documents
it: shipment_headers.csv (one row per physical document -- warehouse,
carrier, transaction_type, event_date) and shipment_line_items.csv (the
order-line-level quantity detail), joined on shipment_id. shipment_id is
only unique within a warehouse (each warehouse's system numbers its own
shipments independently, restarting at 1), not globally -- joining on
shipment_id alone fans line items out across every warehouse sharing that
number. The join key is always (warehouse_id, shipment_id).

Quantity fulfilled (net) per order line nets every Shipment, Return, and
Cancellation header recorded against that line, filtered to
event_date <= REPORT_AS_OF. quantity in shipment_line_items.csv is signed
the way this WMS actually records it: positive for a Shipment row,
negative for a Return or Cancellation row (a credit against what went
out). Summing quantity across Shipment/Return/Cancellation rows nets
correctly with a plain sum.

shipment_headers.csv also contains a fourth transaction_type, Backorder,
recording quantity still awaiting fulfillment. It carries a positive
quantity like a Shipment row, but represents goods not yet sent -- the
customer has not received or kept anything against it. It must be excluded
from quantity fulfilled (net); summing quantity across every
transaction_type instead of restricting to Shipment/Return/Cancellation
folds backordered (not-yet-sent) quantity in as if it were fulfilled and
overstates fulfillment by whatever was backordered.

Fulfillment region is the region of the warehouse assigned to the order
(orders.assigned_warehouse_id -> warehouses.region), not the customer's own
region column.
"""

import json
import pandas as pd
from pathlib import Path

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")

Q1_START = pd.Timestamp("2024-01-01")
Q1_END = pd.Timestamp("2024-03-31")
REPORT_AS_OF = pd.Timestamp("2024-04-15")


def load_data():
    warehouses = pd.read_csv(DATA_DIR / "warehouses.csv")
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    return warehouses, orders, order_lines, headers, line_items


def scope_order_lines(orders, order_lines, warehouses):
    """Order lines belonging to Q1-2024 orders, tagged with fulfillment region."""
    q1_orders = orders[(orders["order_date"] >= Q1_START) & (orders["order_date"] <= Q1_END)]
    q1_orders = q1_orders.merge(warehouses[["warehouse_id", "region"]],
                                 left_on="assigned_warehouse_id", right_on="warehouse_id")
    scoped = order_lines.merge(q1_orders[["order_id", "region"]], on="order_id")
    return scoped


FULFILLMENT_TYPES = ["Shipment", "Return", "Cancellation"]


def net_fulfilled_by_line(scoped_lines, headers, line_items):
    """Net fulfilled quantity per (order_id, line_id).

    line_items is joined to headers on the composite (warehouse_id,
    shipment_id) key -- shipment_id alone repeats across warehouses -- to
    recover transaction_type and event_date, then filtered to the cutoff.
    quantity is already signed correctly (positive for Shipment, negative
    for Return/Cancellation), so summing nets Shipment/Return/Cancellation
    for free; Backorder rows are dropped since they represent quantity not
    yet sent, not something the customer received and kept.
    """
    events = line_items.merge(headers, on=["warehouse_id", "shipment_id"])
    in_window = events[events["event_date"] <= REPORT_AS_OF].copy()
    in_window = in_window[in_window["transaction_type"].isin(FULFILLMENT_TYPES)]

    matched = scoped_lines[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    return matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )


def build_report(scoped_lines, fulfilled_by_line, warehouses):
    lines = scoped_lines.merge(fulfilled_by_line, on=["order_id", "line_id"], how="left")
    lines["quantity_fulfilled_net"] = lines["quantity_fulfilled_net"].fillna(0)

    agg = lines.groupby("region").agg(
        quantity_ordered=("quantity_ordered", "sum"),
        quantity_fulfilled_net=("quantity_fulfilled_net", "sum"),
    )

    all_regions = warehouses["region"].drop_duplicates().sort_values()
    agg = agg.reindex(all_regions, fill_value=0)

    agg["fill_rate"] = (agg["quantity_fulfilled_net"] / agg["quantity_ordered"]).round(4)
    agg = agg.reset_index().rename(columns={"index": "region"})
    agg["quantity_ordered"] = agg["quantity_ordered"].astype(int)
    agg["quantity_fulfilled_net"] = agg["quantity_fulfilled_net"].astype(int)
    return agg.sort_values("region").reset_index(drop=True)


def main():
    warehouses, orders, order_lines, headers, line_items = load_data()

    scoped_lines = scope_order_lines(orders, order_lines, warehouses)
    fulfilled_by_line = net_fulfilled_by_line(scoped_lines, headers, line_items)
    report = build_report(scoped_lines, fulfilled_by_line, warehouses)

    report.to_csv(WORKSPACE_DIR / "region_fulfillment_report.csv", index=False)

    total_ordered = int(report["quantity_ordered"].sum())
    total_fulfilled = int(report["quantity_fulfilled_net"].sum())

    summary = {
        "total_quantity_ordered": total_ordered,
        "total_quantity_fulfilled_net": total_fulfilled,
        "overall_fill_rate": float(round(total_fulfilled / total_ordered, 4)),
        "best_performing_region": str(report.loc[report["fill_rate"].idxmax(), "region"]),
        "worst_performing_region": str(report.loc[report["fill_rate"].idxmin(), "region"]),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
