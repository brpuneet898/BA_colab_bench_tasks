"""
Tests for the 2024 retail profitability report.

Verifies the four required artifacts:
    /workspace/channel_profitability.csv
    /workspace/category_profitability.csv
    /workspace/monthly_profitability.csv
    /workspace/summary.json
"""

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

# Total returns: 7,762 base + 208 multi-return (Trap 13) + 400 straddle (Trap 14).
EXPECTED_RETURNS_COUNT = 8_370

CH_PATH  = WORKSPACE_DIR / "channel_profitability.csv"
CAT_PATH = WORKSPACE_DIR / "category_profitability.csv"
MO_PATH  = WORKSPACE_DIR / "monthly_profitability.csv"
SUM_PATH = WORKSPACE_DIR / "summary.json"


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Hardcoded row counts ensure the agent cannot game dynamic ground truth by
    truncating or modifying input files."""
    orders       = pd.read_csv(DATA_DIR / "orders.csv")
    returns      = pd.read_csv(DATA_DIR / "returns.csv")
    products     = pd.read_csv(DATA_DIR / "products.csv")
    cost_hist    = pd.read_csv(DATA_DIR / "product_cost_history.csv")
    shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions   = pd.read_csv(DATA_DIR / "promotions.csv")
    ch_fees      = pd.read_csv(DATA_DIR / "channel_return_fees.csv")

    # orders.csv must not contain category_id — category mapping must come from products.csv
    assert "category_id" not in orders.columns, \
        "orders.csv must not contain a category_id column."
    # orders.csv must not contain unit_cost — cost must be derived from product_cost_history.csv
    assert "unit_cost" not in orders.columns, \
        "orders.csv must not contain a unit_cost column."

    # Trap 1 anchors
    assert len(orders) == 50_988, \
        f"orders.csv must not be modified (expected 50,988 rows, got {len(orders)})."
    # returns.csv includes both base returns and multi-return records (Trap 13).
    # Exact count is set after generation; must not be modified.
    assert len(returns) == EXPECTED_RETURNS_COUNT, \
        f"returns.csv must not be modified (expected {EXPECTED_RETURNS_COUNT} rows, got {len(returns)})."
    ch03_returns = returns[returns["channel_id"] == "CH03"]
    assert len(ch03_returns) >= 6_000, \
        f"CH03 return rows must not be removed (expected ≥6,000, got {len(ch03_returns)})."

    # Trap 2 anchors
    assert len(shared_costs) == 24, \
        f"shared_costs.csv must not be modified (expected 24 rows, got {len(shared_costs)})."
    assert float(shared_costs["total_cost"].sum()) >= 500_000, \
        "shared_costs.csv annual total must not be reduced below $500,000."
    assert set(alloc_rules["allocation_basis"].unique()) == {"revenue", "order_count"}, \
        "cost_allocation_rules.csv allocation_basis values must not be changed."
    assert "effective_from" in alloc_rules.columns, \
        "cost_allocation_rules.csv must contain an effective_from column."
    assert len(alloc_rules) == 3, \
        f"cost_allocation_rules.csv must not be modified (expected 3 rows, got {len(alloc_rules)})."

    assert len(products) == 50, \
        f"products.csv must not be modified (expected 50 rows, got {len(products)})."
    assert products["category_id"].nunique() == 5, \
        "products.csv must contain exactly 5 distinct category IDs."

    # SCD trap anchor: 50 initial rows (effective 2023-01-01) + 10 CAT01 update rows = 60
    assert len(cost_hist) == 60, \
        f"product_cost_history.csv must not be modified (expected 60 rows, got {len(cost_hist)})."
    assert cost_hist["product_id"].nunique() == 50, \
        "product_cost_history.csv must cover all 50 products."
    assert cost_hist["effective_date"].nunique() == 2, \
        "product_cost_history.csv must contain exactly 2 effective dates."
    # Base effective_date backdated to 2023-01-01 so Dec-2023 straddle orders price cleanly.
    assert pd.Timestamp("2023-01-01") in pd.to_datetime(cost_hist["effective_date"]).values, \
        "product_cost_history.csv base effective_date must be 2023-01-01."

    # Trap 9 anchor: CH03 has 12 monthly platform fee rows
    assert len(ch_fees) == 12, \
        f"channel_return_fees.csv must not be modified (expected 12 rows, got {len(ch_fees)})."
    assert set(ch_fees["channel_id"].unique()) == {"CH03"}, \
        "channel_return_fees.csv must contain fees for CH03."
    assert float(ch_fees["fee_amount"].sum()) > 100_000, \
        "channel_return_fees.csv total fees must not be reduced below $100,000."

    # Return reason codes anchor
    reason_codes = pd.read_csv(DATA_DIR / "return_reason_codes.csv")
    assert len(reason_codes) == 20, \
        f"return_reason_codes.csv must not be modified (expected 20 rows, got {len(reason_codes)})."
    assert reason_codes["reason_code"].nunique() == 4, \
        "return_reason_codes.csv must contain exactly 4 distinct reason codes."
    assert set(reason_codes["channel_id"].unique()) == {"CH01", "CH02", "CH03", "CH04", "CH05"}, \
        "return_reason_codes.csv must cover all 5 channels."
    assert "reason_code" in returns.columns, \
        "returns.csv must contain a reason_code column."
    # CH03 CHANGED_MIND refund_pct must differ from other channels
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

    # Trap 13 anchor: cumulative-excess cases must exist in returns.csv.
    # Some (channel_id, order_id) pairs have multiple return records where the
    # cumulative quantity_returned exceeds the order quantity, even though each
    # individual record is ≤ the order quantity.
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
        f"returns.csv must contain ≥200 cumulative-excess records (got {len(cumulative_excess)}). " \
        "Multi-return records may have been removed."

    # Trap 14 anchor: Dec-2023 straddle orders with cross-year returns must be present.
    # Each straddle order has one return in Dec-2023 and one in Jan-2024; the 2024 return's
    # quantity_returned equals the full order quantity (individually ≤ order_qty, safe per-record),
    # but cumulatively with the Dec-2023 return it exceeds the cap.
    straddle_orders = orders[pd.to_datetime(orders["order_date"]).dt.year == 2023]
    assert len(straddle_orders) >= 150, \
        f"orders.csv must contain ≥150 Dec-2023 straddle orders (got {len(straddle_orders)})."
    straddle_ch = set(straddle_orders["channel_id"].unique())
    assert "CH01" in straddle_ch and "CH02" in straddle_ch, \
        "Straddle orders must exist in both CH01 and CH02."
    # Confirm cross-year returns exist: pairs with a 2023 return AND a 2024 return.
    returns["return_year"] = pd.to_datetime(returns["return_date"]).dt.year
    straddle_pairs = returns[returns[["channel_id","order_id"]].apply(
        tuple, axis=1).isin(straddle_orders[["channel_id","order_id"]].apply(tuple, axis=1))]
    has_2023 = set(straddle_pairs[straddle_pairs["return_year"] == 2023]
                   .set_index(["channel_id","order_id"]).index)
    has_2024 = set(straddle_pairs[straddle_pairs["return_year"] == 2024]
                   .set_index(["channel_id","order_id"]).index)
    cross_year_pairs = has_2023 & has_2024
    assert len(cross_year_pairs) >= 150, \
        f"returns.csv must contain ≥150 cross-year (2023+2024) return pairs (got {len(cross_year_pairs)})."

    # Trap 6 anchor: both files use Title Case "Warehousing" — normalization required before join
    assert "Warehousing" in alloc_rules["cost_type"].values, \
        "cost_allocation_rules.csv must not be modified (expected 'Warehousing' with Title Case)."
    assert "Warehousing" in shared_costs["cost_type"].values, \
        "shared_costs.csv must not be modified (expected 'Warehousing' with Title Case)."

    # Trap 3 anchors
    orders["month"] = pd.to_datetime(orders["order_date"]).dt.month
    nov_count = len(orders[orders["month"] == 11])
    jun_count = len(orders[orders["month"] == 6])
    assert nov_count > jun_count * 1.5, \
        f"November order count ({nov_count}) should be >1.5x June ({jun_count})."
    assert len(promotions) == 2, \
        f"promotions.csv must not be modified (expected 2 rows, got {len(promotions)})."
    assert "min_monthly_guarantee" in promotions.columns, \
        "promotions.csv must contain a min_monthly_guarantee column."
    holiday_promo = promotions[promotions["promotion_id"] == "PROMO-001"]
    assert float(holiday_promo.iloc[0]["cashback_pct"]) >= 0.40, \
        "promotions.csv Holiday Kickoff cashback_pct must not be reduced below 0.40."
    q1_promo = promotions[promotions["promotion_id"] == "PROMO-002"]
    assert len(q1_promo) == 1, \
        "promotions.csv must contain the Q1 Loyalty Commitment promotion (PROMO-002)."
    assert float(q1_promo.iloc[0]["min_monthly_guarantee"]) > 0, \
        "PROMO-002 min_monthly_guarantee must be positive."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_outputs_exist():
    assert CH_PATH.exists(),  "channel_profitability.csv not found in /workspace."
    assert CAT_PATH.exists(), "category_profitability.csv not found in /workspace."
    assert MO_PATH.exists(),  "monthly_profitability.csv not found in /workspace."
    assert SUM_PATH.exists(), "summary.json not found in /workspace."


def test_case_03_csv_schemas_and_sort():
    """Validates columns, row counts, and sort order for all three output CSVs."""
    orders   = pd.read_csv(DATA_DIR / "orders.csv")
    products = pd.read_csv(DATA_DIR / "products.csv")
    n_channels   = orders["channel_id"].nunique()
    n_categories = products["category_id"].nunique()
    # instruction.md specifies "One row per calendar month (January–December 2024)"
    n_months = 12

    ch = pd.read_csv(CH_PATH)
    assert {"channel_id", "net_profit"} <= set(ch.columns), \
        "channel_profitability.csv missing required columns."
    assert len(ch) == n_channels, f"Expected {n_channels} channel rows, got {len(ch)}."
    assert ch["net_profit"].tolist() == sorted(ch["net_profit"].tolist(), reverse=True), \
        "channel_profitability.csv must be sorted by net_profit descending."

    cat = pd.read_csv(CAT_PATH)
    assert {"category_id", "contribution_margin"} <= set(cat.columns), \
        "category_profitability.csv missing required columns."
    assert len(cat) == n_categories, f"Expected {n_categories} category rows, got {len(cat)}."
    assert cat["contribution_margin"].tolist() == sorted(cat["contribution_margin"].tolist(), reverse=True), \
        "category_profitability.csv must be sorted by contribution_margin descending."

    mo = pd.read_csv(MO_PATH)
    assert {"month", "net_profit"} <= set(mo.columns), \
        "monthly_profitability.csv missing required columns."
    assert len(mo) == n_months, f"Expected {n_months} monthly rows (Jan–Dec), got {len(mo)}."
    assert mo["month"].tolist() == sorted(mo["month"].tolist()), \
        "monthly_profitability.csv must be sorted by month ascending."


def test_case_04_summary_schema():
    with open(SUM_PATH) as f:
        s = json.load(f)
    required = {
        "most_profitable_channel", "least_profitable_channel",
        "most_profitable_category", "least_profitable_category",
        "best_margin_month", "worst_margin_month",
        "total_net_profit",
    }
    missing = required - set(s.keys())
    assert not missing, f"summary.json missing keys: {missing}"
    assert isinstance(s["most_profitable_channel"],  str)
    assert isinstance(s["least_profitable_channel"], str)
    assert isinstance(s["most_profitable_category"], str)
    assert isinstance(s["total_net_profit"],          float)


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

    # Normalize product_id separator and resolve category mapping.
    products["product_id"] = products["product_id"].str.replace("_", "-")
    orders = orders.merge(products[["product_id", "category_id"]], on="product_id", how="left")

    # SCD cost lookup: for each order, find the unit_cost effective at order_date.
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

    # Gross profit uses 2024 orders only; returns must price from all orders (incl. 2023 straddle).
    report_year = 2024
    orders_2024 = orders[pd.to_datetime(orders["order_date"]).dt.year == report_year].copy()

    # Channel net profit — Trap 14: apply cumulative FIFO cap across ALL returns first,
    # THEN filter to in-window (2024) returns for loss recognition.
    ret = returns.copy()
    ret = ret.merge(orders[["channel_id", "order_id", "unit_price", "quantity"]], on=["channel_id", "order_id"])
    # Cumulative FIFO cap across ALL return records for each order (both 2023 and 2024).
    ret = ret.sort_values("return_date")
    ret["_cum"]       = ret.groupby(["channel_id", "order_id"])["quantity_returned"].cumsum()
    ret["_prev_cum"]  = ret["_cum"] - ret["quantity_returned"]
    ret["_available"] = (ret["quantity"] - ret["_prev_cum"]).clip(lower=0)
    ret["quantity_returned"] = ret[["quantity_returned", "_available"]].min(axis=1)
    ret.drop(columns=["_cum", "_prev_cum", "_available"], inplace=True)
    # Filter to in-window (2024) returns AFTER the cap.
    ret = ret[pd.to_datetime(ret["return_date"]).dt.year == report_year].copy()
    # Apply return reason code policies: composite key is (reason_code, channel_id).
    ret = ret.merge(reason_codes, on=["reason_code", "channel_id"], how="left")
    ret["refund_pct"] = ret["refund_pct"].fillna(1.0)
    ret["fee_waived"]  = ret["fee_waived"].fillna(0).astype(int)
    ret["return_cost"] = (
        ret["quantity_returned"] * ret["unit_price"] * ret["refund_pct"]
        + ret["quantity_returned"] * ret["processing_cost_per_unit"] * (1 - ret["fee_waived"])
    )
    ch_gp = orders_2024.groupby("channel_id")["gross_profit"].sum().copy()
    ch_ret      = ret.groupby("channel_id")["return_cost"].sum()
    ch_fees_sum = ch_fees.groupby("channel_id")["fee_amount"].sum()
    ch_net = (
        ch_gp
        - ch_ret.reindex(ch_gp.index, fill_value=0.0)
        - ch_fees_sum.reindex(ch_gp.index, fill_value=0.0)
    )

    # Category net profit (Trap 2 — versioned allocation rules)
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

    # Monthly net profit
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
            mo_ts = pd.Timestamp(mo)
                computed = float(rev_by_month.get(mo, 0.0)) * pct
                cashback_by_month[mo] += max(computed, min_g)
        float(orders_2024["gross_profit"].sum())
        - float(ret["return_cost"].sum())
        - float(ch_fees["fee_amount"].sum())
        - float(shared_costs["total_cost"].sum())
        - float(cashback_by_month.sum()),
        2,
    ))

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
    }


# ── Hard test 1: top performers across all dimensions ────────────────────────

def test_case_05_top_performers(ground_truth):
    """Most profitable channel and category must match ground truth."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    assert s.get("most_profitable_channel") == ground_truth["most_profitable_channel"], (
        f"most_profitable_channel: got {s.get('most_profitable_channel')!r}, "
        f"expected {ground_truth['most_profitable_channel']!r}."
    )
    assert s.get("most_profitable_category") == ground_truth["most_profitable_category"], (
        f"most_profitable_category: got {s.get('most_profitable_category')!r}, "
        f"expected {ground_truth['most_profitable_category']!r}."
    )


# ── Hard test 2: margin months (Trap 3) ──────────────────────────────────────

def test_case_06_margin_months(ground_truth):
    """Best and worst net-profit months must match ground truth."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    assert s.get("best_margin_month") == ground_truth["best_margin_month"], (
        f"best_margin_month: got {s.get('best_margin_month')!r}, "
        f"expected {ground_truth['best_margin_month']!r}."
    )
    assert s.get("worst_margin_month") == ground_truth["worst_margin_month"], (
        f"worst_margin_month: got {s.get('worst_margin_month')!r}, "
        f"expected {ground_truth['worst_margin_month']!r}."
    )


# ── Hard test 3: least profitable channel (Trap 1) ───────────────────────────

def test_case_07_least_profitable_channel(ground_truth):
    """Least profitable channel must match ground truth."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["least_profitable_channel"]
    got = s.get("least_profitable_channel", "")
    assert got == exp, f"least_profitable_channel: got {got!r}, expected {exp!r}."


# ── Hard test 4: channel net profit values (Trap 1 + composite key sub-trap) ─

def test_case_08_channel_net_profit_values(ground_truth):
    """All channel net_profit values must be within ±10% of ground truth."""
    if not CH_PATH.exists():
        pytest.skip("channel_profitability.csv not found")
    df    = pd.read_csv(CH_PATH).set_index("channel_id")
    ch_gt = ground_truth["ch_net"]
    errors = []
    for ch_id, exp in ch_gt.items():
        if ch_id not in df.index:
            errors.append(f"  {ch_id}: missing from report")
            continue
        got = float(df.loc[ch_id, "net_profit"])
        tol = max(abs(exp) * 0.03, 500.0)
        if abs(got - exp) > tol:
            errors.append(f"  {ch_id}: got ${got:,.0f}, expected ${exp:,.0f} (±3%)")
    assert not errors, "Channel net profit values outside ±3% tolerance:\n" + "\n".join(errors)


# ── Hard test 5: category and monthly values ─────────────────────────────────

def test_case_09_profitability_values(ground_truth):
    """Category contribution margins (Trap 2) and best/worst month net profits
    (Trap 3) must be within tolerance of ground truth."""
    if not CAT_PATH.exists() or not MO_PATH.exists():
        pytest.skip("output CSVs not found")

    cat_df = pd.read_csv(CAT_PATH).set_index("category_id")
    cat_gt = ground_truth["cat_net"]
    errors = []
    for cat_id, exp in cat_gt.items():
        if cat_id not in cat_df.index:
            errors.append(f"  {cat_id}: missing from category report")
            continue
        got = float(cat_df.loc[cat_id, "contribution_margin"])
        tol = max(abs(exp) * 0.03, 2_000.0)
        if abs(got - exp) > tol:
            errors.append(f"  {cat_id}: got ${got:,.0f}, expected ${exp:,.0f} (±3%)")

    mo_df = pd.read_csv(MO_PATH).set_index("month")
    mo_gt = ground_truth["mo_net"]
    for label, mo_id in [
        ("best_margin_month",  ground_truth["best_margin_month"]),
        ("worst_margin_month", ground_truth["worst_margin_month"]),
    ]:
        if mo_id not in mo_df.index:
            errors.append(f"  {mo_id}: missing from monthly report")
            continue
        exp = float(mo_gt[mo_id])
        got = float(mo_df.loc[mo_id, "net_profit"])
        tol = max(abs(exp) * 0.03, 5_000.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {label} ({mo_id}): got ${got:,.0f}, expected ${exp:,.0f} (±3%)"
            )

    assert not errors, "Profitability values outside tolerance:\n" + "\n".join(errors)


# ── Hard test 6: total net profit (all three deductions) ─────────────────────

def test_case_10_total_net_profit(ground_truth):
    """total_net_profit must account for all three cost categories: return losses,
    shared overhead, and promotional cashback. Omitting any one deduction inflates
    the result significantly."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["total_net_profit"]
    got = float(s.get("total_net_profit", 0.0))
    tol = max(abs(exp) * 0.02, 5_000.0)
    assert abs(got - exp) <= tol, (
        f"total_net_profit: got ${got:,.2f}, expected ${exp:,.2f} (±2%). "
        f"Must equal total gross profit minus return losses, shared overhead, and cashback."
    )


# ── Hard test 7: straddle channels (Trap 14 — cap × date-window) ─────────────

def test_case_11_straddle_channel_net_profit(ground_truth):
    """CH01 and CH02 net profits must match ground truth within ±3%.
    These channels have orders spanning December 2023 whose return histories
    cross the reporting-year boundary; correctly accounting for all returns
    when applying the cumulative cap is required to get the right loss figure."""
    if not CH_PATH.exists():
        pytest.skip("channel_profitability.csv not found")
    df    = pd.read_csv(CH_PATH).set_index("channel_id")
    ch_gt = ground_truth["ch_net"]
    errors = []
    for ch_id in ["CH01", "CH02"]:
        if ch_id not in ch_gt.index:
            errors.append(f"  {ch_id}: not found in ground truth")
            continue
        exp = float(ch_gt[ch_id])
        if ch_id not in df.index:
            errors.append(f"  {ch_id}: missing from channel_profitability.csv")
            continue
        got = float(df.loc[ch_id, "net_profit"])
        tol = max(abs(exp) * 0.03, 500.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {ch_id}: got ${got:,.2f}, expected ${exp:,.2f} (±3%)"
            )
    assert not errors, (
        "CH01 and CH02 net profits outside ±3% tolerance:\n" + "\n".join(errors)
    )


# ── Hard test 8: February minimum guarantee (Trap 16) ────────────────────────

def test_case_12_february_minimum_cashback(ground_truth):
    """February net profit must reflect the Q1 promotion's minimum monthly guarantee.
    February has no orders, so revenue-based cashback is zero, but the guarantee
    still applies.  A model that iterates over orders to compute cashback never
    visits February and omits the obligation entirely."""
    if not MO_PATH.exists():
        pytest.skip("monthly_profitability.csv not found")
    mo_df = pd.read_csv(MO_PATH).set_index("month")
    mo_gt = ground_truth["mo_net"]
    feb = "2024-02"
    if feb not in mo_df.index:
        pytest.fail("2024-02 is missing from monthly_profitability.csv")
    exp = float(mo_gt[feb])
    got = float(mo_df.loc[feb, "net_profit"])
    tol = max(abs(exp) * 0.03, 2_000.0)
    assert abs(got - exp) <= tol, (
        f"February 2024 net_profit: got ${got:,.2f}, expected ${exp:,.2f} (±$2,000). "
        f"The Q1 promotion's minimum monthly guarantee must be applied even when "
        f"no orders were placed in February."
    )

