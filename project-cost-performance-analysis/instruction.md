The project controls team requires an Earned Value Management (EVM) cost performance report for a portfolio of five capital infrastructure projects as of the reporting date **2024-03-31**. The report is used to assess cost and schedule efficiency, identify overrunning work packages, and forecast final cost at completion. Include all 200 work packages from `work_packages.csv`.

All input files are in `/workspace/data/`. The files `resource_rates.csv`, `control_accounts.csv`, and `change_orders.csv` are provided for context only and are not required for the computations below.

Budget at Completion (BAC) for each work package comes from `baselines.csv`, where each row defines an approved budget over a validity period given by `baseline_effective_from` and `baseline_effective_to`. Use the baseline record that was in effect on the reporting date. Where a work package has been re-baselined and multiple records could apply, use the most recently approved.

Actual Cost (AC) is the sum of all `cost_amount_usd` values from `actuals.csv` for the work package where `billing_period_date` is on or before the reporting date. The `billing_period_date` field represents the first day of the accounting period in which the cost was incurred.

Percent complete for each work package comes from `progress_entries.csv` for `reporting_period = "2024-03"`.

Earned Value (EV) is determined by the `ev_technique` field in `work_packages.csv`. For the `percent_complete` technique, `ev_usd = (percent_complete / 100) × bac_usd`. For the `0_100` technique, `ev_usd = bac_usd` if the work package had been formally completed as of the reporting date (2024-03-31), otherwise `ev_usd = 0`.

Planned Value (PV) for each work package is the `cumulative_pv_usd` value from `planned_value_schedule.csv` for `reporting_period = "2024-03"`.

From BAC, PV, EV, and AC, compute the following derived metrics per work package: cost variance `cv_usd = ev_usd − ac_usd`; schedule variance `sv_usd = ev_usd − pv_usd`; cost performance index `cpi = ev_usd / ac_usd` (null if `ac_usd = 0`); schedule performance index `spi = ev_usd / pv_usd` (null if `pv_usd = 0`); estimate at completion `eac_usd = bac_usd / cpi` when `cpi > 0`, otherwise `bac_usd`; estimate to complete `etc_usd = eac_usd − ac_usd`; variance at completion `vac_usd = bac_usd − eac_usd`. Round all `_usd` fields to 2 decimal places and `cpi`/`spi` to 4 decimal places.

Save `/workspace/evm_report.csv` with one row per work package sorted by `project_id` then `work_package_id`, containing these columns in order: `project_id`, `work_package_id`, `work_package_name`, `control_account_id`, `ev_technique`, `completion_status`, `percent_complete`, `bac_usd`, `pv_usd`, `ev_usd`, `ac_usd`, `cv_usd`, `sv_usd`, `cpi`, `spi`, `eac_usd`, `etc_usd`, `vac_usd`.

Save `/workspace/summary.json` with keys `reporting_date` (string "2024-03-31"), `total_bac_usd` (float, sum of `bac_usd`, rounded to 2 dp), `total_ev_usd` (float, sum of `ev_usd`, rounded to 2 dp), `total_ac_usd` (float, sum of `ac_usd`, rounded to 2 dp), `total_pv_usd` (float, sum of `pv_usd`, rounded to 2 dp), `portfolio_cpi` (float, `total_ev_usd / total_ac_usd`, rounded to 4 dp), `portfolio_spi` (float, `total_ev_usd / total_pv_usd`, rounded to 4 dp), `total_eac_usd` (float, sum of `eac_usd`, rounded to 2 dp), `overbudget_work_package_count` (integer, count of work packages where `cv_usd < 0`), `behind_schedule_work_package_count` (integer, count of work packages where `sv_usd < 0`).
