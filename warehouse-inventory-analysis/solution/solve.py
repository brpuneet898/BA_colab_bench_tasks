"""
June 2024 Warehouse Inventory Position & COGS Report.

Inventory is costed FIFO: each purchase-order receipt opens a cost layer at
(product_id, warehouse_id), and outbound quantity is drawn from the oldest
eligible layer first. A customer return is its own event, dated at the
return's own receipt_date -- it opens a new layer wherever that date falls in
the FIFO queue relative to any receipts that landed in between, and its own
unit_cost column is NOT its valuation: it's valued at the unit cost of the
units that were originally shipped, which is the weighted-average cost that
the referenced original shipment (receipts.csv's original_shipment_id) itself
realized when it was processed earlier in this same simulation -- a value
that exists nowhere in the input files, only in the simulation's own state.

Transfers move physical stock between warehouses. The source warehouse's FIFO
layers are consumed at ship_date, same as a shipment. The destination
warehouse only gains a new layer -- at the FIFO cost drawn from the source --
once receive_date actually arrives. A transfer that has shipped but not yet
been received as of the report cutoff is in neither warehouse's on-hand: gone
from the source, not yet landed at the destination.

hold_status == 'on_hold' receipts are physically in the warehouse (they count
toward on_hand_qty and on_hand_value) but are not eligible for outbound
consumption or for available_to_promise_qty until released.

open_orders.csv holds demand reserved against a product/warehouse as of the
report cutoff: it reduces available_to_promise_qty but not on_hand_qty.
Some open orders already have a shipment booked against the same order_id
(shipments.csv carries its own order_id column) -- only the order's quantity
still outstanding after netting out that shipment counts toward
allocated_qty, not the order's original size.

A receipt's unit_cost column is only its invoice/base cost. Receipts that
share a po_batch_id (receipts.csv) were freight-forwarded together on one
shared invoice (freight_invoices.csv) -- that invoice's freight_amount must
be allocated across the batch's receipts in proportion to each receipt's own
extended value (quantity * unit_cost) and folded into that receipt's true
landed cost before it becomes a FIFO layer. Likewise, a transfer's
destination layer cost is the source consumption's weighted-average cost
plus that transfer's own handling_fee (transfer_fees.csv, present for most
but not all transfers) divided across its quantity -- moving stock between
warehouses has a real, capitalizable cost of its own.
"""
import json
from pathlib import Path

import pandas as pd

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")

JUNE_START = pd.Timestamp("2024-06-01")
JUNE_END = pd.Timestamp("2024-06-30")
CUTOFF = JUNE_END


def load_data():
    products = pd.read_csv(DATA_DIR / "products.csv")
    warehouses = pd.read_csv(DATA_DIR / "warehouses.csv")
    receipts = pd.read_csv(DATA_DIR / "receipts.csv", parse_dates=["receipt_date"])
    shipments = pd.read_csv(DATA_DIR / "shipments.csv", parse_dates=["ship_date"])
    transfers = pd.read_csv(
        DATA_DIR / "transfers.csv", parse_dates=["ship_date", "receive_date"]
    )
    open_orders = pd.read_csv(DATA_DIR / "open_orders.csv", parse_dates=["order_date"])
    freight_invoices = pd.read_csv(DATA_DIR / "freight_invoices.csv")
    transfer_fees = pd.read_csv(DATA_DIR / "transfer_fees.csv")
    return products, warehouses, receipts, shipments, transfers, open_orders, freight_invoices, transfer_fees


def apply_landed_cost(receipts, freight_invoices):
    """A receipt's own unit_cost is only its invoice/base cost. Receipts that
    share a po_batch_id had their batch's freight_amount allocated across
    them in proportion to each receipt's own extended value -- return a
    Series of true landed unit_cost, indexed like receipts.

    A batch's freight can be billed as more than one invoice line (e.g. base
    freight plus a separate surcharge) sharing the same po_batch_id -- the
    full landed cost needs all of a batch's freight, so lines must be summed
    per batch, not looked up as if po_batch_id were a unique key."""
    landed = receipts["unit_cost"].astype(float).copy()
    batched = receipts[receipts["po_batch_id"].notna()]
    if batched.empty:
        return landed

    extended_value = batched["quantity"] * batched["unit_cost"]
    batch_total_value = extended_value.groupby(batched["po_batch_id"]).transform("sum")
    freight_by_batch = freight_invoices.groupby("po_batch_id")["freight_amount"].sum()
    freight_amount = batched["po_batch_id"].map(freight_by_batch)
    allocated_freight_per_unit = (freight_amount * extended_value / batch_total_value) / batched["quantity"]
    landed.loc[batched.index] = batched["unit_cost"] + allocated_freight_per_unit
    return landed


def build_initial_layers(receipts, freight_invoices):
    """One FIFO layer per purchase-order receipt row, keyed by (product_id,
    warehouse_id), sorted by receipt_date (ties broken by receipt_id for
    determinism). Customer returns are NOT pre-loaded here -- they're only
    resolvable once the shipment they reference has been processed, so they
    join the global event queue instead."""
    receipts = receipts.copy()
    receipts["landed_unit_cost"] = apply_landed_cost(receipts, freight_invoices)

    layers = {}
    po_receipts = receipts[receipts["source"] == "purchase_order"]
    ordered = po_receipts.sort_values(["receipt_date", "receipt_id"])
    for row in ordered.itertuples(index=False):
        key = (row.product_id, row.warehouse_id)
        layers.setdefault(key, []).append(
            {
                "date": row.receipt_date,
                "qty_remaining": float(row.quantity),
                "unit_cost": float(row.landed_unit_cost),
                "hold_status": row.hold_status,
            }
        )
    return layers


def consume_fifo(layers_for_combo, event_date, qty_needed):
    """Consume qty_needed from the oldest released layers with date <=
    event_date. Returns the weighted-average unit cost of what was consumed."""
    remaining = qty_needed
    cost_total = 0.0
    for layer in layers_for_combo:
        if remaining <= 0:
            break
        if layer["hold_status"] != "released":
            continue
        if layer["date"] > event_date:
            continue
        if layer["qty_remaining"] <= 0:
            continue
        take = min(layer["qty_remaining"], remaining)
        layer["qty_remaining"] -= take
        cost_total += take * layer["unit_cost"]
        remaining -= take
    consumed = qty_needed - remaining
    avg_cost = cost_total / consumed if consumed > 0 else 0.0
    return avg_cost


def build_global_events(shipments, transfers, returns):
    events = []
    for row in shipments.itertuples(index=False):
        events.append(
            {
                "type": "shipment",
                "id": row.shipment_id,
                "key": (row.product_id, row.warehouse_id),
                "date": row.ship_date,
                "qty": float(row.quantity),
            }
        )
    for row in transfers.itertuples(index=False):
        events.append(
            {
                "type": "transfer",
                "id": row.transfer_id,
                "key": (row.product_id, row.source_warehouse_id),
                "dest_key": (row.product_id, row.dest_warehouse_id),
                "date": row.ship_date,
                "qty": float(row.quantity),
                "receive_date": row.receive_date,
            }
        )
    for row in returns.itertuples(index=False):
        events.append(
            {
                "type": "return",
                "id": row.receipt_id,
                "key": (row.product_id, row.warehouse_id),
                "date": row.receipt_date,
                "qty": float(row.quantity),
                "original_shipment_id": row.original_shipment_id,
            }
        )
    events.sort(key=lambda e: (e["date"], e["type"], e["id"]))
    return events


def run_simulation(receipts, shipments, transfers, freight_invoices, transfer_fees):
    layers = build_initial_layers(receipts, freight_invoices)
    returns = receipts[receipts["source"] == "customer_return"]
    events = build_global_events(shipments, transfers, returns)
    fee_by_transfer = transfer_fees.set_index("transfer_id")["handling_fee"]

    shipment_avg_cost = {}
    cogs_rows = []
    for event in events:
        if event["type"] == "return":
            avg_cost = shipment_avg_cost[event["original_shipment_id"]]
            dest_layers = layers.setdefault(event["key"], [])
            dest_layers.append(
                {
                    "date": event["date"],
                    "qty_remaining": event["qty"],
                    "unit_cost": avg_cost,
                    "hold_status": "released",
                }
            )
            continue

        combo_layers = layers.setdefault(event["key"], [])
        avg_cost = consume_fifo(combo_layers, event["date"], event["qty"])

        if event["type"] == "shipment":
            shipment_avg_cost[event["id"]] = avg_cost
            cogs_rows.append(
                {
                    "shipment_id": event["id"],
                    "product_id": event["key"][0],
                    "warehouse_id": event["key"][1],
                    "ship_date": event["date"],
                    "quantity": event["qty"],
                    "cogs_value": avg_cost * event["qty"],
                }
            )
        else:  # transfer
            if event["receive_date"] <= CUTOFF:
                handling_fee = fee_by_transfer.get(event["id"], 0.0)
                landed_cost = avg_cost + (handling_fee / event["qty"] if event["qty"] > 0 else 0.0)
                dest_layers = layers.setdefault(event["dest_key"], [])
                dest_layers.append(
                    {
                        "date": event["receive_date"],
                        "qty_remaining": event["qty"],
                        "unit_cost": landed_cost,
                        "hold_status": "released",
                    }
                )

    return layers, pd.DataFrame(cogs_rows)


def compute_allocated_by_combo(open_orders, shipments):
    """Each open order's allocated (still-outstanding) quantity is its own
    quantity minus whatever's already shipped against the same order_id --
    not the order's raw original size."""
    shipped_by_order = (
        shipments.loc[shipments["order_id"].notna()]
        .groupby("order_id")["quantity"]
        .sum()
    )
    already_shipped = open_orders["order_id"].map(shipped_by_order).fillna(0.0)
    outstanding = (open_orders["quantity"] - already_shipped).clip(lower=0.0)
    return (
        open_orders.assign(outstanding_qty=outstanding)
        .groupby(["product_id", "warehouse_id"])["outstanding_qty"]
        .sum()
    )


def build_inventory_position(layers, open_orders, shipments, active_combos):
    open_by_combo = compute_allocated_by_combo(open_orders, shipments)

    rows = []
    for pid, wh in active_combos:
        combo_layers = layers.get((pid, wh), [])
        on_hand_qty = sum(l["qty_remaining"] for l in combo_layers)
        on_hand_value = sum(l["qty_remaining"] * l["unit_cost"] for l in combo_layers)
        held_qty = sum(
            l["qty_remaining"] for l in combo_layers if l["hold_status"] != "released"
        )
        allocated_qty = float(open_by_combo.get((pid, wh), 0.0))
        atp_qty = on_hand_qty - allocated_qty - held_qty

        rows.append(
            {
                "product_id": pid,
                "warehouse_id": wh,
                "on_hand_qty": round(on_hand_qty, 2),
                "on_hand_value": round(on_hand_value, 2),
                "allocated_qty": round(allocated_qty, 2),
                "available_to_promise_qty": round(atp_qty, 2),
            }
        )

    return pd.DataFrame(rows).sort_values(["product_id", "warehouse_id"]).reset_index(drop=True)


def build_cogs_report(cogs_df):
    june = cogs_df[(cogs_df["ship_date"] >= JUNE_START) & (cogs_df["ship_date"] <= JUNE_END)].copy()
    june["quantity"] = june["quantity"].round(2)
    june["cogs_value"] = june["cogs_value"].round(2)
    return june.sort_values(["product_id", "warehouse_id", "shipment_id"]).reset_index(drop=True)


def main():
    products, warehouses, receipts, shipments, transfers, open_orders, freight_invoices, transfer_fees = load_data()

    active_combos = sorted(set(zip(receipts["product_id"], receipts["warehouse_id"])))

    layers, cogs_df = run_simulation(receipts, shipments, transfers, freight_invoices, transfer_fees)

    inventory_position = build_inventory_position(layers, open_orders, shipments, active_combos)
    inventory_position.to_csv(WORKSPACE_DIR / "inventory_position.csv", index=False)

    cogs_report = build_cogs_report(cogs_df)
    cogs_report.to_csv(WORKSPACE_DIR / "cogs_report.csv", index=False)

    summary = {
        "combo_count": int(len(inventory_position)),
        "total_on_hand_value": float(round(inventory_position["on_hand_value"].sum(), 2)),
        "total_available_to_promise_qty": float(round(inventory_position["available_to_promise_qty"].sum(), 2)),
        "total_june_cogs": float(round(cogs_report["cogs_value"].sum(), 2)),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
