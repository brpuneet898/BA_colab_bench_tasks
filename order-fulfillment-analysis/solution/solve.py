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
event_date <= REPORT_AS_OF. Checking quantity by transaction_type shows it
is signed: positive for Shipment (and Backorder), negative for Return or
Cancellation (a credit against what went out) -- see the sign check in
net_fulfilled_by_line(). Summing quantity across Shipment/Return/
Cancellation rows therefore nets correctly with a plain sum.

shipment_headers.csv also contains a fourth transaction_type, Backorder,
recording quantity still awaiting fulfillment. It carries a positive
quantity like a Shipment row, but represents goods not yet sent -- the
customer has not received or kept anything against it. It must be excluded
from quantity fulfilled (net); summing quantity across every
transaction_type instead of restricting to Shipment/Return/Cancellation
folds backordered (not-yet-sent) quantity in as if it were fulfilled and
overstates fulfillment by whatever was backordered.

carrier_fulfillment_agreements.csv lists (carrier_id, warehouse_id) pairs
shipping under FOB Origin freight terms; every value present in its
freight_terms column is "FOB Origin" (checked in net_fulfilled_by_line()),
confirming it is a sparse exceptions list rather than a full record of
every pair's terms -- a pair absent from the file ships under the
unstated default, FOB Destination. Under standard Incoterms usage, FOB
Origin means title and fulfillment responsibility pass to the customer
the moment goods leave the warehouse dock, so a later Return or
Cancellation is a matter between the customer and the carrier, not a
failure of the warehouse's own fulfillment -- it does not net against
quantity fulfilled, the shipped quantity still counts in full. Under FOB
Destination (the default), the warehouse's responsibility continues until
delivery, so Return/Cancellation nets against fulfillment as usual.
carrier_id is not unique in this table (checked in net_fulfilled_by_line()),
so the lookup must join on (warehouse_id, carrier_id), never carrier_id
alone; collapsing a carrier's rows down to one (e.g. via
drop_duplicates(subset="carrier_id")) applies FOB Origin treatment to
every warehouse that carrier serves, not just the ones it actually
applies to.

Fulfillment region is the region assigned to the order's
assigned_warehouse_id, as recorded in warehouse_region_assignments.csv, in
effect on the order's order_date -- not the customer's own region column.
A warehouse can have more than one effective-dated segment in that file if
its assigned region changed over time; the currently-active segment for a
warehouse has a blank effective_to. Resolving region requires matching each
order's order_date against [effective_from, effective_to] per warehouse,
treating a blank effective_to as open-ended (still in effect) rather than
excluding it.
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
    assignments = pd.read_csv(
        DATA_DIR / "warehouse_region_assignments.csv",
        parse_dates=["effective_from", "effective_to"],
    )
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    fulfillment_agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")
    return assignments, orders, order_lines, headers, line_items, fulfillment_agreements


def resolve_order_regions(orders, assignments):
    """Region in effect on each order's order_date, per assigned_warehouse_id.

    A warehouse's region segments never overlap, so exactly one assignment
    row matches per order once effective_from/effective_to are compared
    against order_date -- but a blank effective_to must be treated as
    open-ended (still active), not as failing the upper-bound check.
    """
    merged = orders[["order_id", "order_date", "assigned_warehouse_id"]].merge(
        assignments, left_on="assigned_warehouse_id", right_on="warehouse_id"
    )
    in_effect = merged[
        (merged["effective_from"] <= merged["order_date"])
        & (merged["effective_to"].isna() | (merged["order_date"] <= merged["effective_to"]))
    ]
    return in_effect[["order_id", "region"]]


def scope_order_lines(orders, order_lines, assignments):
    """Order lines belonging to Q1-2024 orders, tagged with fulfillment region."""
    q1_orders = orders[(orders["order_date"] >= Q1_START) & (orders["order_date"] <= Q1_END)]
    order_regions = resolve_order_regions(q1_orders, assignments)
    scoped = order_lines.merge(order_regions, on="order_id")
    return scoped


FULFILLMENT_TYPES = ["Shipment", "Return", "Cancellation"]


def net_fulfilled_by_line(scoped_lines, headers, line_items, fulfillment_agreements):
    """Net fulfilled quantity per (order_id, line_id).

    line_items is joined to headers on the composite (warehouse_id,
    shipment_id) key -- shipment_id alone repeats across warehouses -- to
    recover transaction_type, carrier_id and event_date, then filtered to
    the cutoff. quantity is already signed correctly (positive for
    Shipment, negative for Return/Cancellation), so summing nets
    Shipment/Return/Cancellation for free; Backorder rows are dropped since
    they represent quantity not yet sent, not something the customer
    received and kept.

    freight_terms is then resolved via a left join on the composite
    (warehouse_id, carrier_id) key into carrier_fulfillment_agreements --
    carrier_id alone is not unique in that table -- defaulting to FOB
    Destination for any pair absent from that (sparse, exceptions-only)
    file. Under FOB Origin, a Return/Cancellation row is dropped before
    netting (the warehouse's responsibility ended at shipment, so it
    doesn't lose credit for a later return); the matching Shipment row is
    untouched either way.
    """
    events = line_items.merge(headers, on=["warehouse_id", "shipment_id"])

    sign_by_type = events.groupby("transaction_type")["quantity"].agg(["min", "max"])
    assert (sign_by_type.loc[["Shipment", "Backorder"], "min"] > 0).all(), (
        "expected Shipment/Backorder quantity to be positive"
    )
    assert (sign_by_type.loc[["Return", "Cancellation"], "max"] < 0).all(), (
        "expected Return/Cancellation quantity to be negative"
    )
    assert set(fulfillment_agreements["freight_terms"].unique()) == {"FOB Origin"}, (
        "expected carrier_fulfillment_agreements.csv to list only FOB Origin exceptions"
    )
    assert fulfillment_agreements["carrier_id"].duplicated().any(), (
        "expected carrier_id to repeat across warehouses in carrier_fulfillment_agreements.csv"
    )

    in_window = events[events["event_date"] <= REPORT_AS_OF].copy()
    in_window = in_window[in_window["transaction_type"].isin(FULFILLMENT_TYPES)]

    in_window = in_window.merge(
        fulfillment_agreements[["warehouse_id", "carrier_id", "freight_terms"]],
        on=["warehouse_id", "carrier_id"], how="left",
    )
    in_window["freight_terms"] = in_window["freight_terms"].fillna("FOB Destination")
    fob_origin_adjustment = (
        (in_window["freight_terms"] == "FOB Origin")
        & (in_window["transaction_type"] != "Shipment")
    )
    in_window = in_window[~fob_origin_adjustment]

    matched = scoped_lines[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    return matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )


def build_report(scoped_lines, fulfilled_by_line, assignments):
    lines = scoped_lines.merge(fulfilled_by_line, on=["order_id", "line_id"], how="left")
    lines["quantity_fulfilled_net"] = lines["quantity_fulfilled_net"].fillna(0)

    agg = lines.groupby("region").agg(
        quantity_ordered=("quantity_ordered", "sum"),
        quantity_fulfilled_net=("quantity_fulfilled_net", "sum"),
    )

    all_regions = assignments["region"].drop_duplicates().sort_values()
    agg = agg.reindex(all_regions, fill_value=0)

    agg["fill_rate"] = (agg["quantity_fulfilled_net"] / agg["quantity_ordered"]).round(4)
    agg = agg.reset_index().rename(columns={"index": "region"})
    agg["quantity_ordered"] = agg["quantity_ordered"].astype(int)
    agg["quantity_fulfilled_net"] = agg["quantity_fulfilled_net"].astype(int)
    return agg.sort_values("region").reset_index(drop=True)


def main():
    assignments, orders, order_lines, headers, line_items, fulfillment_agreements = load_data()

    scoped_lines = scope_order_lines(orders, order_lines, assignments)
    fulfilled_by_line = net_fulfilled_by_line(scoped_lines, headers, line_items, fulfillment_agreements)
    report = build_report(scoped_lines, fulfilled_by_line, assignments)

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
