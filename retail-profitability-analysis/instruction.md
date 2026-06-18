Build a 2024 annual profitability report for a multi-channel retailer that sells across five sales channels and five product categories. Compute net profit by channel, contribution margin by product category, and net profit by calendar month, then identify the top and bottom performer in each dimension. Gross profit is computed from orders placed in calendar year 2024.

Each report covers only the costs directly attributable to its dimension. Channel net profit is computed as channel gross profit minus return losses. Channels operating through third-party platforms may also incur fixed monthly access fees; these are recorded in `channel_return_fees.csv` and form part of that channel's return losses. Category contribution margin deducts allocated shared overhead only — shared operational costs must be allocated to product categories according to the rules defined in `cost_allocation_rules.csv`. Cost types that have no entry in `cost_allocation_rules.csv` are not allocated to any product category but are still deducted when computing total net profit. Monthly net profit deducts promotional cashback only — cashback obligations apply to revenue earned during a campaign's active dates and are recorded in `promotions.csv`. These three reports are independent analytical views and are not intended to sum to `total_net_profit`.

Return losses are deducted from channel net profit and from `total_net_profit`. For each return with a return_date within calendar year 2024, the loss is the applicable customer refund (`unit_price` from the originating order × `quantity_returned` × `refund_pct`) plus, where applicable, the return-handling fee (`processing_cost_per_unit` × `quantity_returned`). The handling fee is waived when `quantity_returned` does not exceed `waiver_max_qty` for that return's reason code and channel. The applicable `refund_pct` and `waiver_max_qty` for each return are specified in `return_reason_codes.csv`. Returned items are not restocked. A return dated outside 2024 does not itself incur a loss. Across all return transactions for the same order, total quantity returned cannot exceed the original order quantity; where multiple return records exist for the same order, returns are processed in ascending order of `return_date` and earlier transactions take priority in filling the available quantity.

The company's total net profit, reported in `summary.json`, is total gross profit minus all return losses (as defined above, including fixed monthly channel platform fees), minus all shared overhead costs incurred during the year, minus all promotional cashback obligations.

**Input data**

`/workspace/data/orders.csv` — order_id, channel_id, product_id, order_date (YYYY-MM-DD), quantity, unit_price

`/workspace/data/returns.csv` — return_id, order_id, channel_id, quantity_returned, processing_cost_per_unit, return_date, reason_code

`/workspace/data/products.csv` — product_id, category_id, category_name, base_price, unit_cost

`/workspace/data/product_cost_history.csv` — product_id, effective_date (YYYY-MM-DD), unit_cost. Historical product costs are tracked in this file.

`/workspace/data/shared_costs.csv` — month (YYYY-MM), cost_type, total_cost. Negative entries represent vendor credits or rebates that reduce the overhead for that cost type and month.

`/workspace/data/cost_allocation_rules.csv` — cost_type, allocation_basis, effective_from (YYYY-MM-DD). Each entry records the allocation basis in effect from that date forward.

`/workspace/data/promotions.csv` — promotion_id, promotion_name, start_date, end_date, cashback_pct, min_monthly_guarantee. For each calendar month in which a promotion is active — including months where the promotion's date range only partially overlaps with the month — the cashback obligation for that month is the greater of the revenue-based computation and the promotion's minimum monthly guarantee.

`/workspace/data/channel_return_fees.csv` — channel_id, month (YYYY-MM), fee_amount

`/workspace/data/return_reason_codes.csv` — reason_code, channel_id, refund_pct, waiver_max_qty. Defines the return policy for each reason code and channel combination.

**Required outputs**

Save `/workspace/channel_profitability.csv` with columns:
`channel_id`, `net_profit` (float, 2 decimal places).
One row per channel, sorted by `net_profit` descending.

Save `/workspace/category_profitability.csv` with columns:
`category_id`, `contribution_margin` (float, 2 decimal places).
One row per category, sorted by `contribution_margin` descending.

Save `/workspace/monthly_profitability.csv` with columns:
`month` (YYYY-MM), `net_profit` (float, 2 decimal places).
One row per calendar month (January–December 2024), sorted by `month` ascending.

Save `/workspace/summary.json` with the following keys:
- `most_profitable_channel` (str) — channel_id with the highest net profit
- `least_profitable_channel` (str) — channel_id with the lowest net profit
- `most_profitable_category` (str) — category_id with the highest contribution margin
- `least_profitable_category` (str) — category_id with the lowest contribution margin
- `best_margin_month` (str, YYYY-MM) — month with the highest net profit
- `worst_margin_month` (str, YYYY-MM) — month with the lowest net profit
- `total_net_profit` (float, rounded to 2 decimal places) — total gross profit minus all return losses, all allocated and unallocated shared overhead, and all promotional cashback obligations
