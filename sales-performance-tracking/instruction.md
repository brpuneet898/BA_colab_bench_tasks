Build a monthly quota attainment report for a B2B SaaS sales team for Q1 2024 (February, March, and April), comparing each rep's Net Annual Recurring Revenue (ARR) against their effective monthly quota.

Attribution is determined by `account_teams.csv` and deal attributes. Deals start with 100% credit assigned to the account's `Primary_AE`.
- If the account team includes an `SDR`, the `SDR` receives 20% of the deal's ARR, and the `Primary_AE` is reduced to 80%.
- If the deal's `product_line` is "Enterprise Suite" and the account team includes an `Overlay_Specialist`, the `Overlay_Specialist` receives 15%, the `SDR` (if present) receives 15%, and the `Primary_AE` receives the remainder (70%).
- If a deal involves a cross-regional client (which is the case for any deal where the `Primary_AE` is in "EMEA"), the `Global_VP` (rep_id "R999") takes 5% off the top. The remaining 95% is split proportionally among the other team members according to the rules above.

Deal revenue is eligible for mutually exclusive accelerators that multiply the deal's total ARR before attribution. Only the single highest applicable multiplier applies; they do not stack.
- Deals with `product_line` "Enterprise Suite" receive a 1.5× multiplier.
- Deals with `contract_months` of 24 or greater receive a 1.3× multiplier.
- Deals with `deal_source` "Outbound" receive a 1.2× multiplier.

ARR is recognised proportionally over 3 months starting in the month the deal closes. A deal recognizes exactly 1/3 of its ARR in the close month, 1/3 in the second month, and the remainder in the third month.

Deals cancelled within the Q1 window are recorded in `cancellations.csv`. When a deal is cancelled, any unrecognised ARR is voided, and all previously recognised ARR is retroactively clawed back *in the month the cancellation occurs*.
- A cancellation is only valid if its `status` is "approved".
- If multiple cancellations exist for the same deal, the one with the latest `filed_date` takes precedence.
- The Q1 accounting cutoff is `2024-04-05`. If the latest approved cancellation is filed after this cutoff date, it is ignored, and you must use the latest approved pre-cutoff record (if any).

All deal values must be converted to USD. `fx_rates.csv` provides cross-rates between currencies. The rate represents the amount of `quote_currency` equivalent to 1 `base_currency`. You must derive any missing direct USD conversion rates by finding the correct path through the provided currency pairs.

A rep's base monthly quota is their `quota_usd` divided by 3. However, quota shortfalls roll over. If a rep's Net ARR in a month is less than their effective quota, the exact dollar shortfall carries forward and is added to their effective quota for the following month.

Input data:

`/workspace/data/deals.csv` — region, deal_id, account_id, close_date, total_contract_value, currency, contract_months, product_line, deal_source, stage. Only `closed_won` deals count toward attainment. `deal_id` is assigned by each region's CRM system and resets independently per region. ARR is the annualised value of the total contract value.

`/workspace/data/account_teams.csv` — account_id, rep_id, role.

`/workspace/data/cancellations.csv` — region, deal_id, filed_date, cancelled_date, status.

`/workspace/data/reps.csv` — rep_id, rep_name, region, territory, manager_id, hire_date. Reps hired during Q1 have their base monthly quota prorated by their active days in the month.

`/workspace/data/quotas.csv` — rep_id, period_start, period_end, quota_usd. Q1 2024 is the period starting `2024-02-01`.

`/workspace/data/fx_rates.csv` — date, base_currency, quote_currency, rate.

Required outputs:

Save `/workspace/monthly_rep_performance.csv` with columns: `rep_id`, `rep_name`, `region`, `month` (integer 2-4 for Feb-Apr), `base_quota`, `effective_quota`, `net_arr_usd`, `attainment_pct` (rounded to 2 decimal places). Include one row per rep per month, sorted by `rep_id` then `month`.

Save `/workspace/summary.json` with keys: `total_net_arr_usd` (float, rounded to 2), `total_base_quota_usd` (float, rounded to 2).
