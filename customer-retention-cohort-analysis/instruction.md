# Q3-Q4 2023 Signup Cohort Month-3 Retention Report

The product analytics team at a B2B SaaS company needs a Month-3 retention report by signup cohort, used to identify which cohorts of new customers are engaging with the product versus dropping off.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|-------------|
| `customers.csv` | Customer account master data (reference only) |
| `signups.csv` | Signup events, identified by `signup_id`. A record is written to this file each time a customer begins a subscription, with the customer identified by `customer_id` and the event dated by `signup_date`. |
| `subscriptions.csv` | Subscription billing history tied to `signup_id` (reference only) |
| `activity_logs.csv` | Product activity events, one row per event, identified by `customer_id`, with an `event_date`, `event_type`, and `event_category`. |

## Scope

A customer's cohort is determined by their first-ever signup. Include the cohorts of customers whose first signup falls between **2023-07-01** and **2023-12-31**, inclusive, grouped by the calendar month of that signup.

This report was generated on **2024-04-10** and reflects only activity that had been logged into the system by that date.

## Metrics

Compute the following for each cohort month present in the 2023-07-01 to 2023-12-31 range.

**Cohort size**: the number of distinct customers whose first-ever signup falls in that cohort month.

**Month-3 retention**: a customer counts as retained if they engaged with the product at any point during the third calendar month after their cohort month (e.g., a customer whose cohort month is 2023-07 is evaluated for engagement during 2023-10). Count the number of a cohort's customers who meet this bar.

**Retention rate** = retained_month_3 / cohort_size, rounded to 4 decimal places.

## Output Files

### `/workspace/cohort_retention_report.csv`

One row per cohort month. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `cohort_month` | string | `YYYY-MM` |
| `cohort_size` | integer | |
| `retained_month_3` | integer | |
| `retention_rate_month_3` | float | rounded to 4 decimal places |

Sort by `cohort_month` ascending.

### `/workspace/summary.json`

```json
{
  "total_cohort_customers": <integer, sum of cohort_size across all cohort months>,
  "total_retained_month_3": <integer, sum of retained_month_3 across all cohort months>,
  "overall_retention_rate_month_3": <float, total_retained_month_3 / total_cohort_customers, rounded to 4 decimal places>,
  "best_performing_cohort": <string, cohort_month with the highest retention_rate_month_3>,
  "worst_performing_cohort": <string, cohort_month with the lowest retention_rate_month_3>
}
```
