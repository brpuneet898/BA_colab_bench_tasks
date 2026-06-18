"""
2024 Retail Profitability Report.

Traps embedded in the data:

Channel profitability (Trap 1):
    Gross profit per channel minus return losses. CH03 (Marketplace) is #1
    by gross revenue but has a ~46% return rate; after netting returns it
    drops from first to last in net profit.
    Sub-trap: order_id resets per channel — the true join key is
    (channel_id, order_id). Naive merge on order_id alone produces a
    Cartesian explosion that inflates return losses across all channels.

Category profitability (Trap 7):
    orders.csv carries no category_id; agents must join with products.csv to
    resolve it. products.csv stores CAT01 product_ids with an underscore
    separator (SKU_0001) while orders.csv uses a hyphen (SKU-0001).
    A naive merge(on='product_id') silently drops all 4,412 CAT01 orders
    via pandas inner-join default — no NaN, no error. CAT01 disappears from
    category output and all downstream figures (channel GP, monthly totals,
    total_net_profit) are wrong because they too derive from the merged frame.

SCD cost trap:
    orders.csv carries no unit_cost. products.csv holds the CURRENT unit_cost
    (post-July snapshot). product_cost_history.csv is the SCD table: all 50
    SKUs have a base cost from 2024-01-01; CAT01 SKUs have an additional
    15 % increase effective 2024-07-01. A model that joins products.csv for
    unit_cost uses the post-July cost for Q1-H1 CAT01 orders, overstating
    COGS and understating Electronics gross profit and total_net_profit.

Category profitability (Trap 2):
    Gross profit per category minus shared-cost allocation. The
    cost_allocation_rules.csv table is versioned: warehousing used a
    revenue basis for Q1-Q3 2024 and switched to order_count from Q4 2024.
    Electronics (CAT01) has high revenue per order but few orders. The Q1-Q3
    revenue-based warehousing allocation charges Electronics heavily, making
    Apparel (CAT02) the most profitable category on a contribution-margin basis.
    A model that applies only the latest rule (order_count) to all months
    under-allocates to Electronics and incorrectly ranks it first.

Monthly profitability (Trap 3):
    Gross profit per month minus cashback obligations from promotions.csv.
    November has a 2x promotional volume spike (highest gross revenue) but
    a 45% cashback obligation — after cashback, November is the worst month
    (negative net profit).
    Sub-trap: February 2024 has no orders. pandas groupby silently drops
    empty groups, producing 11 rows. The instruction requires January-December;
    the solution must reindex to all 12 months.

Return-cap x date-window straddle (Trap 14):
    orders.csv includes ~200 Dec-2023 straddle orders. Each has two returns:
    a Dec-2023 return (qty=order_qty-1, out of 2024 loss window) and a Jan-2024
    return (qty=order_qty, in-window). The cumulative FIFO cap must be applied
    across ALL returns for each order (both years) BEFORE filtering to 2024
    return_dates for loss recognition. A filter-first implementation excludes
    the Dec-2023 return before the cap runs, letting the Jan-2024 return
    charge the full order_qty instead of only the 1 remaining unit.

Vendor credit trap (Trap A):
    shared_costs.csv contains two negative entries: a June Warehousing rebate
    (-$18,000) and a September fulfillment credit (-$7,500). These credits
    reduce the overhead allocated to categories and reduce total_net_profit.
    A model that filters shared_costs to positive rows only before iterating
    silently ignores $25,500 in vendor credits, overstating overhead and
    understating contribution margins.

Unallocated overhead trap (Trap B):
    shared_costs.csv includes a Marketing cost_type ($25,000/month, $300,000
    annual) that has no matching row in cost_allocation_rules.csv. Marketing
    overhead is deducted from total_net_profit but is NOT allocated to any
    product category. A model that attempts to allocate Marketing anyway
    (guessing a basis) distorts contribution margins. A model that skips
    Marketing from total_net_profit (because it has no rule) overstates
    company-level profit by $300,000.

Partial-month promotion guarantee (Trap C):
    PROMO-002 (Q1 Loyalty Commitment) runs Jan 15 - Apr 14, 2024. The
    minimum monthly guarantee ($10,000) applies to every calendar month that
    overlaps with the promotion date range, including January (active Jan 15-31)
    and April (active Apr 1-14). A model using `start <= month_start <= end`
    to determine active months will exclude January entirely (start=Jan 15 >
    month_start=Jan 1), producing zero cashback for January instead of $10,000.

Fee waiver quantity threshold (Trap D):
    return_reason_codes.csv stores waiver_max_qty (integer) instead of a
    boolean fee_waived flag. The handling fee is waived only when
    quantity_returned <= waiver_max_qty. CHANGED_MIND has waiver_max_qty=3
    and WRONG_SIZE has waiver_max_qty=2; 708 returns across these codes exceed
    their thresholds and must be charged the processing fee. A model that
    treats waiver_max_qty as a truthy boolean (any positive integer = always
    waived) will waive fees on all CHANGED_MIND and WRONG_SIZE returns,
    understating return losses by ~$25,413.
"""

import calendar
import json
import pandas as pd
import numpy as np
from pathlib import Path

if Path("/.dockerenv").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
else:
    _repo = Path(__file__).parent.parent
    DATA_DIR    = _repo / "environment" / "data"
    WORKSPACE_DIR = _repo


# ── Load ──────────────────────────────────────────────────────────────────────

orders          = pd.read_csv(DATA_DIR / "orders.csv")
returns         = pd.read_csv(DATA_DIR / "returns.csv")
products        = pd.read_csv(DATA_DIR / "products.csv")
cost_hist       = pd.read_csv(DATA_DIR / "product_cost_history.csv")
shared_costs    = pd.read_csv(DATA_DIR / "shared_costs.csv")
alloc_rules     = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
alloc_rules["cost_type"] = alloc_rules["cost_type"].str.lower().str.strip()
promotions      = pd.read_csv(DATA_DIR / "promotions.csv")
ch_fees_df      = pd.read_csv(DATA_DIR / "channel_return_fees.csv")
reason_codes_df = pd.read_csv(DATA_DIR / "return_reason_codes.csv")

# Resolve product -> category; normalize product_id separator before joining.
products["product_id"] = products["product_id"].str.replace("_", "-")
orders = orders.merge(products[["product_id", "category_id"]], on="product_id", how="left")

# SCD cost lookup: find the unit_cost in effect at each order's date.
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

report_year  = 2024
orders_2024  = orders[pd.to_datetime(orders["order_date"]).dt.year == report_year].copy()


# ── Channel profitability ─────────────────────────────────────────────────────

ret = returns.copy()
ret = ret.merge(orders[["channel_id", "order_id", "unit_price", "quantity"]], on=["channel_id", "order_id"])

# Preserve original requested quantity before FIFO cap; fee waiver is evaluated
# against the original quantity_returned column, not the capped accepted quantity.
ret["_qty_raw"] = ret["quantity_returned"]

# Cumulative FIFO cap across ALL return records (both 2023 and 2024) before filtering.
ret = ret.sort_values("return_date")
ret["_cum"]       = ret.groupby(["channel_id", "order_id"])["quantity_returned"].cumsum()
ret["_prev_cum"]  = ret["_cum"] - ret["quantity_returned"]
ret["_available"] = (ret["quantity"] - ret["_prev_cum"]).clip(lower=0)
ret["quantity_returned"] = ret[["quantity_returned", "_available"]].min(axis=1)
ret.drop(columns=["_cum", "_prev_cum", "_available"], inplace=True)

ret = ret[pd.to_datetime(ret["return_date"]).dt.year == report_year].copy()

# Apply return reason code policies: composite key is (reason_code, channel_id).
# Fee waiver is evaluated against the original requested quantity (_qty_raw).
ret = ret.merge(reason_codes_df, on=["reason_code", "channel_id"], how="left")
ret["refund_pct"]     = ret["refund_pct"].fillna(1.0)
ret["waiver_max_qty"] = ret["waiver_max_qty"].fillna(0).astype(int)
ret["fee_applies"]    = (ret["_qty_raw"] > ret["waiver_max_qty"]).astype(int)
ret.drop(columns=["_qty_raw"], inplace=True)
ret["return_cost"] = (
    ret["quantity_returned"] * ret["unit_price"] * ret["refund_pct"]
    + ret["quantity_returned"] * ret["processing_cost_per_unit"] * ret["fee_applies"]
)

ch_gp = orders_2024.groupby("channel_id")["gross_profit"].sum().copy()

ch_ret      = ret.groupby("channel_id")["return_cost"].sum()
ch_fees_sum = ch_fees_df.groupby("channel_id")["fee_amount"].sum()
ch_net = (
    ch_gp
    - ch_ret.reindex(ch_gp.index, fill_value=0.0)
    - ch_fees_sum.reindex(ch_gp.index, fill_value=0.0)
)

ch_report = (
    pd.DataFrame({
        "channel_id": ch_net.index,
        "net_profit": ch_net.round(2).values,
    })
    .sort_values("net_profit", ascending=False)
    .reset_index(drop=True)
)
ch_report.to_csv(WORKSPACE_DIR / "channel_profitability.csv", index=False)


# ── Category profitability ────────────────────────────────────────────────────

# Trap 2: cost_allocation_rules.csv is versioned via effective_from.
# Trap A: shared_costs.csv includes negative vendor credit rows — do NOT filter
#         them out; they reduce the overhead charged to categories.
# Trap B: Marketing cost_type has no allocation rule; it is silently skipped
#         by the `if applicable.empty: continue` guard and is NOT allocated to
#         any category, but it IS deducted from total_net_profit below.

orders_2024["month"] = pd.to_datetime(orders_2024["order_date"]).dt.to_period("M").astype(str)

alloc_rules["effective_from"] = pd.to_datetime(alloc_rules["effective_from"])
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

cat_report = (
    pd.DataFrame({
        "category_id":         cat_net.index,
        "contribution_margin": cat_net.round(2).values,
    })
    .sort_values("contribution_margin", ascending=False)
    .reset_index(drop=True)
)
cat_report.to_csv(WORKSPACE_DIR / "category_profitability.csv", index=False)


# ── Monthly profitability ─────────────────────────────────────────────────────

mo_gp  = orders_2024.groupby("month")["gross_profit"].sum()

# Trap C: PROMO-002 starts Jan 15, so the simple `start <= month_start <= end`
# check excludes January entirely (Jan 15 > Jan 1). The correct check is whether
# the promotion date range OVERLAPS with the calendar month at all. The minimum
# monthly guarantee applies to every overlapping month, including partial ones.
all_months = [f"{report_year}-{str(m).zfill(2)}" for m in range(1, 13)]
cashback_by_month = pd.Series(0.0, index=all_months)
for _, promo in promotions.iterrows():
    start = pd.Timestamp(promo["start_date"])
    end   = pd.Timestamp(promo["end_date"])
    pct   = float(promo["cashback_pct"])
    min_g = float(promo.get("min_monthly_guarantee", 0.0))
    mask  = (
        (pd.to_datetime(orders_2024["order_date"]) >= start)
        & (pd.to_datetime(orders_2024["order_date"]) <= end)
    )
    rev_by_month = orders_2024[mask].groupby("month")["revenue"].sum()
    for mo in all_months:
        mo_ts  = pd.Timestamp(mo)
        last_d = calendar.monthrange(mo_ts.year, mo_ts.month)[1]
        mo_end = pd.Timestamp(f"{mo_ts.year}-{mo_ts.month:02d}-{last_d}")
        # Promotion overlaps with this month if its range intersects [mo_ts, mo_end].
        if start <= mo_end and end >= mo_ts:
            computed = float(rev_by_month.get(mo, 0.0)) * pct
            cashback_by_month[mo] += max(computed, min_g)

mo_net = mo_gp.reindex(all_months, fill_value=0.0) - cashback_by_month

mo_report = (
    pd.DataFrame({
        "month":      mo_net.index,
        "net_profit": mo_net.round(2).values,
    })
    .sort_values("month")
    .reset_index(drop=True)
)
mo_report.to_csv(WORKSPACE_DIR / "monthly_profitability.csv", index=False)


# ── Summary JSON ──────────────────────────────────────────────────────────────

# shared_costs["total_cost"].sum() correctly includes negative vendor credits
# (Trap A) and Marketing overhead (Trap B), so both are deducted from total_net_profit.
total_net_profit = float(round(
    float(orders_2024["gross_profit"].sum())
    - float(ret["return_cost"].sum())
    - float(ch_fees_df["fee_amount"].sum())
    - float(shared_costs["total_cost"].sum())
    - float(cashback_by_month.sum()),
    2,
))

summary = {
    "most_profitable_channel":   str(ch_net.idxmax()),
    "least_profitable_channel":  str(ch_net.idxmin()),
    "most_profitable_category":  str(cat_net.idxmax()),
    "least_profitable_category": str(cat_net.idxmin()),
    "best_margin_month":         str(mo_net.idxmax()),
    "worst_margin_month":        str(mo_net.idxmin()),
    "total_net_profit":          total_net_profit,
}

with open(WORKSPACE_DIR / "summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print("Done.")
print(f"  most_profitable_channel:  {summary['most_profitable_channel']}")
print(f"  least_profitable_channel: {summary['least_profitable_channel']}")
print(f"  most_profitable_category: {summary['most_profitable_category']}")
print(f"  best_margin_month:        {summary['best_margin_month']}")
print(f"  worst_margin_month:       {summary['worst_margin_month']}")
print(f"  total_net_profit:         ${summary['total_net_profit']:,.2f}")
