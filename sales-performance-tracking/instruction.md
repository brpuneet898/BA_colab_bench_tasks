Build a Q1 2024 (February 1 – April 30) quota attainment report for a B2B SaaS sales team.

Compute each sales rep's gross and net Annual Recurring Revenue (ARR) in USD, and their attainment against their Q1 quota. 

Note: ARR is the annualized value of the contract. If a rep was hired during Q1, their active quota must be prorated based on the number of days they were employed during the quarter.

Input data:
- `/workspace/data/deals.csv`: Record of all closed deals.
- `/workspace/data/deal_splits.csv`: Deal attribution records.
- `/workspace/data/cancellations.csv`: Deals that were cancelled post-closure.
- `/workspace/data/quotas.csv`: Rep quotas by fiscal period.
- `/workspace/data/fx_rates.csv`: Daily exchange rates for non-USD currencies.
- `/workspace/data/reps.csv`: Rep metadata.

Output 1: `/workspace/rep_performance_report.csv`
A CSV with exactly 200 rows (one per rep) with the following columns:
- `rep_id`
- `rep_name`
- `region`
- `period_start` (the start date of the reporting period)
- `total_deals` (integer count of unique deals credited to the rep)
- `gross_arr_usd` (total USD ARR credited before cancellations)
- `net_arr_usd` (total USD ARR credited after cancellations)
- `quota_usd` (the rep's active quota for the period)
- `attainment_pct` (`net_arr_usd` / `quota_usd` * 100, rounded to 2 decimal places)

Output 2: `/workspace/summary.json`
A JSON file with the following keys:
- `total_gross_arr_usd` (float, rounded to 2 decimal places)
- `total_net_arr_usd` (float, rounded to 2 decimal places)
- `total_quota_usd` (float, rounded to 2 decimal places)
- `overall_attainment_pct` (float, rounded to 2 decimal places)
- `reps_over_quota` (integer count of reps where `net_arr_usd` > `quota_usd`)
