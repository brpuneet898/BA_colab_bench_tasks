Build a 2024 annual profitability report for a multi-channel retailer that sells across five sales channels and five product categories. Compute net profit by channel, by product category, and by calendar month, then identify the top and bottom performer in each dimension.

Net profit for each dimension must fully account for all costs and obligations associated with that dimension. Channel profitability must reflect the actual net value retained after handling returned merchandise. Category profitability must deduct each category's share of shared operational overhead, allocated according to the rules in `cost_allocation_rules.csv`. Monthly profitability must reflect all promotional obligations recorded in `promotions.csv`.

**Input data**

`/workspace/data/orders.csv` — order_id, channel_id, product_id, category_id, order_date (YYYY-MM-DD), quantity, unit_price, unit_cost

`/workspace/data/returns.csv` — return_id, order_id, channel_id, quantity_returned, processing_cost_per_unit, return_date

`/workspace/data/products.csv` — product_id, category_id, category_name, base_price, unit_cost

`/workspace/data/shared_costs.csv` — month (YYYY-MM), cost_type, total_cost

`/workspace/data/cost_allocation_rules.csv` — cost_type, allocation_basis

`/workspace/data/promotions.csv` — promotion_id, promotion_name, start_date, end_date, cashback_pct

**Required outputs**

Save `/workspace/channel_profitability.csv` with columns:
`channel_id`, `net_profit` (float, 2 decimal places).
One row per channel, sorted by `net_profit` descending.

Save `/workspace/category_profitability.csv` with columns:
`category_id`, `net_profit` (float, 2 decimal places).
One row per category, sorted by `net_profit` descending.

Save `/workspace/monthly_profitability.csv` with columns:
`month` (YYYY-MM), `net_profit` (float, 2 decimal places).
One row per calendar month (January–December 2024), sorted by `month` ascending.

Save `/workspace/summary.json` with the following keys:
- `most_profitable_channel` (str) — channel_id with the highest net profit
- `least_profitable_channel` (str) — channel_id with the lowest net profit
- `most_profitable_category` (str) — category_id with the highest net profit
- `least_profitable_category` (str) — category_id with the lowest net profit
- `best_margin_month` (str, YYYY-MM) — month with the highest net profit
- `worst_margin_month` (str, YYYY-MM) — month with the lowest net profit
- `total_net_profit` (float, rounded to 2 decimal places) — net profit summed across all dimensions after all deductions
