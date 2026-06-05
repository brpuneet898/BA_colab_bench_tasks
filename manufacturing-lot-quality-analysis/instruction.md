Build a Q1 2024 (January 1 – March 31) production quality compliance report for a contract manufacturer operating three production lines. For each combination of line, product, and calendar month that produced at least one lot, compute the pass rate, mean specification deviation, and cost of poor quality.

A lot passes if every one of its quality test results is within the applicable specification limits. Where multiple results exist for the same lot and test, use only the most recently tested result for pass/fail determination. Specification limits per product-test combination are in `product_specifications.csv`; where multiple specification records exist for the same product-test pair, apply the record with the latest `effective_from` date that is on or before the sample's `tested_date`.

Cost of poor quality (COPQ) applies only to lots that fail. Rejection events are recorded in `rejection_events.csv` with a `rework_possible` flag and separate per-unit cost columns for rework and disposal outcomes. Each rejection event includes a `rejection_date`; match each event to the failed lot whose `batch_end_datetime` (date portion) is the most recent date on or before `rejection_date`.

Mean specification deviation for a group is the average of `|measured_value − target_value| / ((upper_spec_limit − lower_spec_limit) / 2)` across all test results from lots in that group, using the most recently tested result per lot-test pair.

Input data:

`/workspace/data/production_lots.csv` — lot_id, product_id, line_id, batch_start_datetime, batch_end_datetime, quantity_produced; lot_id is assigned by each line's production controller and resets at the start of each calendar month, so the same lot_id can appear up to three times across Q1; attribute each lot to the calendar month of its production start (batch_start_datetime); match quality results to the lot whose batch_end_datetime (date portion) is the most recent date on or before tested_date (analytical testing follows batch completion, within five days)

`/workspace/data/quality_results.csv` — result_id, lot_id, test_id, measured_value, units, analyst_id, tested_date

`/workspace/data/product_specifications.csv` — product_id, test_id, lower_spec_limit, upper_spec_limit, target_value, effective_from

`/workspace/data/test_catalog.csv` — test_id, test_name, test_category, reporting_unit

`/workspace/data/rejection_events.csv` — lot_id, rejection_date, rejection_reason, rework_possible, rework_cost_per_unit, disposal_cost_per_unit

Required outputs:

Save `/workspace/quality_report.csv` with columns: line_id, product_id, month (YYYY-MM), lots_produced, lots_passed, pass_rate (float, rounded to 4 decimal places), mean_spec_deviation (float, rounded to 4 decimal places), total_copq (float, rounded to 2 decimal places). Include one row per (line_id, product_id, month) with at least one lot, sorted by line_id then product_id then month.

Save `/workspace/summary.json` with keys total_lots_produced (int), failed_lot_count (int, count of lots that failed at least one quality test), overall_pass_rate (float, rounded to 4 decimal places), total_copq (float, rounded to 2 decimal places), line_with_worst_pass_rate (string, the line_id), product_with_highest_copq (string, the product_id).
