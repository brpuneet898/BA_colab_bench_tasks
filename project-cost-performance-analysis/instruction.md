# Q1 2024 EVM Cost Performance Report

The project controls team requires an Earned Value Management (EVM) cost performance report for a portfolio of five capital infrastructure projects, covering the period through the reporting date of **2024-03-31**. The report is used to assess cost and schedule efficiency, identify overrunning work packages, and forecast final cost at completion.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|-------------|
| `projects.csv` | Master list of all five projects in the portfolio |
| `work_packages.csv` | All 200 work packages across the portfolio, including their planned schedule, EV technique, and completion status |
| `baselines.csv` | Approved performance measurement baselines; each row defines the Budget at Completion (BAC) for a work package over a validity period |
| `actuals.csv` | All cost transactions posted against work packages through the reporting date |
| `progress_entries.csv` | Monthly percent-complete entries submitted by work package managers |
| `planned_value_schedule.csv` | Pre-computed cumulative planned value (PV) per work package per reporting month |
| `resource_rates.csv` | Approved labor and equipment cost rates (reference only) |
| `control_accounts.csv` | Control account hierarchy metadata (reference only) |
| `change_orders.csv` | Approved scope change orders (reference only) |

## Scope

Include all 200 work packages from `work_packages.csv`. The reporting date is **2024-03-31**.

## Computations

### Budget at Completion (BAC)

The applicable BAC is the one from the baseline record in `baselines.csv` whose `baseline_effective_from` is the most recent date on or before the reporting date. Use this record's `bac_usd` as the BAC for all subsequent calculations.

### Actual Cost (AC)

Sum all `cost_amount_usd` values from `actuals.csv` for the work package where `billing_period_date` is on or before the reporting date (2024-03-31). The `billing_period_date` field represents the first day of the accounting period in which the cost was incurred.

### Percent Complete

Use the `percent_complete` value from `progress_entries.csv` for `reporting_period = "2024-03"`.

### Earned Value (EV)

The `ev_technique` field in `work_packages.csv` determines the EV measurement method:

- `percent_complete`: `ev_usd = (percent_complete / 100) × bac_usd`
- `0_100`: `ev_usd = bac_usd` if `completion_status = "complete"`, otherwise `ev_usd = 0`

### Planned Value (PV)

Use the `cumulative_pv_usd` value from `planned_value_schedule.csv` for `reporting_period = "2024-03"` as the planned value for each work package.

### Derived Metrics

| Metric | Formula |
|--------|---------|
| `cv_usd` | `ev_usd − ac_usd` |
| `sv_usd` | `ev_usd − pv_usd` |
| `cpi` | `ev_usd / ac_usd` (null if `ac_usd = 0`) |
| `spi` | `ev_usd / pv_usd` (null if `pv_usd = 0`) |
| `eac_usd` | `bac_usd / cpi` when `cpi > 0`; otherwise `bac_usd` |
| `etc_usd` | `eac_usd − ac_usd` |
| `vac_usd` | `bac_usd − eac_usd` |

Round all `_usd` fields to 2 decimal places. Round `cpi` and `spi` to 4 decimal places.

## Output Files

### `/workspace/evm_report.csv`

One row per work package. Sort by `project_id` ascending, then `work_package_id` ascending. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `project_id` | string | |
| `work_package_id` | string | |
| `work_package_name` | string | |
| `control_account_id` | string | |
| `ev_technique` | string | `percent_complete` or `0_100` |
| `completion_status` | string | |
| `percent_complete` | float | From March progress entry |
| `bac_usd` | float | From applicable baseline |
| `pv_usd` | float | Cumulative PV for March |
| `ev_usd` | float | |
| `ac_usd` | float | |
| `cv_usd` | float | |
| `sv_usd` | float | |
| `cpi` | float | null if `ac_usd = 0` |
| `spi` | float | null if `pv_usd = 0` |
| `eac_usd` | float | |
| `etc_usd` | float | |
| `vac_usd` | float | |

### `/workspace/summary.json`

```json
{
  "reporting_date": "2024-03-31",
  "total_bac_usd": <float, sum of bac_usd across all 200 WPs, rounded to 2 dp>,
  "total_ev_usd": <float, sum of ev_usd, rounded to 2 dp>,
  "total_ac_usd": <float, sum of ac_usd, rounded to 2 dp>,
  "total_pv_usd": <float, sum of pv_usd, rounded to 2 dp>,
  "portfolio_cpi": <float, total_ev_usd / total_ac_usd, rounded to 4 dp>,
  "portfolio_spi": <float, total_ev_usd / total_pv_usd, rounded to 4 dp>,
  "total_eac_usd": <float, sum of eac_usd across all 200 WPs, rounded to 2 dp>,
  "overbudget_work_package_count": <integer, count of WPs where cv_usd < 0>,
  "behind_schedule_work_package_count": <integer, count of WPs where sv_usd < 0>
}
```
