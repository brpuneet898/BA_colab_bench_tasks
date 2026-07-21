"""
Tests for the June 2024 warehouse inventory position & COGS report.

Reasoning challenges this suite verifies (none of these are named in
instruction.md -- each is discoverable by inspecting the raw data):

1. In-transit exclusion -- transfers.csv carries separate ship_date and
   receive_date. A transfer that has shipped by the report cutoff but not
   yet been received is gone from the source warehouse's on-hand but has NOT
   yet landed at the destination -- it belongs to neither location's
   on_hand_qty/on_hand_value as of June 30. Treating a transfer as an
   instantaneous single-date event overstates the destination's on-hand.

2. FIFO re-insertion order -- a customer return (receipts.csv,
   source == "customer_return") opens a new cost layer dated at its OWN
   receipt_date, not at the original shipment's date. Whenever another
   receipt landed between the original shipment and the return, "restoring"
   the return to the original layer (instead of inserting it at its actual
   date) changes which units later June shipments draw from, and therefore
   their cogs_value.

3. Allocation netting -- available_to_promise_qty = on_hand_qty -
   allocated_qty - held quantity. on_hand_qty itself is NOT reduced by
   allocation.

4. Quality hold (same-axis false friend) -- receipts.csv rows with
   hold_status == "on_hold" are physically received (they count toward
   on_hand_qty/on_hand_value) but are not eligible for outbound consumption
   and must be excluded from available_to_promise_qty.

5. Partial order fulfillment -- shipments.csv carries its own order_id
   column. A slice of open_orders.csv rows already have a shipment booked
   against the same order_id; allocated_qty for that order is its quantity
   minus whatever's already shipped against it, not the order's raw size.
   This is only visible by joining the two files on order_id -- nothing in
   either file's own .describe()/.value_counts() shows it.

Ground truth is computed inline below, directly from /workspace/data/*.csv,
via an independent re-implementation of the FIFO simulation -- never from a
pre-baked constant and never by importing solution/solve.py.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

WORKSPACE_DIR = Path("/workspace") if Path("/workspace").exists() else Path(__file__).parent.parent
DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
    else Path(__file__).parent.parent / "environment" / "data"

JUNE_START = pd.Timestamp("2024-06-01")
JUNE_END = pd.Timestamp("2024-06-30")
CUTOFF = JUNE_END

INVENTORY_POSITION_COLS = {
    "product_id", "warehouse_id", "on_hand_qty", "on_hand_value",
    "allocated_qty", "available_to_promise_qty",
}
COGS_REPORT_COLS = {
    "shipment_id", "product_id", "warehouse_id", "ship_date", "quantity", "cogs_value",
}


@pytest.fixture(scope="module")
def ground_truth():
    receipts = pd.read_csv(DATA_DIR / "receipts.csv", parse_dates=["receipt_date"])
    shipments = pd.read_csv(DATA_DIR / "shipments.csv", parse_dates=["ship_date"])
    transfers = pd.read_csv(DATA_DIR / "transfers.csv", parse_dates=["ship_date", "receive_date"])
    open_orders = pd.read_csv(DATA_DIR / "open_orders.csv", parse_dates=["order_date"])
    linked_shipments = shipments[shipments["order_id"].notna()]

    # --- build initial FIFO layers, one per receipt row ---
    layers = {}
    for row in receipts.sort_values(["receipt_date", "receipt_id"]).itertuples(index=False):
        key = (row.product_id, row.warehouse_id)
        layers.setdefault(key, []).append({
            "date": row.receipt_date, "qty_remaining": float(row.quantity),
            "unit_cost": float(row.unit_cost), "hold_status": row.hold_status,
        })

    # --- merge shipments + transfers into one global chronological event queue ---
    events = []
    for row in shipments.itertuples(index=False):
        events.append({"type": "shipment", "key": (row.product_id, row.warehouse_id),
                        "date": row.ship_date, "qty": float(row.quantity), "id": row.shipment_id})
    for row in transfers.itertuples(index=False):
        events.append({"type": "transfer", "key": (row.product_id, row.source_warehouse_id),
                        "dest_key": (row.product_id, row.dest_warehouse_id),
                        "date": row.ship_date, "qty": float(row.quantity),
                        "receive_date": row.receive_date, "id": row.transfer_id})
    events.sort(key=lambda e: (e["date"], e["type"], e["id"]))

    def consume(combo_layers, event_date, qty_needed):
        remaining = qty_needed
        cost_total = 0.0
        for layer in combo_layers:
            if remaining <= 0:
                break
            if layer["hold_status"] != "released" or layer["date"] > event_date or layer["qty_remaining"] <= 0:
                continue
            take = min(layer["qty_remaining"], remaining)
            layer["qty_remaining"] -= take
            cost_total += take * layer["unit_cost"]
            remaining -= take
        consumed = qty_needed - remaining
        return cost_total / consumed if consumed > 0 else 0.0

    cogs_rows = []
    for event in events:
        combo_layers = layers.setdefault(event["key"], [])
        avg_cost = consume(combo_layers, event["date"], event["qty"])
        if event["type"] == "shipment":
            cogs_rows.append({
                "shipment_id": event["id"], "product_id": event["key"][0],
                "warehouse_id": event["key"][1], "ship_date": event["date"],
                "quantity": event["qty"], "cogs_value": avg_cost * event["qty"],
            })
        elif event["receive_date"] <= CUTOFF:
            layers.setdefault(event["dest_key"], []).append({
                "date": event["receive_date"], "qty_remaining": event["qty"],
                "unit_cost": avg_cost, "hold_status": "released",
            })

    active_combos = sorted(set(zip(receipts.product_id, receipts.warehouse_id)))

    shipped_by_order = linked_shipments.groupby("order_id").quantity.sum()
    already_shipped = open_orders["order_id"].map(shipped_by_order).fillna(0.0)
    outstanding_qty = (open_orders["quantity"] - already_shipped).clip(lower=0.0)
    open_by_combo = (
        open_orders.assign(outstanding_qty=outstanding_qty)
        .groupby(["product_id", "warehouse_id"]).outstanding_qty.sum()
    )

    pos_rows = []
    for pid, wh in active_combos:
        combo_layers = layers.get((pid, wh), [])
        on_hand_qty = sum(l["qty_remaining"] for l in combo_layers)
        on_hand_value = sum(l["qty_remaining"] * l["unit_cost"] for l in combo_layers)
        held_qty = sum(l["qty_remaining"] for l in combo_layers if l["hold_status"] != "released")
        allocated_qty = float(open_by_combo.get((pid, wh), 0.0))
        pos_rows.append({
            "product_id": pid, "warehouse_id": wh,
            "on_hand_qty": round(on_hand_qty, 2), "on_hand_value": round(on_hand_value, 2),
            "allocated_qty": round(allocated_qty, 2),
            "available_to_promise_qty": round(on_hand_qty - allocated_qty - held_qty, 2),
        })
    inventory_position = pd.DataFrame(pos_rows).sort_values(["product_id", "warehouse_id"]).reset_index(drop=True)

    cogs_df = pd.DataFrame(cogs_rows)
    june_cogs = cogs_df[(cogs_df.ship_date >= JUNE_START) & (cogs_df.ship_date <= JUNE_END)].copy()
    june_cogs["quantity"] = june_cogs["quantity"].round(2)
    june_cogs["cogs_value"] = june_cogs["cogs_value"].round(2)
    june_cogs = june_cogs.sort_values(["product_id", "warehouse_id", "shipment_id"]).reset_index(drop=True)

    # --- trap-affected id sets, derived from raw data properties ---
    intransit = transfers[(transfers.ship_date <= CUTOFF) & (transfers.receive_date > CUTOFF)]
    intransit_dest_ids = sorted(set(zip(intransit.product_id, intransit.dest_warehouse_id)))

    return_combo_ids = sorted(set(zip(
        receipts[receipts.source == "customer_return"].product_id,
        receipts[receipts.source == "customer_return"].warehouse_id,
    )))

    allocation_ids = sorted(set(zip(open_orders.product_id, open_orders.warehouse_id)))

    hold_ids = sorted(set(zip(
        receipts[receipts.hold_status == "on_hold"].product_id,
        receipts[receipts.hold_status == "on_hold"].warehouse_id,
    )))

    partial_orders = open_orders[open_orders.order_id.isin(set(linked_shipments.order_id))]
    partial_order_ids = sorted(set(zip(partial_orders.product_id, partial_orders.warehouse_id)))

    return dict(
        inventory_position=inventory_position, cogs_report=june_cogs,
        intransit_dest_ids=intransit_dest_ids, return_combo_ids=return_combo_ids,
        allocation_ids=allocation_ids, hold_ids=hold_ids, partial_order_ids=partial_order_ids,
        receipts=receipts, transfers=transfers, open_orders=open_orders,
        shipments=shipments,
    )


def _load_csv(name):
    path = WORKSPACE_DIR / name
    assert path.exists(), f"Expected output file missing: {name}"
    return pd.read_csv(path)


def test_case_01_output_files_exist():
    for name in ["inventory_position.csv", "cogs_report.csv", "summary.json"]:
        assert (WORKSPACE_DIR / name).exists(), f"Missing required output: {name}"


def test_case_02_inventory_position_structure(ground_truth):
    df = _load_csv("inventory_position.csv")
    missing = INVENTORY_POSITION_COLS - set(df.columns)
    assert not missing, f"inventory_position.csv missing columns: {missing}"
    expected_n = len(ground_truth["inventory_position"])
    assert len(df) == expected_n, (
        f"inventory_position.csv must have one row per (product_id, warehouse_id) combo "
        f"with any receipt activity ({expected_n}), got {len(df)}"
    )
    got = set(zip(df.product_id, df.warehouse_id))
    expected = set(zip(ground_truth["inventory_position"].product_id, ground_truth["inventory_position"].warehouse_id))
    assert got == expected, "inventory_position.csv combos don't match the active (product, warehouse) set"


def test_case_03_cogs_report_structure(ground_truth):
    df = _load_csv("cogs_report.csv")
    missing = COGS_REPORT_COLS - set(df.columns)
    assert not missing, f"cogs_report.csv missing columns: {missing}"
    expected_n = len(ground_truth["cogs_report"])
    assert len(df) == expected_n, (
        f"cogs_report.csv must have exactly one row per shipment dated in June 2024 "
        f"({expected_n}), got {len(df)}. A different count usually means the June date "
        f"filter was applied to the wrong column or window."
    )


def test_case_04_intransit_exclusion(ground_truth):
    """A transfer that shipped by June 30 but hasn't been received yet must
    not appear in the destination warehouse's on-hand."""
    df = _load_csv("inventory_position.csv").set_index(["product_id", "warehouse_id"])
    gt = ground_truth["inventory_position"].set_index(["product_id", "warehouse_id"])
    ids = ground_truth["intransit_dest_ids"]
    assert len(ids) >= 10, "sanity: expected a meaningful in-transit-destination cohort in the fixture"

    deltas = (df.loc[ids, "on_hand_qty"] - gt.loc[ids, "on_hand_qty"]).abs()
    n_within = (deltas <= 5.0).sum()
    assert n_within / len(ids) >= 0.80, (
        f"on_hand_qty wrong for {len(ids) - n_within}/{len(ids)} destination combos of a "
        f"transfer still in transit at the June 30 cutoff -- check whether on-hand is being "
        f"credited at ship_date instead of receive_date."
    )


def test_case_05_return_reinsertion_cogs(ground_truth):
    """A customer return opens a new FIFO layer dated at its own receipt_date,
    not the original shipment's date -- this changes which layers later June
    shipments on the same combo draw from."""
    df = _load_csv("cogs_report.csv").set_index(["product_id", "warehouse_id", "shipment_id"])
    gt = ground_truth["cogs_report"].set_index(["product_id", "warehouse_id", "shipment_id"])
    combo_ids = set(ground_truth["return_combo_ids"])
    affected = [idx for idx in gt.index if (idx[0], idx[1]) in combo_ids]
    assert len(affected) >= 25, "sanity: expected a meaningful return-affected shipment cohort"

    common = [idx for idx in affected if idx in df.index]
    assert len(common) == len(affected), "cogs_report.csv is missing shipment rows present in the ground truth"

    deltas = (df.loc[common, "cogs_value"] - gt.loc[common, "cogs_value"]).abs()
    pct = deltas / gt.loc[common, "cogs_value"].replace(0, pd.NA) * 100
    n_within = (pct.fillna(0) <= 3.0).sum()
    assert n_within / len(common) >= 0.75, (
        f"cogs_value wrong for {len(common) - n_within}/{len(common)} June shipments on combos "
        f"with a customer return -- check where the return is inserted in FIFO layer order."
    )


def test_case_06_allocation_netting(ground_truth):
    """available_to_promise_qty must subtract allocated_qty; on_hand_qty must not."""
    df = _load_csv("inventory_position.csv").set_index(["product_id", "warehouse_id"])
    gt = ground_truth["inventory_position"].set_index(["product_id", "warehouse_id"])
    ids = ground_truth["allocation_ids"]
    assert len(ids) >= 30, "sanity: expected most combos to carry an open order in the fixture"

    deltas = (df.loc[ids, "available_to_promise_qty"] - gt.loc[ids, "available_to_promise_qty"]).abs()
    n_within = (deltas <= 3.0).sum()
    assert n_within / len(ids) >= 0.85, (
        f"available_to_promise_qty wrong for {len(ids) - n_within}/{len(ids)} combos with open "
        f"orders -- check that allocated_qty is netted out of available_to_promise_qty."
    )

    onhand_deltas = (df.loc[ids, "on_hand_qty"] - gt.loc[ids, "on_hand_qty"]).abs()
    n_onhand_within = (onhand_deltas <= 3.0).sum()
    assert n_onhand_within / len(ids) >= 0.85, (
        "on_hand_qty should NOT be reduced by allocated_qty -- on-hand reflects physical stock."
    )


def test_case_07_partial_order_netting(ground_truth):
    """A slice of open_orders.csv rows already have a shipment booked against
    the same order_id (shipments.csv carries its own order_id column) --
    allocated_qty for that order is its quantity minus whatever's already
    shipped against it, not the order's raw original size."""
    df = _load_csv("inventory_position.csv").set_index(["product_id", "warehouse_id"])
    gt = ground_truth["inventory_position"].set_index(["product_id", "warehouse_id"])
    ids = ground_truth["partial_order_ids"]
    assert len(ids) >= 80, "sanity: expected a meaningful partial-fulfillment cohort in the fixture"

    deltas = (df.loc[ids, "allocated_qty"] - gt.loc[ids, "allocated_qty"]).abs()
    n_within = (deltas <= 3.0).sum()
    assert n_within / len(ids) >= 0.75, (
        f"allocated_qty wrong for {len(ids) - n_within}/{len(ids)} combos carrying an order "
        f"that's already partially shipped -- check whether allocated_qty nets out shipments "
        f"already booked against the same order_id, or just sums each open order's own quantity."
    )

    atp_deltas = (df.loc[ids, "available_to_promise_qty"] - gt.loc[ids, "available_to_promise_qty"]).abs()
    n_atp_within = (atp_deltas <= 3.0).sum()
    assert n_atp_within / len(ids) >= 0.75, (
        f"available_to_promise_qty wrong for {len(ids) - n_atp_within}/{len(ids)} combos with a "
        f"partially-shipped open order."
    )


def test_case_08_quality_hold_onhand_inclusion(ground_truth):
    """on_hold receipts are physically in the warehouse -- they must still
    count toward on_hand_qty and on_hand_value."""
    df = _load_csv("inventory_position.csv").set_index(["product_id", "warehouse_id"])
    gt = ground_truth["inventory_position"].set_index(["product_id", "warehouse_id"])
    ids = ground_truth["hold_ids"]
    assert len(ids) >= 10, "sanity: expected a meaningful hold cohort in the fixture"

    deltas = (df.loc[ids, "on_hand_qty"] - gt.loc[ids, "on_hand_qty"]).abs()
    n_within = (deltas <= 3.0).sum()
    assert n_within / len(ids) >= 0.80, (
        f"on_hand_qty wrong for {len(ids) - n_within}/{len(ids)} combos with an on_hold receipt "
        f"-- held stock is still physically received and must count toward on-hand."
    )


def test_case_09_quality_hold_atp_exclusion(ground_truth):
    """The same on_hold combos must exclude held quantity from
    available_to_promise_qty -- physically-present and available diverge
    specifically on hold status here."""
    df = _load_csv("inventory_position.csv").set_index(["product_id", "warehouse_id"])
    gt = ground_truth["inventory_position"].set_index(["product_id", "warehouse_id"])
    ids = ground_truth["hold_ids"]

    deltas = (df.loc[ids, "available_to_promise_qty"] - gt.loc[ids, "available_to_promise_qty"]).abs()
    n_within = (deltas <= 3.0).sum()
    assert n_within / len(ids) >= 0.80, (
        f"available_to_promise_qty wrong for {len(ids) - n_within}/{len(ids)} combos with an "
        f"on_hold receipt -- held stock is not eligible for fulfillment and must be excluded "
        f"from available_to_promise_qty even though it counts toward on_hand_qty."
    )


def test_case_10_summary_json_aggregates(ground_truth):
    """Portfolio-level cross-check on summary.json -- catches drift that
    individual-row tolerance checks could average out, and confirms all
    four promised keys are present and correctly aggregated."""
    with open(WORKSPACE_DIR / "summary.json") as f:
        summary = json.load(f)
    required_keys = {"combo_count", "total_on_hand_value", "total_available_to_promise_qty", "total_june_cogs"}
    missing = required_keys - set(summary)
    assert not missing, f"summary.json missing keys: {missing}"

    assert summary["combo_count"] == len(ground_truth["inventory_position"])

    expected_value = ground_truth["inventory_position"].on_hand_value.sum()
    assert abs(summary["total_on_hand_value"] - expected_value) <= max(500.0, expected_value * 0.02), (
        f"summary.json total_on_hand_value = {summary['total_on_hand_value']}, expected ~{expected_value:.2f}"
    )

    expected_atp = ground_truth["inventory_position"].available_to_promise_qty.sum()
    assert abs(summary["total_available_to_promise_qty"] - expected_atp) <= max(50.0, abs(expected_atp) * 0.05), (
        f"summary.json total_available_to_promise_qty = {summary['total_available_to_promise_qty']}, "
        f"expected ~{expected_atp:.2f}"
    )

    expected_cogs = ground_truth["cogs_report"].cogs_value.sum()
    assert abs(summary["total_june_cogs"] - expected_cogs) <= max(500.0, expected_cogs * 0.03), (
        f"summary.json total_june_cogs = {summary['total_june_cogs']}, expected ~{expected_cogs:.2f}"
    )


def test_case_11_anti_cheat_conservation(ground_truth):
    """Company-wide mass-conservation sentinel, independent of any single
    trap -- catches silent row loss (e.g. a groupby that drops rows) even
    when the per-trap checks above happen to look fine."""
    df = _load_csv("inventory_position.csv")
    receipts = ground_truth["receipts"]
    transfers = ground_truth["transfers"]

    total_received = receipts.quantity.sum()
    shipments = pd.read_csv(DATA_DIR / "shipments.csv")
    total_shipped = shipments.quantity.sum()
    total_transferred_out = transfers.quantity.sum()
    settled_transfers = transfers[transfers.receive_date <= CUTOFF]
    total_transferred_in = settled_transfers.quantity.sum()

    expected_total_on_hand = total_received - total_shipped - total_transferred_out + total_transferred_in
    actual_total_on_hand = df.on_hand_qty.sum()

    assert abs(actual_total_on_hand - expected_total_on_hand) <= max(50.0, expected_total_on_hand * 0.02), (
        f"company-wide on_hand_qty total = {actual_total_on_hand:.2f}, expected ~"
        f"{expected_total_on_hand:.2f} from receipts - shipments - transfers-out + transfers-in. "
        f"A mismatch here usually means rows were silently dropped somewhere in the pipeline."
    )


def test_case_12_anti_cheat_sentinels(ground_truth):
    """Structural sentinels on the immutable source data -- this generator
    is deterministic (fixed seed), so these are fixed facts about the
    shipped CSVs, not values the agent's output could ever legitimately
    change."""
    receipts = ground_truth["receipts"]
    transfers = ground_truth["transfers"]
    shipments = ground_truth["shipments"]

    assert (receipts.source == "customer_return").sum() == 720
    assert (receipts.hold_status == "on_hold").sum() == 243
    assert shipments["order_id"].notna().sum() == 300

    intransit_count = ((transfers.ship_date <= CUTOFF) & (transfers.receive_date > CUTOFF)).sum()
    settled_count = ((transfers.ship_date <= CUTOFF) & (transfers.receive_date <= CUTOFF)).sum()
    assert intransit_count == 268
    assert settled_count == 951
