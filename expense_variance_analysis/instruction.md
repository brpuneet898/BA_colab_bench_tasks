Build a Q1 2024 (January 1 – March 31) weekly payroll cost variance report for a warehouse operation, comparing actual labour cost to budget by department and week.

Overtime rule: all employees are paid at their base hourly rate for the first 40 hours worked within a Monday-through-Sunday week. Any hours above 40 in that same week are paid at 1.5 times the base rate. The 40-hour threshold applies per employee per week.

Shift attribution: each shift is recorded with an exact start and end timestamp. When a shift spans a week boundary (i.e., begins in one Monday-to-Sunday week and ends in the next), the hours that fall before and after the boundary must each be counted in their respective week. A week boundary falls at midnight at the start of each Monday.

Payroll calculation steps:
1. Compute the total hours each employee worked within each Monday-to-Sunday week (respecting the shift attribution rule above).
2. Apply the overtime rule to determine regular hours and overtime hours per employee per week.
3. Compute each employee's weekly payroll cost as: regular_hours × hourly_rate + overtime_hours × hourly_rate × 1.5.
4. Aggregate weekly payroll cost by department and week.

Budget: weekly_budget.csv provides the budgeted_hours and avg_hourly_rate for each department and week. Budgeted cost = budgeted_hours × avg_hourly_rate. The budget does not include an overtime premium.

Input data:

/workspace/data/shifts.csv — shift_id, employee_id, shift_start (datetime, local warehouse time), shift_end (datetime, local warehouse time); a shift whose shift_end falls in a later Monday-to-Sunday week than its shift_start spans a week boundary

/workspace/data/employees.csv — employee_id, department_id, hourly_rate

/workspace/data/departments.csv — department_id, department_name

/workspace/data/weekly_budget.csv — department_id, week_start (date, always a Monday), budgeted_hours, avg_hourly_rate

Required outputs:

Save /workspace/payroll_variance_report.csv with columns: department_id, department_name, week_start, budgeted_cost, actual_cost, variance (actual_cost − budgeted_cost). Include one row per department per week (10 departments × 13 weeks = 130 rows), sorted by department_id then week_start.

Assign the following top-level notebook variables as JSON-serializable scalars rounded to 2 decimal places:

total_budgeted_cost — sum of budgeted_cost across all rows in the variance report
total_actual_cost — sum of actual_cost across all rows in the variance report
total_variance — total_actual_cost minus total_budgeted_cost
total_overtime_hours — total overtime hours worked across all employees and all weeks in Q1
over_budget_week_count — number of department-week rows where actual_cost exceeds budgeted_cost (integer)
cross_week_shift_count — number of shifts in shifts.csv whose shift_end falls in a different Monday-to-Sunday week than their shift_start (integer, computed from the raw file before any processing)
