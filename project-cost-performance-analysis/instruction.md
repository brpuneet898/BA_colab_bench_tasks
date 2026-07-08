The project controls team requires an Earned Value Management (EVM) cost performance report as of **2024-03-31** for a portfolio of five capital infrastructure projects. All 200 work packages must be included. All input files are in `/workspace/data/`.

Each work package has an approved budget baseline in `baselines.csv`, valid over the period defined by `baseline_effective_from` and `baseline_effective_to`. Actual Cost for each work package is the cumulative net cost incurred through the reporting date, drawn from `actuals.csv`. Planned value schedules are in `planned_value_schedule.csv`. Progress as of March 2024 comes from `progress_entries.csv`.

Earned value technique varies by work package and is recorded in the `ev_technique` field. For the `percent_complete` technique, earned value scales with the reported completion percentage. For the `0_100` technique, a work package earns its full budget only upon formal completion by the reporting date — formal completion is the date recorded in `completion_date` — otherwise it earns nothing.

Compute standard EVM cost and schedule performance metrics per work package. Use the CPI-based estimate at completion (`bac / cpi`, defaulting to `bac` when CPI is not positive). Set CPI and SPI to null when the denominator is zero. Round monetary values to 2 decimal places and performance indices to 4 decimal places.

Save `/workspace/evm_report.csv` sorted by `project_id` then `work_package_id` with columns: `project_id`, `work_package_id`, `work_package_name`, `control_account_id`, `ev_technique`, `completion_status`, `percent_complete`, `bac_usd`, `pv_usd`, `ev_usd`, `ac_usd`, `cv_usd`, `sv_usd`, `cpi`, `spi`, `eac_usd`, `etc_usd`, `vac_usd`.

Save `/workspace/summary.json` with portfolio-level totals and indices: `reporting_date` (string "2024-03-31"), `total_bac_usd`, `total_ev_usd`, `total_ac_usd`, `total_pv_usd` (sums, rounded to 2 dp), `portfolio_cpi` and `portfolio_spi` (computed from portfolio totals, rounded to 4 dp), `total_eac_usd` (sum of work package EAC, rounded to 2 dp), `overbudget_work_package_count` (negative cost variance), `behind_schedule_work_package_count` (negative schedule variance).
