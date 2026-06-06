Build a 2024 annual profitability report for a multi-channel retailer that sells across five sales channels and five product categories. Compute net profit by channel, by product category, and by calendar month, then identify the top and bottom performer in each dimension.

**Net profit definitions**

*Channel net profit* = gross profit − return losses.
Gross profit for an order = (unit_price − unit_cost) × quantity.
Return losses for a return = (quantity_returned × unit_price) + (quantity_returned × processing_cost_per_unit).
Attribute return losses to the order's channel_id.

*Category net profit* = gross profit − allocated shared cost.
Shared costs are recorded in `shared_costs.csv` as monthly amounts per cost_type. For each cost_type, `cost_allocation_rules.csv` specifies the `allocation_basis` column.
- If `allocation_basis` is `order_count`: split the annual total for that cost_type proportionally to each category's share of total 2024 order count.
- If `allocation_basis` is `revenue`: split proportionally to each category's share of total 2024 revenue.
Apply the resulting allocation to reduce each category's gross profit.

*Monthly net profit* = gross profit − cashback obligations.
`promotions.csv` records promotional campaigns. For orders whose `order_date` falls within a promotion's `[start_date, end_date]`, the retailer owes a cashback liability equal to `cashback_pct × order revenue`. Subtract the total cashback obligation for each calendar month from that month's gross profit.

**Input data**

`/workspace/data/orders.csv` — order_id, channel_id, product_id, category_id, order_date (YYYY-MM-DD), quantity, unit_price, unit_cost

`/workspace/data/returns.csv` — return_id, order_id, channel_id, quantity_returned, processing_cost_per_unit, return_date

`/workspace/data/products.csv` — product_id, category_id, category_name, base_price, unit_cost

`/workspace/data/shared_costs.csv` — month (YYYY-MM), cost_type, total_cost

`/workspace/data/cost_allocation_rules.csv` — cost_type, allocation_basis

`/workspace/data/promotions.csv` — promotion_id, promotion_name, start_date, end_date, cashback_pct

**Required outputs**

Save `/workspace/channel_profitability.csv` with columns:
`channel_id`, `gross_profit` (float, 2 decimal places), `return_losses` (float, 2 decimal places), `net_profit` (float, 2 decimal places).
Include one row per channel. Sort by `net_profit` descending.

Save `/workspace/category_profitability.csv` with columns:
`category_id`, `gross_profit` (float, 2 decimal places), `allocated_shared_cost` (float, 2 decimal places), `net_profit` (float, 2 decimal places).
Include one row per category. Sort by `net_profit` descending.

Save `/workspace/monthly_profitability.csv` with columns:
`month` (YYYY-MM), `gross_profit` (float, 2 decimal places), `cashback_obligation` (float, 2 decimal places), `net_profit` (float, 2 decimal places).
Include one row per calendar month (January–December 2024). Sort by `month` ascending.

Save `/workspace/summary.json` with the following keys:
- `most_profitable_channel` (str) — channel_id with the highest net profit
- `least_profitable_channel` (str) — channel_id with the lowest net profit
- `most_profitable_category` (str) — category_id with the highest net profit
- `least_profitable_category` (str) — category_id with the lowest net profit
- `best_margin_month` (str, YYYY-MM) — month with the highest net profit
- `worst_margin_month` (str, YYYY-MM) — month with the lowest net profit
- `total_net_profit` (float, rounded to 2 decimal places) — gross profit across all orders minus total return losses minus total allocated shared costs minus total cashback obligations
