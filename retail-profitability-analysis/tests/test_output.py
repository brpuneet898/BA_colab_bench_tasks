"""
Tests for the 2024 retail profitability report.

Contract (instruction.md): the deliverable is
    /workspace/channel_profitability.csv
    /workspace/category_profitability.csv
    /workspace/monthly_profitability.csv
    /workspace/summary.json

Three assumption-blindness traps:
    1. CH03 (Marketplace) has the highest gross revenue but a ~46% return rate.
       After netting return losses, CH03 drops from #1 to last place.
    2. cost_allocation_rules.csv specifies order_count allocation for shared costs.
       Electronics (CAT01) has few orders but high revenue per order; Apparel (CAT02)
       has many orders but low revenue per order. Revenue-based allocation makes
       Apparel appear most profitable; order-count allocation flips Electronics to first.
    3. November has a 2x order-volume spike from a promotion but also a 45% cashback
       obligation on all November revenue. Without applying cashback, November looks like
       the best month; after cashback, November is the worst (negative net profit).
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
    truncating the input files."""
    orders  = pd.read_csv(DATA_DIR / "orders.csv")
    returns = pd.read_csv(DATA_DIR / "returns.csv")

    assert len(orders) == 54_894, \
        f"orders.csv must not be modified (expected 54,894 rows, got {len(orders)})."
    assert len(returns) == 8_363, \
        f"returns.csv must not be modified (expected 8,363 rows, got {len(returns)})."

    # CH03 return signal must be present
    ch03_returns = returns[returns["channel_id"] == "CH03"]
    assert len(ch03_returns) >= 6_000, \
        f"CH03 return rows must not be removed (expected ≥6,000, got {len(ch03_returns)})."

    # November volume spike must be present (promotions.csv must not be emptied)
    orders["month"] = pd.to_datetime(orders["order_date"]).dt.month
    nov_count = len(orders[orders["month"] == 11])
    jun_count = len(orders[orders["month"] == 6])
    assert nov_count > jun_count * 1.5, \
        f"November order count ({nov_count}) should be >1.5x June ({jun_count}); " \
        f"promotions.csv may have been modified."


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_02_outputs_exist():
    assert CH_PATH.exists(),  "channel_profitability.csv not found in /workspace."
    assert CAT_PATH.exists(), "category_profitability.csv not found in /workspace."
    assert MO_PATH.exists(),  "monthly_profitability.csv not found in /workspace."
    assert SUM_PATH.exists(), "summary.json not found in /workspace."


def test_case_03_channel_report_schema():
    df = pd.read_csv(CH_PATH)
    required = {"channel_id", "net_profit"}
    missing  = required - set(df.columns)
    assert not missing, f"channel_profitability.csv missing columns: {missing}"
    assert len(df) == 5, f"Expected 5 rows (one per channel), got {len(df)}."


def test_case_04_category_report_schema():
    df = pd.read_csv(CAT_PATH)
    required = {"category_id", "net_profit"}
    missing  = required - set(df.columns)
    assert not missing, f"category_profitability.csv missing columns: {missing}"
    assert len(df) == 5, f"Expected 5 rows (one per category), got {len(df)}."


def test_case_05_summary_schema():
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
    orders      = pd.read_csv(DATA_DIR / "orders.csv")
    returns     = pd.read_csv(DATA_DIR / "returns.csv")
    shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
    alloc_rules = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
    promotions  = pd.read_csv(DATA_DIR / "promotions.csv")

    orders["gross_profit"] = (orders["unit_price"] - orders["unit_cost"]) * orders["quantity"]
    orders["revenue"]      = orders["unit_price"] * orders["quantity"]

    # Channel net profit (Trap 1)
    ret = returns.merge(orders[["order_id", "unit_price"]], on="order_id")
    ret["return_cost"] = (
        ret["quantity_returned"] * ret["unit_price"]
        + ret["quantity_returned"] * ret["processing_cost_per_unit"]
    )
    ch_gp  = orders.groupby("channel_id")["gross_profit"].sum()
    ch_ret = ret.groupby("channel_id")["return_cost"].sum()
    ch_net = ch_gp - ch_ret.reindex(ch_gp.index, fill_value=0.0)

    # Category net profit (Trap 2)
    cat_gp     = orders.groupby("category_id")["gross_profit"].sum()
    cat_orders = orders.groupby("category_id")["order_id"].count()
    cat_rev    = orders.groupby("category_id")["revenue"].sum()
    total_ord  = cat_orders.sum()
    total_rev  = cat_rev.sum()

    cat_alloc = pd.Series(0.0, index=cat_gp.index)
    for _, rule in alloc_rules.iterrows():
        ct    = rule["cost_type"]
        basis = rule["allocation_basis"]
        amt   = float(shared_costs[shared_costs["cost_type"] == ct]["total_cost"].sum())
        if basis == "order_count":
            cat_alloc += (cat_orders / total_ord) * amt
        else:
            cat_alloc += (cat_rev / total_rev) * amt

    cat_net = cat_gp - cat_alloc

    # Monthly net profit (Trap 3)
    orders["month"] = pd.to_datetime(orders["order_date"]).dt.to_period("M").astype(str)
    mo_gp  = orders.groupby("month")["gross_profit"].sum()
    mo_rev = orders.groupby("month")["revenue"].sum()

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
        float(ch_net.sum()) - float(cat_alloc.sum()) - float(cashback_by_month.sum()),
        2,
    ))

    return {
        "most_profitable_channel":  str(ch_net.idxmax()),
        "least_profitable_channel": str(ch_net.idxmin()),
        "ch_net":                   ch_net,
        "most_profitable_category": str(cat_net.idxmax()),
        "least_profitable_category": str(cat_net.idxmin()),
        "best_margin_month":        str(mo_net.idxmax()),
        "worst_margin_month":       str(mo_net.idxmin()),
        "total_net_profit":         total_net,
    }


# ── Hard test 1: most profitable channel (Trap 1) ────────────────────────────

def test_case_06_most_profitable_channel(ground_truth):
    """CH03 is #1 by gross revenue but drops to last after return losses.
    Naive model returns 'CH03'; correct model returns the fixture value."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["most_profitable_channel"]
    got = s.get("most_profitable_channel", "")
    assert got == exp, (
        f"most_profitable_channel: got {got!r}, expected {exp!r}. "
        f"Net channel profit = gross profit − return losses."
    )


# ── Hard test 2: least profitable channel (Trap 1) ───────────────────────────

def test_case_07_least_profitable_channel(ground_truth):
    """CH03's ~46% return rate drives it to negative net profit.
    Naive model (no returns) predicts a low-volume channel; correct model returns 'CH03'."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["least_profitable_channel"]
    got = s.get("least_profitable_channel", "")
    assert got == exp, (
        f"least_profitable_channel: got {got!r}, expected {exp!r}. "
        f"Return losses wipe out the channel's gross profit."
    )


# ── Hard test 3: channel net profit values (Trap 1 support) ──────────────────

def test_case_08_channel_net_profit_values(ground_truth):
    """All five channel net_profit values must be within ±10% of ground truth.
    A model that ignores returns will report CH03 net profit ~$1.1M instead of -$143K."""
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


# ── Hard test 4: most profitable category (Trap 2) ───────────────────────────

def test_case_09_most_profitable_category(ground_truth):
    """Revenue-based allocation makes Apparel (CAT02) appear most profitable.
    Correct order-count allocation flips Electronics (CAT01) to first."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)
    exp = ground_truth["most_profitable_category"]
    got = s.get("most_profitable_category", "")
    assert got == exp, (
        f"most_profitable_category: got {got!r}, expected {exp!r}. "
        f"Shared costs must be split by order_count as specified in "
        f"cost_allocation_rules.csv, not by revenue."
    )


# ── Hard test 5: best and worst margin months (Trap 3) ───────────────────────

def test_case_10_margin_months(ground_truth):
    """November has the highest gross revenue due to a 2x promotional volume spike,
    but a 45% cashback obligation turns it into the worst net-profit month.
    Naive model (no cashback) returns November as best; correct model returns it as worst."""
    if not SUM_PATH.exists():
        pytest.skip("summary.json not found")
    with open(SUM_PATH) as f:
        s = json.load(f)

    exp_best  = ground_truth["best_margin_month"]
    exp_worst = ground_truth["worst_margin_month"]
    got_best  = s.get("best_margin_month", "")
    got_worst = s.get("worst_margin_month", "")

    assert got_best == exp_best, (
        f"best_margin_month: got {got_best!r}, expected {exp_best!r}. "
        f"Cashback obligations from promotions.csv must be subtracted from "
        f"the promotion month's gross profit."
    )
    assert got_worst == exp_worst, (
        f"worst_margin_month: got {got_worst!r}, expected {exp_worst!r}. "
        f"November's cashback obligation ({exp_worst}) drives it to negative net profit."
    )
