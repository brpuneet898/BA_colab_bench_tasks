Build a Q1 2024 (January 1 – March 31) weekly payroll cost variance report for a warehouse operation, comparing actual labour cost to budget by department and week.

Shifts starting at or after 21:00 are classified as night shifts and are paid at 1.3× the employee's base hourly rate. Overtime is assessed on total weekly hours; the overtime premium is 0.5× the applicable shift rate (base rate for day shifts, 1.3× base rate for night shifts). The weekly overtime threshold resets each Monday. Weeks run Monday through Sunday.

When an employee is temporarily reassigned to another department, the receiving department is charged for those hours at the employee's base hourly rate. Overtime is calculated on the employee's total weekly hours and the overtime premium stays with the home department.

Budget: `weekly_budget.csv` provides budgeted_hours and avg_hourly_rate per department per week. Budgeted cost = budgeted_hours × avg_hourly_rate with no overtime premium.

Input data:

`/workspace/data/shifts.csv` — shift_id, employee_id, shift_start (US Eastern), shift_end (US Eastern)

`/workspace/data/employees.csv` — employee_id, department_id, hourly_rate, employee_type (full_time or contractor). Contractor hourly_rate values are stored in US cents by the HR export system.

`/workspace/data/departments.csv` — department_id, department_name

`/workspace/data/ot_policy.csv` — department_id, weekly_ot_threshold (hours)

`/workspace/data/reassignments.csv` — employee_id, week_start (YYYY-MM-DD, always a Monday), to_dept_id, reassigned_hours

`/workspace/data/weekly_budget.csv` — department_id, week_start (date, always a Monday), budgeted_hours, avg_hourly_rate

Required outputs:

Save `/workspace/payroll_variance_report.csv` with columns: department_id, department_name, week_start, budgeted_cost, actual_cost, variance (actual_cost − budgeted_cost). Include one row per department per week (10 departments × 13 weeks = 130 rows), sorted by department_id then week_start.

Save `/workspace/summary.json` with keys total_budgeted_cost, total_actual_cost, total_variance, total_overtime_hours (float, rounded to 2 decimal places) and over_budget_week_count (integer).
