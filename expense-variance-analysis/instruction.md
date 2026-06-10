Build a Q1 2024 (January 1 – March 31) weekly payroll cost variance report for a warehouse operation, comparing actual labour cost to budget by department and week.

The warehouse observes three company holidays in Q1: January 15, February 19, and March 10. Shifts starting on these dates earn a 1.5× holiday premium.
Shifts starting on a Saturday or Sunday earn a 1.15× weekend premium.
Shifts starting at or after 21:00 are classified as night shifts and earn a 1.3× night premium.
IMPORTANT: Premiums are mutually exclusive. Only the highest applicable premium applies to a shift (they are never stacked or added). The highest premium applies to all hours of the qualifying shift.

Overtime is assessed on total weekly hours. The overtime premium is 0.5× the shift's applicable rate. The weekly overtime threshold resets each Monday. Weeks run Monday through Sunday. Shifts spanning the Sunday/Monday midnight boundary contribute hours to each respective week. When a corrected shift spans this boundary, distribute the corrected hours across the two weeks proportionally to the original (pre-correction) pre-midnight and post-midnight hour split.

Shifts are attributed to the department the employee was assigned to at the time of the shift. When an employee is temporarily reassigned to another department, the receiving department is charged for those hours at the employee's base hourly rate. Overtime is calculated on the employee's total weekly hours and the overtime premium stays with the home department.

`weekly_budget.csv` provides the base budgeted_hours and avg_hourly_rate per department per week. Base budgeted cost = budgeted_hours × avg_hourly_rate.
However, unused budget rolls over: if a department's actual cost for week N is below its effective budget, 80% of the surplus carries forward and increases the department's budget for week N+1. This carryover addition is capped at 12% of the base budget for week N+1. The effective budget for week 1 equals the base budget. Variance is calculated against the effective budget.

Input data:

`/workspace/data/shifts.csv` — shift_id, employee_id, shift_start, shift_end. Warehouse shifts run 6–14 hours; records with durations outside this range are data-entry errors and must be excluded before analysis.

`/workspace/data/corrections.csv` — retroactive timesheet corrections.
Four rules govern corrections:
1. Only corrections with status `approved` apply.
2. If multiple approved corrections exist for the same shift, use the latest one by `correction_date`.
3. The Q1 payroll cutoff is 2024-04-05. Corrections filed after this date do not apply to Q1. If the latest approved correction is post-cutoff, the latest pre-cutoff approved correction (if any) applies instead.
4. If an eligible approved correction reduces a shift's total duration below 6 hours, the entire shift is excluded from analysis.

`/workspace/data/employees.csv` — employee_id, department_id (snapshot generated on April 1, 2024), hourly_rate, employee_type (full_time or contractor). Contractor hourly_rate values are stored in US cents by the HR export system.

`/workspace/data/departments.csv` — department_id, department_name

`/workspace/data/ot_policy.csv` — department_id, weekly_ot_threshold (hours)

`/workspace/data/reassignments.csv` — employee_id, week_start (YYYY-MM-DD, always a Monday), to_dept_id, reassigned_hours

`/workspace/data/weekly_budget.csv` — department_id, week_start (date, always a Monday), budgeted_hours, avg_hourly_rate

`/workspace/data/transfers.csv` — employee_id, from_dept_id, to_dept_id, effective_date

Required outputs:

Save `/workspace/payroll_variance_report.csv` with columns: department_id, department_name, week_start, base_budgeted_cost, effective_budgeted_cost, actual_cost, variance (actual_cost − effective_budgeted_cost). Include one row per department per week (10 departments × 13 weeks = 130 rows), sorted by department_id then week_start.

Save `/workspace/summary.json` with keys total_budgeted_cost (sum of effective budgets), total_actual_cost, total_variance, total_overtime_hours (float, rounded to 2 decimal places) and over_budget_week_count (integer).
