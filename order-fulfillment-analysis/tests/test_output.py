"""
Tests for the Q1 2024 order fulfillment rate report.

Contract (instruction.md): the deliverable is
    /workspace/region_fulfillment_report.csv — one row per region
    /workspace/summary.json                  — five scalar keys

Reasoning challenges this suite exercises:
  1. Composite key, order side — line_id resets per order, so
     shipment_line_items.csv must be joined to order_lines.csv on
     (order_id, line_id), never line_id alone.
  2. Effective-dated region assignment — a warehouse's fulfillment region
     (warehouse_region_assignments.csv) can change over time. Region must
     be resolved as of each order's order_date via a proper
     effective_from/effective_to range match, treating a blank
     effective_to as open-ended -- not by taking each warehouse's most
     recent assignment regardless of order_date. Because this only moves
     order lines between regions, every grand total is unaffected by it;
     it is only visible in the per-region breakdown.
  3. Composite key, shipment side — shipment_id in shipment_line_items.csv
     is a per-warehouse counter, not globally unique. It must be joined to
     shipment_headers.csv on (warehouse_id, shipment_id), never shipment_id
     alone. Cross-warehouse shipment_id collisions only affect a minority
     of rows, so a wrong join does not produce an obviously-impossible
     aggregate -- it must be caught by checking key uniqueness, not by
     eyeballing whether the final numbers look plausible.
  4. Netting — quantity in shipment_line_items.csv is signed the way the
     WMS records it (positive for Shipment, negative for Return/
     Cancellation), so it nets correctly with a plain sum once the row set
     is scoped correctly.
  5. Reporting cutoff — only headers with event_date <= 2024-04-15 count,
     regardless of order_date.
  6. Domain knowledge gap — shipment_headers.csv carries a fourth
     transaction_type, Backorder, recording quantity still awaiting
     fulfillment. It has a positive quantity like a Shipment row, but
     nothing has gone out against it, and must be excluded.
  7. Composite key + domain knowledge gap, combined — carrier_fulfillment_
     agreements.csv is a sparse exceptions list of (carrier_id,
     warehouse_id) pairs shipping under FOB Origin freight terms; every
     absent pair is FOB Destination (the default). Under FOB Origin, a
     later Return/Cancellation does not net against quantity fulfilled --
     the warehouse's responsibility ended once the shipment left the dock,
     so a subsequent return is the customer's and carrier's matter, not a
     fulfillment failure -- but the Shipment quantity still counts in full
     either way. carrier_id alone is not a unique key into this table:
     six carriers are FOB Origin at exactly two of the warehouses they
     serve, FOB Destination everywhere else.

test_case_01 is a single anti-tamper sentinel on all input data, not graded
reasoning. test_case_02-03 check output structure/schema. test_case_04-06
are hard, ground-truth-derived checks tied to challenges 1-7 above (each
checks both the total and the per-region breakdown, since the region trap
in particular only shows up per-region); test_case_07 checks the derived
best/worst region labels. test_case_08-11 each reject one specific naive
mistake (Backorder inclusion, shipment_id-only join, current-only region
assignment, carrier_id-only fulfillment-agreement lookup) by checking the
reported figure isn't suspiciously close to what that specific mistake
would produce.
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

ORDERS_ROWS = 18_000
ORDER_LINES_ROWS = 29_657
SHIPMENT_ROWS = 40_278
ASSIGNMENT_ROWS = 23
CARRIER_AGREEMENT_ROWS = 12


# ── Anti-cheat sentinel ────────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel over all seven traps: row counts, line_id collision,
    Return/Cancellation sign, shipment_id cross-warehouse collision,
    Backorder presence/sign, warehouse_region_assignments structure, and
    carrier_fulfillment_agreements structure."""
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv")
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")

    assert len(orders) == ORDERS_ROWS, \
        f"orders.csv row count must not be modified (expected {ORDERS_ROWS}, got {len(orders)})."
    assert len(order_lines) == ORDER_LINES_ROWS, \
        f"order_lines.csv row count must not be modified (expected {ORDER_LINES_ROWS}, got {len(order_lines)})."
    assert len(headers) == SHIPMENT_ROWS, \
        f"shipment_headers.csv row count must not be modified (expected {SHIPMENT_ROWS}, got {len(headers)})."
    assert len(line_items) == SHIPMENT_ROWS, \
        f"shipment_line_items.csv row count must not be modified (expected {SHIPMENT_ROWS}, got {len(line_items)})."
    assert len(assignments) == ASSIGNMENT_ROWS, \
        (f"warehouse_region_assignments.csv row count must not be modified "
         f"(expected {ASSIGNMENT_ROWS}, got {len(assignments)}).")

    orders_per_line1 = order_lines[order_lines["line_id"] == 1]["order_id"].nunique()
    assert orders_per_line1 > 15_000, \
        (f"line_id is expected to repeat across essentially every order (line_id=1 shared "
         f"by {orders_per_line1} distinct orders, expected >15,000). "
         f"line_id must not be treated as a globally unique identifier.")

    type_counts = headers["transaction_type"].value_counts()
    assert type_counts.get("Return", 0) > 1_000, \
        f"Expected >1,000 Return headers, got {type_counts.get('Return', 0)}."
    assert type_counts.get("Cancellation", 0) > 500, \
        f"Expected >500 Cancellation headers, got {type_counts.get('Cancellation', 0)}."
    assert type_counts.get("Backorder", 0) > 3_000, \
        f"Expected >3,000 Backorder headers, got {type_counts.get('Backorder', 0)}."

    adj_events = line_items.merge(
        headers[headers["transaction_type"].isin(["Return", "Cancellation"])][
            ["warehouse_id", "shipment_id"]
        ],
        on=["warehouse_id", "shipment_id"],
    )
    assert (adj_events["quantity"] < 0).all(), \
        "Return/Cancellation line items are expected to carry a negative quantity."

    backorder_events = line_items.merge(
        headers[headers["transaction_type"] == "Backorder"][["warehouse_id", "shipment_id"]],
        on=["warehouse_id", "shipment_id"],
    )
    assert (backorder_events["quantity"] > 0).all(), \
        "Backorder line items are expected to carry a positive quantity."

    dup_rows = headers.duplicated(subset=["shipment_id"]).sum()
    assert dup_rows > 1_000, \
        (f"Expected >1,000 shipment_headers rows whose shipment_id also appears under "
         f"another warehouse (got {dup_rows}). shipment_id must not be treated as a "
         f"globally unique identifier.")

    segment_counts = assignments.groupby("warehouse_id").size()
    n_reassigned = (segment_counts > 1).sum()
    assert n_reassigned >= 3, \
        (f"Expected at least 3 warehouses with more than one region-assignment "
         f"segment, got {n_reassigned}. warehouse_id must not be treated as "
         f"having a single, static region.")

    n_open_ended = assignments["effective_to"].isna().sum()
    assert n_open_ended == assignments["warehouse_id"].nunique(), \
        (f"Expected exactly one open-ended (blank effective_to) segment per "
         f"warehouse, got {n_open_ended} open-ended rows for "
         f"{assignments['warehouse_id'].nunique()} warehouses.")

    assert len(agreements) == CARRIER_AGREEMENT_ROWS, \
        (f"carrier_fulfillment_agreements.csv row count must not be modified "
         f"(expected {CARRIER_AGREEMENT_ROWS}, got {len(agreements)}).")

    assert (agreements["freight_terms"] == "FOB Origin").all(), \
        "carrier_fulfillment_agreements.csv is expected to list FOB Origin exceptions only."

    rows_per_carrier = agreements.groupby("carrier_id").size()
    n_multi_warehouse_carriers = (rows_per_carrier > 1).sum()
    assert n_multi_warehouse_carriers >= 3, \
        (f"Expected at least 3 carriers with FOB Origin terms at more than "
         f"one warehouse, got {n_multi_warehouse_carriers}. carrier_id "
         f"alone must not be treated as determining freight_terms.")

    fob_origin_carriers = set(agreements["carrier_id"].unique())
    shipments_from_fob_carriers = headers[
        headers["carrier_id"].isin(fob_origin_carriers) & (headers["transaction_type"] == "Shipment")
    ]
    warehouses_served = shipments_from_fob_carriers.groupby("carrier_id")["warehouse_id"].nunique()
    warehouses_listed = agreements.groupby("carrier_id")["warehouse_id"].nunique()
    n_partial_coverage = (warehouses_served.reindex(warehouses_listed.index) > warehouses_listed).sum()
    assert n_partial_coverage >= 3, \
        (f"Expected all {len(fob_origin_carriers)} FOB Origin carriers to also ship from "
         f"warehouses not listed in carrier_fulfillment_agreements.csv (got {n_partial_coverage} "
         f"such carriers), so that a carrier_id-only lookup is a real, checkable mistake.")


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_report_exists_shape_and_sort():
    assert REPORT_PATH.exists(), "region_fulfillment_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"region", "quantity_ordered", "quantity_fulfilled_net", "fill_rate"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 5, f"Expected exactly 5 rows (one per region), got {len(df)}."

    expected_order = df.sort_values("region").reset_index(drop=True)
    assert list(df["region"]) == list(expected_order["region"]), \
        "Report must be sorted by region ascending."


def test_case_03_summary_schema():
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

def _resolve_q1_order_regions(orders, assignments):
    q1_orders = orders[(orders["order_date"] >= Q1_START) & (orders["order_date"] <= Q1_END)]
    merged = q1_orders[["order_id", "order_date", "assigned_warehouse_id"]].merge(
        assignments, left_on="assigned_warehouse_id", right_on="warehouse_id"
    )
    in_effect = merged[
        (merged["effective_from"] <= merged["order_date"])
        & (merged["effective_to"].isna() | (merged["order_date"] <= merged["effective_to"]))
    ]
    return in_effect[["order_id", "region"]]


@pytest.fixture(scope="module")
def ground_truth():
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")

    # Scope: Q1 2024 orders, tagged with the fulfillment region in effect on
    # each order's order_date. A warehouse's region can have more than one
    # effective-dated segment; the active one is found by range-matching
    # order_date against effective_from/effective_to, treating a blank
    # effective_to as open-ended (still in effect) rather than excluding it.
    order_regions = _resolve_q1_order_regions(orders, assignments)
    scoped = order_lines.merge(order_regions, on="order_id")

    # shipment_id is a per-warehouse counter, not globally unique -- line_items
    # must be joined to headers on (warehouse_id, shipment_id), never
    # shipment_id alone -- then filtered to the cutoff and restricted to
    # Shipment/Return/Cancellation. quantity is already signed correctly
    # (positive for Shipment, negative for Return/Cancellation), so it nets
    # with a plain sum; Backorder rows are dropped entirely -- they record
    # quantity still awaiting fulfillment, not quantity the customer has
    # actually received.
    FULFILLMENT_TYPES = ["Shipment", "Return", "Cancellation"]
    events = line_items.merge(headers, on=["warehouse_id", "shipment_id"])
    in_window = events[events["event_date"] <= REPORT_AS_OF]
    in_window = in_window[in_window["transaction_type"].isin(FULFILLMENT_TYPES)]

    # freight_terms is resolved via a left join on the composite
    # (warehouse_id, carrier_id) key -- carrier_id alone is not unique in
    # carrier_fulfillment_agreements.csv -- defaulting to FOB Destination
    # for any pair absent from that (sparse, exceptions-only) file. Under
    # FOB Origin, a Return/Cancellation does not net against fulfillment;
    # the matching Shipment row is untouched either way.
    in_window = in_window.merge(
        agreements[["warehouse_id", "carrier_id", "freight_terms"]],
        on=["warehouse_id", "carrier_id"], how="left",
    )
    in_window["freight_terms"] = in_window["freight_terms"].fillna("FOB Destination")
    fob_origin_adjustment = (
        (in_window["freight_terms"] == "FOB Origin")
        & (in_window["transaction_type"] != "Shipment")
    )
    in_window = in_window[~fob_origin_adjustment]

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
    all_regions = assignments["region"].drop_duplicates().sort_values()
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


# ── Hard test 1: quantity ordered — total and per-region exact ────────────────

def test_case_04_quantity_ordered_exact(ground_truth):
    """quantity_ordered depends only on Q1 scoping and effective-dated region
    resolution; must match exactly, both in total and per region.

    Region reassignment only moves order lines between regions -- it never
    changes the total -- so a region-blind mistake can only be caught by
    checking each region's figure individually, not by checking the total.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["total_quantity_ordered"] == ground_truth["total_quantity_ordered"], \
        (f"total_quantity_ordered: got {s['total_quantity_ordered']}, "
         f"expected {ground_truth['total_quantity_ordered']}.")

    report = pd.read_csv(REPORT_PATH).set_index("region")
    gt = ground_truth["report"]
    for region in gt.index:
        assert region in report.index, f"Missing region '{region}' in report."
        exp = int(gt.loc[region, "quantity_ordered"])
        got = int(report.loc[region, "quantity_ordered"])
        assert got == exp, \
            f"quantity_ordered for region {region}: got {got}, expected {exp}."


# ── Hard test 2: quantity fulfilled (net) — total and per-region exact ────────

def test_case_05_quantity_fulfilled_net_exact(ground_truth):
    """quantity_fulfilled_net must match exactly, both in total and per
    region. A region-blind netting mistake could still land close on the
    overall total by coincidence; checking every region individually closes
    that gap.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["total_quantity_fulfilled_net"] == ground_truth["total_quantity_fulfilled_net"], \
        (f"total_quantity_fulfilled_net: got {s['total_quantity_fulfilled_net']}, "
         f"expected {ground_truth['total_quantity_fulfilled_net']}.")

    report = pd.read_csv(REPORT_PATH).set_index("region")
    gt = ground_truth["report"]
    for region in gt.index:
        assert region in report.index, f"Missing region '{region}' in report."
        exp = int(gt.loc[region, "quantity_fulfilled_net"])
        got = int(report.loc[region, "quantity_fulfilled_net"])
        assert got == exp, \
            f"quantity_fulfilled_net for region {region}: got {got}, expected {exp}."


# ── Hard test 3: fill_rate — per-region and overall, within tolerance ─────────

def test_case_06_fill_rate_within_tolerance(ground_truth):
    """Each region's fill_rate, and overall_fill_rate, must be within +/-0.01
    of ground truth.

    A line_id-only join, ignoring returns/cancellations, including Backorder
    quantity, or including activity dated after the 2024-04-15 cutoff, each
    overstates or understates this figure enough to blow past this tolerance.
    """
    report = pd.read_csv(REPORT_PATH).set_index("region")
    gt = ground_truth["report"]
    for region in gt.index:
        assert region in report.index, f"Missing region '{region}' in report."
        exp = float(gt.loc[region, "fill_rate"])
        got = float(report.loc[region, "fill_rate"])
        assert abs(got - exp) <= 0.01, \
            f"fill_rate for region {region}: got {got}, expected {exp} (+/-0.01)."

    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    exp_overall = ground_truth["overall_fill_rate"]
    got_overall = s["overall_fill_rate"]
    assert abs(got_overall - exp_overall) <= 0.01, \
        f"overall_fill_rate: got {got_overall}, expected {exp_overall} (+/-0.01)."


# ── Medium test: best/worst region identifiers ────────────────────────────────

def test_case_07_best_worst_region(ground_truth):
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["best_performing_region"] == ground_truth["best_performing_region"], \
        (f"best_performing_region: got {s['best_performing_region']!r}, "
         f"expected {ground_truth['best_performing_region']!r}")
    assert s["worst_performing_region"] == ground_truth["worst_performing_region"], \
        (f"worst_performing_region: got {s['worst_performing_region']!r}, "
         f"expected {ground_truth['worst_performing_region']!r}")


# ── Hard test 4: rejects naive Backorder inclusion ─────────────────────────────

@pytest.fixture(scope="module")
def naive_total_fulfilled_with_backorder():
    """quantity_fulfilled_net if every transaction_type row within the cutoff
    is summed as-is, without restricting to Shipment/Return/Cancellation
    (headers/line_items still joined correctly on (warehouse_id, shipment_id),
    region still resolved correctly via effective-dating, and FOB Origin
    Return/Cancellation events still correctly excluded from netting via the
    composite (warehouse_id, carrier_id) agreement lookup)."""
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")

    order_regions = _resolve_q1_order_regions(orders, assignments)
    scoped = order_lines.merge(order_regions, on="order_id")

    events = line_items.merge(headers, on=["warehouse_id", "shipment_id"])
    in_window = events[events["event_date"] <= REPORT_AS_OF].copy()
    in_window = in_window.merge(
        agreements[["warehouse_id", "carrier_id", "freight_terms"]],
        on=["warehouse_id", "carrier_id"], how="left",
    )
    in_window["freight_terms"] = in_window["freight_terms"].fillna("FOB Destination")
    fob_origin_adjustment = (
        (in_window["freight_terms"] == "FOB Origin")
        & (in_window["transaction_type"].isin(["Return", "Cancellation"]))
    )
    in_window = in_window[~fob_origin_adjustment]
    matched = scoped[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    fulfilled = matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )
    lines = scoped.merge(fulfilled, on=["order_id", "line_id"], how="left")
    lines["quantity_fulfilled_net"] = lines["quantity_fulfilled_net"].fillna(0)
    return int(lines["quantity_fulfilled_net"].sum())


def test_case_08_quantity_fulfilled_net_rejects_backorder_inclusion(
    ground_truth, naive_total_fulfilled_with_backorder
):
    """total_quantity_fulfilled_net must not land near the naive figure
    obtained by summing every transaction_type row (including Backorder)
    within the cutoff."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    got = s["total_quantity_fulfilled_net"]
    exp = ground_truth["total_quantity_fulfilled_net"]
    rel_err_from_naive = abs(got - naive_total_fulfilled_with_backorder) / naive_total_fulfilled_with_backorder
    assert rel_err_from_naive > 0.03, \
        (f"total_quantity_fulfilled_net ({got}) is suspiciously close to the naive "
         f"figure that includes Backorder quantity ({naive_total_fulfilled_with_backorder}); "
         f"Backorder rows must be excluded before summing. Correct total is {exp}.")


# ── Hard test 5: rejects naive shipment_id-only join ───────────────────────────

@pytest.fixture(scope="module")
def naive_total_fulfilled_shipment_id_only():
    """quantity_fulfilled_net if shipment_line_items.csv is joined to
    shipment_headers.csv on shipment_id alone, ignoring that shipment_id is
    only unique within a warehouse. A minority of shipment_id values collide
    across warehouses, so this fans a minority of line items out against the
    wrong header's transaction_type/event_date. Region is still resolved
    correctly via effective-dating, and FOB Origin Return/Cancellation
    events are still correctly excluded from netting via the composite
    (warehouse_id, carrier_id) agreement lookup (using the warehouse_id
    carried on the wrongly-joined header row, since that's the only
    warehouse_id available once the shipment_id-only join has already
    discarded the line item's own)."""
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")

    order_regions = _resolve_q1_order_regions(orders, assignments)
    scoped = order_lines.merge(order_regions, on="order_id")

    events = line_items.merge(headers, on="shipment_id", suffixes=("", "_header"))
    in_window = events[
        (events["event_date"] <= REPORT_AS_OF)
        & (events["transaction_type"].isin(["Shipment", "Return", "Cancellation"]))
    ].copy()
    in_window = in_window.merge(
        agreements[["warehouse_id", "carrier_id", "freight_terms"]],
        left_on=["warehouse_id_header", "carrier_id"], right_on=["warehouse_id", "carrier_id"],
        how="left",
    )
    in_window["freight_terms"] = in_window["freight_terms"].fillna("FOB Destination")
    fob_origin_adjustment = (
        (in_window["freight_terms"] == "FOB Origin")
        & (in_window["transaction_type"] != "Shipment")
    )
    in_window = in_window[~fob_origin_adjustment]
    matched = scoped[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    fulfilled = matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )
    lines = scoped.merge(fulfilled, on=["order_id", "line_id"], how="left")
    lines["quantity_fulfilled_net"] = lines["quantity_fulfilled_net"].fillna(0)
    return int(lines["quantity_fulfilled_net"].sum())


def test_case_09_quantity_fulfilled_net_rejects_shipment_id_only_join(
    ground_truth, naive_total_fulfilled_shipment_id_only
):
    """total_quantity_fulfilled_net must not land near the naive figure
    obtained by joining shipment_line_items.csv to shipment_headers.csv on
    shipment_id alone, without also matching on warehouse_id."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    got = s["total_quantity_fulfilled_net"]
    exp = ground_truth["total_quantity_fulfilled_net"]
    rel_err_from_naive = abs(got - naive_total_fulfilled_shipment_id_only) / naive_total_fulfilled_shipment_id_only
    assert rel_err_from_naive > 0.03, \
        (f"total_quantity_fulfilled_net ({got}) is suspiciously close to the naive "
         f"figure obtained by joining on shipment_id alone ({naive_total_fulfilled_shipment_id_only}); "
         f"shipment_id is only unique within a warehouse, the join must also match on "
         f"warehouse_id. Correct total is {exp}.")


# ── Hard test 6: rejects naive current-only region assignment ─────────────────

@pytest.fixture(scope="module")
def naive_quantity_ordered_by_region_current_only():
    """quantity_ordered by region if each warehouse's fulfillment region is
    taken as its single most recent assignment (sort by effective_from,
    keep the last row per warehouse_id), ignoring whether that assignment
    was actually in effect as of each order's order_date. This is correct
    for warehouses whose region never changed, and for warehouses whose
    only reassignment landed before Q1 2024 started -- but silently
    misattributes Q1 orders for any warehouse reassigned during or after
    Q1 to the wrong region."""
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")

    current = assignments.sort_values("effective_from").drop_duplicates(
        subset="warehouse_id", keep="last"
    )
    q1_orders = orders[(orders["order_date"] >= Q1_START) & (orders["order_date"] <= Q1_END)]
    naive_orders = q1_orders.merge(
        current[["warehouse_id", "region"]], left_on="assigned_warehouse_id", right_on="warehouse_id"
    )
    scoped = order_lines.merge(naive_orders[["order_id", "region"]], on="order_id")
    return scoped.groupby("region")["quantity_ordered"].sum()


def test_case_10_region_quantity_ordered_rejects_current_only_assignment(
    ground_truth, naive_quantity_ordered_by_region_current_only
):
    """The per-region quantity_ordered breakdown must not land near the naive
    figures obtained by taking each warehouse's single most recent region
    assignment regardless of order_date."""
    report = pd.read_csv(REPORT_PATH).set_index("region")
    gt = ground_truth["report"]
    naive = naive_quantity_ordered_by_region_current_only
    total_ordered = ground_truth["total_quantity_ordered"]

    total_abs_diff_from_naive = sum(
        abs(int(report.loc[region, "quantity_ordered"]) - int(naive.get(region, 0)))
        for region in gt.index
    )
    rel_err_from_naive = total_abs_diff_from_naive / total_ordered
    assert rel_err_from_naive > 0.03, \
        (f"Per-region quantity_ordered is suspiciously close to the naive breakdown "
         f"obtained by taking each warehouse's single most recent region assignment "
         f"regardless of order_date; region must be resolved as of each order's "
         f"order_date. Correct per-region figures: {gt['quantity_ordered'].to_dict()}.")


# ── Hard test 7: rejects naive carrier_id-only fulfillment-agreement join ──────

@pytest.fixture(scope="module")
def naive_total_fulfilled_carrier_id_only():
    """quantity_fulfilled_net if carrier_fulfillment_agreements.csv is
    reduced to one row per carrier_id (drop_duplicates(subset="carrier_id",
    keep="first")) before being applied, ignoring that carrier_id alone
    does not determine freight_terms -- only the (carrier_id, warehouse_id)
    pair does. Collapsing a carrier's two FOB Origin rows down to one still
    yields "FOB Origin" (both rows carry the same value), so the naive
    lookup treats that carrier as FOB Origin everywhere it ships, not just
    the two warehouses it actually applies to. Every other step (composite
    shipment join, Backorder exclusion, region resolution) is done
    correctly."""
    assignments = pd.read_csv(DATA_DIR / "warehouse_region_assignments.csv",
                               parse_dates=["effective_from", "effective_to"])
    orders = pd.read_csv(DATA_DIR / "orders.csv", parse_dates=["order_date"])
    order_lines = pd.read_csv(DATA_DIR / "order_lines.csv")
    headers = pd.read_csv(DATA_DIR / "shipment_headers.csv", parse_dates=["event_date"])
    line_items = pd.read_csv(DATA_DIR / "shipment_line_items.csv")
    agreements = pd.read_csv(DATA_DIR / "carrier_fulfillment_agreements.csv")

    order_regions = _resolve_q1_order_regions(orders, assignments)
    scoped = order_lines.merge(order_regions, on="order_id")

    events = line_items.merge(headers, on=["warehouse_id", "shipment_id"])
    in_window = events[events["event_date"] <= REPORT_AS_OF].copy()
    in_window = in_window[in_window["transaction_type"].isin(["Shipment", "Return", "Cancellation"])]

    agreements_by_carrier = agreements.drop_duplicates(subset="carrier_id", keep="first")
    in_window = in_window.merge(
        agreements_by_carrier[["carrier_id", "freight_terms"]], on="carrier_id", how="left"
    )
    in_window["freight_terms"] = in_window["freight_terms"].fillna("FOB Destination")
    fob_origin_adjustment = (
        (in_window["freight_terms"] == "FOB Origin")
        & (in_window["transaction_type"] != "Shipment")
    )
    in_window = in_window[~fob_origin_adjustment]

    matched = scoped[["order_id", "line_id"]].merge(
        in_window[["order_id", "line_id", "quantity"]], on=["order_id", "line_id"]
    )
    fulfilled = matched.groupby(["order_id", "line_id"])["quantity"].sum().reset_index(
        name="quantity_fulfilled_net"
    )
    lines = scoped.merge(fulfilled, on=["order_id", "line_id"], how="left")
    lines["quantity_fulfilled_net"] = lines["quantity_fulfilled_net"].fillna(0)
    return int(lines["quantity_fulfilled_net"].sum())


def test_case_11_quantity_fulfilled_net_rejects_carrier_id_only_agreement_join(
    ground_truth, naive_total_fulfilled_carrier_id_only
):
    """total_quantity_fulfilled_net must not land near the naive figure
    obtained by reducing carrier_fulfillment_agreements.csv to one row per
    carrier_id before applying it, without also matching on warehouse_id.

    This trap only ever adjusts Return/Cancellation netting, never Shipment
    quantity, so its available pool is a small slice of total volume (~5-6%
    of shipment activity is a Return/Cancellation to begin with) -- unlike
    test_08/09/10, whose naive mistakes corrupt Shipment quantity directly
    and can clear a 3% threshold. 0.01 is calibrated to this trap's actual
    ceiling: test_case_04/05 (exact per-region match) are the primary
    defense regardless of this test's threshold -- any deviation at all
    fails those outright. This is supplementary insurance, not the primary
    catch.
    """
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    got = s["total_quantity_fulfilled_net"]
    exp = ground_truth["total_quantity_fulfilled_net"]
    rel_err_from_naive = abs(got - naive_total_fulfilled_carrier_id_only) / naive_total_fulfilled_carrier_id_only
    assert rel_err_from_naive > 0.006, \
        (f"total_quantity_fulfilled_net ({got}) is suspiciously close to the naive "
         f"figure obtained by treating carrier_id as uniquely determining "
         f"freight_terms ({naive_total_fulfilled_carrier_id_only}); the lookup "
         f"must also match on warehouse_id. Correct total is {exp}.")
