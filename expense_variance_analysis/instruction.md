Build a Q1 2024 (January 1 – March 31) weekly payroll cost variance report for a warehouse operation, comparing actual labour cost to budget by department and week.

Overtime rule: all employees are paid at their base hourly rate for the first 40 hours worked within a Monday-through-Sunday week. Any hours above 40 in that same week are paid at 1.5 times the base rate. The 40-hour threshold resets each Monday and applies per employee per week.

Payroll calculation:
1. For each employee, determine the total hours worked within each Monday-to-Sunday week.
2. Split those hours into regular hours (up to 40) and overtime hours (above 40).
3. Compute each employee's weekly payroll cost: regular_hours × hourly_rate + overtime_hours × hourly_rate × 1.5.
4. Aggregate weekly payroll cost by department and week.

Budget: weekly_budget.csv provides budgeted_hours and avg_hourly_rate for each department and week. Budgeted cost = budgeted_hours × avg_hourly_rate. The budget does not include an overtime premium.

Input data:

/workspace/data/shifts.csv — shift_id, employee_id, shift_start (datetime), shift_end (datetime)

/workspace/data/employees.csv — employee_id, department_id, hourly_rate

/workspace/data/departments.csv — department_id, department_name

/workspace/data/weekly_budget.csv — department_id, week_start (date, always a Monday), budgeted_hours, avg_hourly_rate

Required outputs:

Save /workspace/payroll_variance_report.csv with columns: department_id, department_name, week_start, budgeted_cost, actual_cost, variance (actual_cost − budgeted_cost). Include one row per department per week (10 departments × 13 weeks = 130 rows), sorted by department_id then week_start.

Assign the following top-level notebook variables as JSON-serializable scalars rounded to 2 decimal places: total_budgeted_cost, total_actual_cost, total_variance, total_overtime_hours, over_budget_week_count (integer).
