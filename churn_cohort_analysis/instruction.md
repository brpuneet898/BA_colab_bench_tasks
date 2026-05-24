# Customer Churn & Risk Analysis

A subscription business has shared three CSV files in `/workspace/data/`. 

## Cohort

The cohort is the set of **engaged customers** — those with at least one engagement event recorded — whose subscription started on or before `reference_date - 60 days`. 

Report `cohort_size`.

## In-period activity

For each cohort customer whose subscription has ended, locate their most recent engagement event whose date is **within the subscription period as it was being served**.

When defined, the customer's *inactivity gap* is the whole days between this date and the subscription end date.

## Inactivity threshold

`inactivity_threshold` is the **lower median** of the inactivity gaps observed for cohort customers whose subscription has ended, who have a defined inactivity gap, and whose subscription duration is at least 90 days.

## Churn

A cohort customer is **churned** iff their subscription has ended AND at least one of the following holds:

1. They have no last in-period activity (i.e., it is undefined).
2. Their inactivity gap is greater than or equal to `inactivity_threshold`.

Active subscriptions are never churned. The churn rule is applied to every ended cohort customer regardless of their subscription duration. Report `churned_users` (count) and `churn_rate` (= `churned_users / cohort_size`, rounded to 4 decimal places).

## High-risk watchlist

A customer is a **watchlist candidate** iff all of the following hold:

- The customer is in the cohort.
- Their subscription is currently active.
- They have at least one engagement event.
- Their most recent engagement is on or before `reference_date - 7 days`.

For each candidate, derive two labels:

- The calendar month of their subscription start, formatted as `month:YYYY-MM`.
- The Python weekday number of their most recent engagement, formatted as `dow:N`.

`high_risk_users` is the **smallest** subset of candidates whose union of labels equals the union of labels across the full candidate pool. Resolve ties in the following order:

1. Among smallest subsets, choose the one whose **minimum stale-days** value is **largest**, where the stale-days for a customer is the whole days between their most recent engagement and the reference date.
2. If still tied, choose the lexicographically smallest sorted user-id list.

If the candidate pool is empty, `high_risk_users` is `[]`.

## Outputs

Assign the following as top-level notebook variables:

- `cohort_size` (int).
- `churned_users` (int).
- `churn_rate` (float, 4 decimal places).
- `inactivity_threshold` (float).
- `high_risk_users` (sorted list of customer-id strings).
