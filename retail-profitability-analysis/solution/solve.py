"""
2024 Retail Profitability Report.

Four assumption-blindness traps:

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
    revenue basis for Q1–Q3 2024 and switched to order_count from Q4 2024.
    Electronics (CAT01) has high revenue per order but few orders. The Q1–Q3
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
    empty groups, producing 11 rows. The instruction requires January–December;
    the solution must reindex to all 12 months.
"""

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

orders       = pd.read_csv(DATA_DIR / "orders.csv")
returns      = pd.read_csv(DATA_DIR / "returns.csv")
products     = pd.read_csv(DATA_DIR / "products.csv")
cost_hist    = pd.read_csv(DATA_DIR / "product_cost_history.csv")
shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
alloc_rules["cost_type"] = alloc_rules["cost_type"].str.lower().str.strip()
promotions   = pd.read_csv(DATA_DIR / "promotions.csv")
ch_fees_df   = pd.read_csv(DATA_DIR / "channel_return_fees.csv")

# Resolve product → category; normalize product_id separator before joining.
products["product_id"] = products["product_id"].str.replace("_", "-")
orders = orders.merge(products[["product_id", "category_id"]], on="product_id", how="left")

# SCD cost lookup: find the unit_cost in effect at each order's date.
# products.csv carries only the current snapshot; product_cost_history.csv
# has every effective rate.  Using the snapshot for all orders overstates COGS
# for Q1-H1 Electronics orders where the lower historical cost applies.
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


# ── Channel profitability ─────────────────────────────────────────────────────

# BOTTLENECK: some returns from late-December orders have return_date in January 2025.
# Instruction scopes return losses to return_date within the reporting calendar year.
report_year = int(pd.to_datetime(orders["order_date"]).dt.year.mode()[0])
ret = returns[pd.to_datetime(returns["return_date"]).dt.year == report_year].copy()
# BOTTLENECK: order_id resets per channel; the true join key is (channel_id, order_id).
# Naive merge on order_id alone creates a Cartesian explosion that inflates return losses.
ret = ret.merge(orders[["channel_id", "order_id", "unit_price", "quantity"]], on=["channel_id", "order_id"])
# BOTTLENECK: some records have quantity_returned > order quantity (data entry error); cap before computing losses.
ret["quantity_returned"] = ret[["quantity_returned", "quantity"]].min(axis=1)
ret["return_cost"] = (
    ret["quantity_returned"] * ret["unit_price"]
    + ret["quantity_returned"] * ret["processing_cost_per_unit"]
)

ch_gp       = orders.groupby("channel_id")["gross_profit"].sum()
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

# BOTTLENECK: cost_allocation_rules.csv is versioned via effective_from. Warehousing
# switches from revenue to order_count in Q4 2024. Applying a single annual rule
# misattributes 9 months of revenue-based warehouse costs. Must iterate over each
# cost month, pick the latest effective rule on or before that month, and allocate.

orders["month"] = pd.to_datetime(orders["order_date"]).dt.to_period("M").astype(str)

alloc_rules["effective_from"] = pd.to_datetime(alloc_rules["effective_from"])
alloc_rules_sorted = alloc_rules.sort_values("effective_from")

cat_gp    = orders.groupby("category_id")["gross_profit"].sum()
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

    month_orders = orders[orders["month"] == sc_row["month"]]
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
        "category_id":        cat_net.index,
        "contribution_margin": cat_net.round(2).values,
    })
    .sort_values("contribution_margin", ascending=False)
    .reset_index(drop=True)
)
cat_report.to_csv(WORKSPACE_DIR / "category_profitability.csv", index=False)


# ── Monthly profitability ─────────────────────────────────────────────────────

mo_gp  = orders.groupby("month")["gross_profit"].sum()

cashback_by_month = pd.Series(0.0, index=mo_gp.index)
for _, promo in promotions.iterrows():
    start = pd.Timestamp(promo["start_date"])
    end   = pd.Timestamp(promo["end_date"])
    pct   = float(promo["cashback_pct"])
    mask  = (
        (pd.to_datetime(orders["order_date"]) >= start)
        & (pd.to_datetime(orders["order_date"]) <= end)
    )
    for mo, rev in orders[mask].groupby("month")["revenue"].sum().items():
        cashback_by_month[mo] = cashback_by_month.get(mo, 0.0) + rev * pct

mo_net = mo_gp - cashback_by_month.reindex(mo_gp.index, fill_value=0.0)

# BOTTLENECK: February 2024 has no orders. pandas groupby silently drops empty groups.
# Instruction requires one row per calendar month (Jan-Dec 2024).
all_months = [f"{report_year}-{str(m).zfill(2)}" for m in range(1, 13)]
mo_net = mo_net.reindex(all_months, fill_value=0.0)

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

total_net_profit = float(round(
    float(orders["gross_profit"].sum())
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
