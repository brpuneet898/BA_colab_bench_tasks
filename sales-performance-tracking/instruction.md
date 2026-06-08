Build a Q1 2024 sales performance report for a B2B SaaS company's sales team, showing each rep's quota attainment for the fiscal period covering February 1 – April 30, 2024.

Deal revenue is credited to reps according to their share defined in `deal_splits.csv`. This file contains one or more rows per deal; each row specifies a `rep_id` and a `credit_pct`. Credit percentages for a deal sum to 1.0. Use `deal_splits.csv` as the authoritative source of revenue attribution for every deal.

All revenue must be reported in USD. Deals in `deals.csv` carry a `currency` column (`USD`, `EUR`, or `GBP`). Convert non-USD deal values to USD using the exchange rate on the deal's `close_date` from `fx_rates.csv`. USD deals have an implicit rate of 1.0 and do not appear in `fx_rates.csv`.

Closed-won revenue is net of any cancellations that occurred within the same quota period as the original close. `cancellations.csv` lists cancelled deals with their `cancelled_date`. A deal cancelled within the Feb 1 – Apr 30 period does not contribute revenue for that period.

Quota periods and their exact boundaries are defined in `quotas.csv` (`period_start`, `period_end`). Each rep has one quota per period. Attainment is net revenue divided by quota for the Feb 1 – Apr 30 period.

Input data:

`/workspace/data/deals.csv` — deal_id, rep_id, close_date, deal_value, currency, stage. All rows have stage = closed_won.

`/workspace/data/reps.csv` — rep_id, rep_name, region (NA / EMEA / APAC).

`/workspace/data/deal_splits.csv` — deal_id, rep_id, credit_pct. Every deal appears here; solo deals have a single row with credit_pct = 1.0.

`/workspace/data/quotas.csv` — rep_id, period_start, period_end, quota_usd. Three rows per rep (one per fiscal period).

`/workspace/data/cancellations.csv` — deal_id, cancelled_date.

`/workspace/data/fx_rates.csv` — date, currency, rate_to_usd. Contains daily rates for EUR and GBP only.

Required outputs:

Save `/workspace/rep_performance_report.csv` with columns: rep_id, rep_name, region, period_start, total_deals, gross_revenue_usd, net_revenue_usd, quota_usd, attainment_pct. One row per rep (50 rows total), sorted by rep_id. period_start must be 2024-02-01 for all rows. gross_revenue_usd is credited revenue before subtracting cancellations; net_revenue_usd is after. attainment_pct = net_revenue_usd / quota_usd × 100, rounded to 2 decimal places.

Save `/workspace/summary.json` with keys: total_gross_revenue_usd (float), total_net_revenue_usd (float), total_quota_usd (float), overall_attainment_pct (float, rounded to 2 decimal places), reps_over_quota (int — count of reps where net_revenue_usd > quota_usd).
