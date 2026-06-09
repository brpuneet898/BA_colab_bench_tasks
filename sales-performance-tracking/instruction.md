Build a Q1 2024 (February 1 – April 30) quota attainment report for a B2B SaaS sales team, comparing each rep's Annual Recurring Revenue (ARR) against their assigned quota for the period.

Revenue attribution is governed by `deal_splits.csv`, which records each rep's `credit_pct` on each deal. A rep may share credit on a deal with other reps. Deals cancelled within the reporting window are recorded in `cancellations.csv`; cancelled deals are excluded from net ARR but not from gross ARR.

All deal values are in the deal's local currency and must be reported in USD. Exchange rates are in `fx_rates.csv`. The `quote_convention` column indicates whether the rate is expressed as USD per unit of foreign currency or units of foreign currency per USD.

`deals.csv` records `total_contract_value` and `contract_months` for each deal. ARR is the annualised value of the contract.

`reps.csv` includes each rep's `hire_date`. A rep who joined the company during Q1 is entitled only to the proportional share of their full-quarter quota corresponding to the days they were employed within the period.

Input data:

`/workspace/data/deals.csv` — region, deal_id, rep_id, close_date, total_contract_value, currency, contract_months, product_line, deal_source, stage. Only `closed_won` deals count toward attainment. `deal_id` is assigned by each region's CRM system and resets independently per region; the same numeric `deal_id` value may appear across multiple regions and represents distinct deals.

`/workspace/data/deal_splits.csv` — region, deal_id, rep_id, credit_pct.

`/workspace/data/cancellations.csv` — region, deal_id, cancelled_date, reason_code.

`/workspace/data/reps.csv` — rep_id, rep_name, region, territory, manager_id, hire_date.

`/workspace/data/quotas.csv` — rep_id, period_start, period_end, quota_usd. Contains rows for multiple periods; Q1 2024 is the period starting `2024-02-01`.

`/workspace/data/fx_rates.csv` — date, currency, quote_convention, rate. One row per currency per calendar date. USD deals have no entry and require no conversion.

Required outputs:

Save `/workspace/rep_performance_report.csv` with columns: `rep_id`, `rep_name`, `region`, `period_start`, `total_deals` (integer), `gross_arr_usd`, `net_arr_usd`, `quota_usd`, `attainment_pct` (rounded to 2 decimal places). Include exactly 200 rows, one per rep, sorted by `rep_id` ascending.

Save `/workspace/summary.json` with keys: `total_gross_arr_usd` (float, rounded to 2 decimal places), `total_net_arr_usd` (float, rounded to 2 decimal places), `total_quota_usd` (float, rounded to 2 decimal places), `overall_attainment_pct` (float, rounded to 2 decimal places), `reps_over_quota` (integer).
