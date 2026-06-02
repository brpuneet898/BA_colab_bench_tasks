Build a Q1 2024 (January 1 – March 31) weekly payroll cost variance report for a warehouse operation, comparing actual labour cost to budget by department and week.

Overtime rule: all employees are paid at their base hourly rate for hours up to their department's weekly overtime threshold. Hours above that threshold in the same Monday-through-Sunday week are paid at 1.5 times the base rate. The weekly threshold resets each Monday and is defined per department in ot_policy.csv.

Payroll calculation:
1. For each employee, determine the total hours worked within each Monday-to-Sunday week.
2. Split those hours into regular hours (up to the department's weekly threshold) and overtime hours (above the threshold).
3. Compute each employee's weekly payroll cost: regular_hours × hourly_rate + overtime_hours × hourly_rate × 1.5.
4. Temporary reassignments (reassignments.csv) record hours an employee worked in a different department during a given week. Those hours are billed to the receiving department at the employee's base hourly rate; overtime is calculated on the employee's total weekly hours and the overtime premium stays with the home department.
5. Aggregate weekly payroll cost by department and week.

Budget: weekly_budget.csv provides budgeted_hours and avg_hourly_rate for each department and week. Budgeted cost = budgeted_hours × avg_hourly_rate. The budget does not include an overtime premium.

Input data:

/workspace/data/shifts.csv — shift_id, employee_id, shift_start (US Eastern time, naive), shift_end (UTC)

/workspace/data/employees.csv — employee_id, department_id, hourly_rate (USD for full-time staff, US cents for contractors), employee_type (full_time or contractor)

/workspace/data/departments.csv — department_id, department_name

/workspace/data/ot_policy.csv — department_id, weekly_ot_threshold (hours)

/workspace/data/reassignments.csv — employee_id, week_start (YYYY-MM-DD, always a Monday), to_dept_id, reassigned_hours

/workspace/data/weekly_budget.csv — department_id, week_start (date, always a Monday), budgeted_hours, avg_hourly_rate

Required outputs:

Save /workspace/payroll_variance_report.csv with columns: department_id, department_name, week_start, budgeted_cost, actual_cost, variance (actual_cost − budgeted_cost). Include one row per department per week (10 departments × 13 weeks = 130 rows), sorted by department_id then week_start.

Assign the following top-level notebook variables as JSON-serializable scalars rounded to 2 decimal places: total_budgeted_cost, total_actual_cost, total_variance, total_overtime_hours, over_budget_week_count (integer).
