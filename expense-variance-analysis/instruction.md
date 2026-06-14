Build a Q1 2024 (January 1–March 31) weekly payroll cost variance report comparing actual labour cost against budget by department and week.

Shift premiums:

* Holiday premium: 1.5× for shifts starting on 2024-01-15, 2024-02-19, or 2024-03-10.
* Call-in premium: 1.4×.
* Night premium: 1.3× for shifts starting at or after 21:00.
* Weekend premium: 1.15× for shifts starting on Saturday or Sunday.

Premiums are mutually exclusive. Only the highest applicable premium applies to a shift.

Job classifications affect pay through `job_code_rates.csv`.

Overtime is assessed weekly. Weeks run Monday through Sunday and overtime thresholds reset each Monday. Shifts spanning the Sunday/Monday midnight boundary contribute hours to each respective week. Overtime eligibility may depend on department policies, pay groups, reassignment policies, and job classifications. Employees working multiple job classifications within a week receive blended-rate overtime treatment. When an employee is temporarily reassigned to a department whose overtime policy applies cross-departmentally, their effective weekly overtime threshold for that week is the lower of their home department's threshold and the receiving department's threshold.

Departmental labour cost attribution must reflect employee assignments, transfers, reassignments, work-order allocations, overtime ownership rules, and eligible payroll corrections.

Work orders may allocate labour cost across multiple departments.

Weekly budgets originate from `weekly_budget.csv` and may be modified by approved budget amendments. Amendment types include additions and reallocations. Only amendments approved on or before `2024-04-05` affect Q1 reporting.

Unused budget carries forward according to company policy:

* Carry-forward rate: 80% of unused budget.
* Carry-forward cap: 12% of the next week's base budget.

Variance is calculated against effective budget.

Input data may contain inconsistent representations of equivalent values, undocumented columns, invalid records, and retroactive corrections. Use only documented fields relevant to the analysis.

Payroll cutoff date for Q1 corrections: `2024-04-05`.

Warehouse shifts are expected to be between 6 and 14 hours in duration. Invalid shifts do not contribute to the analysis.

Corrections may modify shift hours, pay rate, or department attribution. Only eligible approved corrections affect Q1 payroll results. Corrections may invalidate a shift.

Contractor hourly rates are stored in US cents.

Historical employee rates are available through `rate_history.csv`.

Job codes may appear in multiple textual representations while referring to the same logical classification.

Input files:

`/workspace/data/shifts.csv`
`shift_id`, `employee_id`, `shift_start`, `shift_end`, `job_code`, `work_order_id`, `schedule_type`, `assigned_at`

`/workspace/data/corrections.csv`
`correction_id`, `shift_id`, `correction_date`, `status`, `correction_type`, `corrected_hours`, `corrected_rate`, `corrected_dept_id`

`/workspace/data/employees.csv`
`employee_id`, `department_id`, `hourly_rate`, `employee_type`, `pay_group_id`

`/workspace/data/rate_history.csv`
`employee_id`, `effective_date`, `hourly_rate`

`/workspace/data/pay_groups.csv`
`pay_group_id`, `ot_threshold`

`/workspace/data/job_code_rates.csv`
`job_code`, `rate_multiplier`

`/workspace/data/work_orders.csv`
`work_order_id`, `primary_dept_id`, `project_type`

`/workspace/data/work_order_splits.csv`
`work_order_id`, `secondary_dept_id`, `secondary_pct`

`/workspace/data/departments.csv`
`department_id`, `department_name`

`/workspace/data/ot_policy.csv`
`department_id`, `weekly_ot_threshold`, `applies_cross_dept`, `call_in_window_hours`

`/workspace/data/reassignments.csv`
`employee_id`, `week_start`, `to_dept_id`, `reassigned_hours`

`/workspace/data/weekly_budget.csv`
`department_id`, `week_start`, `budgeted_hours`, `avg_hourly_rate`

`/workspace/data/budget_amendments.csv`
`amendment_id`, `department_id`, `week_start`, `amount`, `type`, `from_dept_id`, `approved_date`

`/workspace/data/transfers.csv`
`employee_id`, `from_dept_id`, `to_dept_id`, `effective_date`

Save `/workspace/payroll_variance_report.csv` with columns:

`department_id`,
`department_name`,
`week_start`,
`base_budgeted_cost`,
`effective_budgeted_cost`,
`actual_cost`,
`variance`

Include one row per department-week combination in Q1 (130 rows total), sorted by `department_id` and `week_start`.

Save `/workspace/summary.json` with:

`total_budgeted_cost`,
`total_actual_cost`,
`total_variance`,
`total_overtime_hours`,
`over_budget_week_count`

`total_budgeted_cost` is the sum of effective budgets.

`total_overtime_hours` must be reported to two decimal places.

`over_budget_week_count` is the number of department-week combinations where `actual_cost` exceeds `effective_budgeted_cost`.
