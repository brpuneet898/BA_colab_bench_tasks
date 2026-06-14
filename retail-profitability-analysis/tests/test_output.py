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

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = (
    WORKSPACE_DIR / "data"
    if (WORKSPACE_DIR / "data").exists()
    else Path(__file__).parent.parent / "environment" / "data"
)

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
    shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions   = pd.read_csv(DATA_DIR / "promotions.csv")

    # Trap 1 anchors
    assert len(orders) == 50_788, \
        f"orders.csv must not be modified (expected 50,788 rows, got {len(orders)})."
    assert len(returns) == 7_824, \
        f"returns.csv must not be modified (expected 7,824 rows, got {len(returns)})."
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

    # Trap 3 anchors
    orders["month"] = pd.to_datetime(orders["order_date"]).dt.month
    nov_count = len(orders[orders["month"] == 11])
    jun_count = len(orders[orders["month"] == 6])
    assert nov_count > jun_count * 1.5, \
        f"November order count ({nov_count}) should be >1.5x June ({jun_count})."
    assert len(promotions) == 1, \
        f"promotions.csv must not be modified (expected 1 row, got {len(promotions)})."
    assert float(promotions.iloc[0]["cashback_pct"]) >= 0.40, \
        "promotions.csv cashback_pct must not be reduced below 0.40."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_outputs_exist():
    assert CH_PATH.exists(),  "channel_profitability.csv not found in /workspace."
    assert CAT_PATH.exists(), "category_profitability.csv not found in /workspace."
    assert MO_PATH.exists(),  "monthly_profitability.csv not found in /workspace."
    assert SUM_PATH.exists(), "summary.json not found in /workspace."


def test_case_03_csv_schemas_and_sort():
    """Validates columns, row counts, and sort order for all three output CSVs."""
    orders = pd.read_csv(DATA_DIR / "orders.csv")
    n_channels   = orders["channel_id"].nunique()
    n_categories = orders["category_id"].nunique()
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
    orders       = pd.read_csv(DATA_DIR / "orders.csv")
    returns      = pd.read_csv(DATA_DIR / "returns.csv")
    shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions   = pd.read_csv(DATA_DIR / "promotions.csv")

    orders["gross_profit"] = (orders["unit_price"] - orders["unit_cost"]) * orders["quantity"]
    orders["revenue"]      = orders["unit_price"] * orders["quantity"]

    # Channel net profit
    report_year = int(pd.to_datetime(orders["order_date"]).dt.year.mode()[0])
    ret = returns[pd.to_datetime(returns["return_date"]).dt.year == report_year].copy()
    ret = ret.merge(orders[["channel_id", "order_id", "unit_price"]], on=["channel_id", "order_id"])
    ret["return_cost"] = (
        ret["quantity_returned"] * ret["unit_price"]
        + ret["quantity_returned"] * ret["processing_cost_per_unit"]
    )
    ch_gp  = orders.groupby("channel_id")["gross_profit"].sum()
    ch_ret = ret.groupby("channel_id")["return_cost"].sum()
    ch_net = ch_gp - ch_ret.reindex(ch_gp.index, fill_value=0.0)

    # Category net profit (Trap 2 — versioned allocation rules)
    orders["month"] = pd.to_datetime(orders["order_date"]).dt.to_period("M").astype(str)
    alloc_rules["effective_from"] = pd.to_datetime(alloc_rules["effective_from"])
    alloc_rules_sorted = alloc_rules.sort_values("effective_from")

    cat_gp    = orders.groupby("category_id")["gross_profit"].sum()
    cat_alloc = pd.Series(0.0, index=cat_gp.index)

    for _, sc_row in shared_costs.iterrows():
        cost_month_ts = pd.to_datetime(sc_row["month"])
        ct  = sc_row["cost_type"]
        amt = float(sc_row["total_cost"])
        applicable = alloc_rules_sorted[
            (alloc_rules_sorted["cost_type"] == ct) &
            (alloc_rules_sorted["effective_from"] <= cost_month_ts)
        ]
        if applicable.empty:
            continue
        basis = applicable.iloc[-1]["allocation_basis"]
        month_orders = orders[orders["month"] == sc_row["month"]]
        if basis == "order_count":
            shares = month_orders.groupby("category_id")["order_id"].count()
        else:
            shares = month_orders.groupby("category_id")["revenue"].sum()
        total = shares.sum()
        if total > 0:
            cat_alloc = cat_alloc.add((shares / total) * amt, fill_value=0.0)

    cat_net = cat_gp - cat_alloc

    # Monthly net profit
    mo_gp = orders.groupby("month")["gross_profit"].sum()
    all_months = [f"{report_year}-{str(m).zfill(2)}" for m in range(1, 13)]
    mo_gp = mo_gp.reindex(all_months, fill_value=0.0)

    cashback_by_month = pd.Series(0.0, index=mo_gp.index)
    for _, promo in promotions.iterrows():
        start = pd.Timestamp(promo["start_date"])
        end   = pd.Timestamp(promo["end_date"])
        pct   = float(promo["cashback_pct"])
        mask  = (
            (pd.to_datetime(orders["order_date"]) >= start) &
            (pd.to_datetime(orders["order_date"]) <= end)
        )
        for mo, rev in orders[mask].groupby("month")["revenue"].sum().items():
            cashback_by_month[mo] = cashback_by_month.get(mo, 0.0) + rev * pct

    mo_net = mo_gp - cashback_by_month.reindex(mo_gp.index, fill_value=0.0)

    total_net = float(round(
        float(orders["gross_profit"].sum())
        - float(ret["return_cost"].sum())
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
        tol = max(abs(exp) * 0.10, 500.0)
        if abs(got - exp) > tol:
            errors.append(f"  {ch_id}: got ${got:,.0f}, expected ${exp:,.0f} (±10%)")
    assert not errors, "Channel net profit values outside ±10% tolerance:\n" + "\n".join(errors)


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
        tol = max(abs(exp) * 0.10, 2_000.0)
        if abs(got - exp) > tol:
            errors.append(f"  {cat_id}: got ${got:,.0f}, expected ${exp:,.0f} (±10%)")

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
        tol = max(abs(exp) * 0.10, 5_000.0)
        if abs(got - exp) > tol:
            errors.append(
                f"  {label} ({mo_id}): got ${got:,.0f}, expected ${exp:,.0f} (±10%)"
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
    tol = max(abs(exp) * 0.05, 5_000.0)
    assert abs(got - exp) <= tol, (
        f"total_net_profit: got ${got:,.2f}, expected ${exp:,.2f} (±5%). "
        f"Must equal total gross profit minus return losses, shared overhead, and cashback."
    )
