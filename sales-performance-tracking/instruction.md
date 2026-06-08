You are a revenue operations analyst for a B2B SaaS company. Your task is to generate the final quota attainment report for the Q1 2024 fiscal period.

Compute each sales rep's gross and net revenue in USD, and their attainment against their quota for this period.

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
- `gross_revenue_usd` (total USD revenue credited before cancellations)
- `net_revenue_usd` (total USD revenue credited after cancellations)
- `quota_usd` (the rep's quota for the period)
- `attainment_pct` (`net_revenue_usd` / `quota_usd` * 100, rounded to 2 decimal places)

Output 2: `/workspace/summary.json`
A JSON file with the following keys:
- `total_gross_revenue_usd` (float, rounded to 2 decimal places)
- `total_net_revenue_usd` (float, rounded to 2 decimal places)
- `total_quota_usd` (float, rounded to 2 decimal places)
- `overall_attainment_pct` (float, rounded to 2 decimal places)
- `reps_over_quota` (integer count of reps where `net_revenue_usd` > `quota_usd`)
