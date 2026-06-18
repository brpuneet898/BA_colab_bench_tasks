"""
Tests for the 2024 retail profitability report.

Verifies the four required artifacts:
    /workspace/channel_profitability.csv
    /workspace/category_profitability.csv
    /workspace/monthly_profitability.csv
    /workspace/summary.json
"""

import calendar
import json
import pandas as pd
import numpy as np
import pytest
from pathlib import Path

if Path("/.dockerenv").exists():
    WORKSPACE_DIR = Path("/workspace")
    DATA_DIR = WORKSPACE_DIR / "data"
else:
    WORKSPACE_DIR = Path(__file__).parent.parent
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

EXPECTED_RETURNS_COUNT = 8_370

CH_PATH  = WORKSPACE_DIR / "channel_profitability.csv"
CAT_PATH = WORKSPACE_DIR / "category_profitability.csv"
MO_PATH  = WORKSPACE_DIR / "monthly_profitability.csv"
SUM_PATH = WORKSPACE_DIR / "summary.json"


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
<<<<<<< HEAD
    """Hardcoded row counts and structural anchors prevent an agent from gaming
=======
    """Hardcoded row counts and structural anchors ensure the agent cannot game
>>>>>>> 7be82eb30ff75cc26ab5eeee0f2c69140663c8a3
    dynamic ground truth by modifying input files."""
    orders       = pd.read_csv(DATA_DIR / "orders.csv")
    returns      = pd.read_csv(DATA_DIR / "returns.csv")
    products     = pd.read_csv(DATA_DIR / "products.csv")
    cost_hist    = pd.read_csv(DATA_DIR / "product_cost_history.csv")
    shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions   = pd.read_csv(DATA_DIR / "promotions.csv")
    ch_fees      = pd.read_csv(DATA_DIR / "channel_return_fees.csv")

    assert "category_id" not in orders.columns, \
        "orders.csv must not contain a category_id column."
    assert "unit_cost" not in orders.columns, \
        "orders.csv must not contain a unit_cost column."

    assert len(orders) == 50_988, \
        f"orders.csv must not be modified (expected 50,988 rows, got {len(orders)})."
    assert len(returns) == EXPECTED_RETURNS_COUNT, \
        f"returns.csv must not be modified (expected {EXPECTED_RETURNS_COUNT} rows, got {len(returns)})."
    ch03_returns = returns[returns["channel_id"] == "CH03"]
    assert len(ch03_returns) >= 6_000, \
        f"CH03 return rows must not be removed (expected >=6,000, got {len(ch03_returns)})."

    # Trap A + Trap B anchors
    assert len(shared_costs) == 38, \
        f"shared_costs.csv must not be modified (expected 38 rows, got {len(shared_costs)})."
    assert float(shared_costs["total_cost"].sum()) >= 800_000, \
        "shared_costs.csv annual total must not be reduced below $800,000."
    neg_rows = shared_costs[shared_costs["total_cost"] < 0]
    assert len(neg_rows) == 2, \
        f"shared_costs.csv must contain exactly 2 vendor credit rows, got {len(neg_rows)}."
    assert float(neg_rows["total_cost"].sum()) < -20_000, \
        "shared_costs.csv vendor credits must total more than -$20,000."
    marketing_rows = shared_costs[shared_costs["cost_type"].str.lower().str.strip() == "marketing"]
    assert len(marketing_rows) == 12, \
        f"shared_costs.csv must contain 12 Marketing rows (one per month), got {len(marketing_rows)}."
    assert float(marketing_rows["total_cost"].sum()) > 200_000, \
        "Marketing overhead must total more than $200,000 annually."
    assert set(alloc_rules["allocation_basis"].unique()) == {"revenue", "order_count"}, \
        "cost_allocation_rules.csv allocation_basis values must not be changed."
    assert "effective_from" in alloc_rules.columns, \
        "cost_allocation_rules.csv must contain an effective_from column."
    assert len(alloc_rules) == 3, \
        f"cost_allocation_rules.csv must not be modified (expected 3 rows, got {len(alloc_rules)})."
    marketing_rules = alloc_rules[alloc_rules["cost_type"].str.lower().str.strip() == "marketing"]
    assert len(marketing_rules) == 0, \
        "Marketing must have no entry in cost_allocation_rules.csv (it is unallocated overhead)."

    assert len(products) == 50, \
        f"products.csv must not be modified (expected 50 rows, got {len(products)})."
    assert products["category_id"].nunique() == 5, \
        "products.csv must contain exactly 5 distinct category IDs."

    assert len(cost_hist) == 60, \
        f"product_cost_history.csv must not be modified (expected 60 rows, got {len(cost_hist)})."
    assert cost_hist["product_id"].nunique() == 50, \
        "product_cost_history.csv must cover all 50 products."
    assert cost_hist["effective_date"].nunique() == 2, \
        "product_cost_history.csv must contain exactly 2 effective dates."
    assert pd.Timestamp("2023-01-01") in pd.to_datetime(cost_hist["effective_date"]).values, \
        "product_cost_history.csv base effective_date must be 2023-01-01."

    assert len(ch_fees) == 12, \
        f"channel_return_fees.csv must not be modified (expected 12 rows, got {len(ch_fees)})."
    assert set(ch_fees["channel_id"].unique()) == {"CH03"}, \
        "channel_return_fees.csv must contain fees for CH03."
    assert float(ch_fees["fee_amount"].sum()) > 100_000, \
        "channel_return_fees.csv total fees must not be reduced below $100,000."

    # Trap D anchors
    reason_codes = pd.read_csv(DATA_DIR / "return_reason_codes.csv")
    assert len(reason_codes) == 20, \
        f"return_reason_codes.csv must not be modified (expected 20 rows, got {len(reason_codes)})."
    assert reason_codes["reason_code"].nunique() == 4, \
        "return_reason_codes.csv must contain exactly 4 distinct reason codes."
    assert set(reason_codes["channel_id"].unique()) == {"CH01", "CH02", "CH03", "CH04", "CH05"}, \
        "return_reason_codes.csv must cover all 5 channels."
    assert "waiver_max_qty" in reason_codes.columns, \
        "return_reason_codes.csv must contain a waiver_max_qty column (not a boolean fee_waived)."
    cm_waiver = reason_codes.loc[reason_codes["reason_code"] == "CHANGED_MIND", "waiver_max_qty"]
    assert (cm_waiver == 3).all(), \
        "CHANGED_MIND waiver_max_qty must be 3 for all channels."
    ws_waiver = reason_codes.loc[reason_codes["reason_code"] == "WRONG_SIZE", "waiver_max_qty"]
    assert (ws_waiver == 2).all(), \
        "WRONG_SIZE waiver_max_qty must be 2 for all channels."
    ch03_changed = float(reason_codes.loc[
        (reason_codes["channel_id"] == "CH03") & (reason_codes["reason_code"] == "CHANGED_MIND"),
        "refund_pct"
    ].iloc[0])
    other_changed = float(reason_codes.loc[
        (reason_codes["channel_id"] != "CH03") & (reason_codes["reason_code"] == "CHANGED_MIND"),
        "refund_pct"
    ].iloc[0])
    assert ch03_changed < other_changed, \
        "CH03 CHANGED_MIND refund_pct must be lower than the standard rate."

    # Trap C anchors
    assert len(promotions) == 2, \
        f"promotions.csv must not be modified (expected 2 rows, got {len(promotions)})."
    assert "min_monthly_guarantee" in promotions.columns, \
        "promotions.csv must contain a min_monthly_guarantee column."
    q1_promo = promotions[promotions["promotion_id"] == "PROMO-002"]
    assert len(q1_promo) == 1, \
        "promotions.csv must contain the Q1 Loyalty Commitment promotion (PROMO-002)."
    assert float(q1_promo.iloc[0]["min_monthly_guarantee"]) > 0, \
        "PROMO-002 min_monthly_guarantee must be positive."
    assert pd.Timestamp(q1_promo.iloc[0]["start_date"]) == pd.Timestamp("2024-01-15"), \
        "PROMO-002 start_date must be 2024-01-15."
    assert pd.Timestamp(q1_promo.iloc[0]["end_date"]) == pd.Timestamp("2024-04-14"), \
        "PROMO-002 end_date must be 2024-04-14."

    # Multi-return FIFO anchor
    orders_qty = orders[["channel_id", "order_id", "quantity"]]
    ret_with_qty = returns.merge(orders_qty, on=["channel_id", "order_id"])
    cum_qty = (
        ret_with_qty
        .sort_values("return_date")
        .groupby(["channel_id", "order_id"])["quantity_returned"]
        .cumsum()
    )
    ret_with_qty = ret_with_qty.copy()
    ret_with_qty["cum_qty"] = cum_qty.values
    cumulative_excess = ret_with_qty[ret_with_qty["cum_qty"] > ret_with_qty["quantity"]]
    assert len(cumulative_excess) >= 200, \
        f"returns.csv must contain >=200 cumulative-excess records (got {len(cumulative_excess)})."

    # Cross-year straddle anchor
    straddle_orders = orders[pd.to_datetime(orders["order_date"]).dt.year == 2023]
    assert len(straddle_orders) >= 150, \
        f"orders.csv must contain >=150 Dec-2023 straddle orders (got {len(straddle_orders)})."
    returns["return_year"] = pd.to_datetime(returns["return_date"]).dt.year
    straddle_pairs = returns[returns[["channel_id", "order_id"]].apply(
        tuple, axis=1).isin(straddle_orders[["channel_id", "order_id"]].apply(tuple, axis=1))]
    has_2023 = set(straddle_pairs[straddle_pairs["return_year"] == 2023]
                   .set_index(["channel_id", "order_id"]).index)
    has_2024 = set(straddle_pairs[straddle_pairs["return_year"] == 2024]
                   .set_index(["channel_id", "order_id"]).index)
    assert len(has_2023 & has_2024) >= 150, \
        "returns.csv must contain >=150 cross-year (2023+2024) return pairs."

    assert "Warehousing" in alloc_rules["cost_type"].values, \
        "cost_allocation_rules.csv must not be modified (expected 'Warehousing' with Title Case)."
    assert "Warehousing" in shared_costs["cost_type"].values, \
        "shared_costs.csv must not be modified (expected 'Warehousing' with Title Case)."

    orders["month"] = pd.to_datetime(orders["order_date"]).dt.month
    nov_count = len(orders[orders["month"] == 11])
    jun_count = len(orders[orders["month"] == 6])
    assert nov_count > jun_count * 1.5, \
        f"November order count ({nov_count}) should be >1.5x June ({jun_count})."


# ── Output schema and sort order ──────────────────────────────────────────────

def test_case_02_csv_schemas_and_sort():
    """Validates columns, row counts, sort order for all output CSVs,
    and required keys in summary.json."""
    orders   = pd.read_csv(DATA_DIR / "orders.csv")
    products = pd.read_csv(DATA_DIR / "products.csv")
    n_channels   = orders["channel_id"].nunique()
    n_categories = products["category_id"].nunique()

    assert CH_PATH.exists(),  "channel_profitability.csv not found in /workspace."
    ch = pd.read_csv(CH_PATH)
    assert {"channel_id", "net_profit"} <= set(ch.columns), \
        "channel_profitability.csv missing required columns."
    assert len(ch) == n_channels, f"Expected {n_channels} channel rows, got {len(ch)}."
    assert ch["net_profit"].tolist() == sorted(ch["net_profit"].tolist(), reverse=True), \
        "channel_profitability.csv must be sorted by net_profit descending."

    assert CAT_PATH.exists(), "category_profitability.csv not found in /workspace."
    cat = pd.read_csv(CAT_PATH)
    assert {"category_id", "contribution_margin"} <= set(cat.columns), \
        "category_profitability.csv missing required columns."
    assert len(cat) == n_categories, f"Expected {n_categories} category rows, got {len(cat)}."
    assert cat["contribution_margin"].tolist() == sorted(cat["contribution_margin"].tolist(), reverse=True), \
        "category_profitability.csv must be sorted by contribution_margin descending."

    assert MO_PATH.exists(),  "monthly_profitability.csv not found in /workspace."
    mo = pd.read_csv(MO_PATH)
    assert {"month", "net_profit"} <= set(mo.columns), \
        "monthly_profitability.csv missing required columns."
    assert len(mo) == 12, f"Expected 12 monthly rows (Jan-Dec), got {len(mo)}."
    assert mo["month"].tolist() == sorted(mo["month"].tolist()), \
        "monthly_profitability.csv must be sorted by month ascending."

    assert SUM_PATH.exists(), "summary.json not found in /workspace."
    with open(SUM_PATH) as f:
        s = json.load(f)
    required = {
        "most_profitable_channel", "least_profitable_channel",
        "most_profitable_category", "least_profitable_category",
        "best_margin_month", "worst_margin_month", "total_net_profit",
    }
    missing = required - set(s.keys())
    assert not missing, f"summary.json missing keys: {missing}"


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    orders          = pd.read_csv(DATA_DIR / "orders.csv")
    returns         = pd.read_csv(DATA_DIR / "returns.csv")
    products        = pd.read_csv(DATA_DIR / "products.csv")
    cost_hist       = pd.read_csv(DATA_DIR / "product_cost_history.csv")
    shared_costs    = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules     = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions      = pd.read_csv(DATA_DIR / "promotions.csv")
    ch_fees         = pd.read_csv(DATA_DIR / "channel_return_fees.csv")
    reason_codes    = pd.read_csv(DATA_DIR / "return_reason_codes.csv")

    products["product_id"] = products["product_id"].str.replace("_", "-")
    orders = orders.merge(products[["product_id", "category_id"]], on="product_id", how="left")

    cost_hist["effective_date"] = pd.to_datetime(cost_hist["effective_date"])
    orders["order_date_ts"]     = pd.to_datetime(orders["order_date"])
    expanded = orders[["channel_id", "order_id", "product_id", "order_date_ts"]].merge(
        cost_hist, on="product_id", how="left"
    )
    expanded = expanded[expanded["effective_date"] <= expanded["order_date_ts"]]
    latest_cost = (
        expanded.sort_values("effective_date")
        .groupby(["channel_id", "order_id"], as_index=False)
        .last()[["channel_id", "order_id", "unit_cost"]]
    )
    orders = orders.merge(latest_cost, on=["channel_id", "order_id"], how="left")
    orders.drop(columns=["order_date_ts"], inplace=True)

    orders["gross_profit"] = (orders["unit_price"] - orders["unit_cost"]) * orders["quantity"]
    orders["revenue"]      = orders["unit_price"] * orders["quantity"]

    report_year = 2024
    orders_2024 = orders[pd.to_datetime(orders["order_date"]).dt.year == report_year].copy()

    # Cumulative FIFO cap across ALL return records before filtering to 2024 return dates.
    ret = returns.copy()
    ret = ret.merge(orders[["channel_id", "order_id", "unit_price", "quantity"]], on=["channel_id", "order_id"])
    ret = ret.sort_values("return_date")
    ret["_cum"]       = ret.groupby(["channel_id", "order_id"])["quantity_returned"].cumsum()
    ret["_prev_cum"]  = ret["_cum"] - ret["quantity_returned"]
    ret["_available"] = (ret["quantity"] - ret["_prev_cum"]).clip(lower=0)
    ret["quantity_returned"] = ret[["quantity_returned", "_available"]].min(axis=1)
    ret.drop(columns=["_cum", "_prev_cum", "_available"], inplace=True)
    ret = ret[pd.to_datetime(ret["return_date"]).dt.year == report_year].copy()

    ret = ret.merge(reason_codes, on=["reason_code", "channel_id"], how="left")
    ret["refund_pct"]     = ret["refund_pct"].fillna(1.0)
    ret["waiver_max_qty"] = ret["waiver_max_qty"].fillna(0).astype(int)
    ret["fee_applies"]    = (ret["quantity_returned"] > ret["waiver_max_qty"]).astype(int)
    ret["return_cost"] = (
        ret["quantity_returned"] * ret["unit_price"] * ret["refund_pct"]
        + ret["quantity_returned"] * ret["processing_cost_per_unit"] * ret["fee_applies"]
    )

    ch_gp = orders_2024.groupby("channel_id")["gross_profit"].sum().copy()
    ch_ret      = ret.groupby("channel_id")["return_cost"].sum()
    ch_fees_sum = ch_fees.groupby("channel_id")["fee_amount"].sum()
    ch_net = (
        ch_gp
        - ch_ret.reindex(ch_gp.index, fill_value=0.0)
        - ch_fees_sum.reindex(ch_gp.index, fill_value=0.0)
    )

    orders_2024["month"] = pd.to_datetime(orders_2024["order_date"]).dt.to_period("M").astype(str)
    alloc_rules["effective_from"] = pd.to_datetime(alloc_rules["effective_from"])
    alloc_rules["cost_type"] = alloc_rules["cost_type"].str.lower().str.strip()
    alloc_rules_sorted = alloc_rules.sort_values("effective_from")

    cat_gp    = orders_2024.groupby("category_id")["gross_profit"].sum()
    cat_alloc = pd.Series(0.0, index=cat_gp.index)

    for _, sc_row in shared_costs.iterrows():
        cost_month_ts = pd.to_datetime(sc_row["month"])
        ct  = sc_row["cost_type"].lower().strip()
        amt = float(sc_row["total_cost"])
        applicable = alloc_rules_sorted[
            (alloc_rules_sorted["cost_type"] == ct) &
            (alloc_rules_sorted["effective_from"] <= cost_month_ts)
        ]
        if applicable.empty:
            continue
        basis = applicable.iloc[-1]["allocation_basis"]
        month_orders = orders_2024[orders_2024["month"] == sc_row["month"]]
        if basis == "order_count":
            shares = month_orders.groupby("category_id")["order_id"].count()
        else:
            shares = month_orders.groupby("category_id")["revenue"].sum()
        total = shares.sum()
        if total > 0:
            cat_alloc = cat_alloc.add((shares / total) * amt, fill_value=0.0)

    cat_net = cat_gp - cat_alloc

    all_months = [f"{report_year}-{str(m).zfill(2)}" for m in range(1, 13)]
    mo_gp = orders_2024.groupby("month")["gross_profit"].sum().reindex(all_months, fill_value=0.0)

    cashback_by_month = pd.Series(0.0, index=all_months)
    for _, promo in promotions.iterrows():
        start = pd.Timestamp(promo["start_date"])
        end   = pd.Timestamp(promo["end_date"])
        pct   = float(promo["cashback_pct"])
        min_g = float(promo.get("min_monthly_guarantee", 0.0))
        mask  = (
            (pd.to_datetime(orders_2024["order_date"]) >= start) &
            (pd.to_datetime(orders_2024["order_date"]) <= end)
        )
        rev_by_month = orders_2024[mask].groupby("month")["revenue"].sum()
        for mo in all_months:
            mo_ts  = pd.Timestamp(mo)
            last_d = calendar.monthrange(mo_ts.year, mo_ts.month)[1]
            mo_end = pd.Timestamp(f"{mo_ts.year}-{mo_ts.month:02d}-{last_d}")
            if start <= mo_end and end >= mo_ts:
                computed = float(rev_by_month.get(mo, 0.0)) * pct
                cashback_by_month[mo] += max(computed, min_g)

    mo_net = mo_gp - cashback_by_month

    total_net = float(round(
        float(orders_2024["gross_profit"].sum())
        - float(ret["return_cost"].sum())
        - float(ch_fees["fee_amount"].sum())
        - float(shared_costs["total_cost"].sum())
        - float(cashback_by_month.sum()),
        2,
    ))

    marketing_total = float(
        shared_costs[shared_costs["cost_type"].str.lower().str.strip() == "marketing"]["total_cost"].sum()
    )

    return {
        "most_profitable_channel":   str(ch_net.idxmax()),
        "least_profitable_channel":  str(ch_net.idxmin()),
        "ch_net":                    ch_net,
        "most_profitable_category":  str(cat_net.idxmax()),
        "least_profitable_category": str(cat_net.idxmin()),
        "cat_net":                   cat_net,
        "best_margin_month":         str(mo_net.idxmax()),
        "worst_margin_month":        str(mo_net.idxmin()),
        "mo_net":                    mo_net,
        "total_net_profit":          total_net,
        "marketing_total":           marketing_total,
    }


# ── Test 03: best/worst margin months (Trap 3) ────────────────────────────────

def test_case_03_best_worst_margin_months(ground_truth):
    """Best and worst net-profit months must match ground truth.
    November carries a 45% cashback spike; after cashback it becomes the worst
    month despite having the highest gross revenue."""
    if not MO_PATH.exists() or not SUM_PATH.exists():
        pytest.skip("output files not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    mo_df = pd.read_csv(MO_PATH).set_index("month")
    mo_gt = ground_truth["mo_net"]

    errors = []
    for label, key in [("best_margin_month", "best_margin_month"),
                       ("worst_margin_month", "worst_margin_month")]:
        exp_mo = ground_truth[key]
        got_mo = s.get(label, "")
        if got_mo != exp_mo:
            errors.append(f"  {label}: got {got_mo!r}, expected {exp_mo!r}")
            continue
        exp_v = float(mo_gt[exp_mo])
        if exp_mo not in mo_df.index:
            errors.append(f"  {exp_mo}: missing from monthly_profitability.csv")
            continue
        got_v = float(mo_df.loc[exp_mo, "net_profit"])
        tol   = max(abs(exp_v) * 0.02, 5_000.0)
        if abs(got_v - exp_v) > tol:
            errors.append(f"  {label} ({exp_mo}): got ${got_v:,.0f}, expected ${exp_v:,.0f} (±2%)")

    assert not errors, "Margin month results outside tolerance:\n" + "\n".join(errors)


# ── Test 04: total net profit ─────────────────────────────────────────────────

def test_case_04_total_net_profit(ground_truth):
    """total_net_profit must account for all deductions: return losses,
    shared overhead (vendor credits + Marketing), and cashback."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["total_net_profit"]
    got = float(s.get("total_net_profit", 0.0))
    tol = max(abs(exp) * 0.01, 5_000.0)
    assert abs(got - exp) <= tol, (
        f"total_net_profit: got ${got:,.2f}, expected ${exp:,.2f} (±1%)."
    )


# ── Test 05: February minimum guarantee ──────────────────────────────────────

def test_case_05_february_minimum_cashback(ground_truth):
    """February net profit must reflect the Q1 promotion's minimum monthly guarantee.
    February has no orders so revenue-based cashback is zero, but the guarantee
    still applies. A model that drives cashback solely from order iteration
    never visits February and omits the obligation entirely."""
    if not MO_PATH.exists():
        pytest.skip("monthly_profitability.csv not found")
    mo_df = pd.read_csv(MO_PATH).set_index("month")
    mo_gt = ground_truth["mo_net"]
    feb = "2024-02"
    if feb not in mo_df.index:
        pytest.fail("2024-02 is missing from monthly_profitability.csv")
    exp = float(mo_gt[feb])
    got = float(mo_df.loc[feb, "net_profit"])
    tol = max(abs(exp) * 0.02, 2_000.0)
    assert abs(got - exp) <= tol, (
        f"February 2024 net_profit: got ${got:,.2f}, expected ${exp:,.2f} (±$2,000)."
    )


# ── Test 06: vendor credit reduces overhead allocation (Trap A) ───────────────

def test_case_06_vendor_credit_reduces_category_overhead(ground_truth):
    """Trap A — negative entries in shared_costs.csv are vendor credits that
    reduce allocated overhead.

    A June Warehousing rebate (-$18,000) and a September fulfillment credit
    (-$7,500) must flow through the allocation loop. Filtering shared_costs
    to positive rows silently ignores $25,500 in credits and understates
    every category's contribution margin by the same amount."""
    if not CAT_PATH.exists():
        pytest.skip("category_profitability.csv not found")
    cat_df = pd.read_csv(CAT_PATH).set_index("category_id")
    cat_gt = ground_truth["cat_net"]

    sc = pd.read_csv(DATA_DIR / "shared_costs.csv")
    credit_abs = abs(float(sc[sc["total_cost"] < 0]["total_cost"].sum()))

    got_sum = float(cat_df["contribution_margin"].sum())
    exp_sum = float(cat_gt.sum())
    tol = max(abs(exp_sum) * 0.005, 1_000.0)
    assert abs(got_sum - exp_sum) <= tol, (
        f"Sum of category contribution_margins: got ${got_sum:,.2f}, "
        f"expected ${exp_sum:,.2f} (diff ${got_sum - exp_sum:+,.2f}). "
        f"Vendor credits totalling ${credit_abs:,.0f} must reduce overhead allocation."
    )


# ── Test 07: unallocated Marketing overhead (Trap B) ─────────────────────────

def test_case_07_marketing_overhead_not_allocated_to_categories(ground_truth):
    """Trap B — Marketing overhead has no allocation rule and must not be charged
    to any product category.

    $300,000 annual Marketing cost has no row in cost_allocation_rules.csv.
    Allocating it proportionally distorts all contribution margins."""
    if not CAT_PATH.exists():
        pytest.skip("category_profitability.csv not found")

    marketing_total = ground_truth["marketing_total"]
    assert marketing_total > 200_000, \
        "Marketing overhead must exceed $200,000 — shared_costs.csv may have been modified."

    cat_df = pd.read_csv(CAT_PATH).set_index("category_id")
    cat_gt = ground_truth["cat_net"]
    errors = []
    for cat_id, exp in cat_gt.items():
        if cat_id not in cat_df.index:
            errors.append(f"  {cat_id}: missing from category report")
            continue
        got = float(cat_df.loc[cat_id, "contribution_margin"])
        tol = max(abs(exp) * 0.01, 1_000.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {cat_id}: got ${got:,.0f}, expected ${exp:,.0f} (±1%)"
            )
    assert not errors, "Category contribution margins outside ±1% tolerance:\n" + "\n".join(errors)


# ── Test 08: partial-month promotion guarantee (Trap C) ──────────────────────

def test_case_08_partial_month_promotion_guarantee(ground_truth):
    """Trap C — the Q1 promotion minimum guarantee applies to every month that
    overlaps with the promotion date range, including partial overlaps.

    PROMO-002 runs Jan 15 – Apr 14, 2024. A model that checks
    `start <= month_start <= end` excludes January entirely (Jan 15 > Jan 1),
    producing zero cashback for January instead of the $10,000 minimum guarantee."""
    if not MO_PATH.exists():
        pytest.skip("monthly_profitability.csv not found")
    mo_df = pd.read_csv(MO_PATH).set_index("month")
    mo_gt = ground_truth["mo_net"]

    promos = pd.read_csv(DATA_DIR / "promotions.csv")
    q1 = promos[promos["promotion_id"] == "PROMO-002"].iloc[0]
    min_g = float(q1["min_monthly_guarantee"])

    errors = []
    for mo, label in [("2024-01", "January"), ("2024-04", "April")]:
        if mo not in mo_df.index:
            errors.append(f"  {mo} ({label}) missing from monthly_profitability.csv")
            continue
        exp = float(mo_gt[mo])
        got = float(mo_df.loc[mo, "net_profit"])
        tol = max(abs(exp) * 0.02, 2_000.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {label} ({mo}): got ${got:,.2f}, expected ${exp:,.2f} "
                f"(diff ${got - exp:+,.2f}). "
                f"PROMO-002 overlaps with {label}; the ${min_g:,.0f} minimum guarantee must apply."
            )
    assert not errors, "\n".join(errors)


# ── Test 09: channel net profit values (Trap 1 + Trap D) ─────────────────────

def test_case_09_channel_net_profit_values(ground_truth):
    """All channel net_profit values must be within ±1.5% of ground truth."""
    if not CH_PATH.exists():
        pytest.skip("channel_profitability.csv not found")
    df    = pd.read_csv(CH_PATH).set_index("channel_id")
    ch_gt = ground_truth["ch_net"]

    returns      = pd.read_csv(DATA_DIR / "returns.csv")
    reason_codes = pd.read_csv(DATA_DIR / "return_reason_codes.csv")
    ret2 = returns.merge(reason_codes, on=["reason_code", "channel_id"], how="left")
    above_thresh = ret2[
        ((ret2["reason_code"] == "CHANGED_MIND") & (ret2["quantity_returned"] > 3)) |
        ((ret2["reason_code"] == "WRONG_SIZE")   & (ret2["quantity_returned"] > 2))
    ]
    assert len(above_thresh) >= 500, (
        f"Expected >=500 returns above waiver threshold, got {len(above_thresh)}. "
        "returns.csv or return_reason_codes.csv may have been modified."
    )

    errors = []
    for ch_id, exp in ch_gt.items():
        if ch_id not in df.index:
            errors.append(f"  {ch_id}: missing from channel_profitability.csv")
            continue
        got = float(df.loc[ch_id, "net_profit"])
        tol = max(abs(exp) * 0.015, 500.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {ch_id}: got ${got:,.2f}, expected ${exp:,.2f} "
                f"(diff ${got - exp:+,.2f})"
            )
    assert not errors, "Channel net profits outside ±1.5% tolerance:\n" + "\n".join(errors)
