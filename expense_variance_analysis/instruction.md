Build a Q1 2024 (January–March) expense variance report that compares approved departmental spending against budget, broken down by category and month.

Variance calculation: for each (department, category, budget_month) combination, compute actual_spend_usd (sum of approved expenses), monthly_budget_usd (see note below), variance_usd = actual_spend_usd − monthly_budget_usd, and variance_pct = (variance_usd / monthly_budget_usd) × 100, rounded to 2 decimal places. Leave variance_pct null (or empty) where monthly_budget_usd is zero.

Budget amounts: budgeted_amount values in budgets.csv represent the monthly allocation for most categories. For the software_licenses and professional_services categories, budgeted_amount is the full annual contract value as exported from the planning system; the monthly budget for these two categories is therefore budgeted_amount / 12.

Expense period: use expense_date to determine which budget month an expense belongs to.

Approved expenses only: exclude any row where is_approved is False. These are submitted but unapproved claims and must not appear in actual spend.

Input data:

/workspace/data/expenses.csv — expense_id, department_id, category_id, expense_date (date the expense was incurred), payment_date (date the payment was issued to the vendor), amount_usd, is_approved, vendor_id; all expense_date values fall within Q1 2024

/workspace/data/budgets.csv — department_id, category_id, budget_month (YYYY-MM), budgeted_amount (see budget amounts note above); one row per department–category–month combination for all three Q1 months

/workspace/data/departments.csv — department_id, department_name, region

/workspace/data/categories.csv — category_id, category_name

Required outputs:

Save /workspace/variance_report.csv with columns: department_id, department_name, category_id, category_name, budget_month, monthly_budget_usd, actual_spend_usd, variance_usd, variance_pct. Include all department–category–month combinations present in budgets.csv; where no approved expenses exist for a combination, set actual_spend_usd to 0.

Assign the following top-level notebook variables as JSON-serializable scalars rounded to 2 decimal places:

total_budgeted_usd — sum of monthly_budget_usd across all rows in the variance report
total_actual_usd — sum of actual_spend_usd across all rows in the variance report
total_variance_usd — total_actual_usd minus total_budgeted_usd
over_budget_department_count — number of departments whose Q1 total actual_spend_usd exceeds their Q1 total monthly_budget_usd (integer)
software_licenses_q1_budget_usd — sum of monthly_budget_usd for the software_licenses category across all departments and all three Q1 months
march_actual_spend — sum of actual_spend_usd for budget_month 2024-03 across all departments and categories
unapproved_expense_count — count of rows in expenses.csv where is_approved is False (integer, from the raw file before any filtering)
