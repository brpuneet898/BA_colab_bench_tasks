An HR team needs a Q2 2024 (April 1 – June 30) attrition report by department for the company's workforce. Input files are in `/workspace/data/`; use only the files named below.

`departments.csv` lists each department's ID and name. `employees.csv` records employment spells — one row per period of employment, identified by `spell_id` — with a `hire_date`, a `home_department_id` (the department the spell started in), a `term_date` (blank if the spell is still active), a `term_reason` (blank if still active), and a `worker_category`. `department_transfers.csv` records every time an employment spell moved to a different department, keyed by `spell_id`, with the date of the move. `leave_records.csv` records periods of leave of absence taken during a spell, keyed by `spell_id`, with a start and end date.

This report covers only spells with `worker_category` `regular_full_time` or `regular_part_time`; exclude `intern` and `contractor` spells entirely.

A spell's department on a given date is its `home_department_id`, updated by every `department_transfers.csv` row for that spell with a `transfer_date` on or before that date, taking the most recent one.

A spell counts toward headcount on a given date if its `hire_date` is on or before that date and it has not yet separated as of that date — that is, its `term_date` is either blank or later than that date. A spell is on leave on a given date if that date falls on or between the `leave_start` and `leave_end` of any of its `leave_records.csv` rows.

A separation with `term_reason` `resignation` or `retirement` is voluntary; `involuntary_termination` or `layoff` is involuntary. A separation belongs to Q2 2024 if its `term_date` falls between April 1 and June 30, 2024, inclusive.

For each department, compute:

`avg_headcount`: the average of the department's headcount on April 30, May 31, and June 30, 2024 (using each spell's department as of that date).

`headcount_jun30`: the department's headcount on June 30, 2024.

`working_headcount_jun30`: the same as `headcount_jun30`, but excluding any spell on leave on June 30, 2024.

`voluntary_separations` and `involuntary_separations`: the count of Q2 2024 separations of each type, attributed to the department the spell was in as of its `term_date`.

`voluntary_turnover_rate` and `involuntary_turnover_rate`: each separations count divided by `avg_headcount`, as a percentage.

Save `/workspace/department_turnover_report.csv` sorted by `department_id`, with one row per department, columns: `department_id`, `department_name`, `avg_headcount`, `headcount_jun30`, `working_headcount_jun30`, `voluntary_separations`, `involuntary_separations`, `voluntary_turnover_rate`, `involuntary_turnover_rate`. Round `avg_headcount` and the two rate columns to 2 decimal places.

Save `/workspace/summary.json` with keys: `total_avg_headcount`, `total_voluntary_separations`, `total_involuntary_separations`, `overall_voluntary_turnover_rate`, `overall_involuntary_turnover_rate` — where the two overall rates are computed from the company-wide totals (not averaged across departments), all rounded to 2 decimal places.
