Build a Q1 2024 employee performance scorecard for a company with four business units: Engineering (BU-01), Sales (BU-02), Operations (BU-03), and Finance (BU-04). The scorecard is used by HR at the end of the quarter to determine bonus eligibility and flag employees on performance improvement plans.

## Scope

Include all employees who were active at any point during Q1 2024 (2024-01-01 through 2024-03-31, inclusive). An employee is active during Q1 if their `hire_date` is on or before 2024-03-31 and, where a `termination_date` is recorded, that date falls on or after 2024-01-01.

## Rating Score

Each employee's Q1 performance rating is recorded in `performance_reviews.csv`.

Convert each rating to a numeric score using `rating_scales.csv`.

## Goal Completion

Each employee has one or more goals for the quarter in `employee_goals.csv`. Compute the weighted average completion percentage across all goals for each employee:

`goal_completion_pct = Σ(weight × completion_pct) / Σ(weight)`, rounded to 2 decimal places.

Scale this to a 0–5 score: `goal_score = goal_completion_pct / 100 × 5`.

## Composite Score

`composite_score = round(0.6 × rating_score + 0.4 × goal_score, 4)`

Employees who accumulate **two or more** documented performance incidents in `pip_events.csv` during Q1 2024 have their `composite_score` capped at **2.5** regardless of their computed value. Employees with fewer than two Q1 incidents are not subject to this cap.

## Bonus Eligibility

An employee is `bonus_eligible` if their `composite_score` is at least 3.5 **and** they have zero Q1 performance incidents (`pip_event_count == 0`).

## Input Data

All files are in `/workspace/data/`.

`employees.csv` — `employee_id`, `business_unit_id`, `name`, `role_level`, `hire_date`, `termination_date`, `manager_id`, `location`

`business_units.csv` — `business_unit_id`, `bu_name`, `region`, `cost_center`, `headcount_budget` (reference)

`performance_reviews.csv` — `employee_id`, `business_unit_id`, `review_date`, `rating`, `reviewer_id`, `review_cycle`

`rating_scales.csv` — `rating`, `score`, `effective_from`

`employee_goals.csv` — `goal_id`, `employee_id`, `department_id`, `goal_description`, `weight`, `completion_pct`, `completion_unit`, `recorded_date`

`pip_events.csv` — `employee_id`, `business_unit_id`, `event_date`, `event_type`, `documented_by`, `notes`

`department_metadata.csv` — `department_id`, `department_name`, `business_unit_id`, `cost_center`

## Required Output

### `/workspace/performance_scorecard.csv`

One row per active employee, sorted by `business_unit_id` then `employee_id`. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `employee_id` | string | |
| `business_unit_id` | string | |
| `name` | string | |
| `role_level` | string | |
| `review_date` | string | YYYY-MM-DD |
| `rating` | string | |
| `rating_score` | float | from rating_scales.csv |
| `goal_completion_pct` | float | rounded to 2 dp |
| `composite_score` | float | rounded to 4 dp |
| `pip_event_count` | integer | Q1 2024 incidents only |
| `bonus_eligible` | boolean | |

### `/workspace/summary.json`

```json
{
  "total_employees_assessed": <integer>,
  "bonus_eligible_count": <integer>,
  "mean_composite_score": <float, rounded to 4 dp, mean across all assessed employees>,
  "bu_with_highest_mean_score": <string, business_unit_id with the highest mean composite_score>,
  "employees_on_pip": <integer, count of employees with pip_event_count >= 1>
}
```
