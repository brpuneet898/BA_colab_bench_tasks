"""
Tests for the Q1 2024 order fulfillment rate report.

Contract (instruction.md): the deliverable is
    /workspace/region_fulfillment_report.csv — one row per region
    /workspace/summary.json                  — five scalar keys
"""

import json
import pandas as pd
import pytest
from pathlib import Path

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
           else Path(__file__).parent.parent / "environment" / "data"
REPORT_PATH = WORKSPACE_DIR / "region_fulfillment_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END = pd.Timestamp("2024-03-31")
REPORT_AS_OF = pd.Timestamp("2024-04-15")


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: order/line/shipment counts, line_id collision, transaction_type mix."""
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    shipments = pd.read_csv(DATA_DIR / "shipments.csv")

    assert len(orders) == 18_000, \
        f"orders.csv row count must not be modified (expected 18,000, got {len(orders)})."
    assert len(order_lines) == 29_662, \
        f"order_lines.csv row count must not be modified (expected 29,662, got {len(order_lines)})."
    assert len(shipments) == 39_491, \
        f"shipments.csv row count must not be modified (expected 39,491, got {len(shipments)})."

    orders_per_line1 = order_lines[order_lines["line_id"] == 1]["order_id"].nunique()
    assert orders_per_line1 > 15_000, \
        (f"line_id is expected to repeat across essentially every order (line_id=1 shared "
         f"by {orders_per_line1} distinct orders, expected >15,000). "
         f"line_id must not be treated as a globally unique identifier.")

    type_counts = shipments["transaction_type"].value_counts()
    assert type_counts.get("Return", 0) > 1_000, \
        f"Expected >1,000 Return events, got {type_counts.get('Return', 0)}."
    assert type_counts.get("Cancellation", 0) > 500, \
        f"Expected >500 Cancellation events, got {type_counts.get('Cancellation', 0)}."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_and_shape():
    assert REPORT_PATH.exists(), "region_fulfillment_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"region", "quantity_ordered", "quantity_fulfilled_net", "fill_rate"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 5, f"Expected exactly 5 rows (one per region), got {len(df)}."


def test_case_03_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values("region").reset_index(drop=True)
    assert list(df["region"]) == list(expected["region"]), \
        "Report must be sorted by region ascending."


def test_case_04_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_quantity_ordered", "total_quantity_fulfilled_net", "overall_fill_rate",
                "best_performing_region", "worst_performing_region"}
    missing = required - set(s.keys())
    assert not missing, f"Missing keys in summary.json: {missing}"
    assert isinstance(s["total_quantity_ordered"], int)
    assert isinstance(s["total_quantity_fulfilled_net"], int)
    assert isinstance(s["overall_fill_rate"], float)
    assert isinstance(s["best_performing_region"], str)
    assert isinstance(s["worst_performing_region"], str)


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    warehouses = pd.read_csv(DATA_DIR / "warehouses.csv")
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    shipments = pd.read_csv(DATA_DIR / "shipments.csv", parse_dates=["event_date"])

    # Scope: Q1 2024 orders, tagged with fulfillment region via assigned_warehouse_id
    q1_orders = orders[(orders["order_date"] >= Q1_START) & (orders["order_date"] <= Q1_END)]
    q1_orders = q1_orders.merge(warehouses[["warehouse_id", "region"]],
                                 left_on="assigned_warehouse_id", right_on="warehouse_id")
    scoped = order_lines.merge(q1_orders[["order_id", "region"]], on="order_id")

    # Net fulfilled quantity per (order_id, line_id) -- composite key join, cutoff applied
    in_window = shipments[shipments["event_date"] <= REPORT_AS_OF]
    matched = scoped[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    fulfilled_by_line = matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )

    lines = scoped.merge(fulfilled_by_line, on=["order_id", "line_id"], how="left")
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
    report = agg.sort_values("region").reset_index(drop=True)

    total_ordered = int(report["quantity_ordered"].sum())
    total_fulfilled = int(report["quantity_fulfilled_net"].sum())

    return {
        "report": report.set_index("region"),
        "total_quantity_ordered": total_ordered,
        "total_quantity_fulfilled_net": total_fulfilled,
        "overall_fill_rate": float(round(total_fulfilled / total_ordered, 4)),
        "best_performing_region": str(report.loc[report["fill_rate"].idxmax(), "region"]),
        "worst_performing_region": str(report.loc[report["fill_rate"].idxmin(), "region"]),
    }


# ── Hard test 1: quantity ordered (basic scope, no trap contamination) ────────

def test_case_05_total_quantity_ordered_exact(ground_truth):
    """quantity_ordered depends only on Q1 scoping and warehouse->region mapping,
    not on either trap; must match exactly."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["total_quantity_ordered"] == ground_truth["total_quantity_ordered"], \
        (f"total_quantity_ordered: got {s['total_quantity_ordered']}, "
         f"expected {ground_truth['total_quantity_ordered']}.")


# ── Hard test 2: composite key + netting — region fill rates ──────────────────

def test_case_06_region_fill_rate_within_tolerance(ground_truth):
    """Each region's fill_rate must be within +/-0.01 of ground truth.

    A line_id-only join (ignoring order_id) inflates fulfilled quantity by
    orders of magnitude; ignoring returns/cancellations overstates it by
    roughly 9%. Either failure blows well past this tolerance.
    """
    report = pd.read_csv(REPORT_PATH).set_index("region")
    gt = ground_truth["report"]
    for region in gt.index:
        assert region in report.index, f"Missing region '{region}' in report."
        exp = float(gt.loc[region, "fill_rate"])
        got = float(report.loc[region, "fill_rate"])
        assert abs(got - exp) <= 0.01, \
            f"fill_rate for region {region}: got {got}, expected {exp} (+/-0.01)."


# ── Hard test 3: netting — total fulfilled quantity ────────────────────────────

def test_case_07_total_quantity_fulfilled_net(ground_truth):
    """Total net fulfilled quantity must be within +/-1% of ground truth.

    A naive sum of positive Shipment rows only (ignoring Return/Cancellation)
    overstates this figure by ~9%, well outside this tolerance.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    exp = ground_truth["total_quantity_fulfilled_net"]
    got = s["total_quantity_fulfilled_net"]
    rel_err = abs(got - exp) / exp
    assert rel_err <= 0.01, \
        f"total_quantity_fulfilled_net: got {got}, expected {exp} (+/-1%)."


# ── Hard test 4: reporting cutoff — events after 2024-04-15 excluded ─────────

def test_case_08_overall_fill_rate_reflects_cutoff(ground_truth):
    """overall_fill_rate must be within +/-0.01 of ground truth.

    Including Return/Cancellation events dated after the 2024-04-15 as-of
    cutoff pulls this figure down by ~0.015, outside this tolerance.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    exp = ground_truth["overall_fill_rate"]
    got = s["overall_fill_rate"]
    assert abs(got - exp) <= 0.01, \
        f"overall_fill_rate: got {got}, expected {exp} (+/-0.01)."


# ── Medium test: best/worst region identifiers ────────────────────────────────

def test_case_09_best_worst_region(ground_truth):
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["best_performing_region"] == ground_truth["best_performing_region"], \
        (f"best_performing_region: got {s['best_performing_region']!r}, "
         f"expected {ground_truth['best_performing_region']!r}")
    assert s["worst_performing_region"] == ground_truth["worst_performing_region"], \
        (f"worst_performing_region: got {s['worst_performing_region']!r}, "
         f"expected {ground_truth['worst_performing_region']!r}")
