The HR team needs a Q2 2024 (April 1 – June 30) attrition report by department, for the whole workforce across both of the company’s business units. The input files are in /workspace/data/, there’s five of them: departments.csv, employees.csv, department_transfers.csv, worker_category_changes.csv and leave_records.csv.

departments.csv has one row per department per business unit. employees.csv has one row per employment spell (a spell is just one continuous period of employment, identified by spell_id, and belongs to one business unit for its whole life). Each row has a hire_date, a term_date (blank if the spell is still active), a term_reason (also blank if still active), and the department and worker category the spell started out with. department_transfers.csv keeps track of the dates a spell’s department changed, and whether the change was a permanent transfer or a temporary secondment. worker_category_changes.csv does the same thing but for worker category, along with whether the change was approved. And leave_records.csv has the periods of leave of absence taken during a spell, with a start and end date.

This report only covers spells that are regular_full_time or regular_part_time on whatever date is being evaluated. If a spell is classified as intern or contractor on a given date, its out of scope for that date.

A spell counts toward headcount on a given date if its hire_date is on or before that date, and it hasn’t separated yet as of that date - meaning its term_date is either blank or later than that date. A spell is on leave on a given date if that date falls within one of its recorded leave periods, including both the start and end day.

A separation is voluntary if term_reason is resignation or retirement, and its involuntary if term_reason is involuntary_termination or layoff. A separation belongs to Q2 2024 if its term_date falls between April 1 and June 30, 2024, inclusive.

For each department in each business unit, work out:

avg_headcount - the average of the department’s headcount on April 30, May 31 and June 30, 2024 (using each spell’s department and worker category as of that date).

headcount_jun30 - the department’s headcount on June 30, 2024.

working_headcount_jun30 - same as headcount_jun30 but leaves out any spell that’s on leave on June 30, 2024.

voluntary_separations and involuntary_separations - the count of Q2 2024 separations of each type, attributed to whatever department the spell was in, and scoped by the worker category it held, as of its term_date.

voluntary_turnover_rate and involuntary_turnover_rate - each separations count divided by avg_headcount, as a percentage.

Save /workspace/department_turnover_report.csv, sorted by business_unit_id then department_id, one row per department per business unit. Columns should be business_unit_id, department_id, department_name, avg_headcount, headcount_jun30, working_headcount_jun30, voluntary_separations, involuntary_separations, voluntary_turnover_rate, involuntary_turnover_rate. Round avg_headcount and the two rate columns to 2 decimal places.

Also save /workspace/summary.json with these keys - total_avg_headcount, total_voluntary_separations, total_involuntary_separations, overall_voluntary_turnover_rate, overall_involuntary_turnover_rate. The two overall rates should be computed from the company wide totals, not averaged across departments, and round everything to 2 decimal places.
