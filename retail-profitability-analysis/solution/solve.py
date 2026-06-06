"""
2024 Retail Profitability Report.

Three independent analyses:

Channel profitability (Trap 1):
    Gross profit per channel minus return losses. Return losses =
    quantity_returned * unit_price (refund) + quantity_returned *
    processing_cost_per_unit (handling). CH03 (Marketplace) is #1 by
    gross revenue but has a ~46% return rate; after netting returns it
    drops from first to last in net profit.

Category profitability (Trap 2):
    Gross profit per category minus shared-cost allocation. The
    allocation_basis in cost_allocation_rules.csv specifies order_count
    (not revenue) for all cost types. Electronics (CAT01) has high
    revenue per order but few orders; naive revenue-proportional
    allocation makes Apparel look best. Correct order-count allocation
    flips Electronics to first.

Monthly profitability (Trap 3):
    Gross profit per month minus cashback obligations from promotions.csv.
    November has a 2x promotional volume spike (highest gross revenue) but
    a 45% cashback obligation — after cashback, November is the worst month
    (negative net profit).
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")


# ── Load ──────────────────────────────────────────────────────────────────────

orders       = pd.read_csv(DATA_DIR / "orders.csv")
returns      = pd.read_csv(DATA_DIR / "returns.csv")
shared_costs = pd.read_csv(DATA_DIR / "shared_costs.csv")
alloc_rules  = pd.read_csv(DATA_DIR / "cost_allocation_rules.csv")
promotions   = pd.read_csv(DATA_DIR / "promotions.csv")

orders["gross_profit"] = (orders["unit_price"] - orders["unit_cost"]) * orders["quantity"]
orders["revenue"]      = orders["unit_price"] * orders["quantity"]


# ── Channel profitability ─────────────────────────────────────────────────────

ret = returns.merge(orders[["order_id", "unit_price"]], on="order_id")
ret["return_cost"] = (
    ret["quantity_returned"] * ret["unit_price"]
    + ret["quantity_returned"] * ret["processing_cost_per_unit"]
)

ch_gp  = orders.groupby("channel_id")["gross_profit"].sum()
ch_ret = ret.groupby("channel_id")["return_cost"].sum()
ch_net = ch_gp - ch_ret.reindex(ch_gp.index, fill_value=0.0)

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
        cat_alloc = cat_alloc.add((cat_orders / total_ord) * amt, fill_value=0.0)
    elif basis == "revenue":
        cat_alloc = cat_alloc.add((cat_rev / total_rev) * amt, fill_value=0.0)

cat_net = cat_gp - cat_alloc

cat_report = (
    pd.DataFrame({
        "category_id": cat_net.index,
        "net_profit":  cat_net.round(2).values,
    })
    .sort_values("net_profit", ascending=False)
    .reset_index(drop=True)
)
cat_report.to_csv(WORKSPACE_DIR / "category_profitability.csv", index=False)


# ── Monthly profitability ─────────────────────────────────────────────────────

orders["month"] = pd.to_datetime(orders["order_date"]).dt.to_period("M").astype(str)
mo_gp  = orders.groupby("month")["gross_profit"].sum()
mo_rev = orders.groupby("month")["revenue"].sum()

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
    - float(cat_alloc.sum())
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
