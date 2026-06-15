Build a monthly quota attainment report for a B2B SaaS sales team covering Q1 2024 (February, March, and April). The report compares each rep's Net ARR against their effective monthly quota.


ARR for a deal is the annualised value of its `total_contract_value`: `total_contract_value / contract_months * 12`.

All deal values must be converted to USD using `fx_rates.csv`. Rates are published on the first day of each month. If no rate exists for a deal's exact `close_date`, use the most recently published rate whose date is on or before the `close_date`. The rate represents the amount of `quote_currency` per unit of `base_currency`. Not all currencies have a direct USD conversion in the rates file; any missing conversions must be derived from the available currency pairs.


Each closed deal's ARR may be multiplied by a single accelerator before attribution. Only the highest applicable multiplier is used; they do not stack.

- `product_line` = "Enterprise Suite": 1.5×
- `contract_months` ≥ 24: 1.3×
- `deal_source` = "Outbound": 1.2×


Attribution starts from `account_teams.csv`. Each row in `account_teams.csv` includes an `effective_from` date indicating when that team member's assignment became active. Only team members whose `effective_from` date is on or before the deal's `close_date` are included in attribution for that deal.

Each account has a `Primary_AE`. Credit is divided as follows:

1. If the account team includes an `SDR`, the SDR receives 20% and the `Primary_AE` receives 80%.
2. If the deal's `product_line` is "Enterprise Suite" and the team includes an `Overlay_Specialist`, the Overlay receives 15%, the SDR (if present) receives 15%, and the `Primary_AE` receives the remainder (85% with no SDR, 70% with an SDR).
3. If the `Primary_AE`'s home region in `reps.csv` is "EMEA", the Global VP (rep_id `R999`) takes 5% of the deal's accelerated ARR off the top. The remaining 95% is then divided among the account team using the rules above.

Only `closed_won` deals count toward attainment. Where multiple `closed_won` records exist for the same `(region, deal_id)`, use only the record with the latest `close_date`.

ARR is recognised in equal thirds over three consecutive months beginning in the close month. Month 1 and Month 2 each receive exactly `arr / 3`. Month 3 receives the remainder (`arr − 2 × (arr / 3)`).

`cancellations.csv` records cancellations that may fall within the Q1 accounting window.

- Only cancellations with `status` = "approved" are valid.
- The accounting cutoff is `2024-04-05`. Cancellations with `filed_date` after this date are ignored entirely.
- If multiple valid (approved, pre-cutoff) cancellation records exist for the same deal, the one with the latest `filed_date` takes precedence.
- When a cancellation applies, any unrecognised future tranches are voided and all tranches previously recognised within Q1 are clawed back. The clawback is recorded as a negative amount in the month of the `cancelled_date`.


For each month in Q1, use the quota record in `quotas.csv` where `period_start` ≤ the first day of that month ≤ `period_end`. The base monthly quota equals `quota_usd` divided by the number of calendar months spanned by that record's period. If more than one record covers a given month, use the one with the latest `period_start`.

Reps hired during Q1 have their base monthly quota prorated by the number of days they were active in that month.

Quota shortfalls roll over: if a rep's Net ARR in a month is less than their effective quota, the exact dollar shortfall is added to their effective quota for the following month. The effective quota for February equals the base quota (no prior period).

Any rep whose February `net_arr_usd` is at least 150% of their February `effective_quota` receives a 20% increase to their `base_quota` for March only. Reps with zero `effective_quota` in February are excluded from this adjustment. The April `base_quota` is not affected.

Required Output:

**`/workspace/monthly_rep_performance.csv`** — one row per rep per month, sorted by `rep_id` then `month`, with columns:

| Column | Description |
| --- | --- |
| `rep_id` | Rep identifier |
| `rep_name` | Rep name |
| `region` | Rep's home region |
| `month` | Integer: 2 = February, 3 = March, 4 = April |
| `base_quota` | Prorated base quota for the month (USD) |
| `effective_quota` | Base quota plus any rolled-over shortfall |
| `net_arr_usd` | Total recognised ARR minus any clawbacks (USD) |
| `attainment_pct` | `net_arr_usd / effective_quota × 100`, rounded to 2 decimal places |

Include one row for every rep in `reps.csv` for each of the three months. Rep `R999` (Global VP) is not in `reps.csv` and must not appear in the output.

**`/workspace/summary.json`** — a JSON object with two keys:

- `total_net_arr_usd`: sum of `net_arr_usd` across the full report, rounded to 2 decimal places (float)
- `total_base_quota_usd`: sum of `base_quota` across the full report, rounded to 2 decimal places (float)

Input Data:

`/workspace/data/deals.csv` — `region`, `deal_id`, `account_id`, `close_date`, `total_contract_value`, `currency`, `contract_months`, `product_line`, `deal_source`, `stage`. `deal_id` is assigned by each region's CRM and resets independently per region.

`/workspace/data/account_teams.csv` — `account_id`, `rep_id`, `role`, `effective_from`. Roles: `Primary_AE`, `SDR`, `Overlay_Specialist`.

`/workspace/data/reps.csv` — `rep_id`, `rep_name`, `region`, `territory`, `manager_id`, `hire_date`.

`/workspace/data/quotas.csv` — `rep_id`, `period_start`, `period_end`, `quota_usd`.

`/workspace/data/cancellations.csv` — `region`, `deal_id`, `filed_date`, `cancelled_date`, `status`.

`/workspace/data/fx_rates.csv` — `date`, `base_currency`, `quote_currency`, `rate`. Rates are published on the 1st of each month only.
