A consulting practice needs its April 2024 monthly resource allocation report. For each resource, the report must show billable utilization, idle (bench) capacity, and how closely actual hours tracked the allocation on record for each of their project assignments that month. Input files are in `/workspace/data/`; use only the files named below.

`resources.csv` lists each resource's role, region, and standard weekly capacity. `projects.csv` identifies each project and the client it is delivered for. `assignment_revisions.csv` records each resource's planned allocation to a project. `timesheets.csv` records actual hours a resource logged against a project, by week. `time_off.csv` records approved leave that reduces a resource's available capacity for the days it covers.

Compute the following for each resource for April 2024.

Available April capacity is the resource's standard weekly capacity prorated over April's business days, reduced by any approved time off during the month.

Billable utilization (`billable_utilization_pct`) is the percentage of available April capacity consumed by hours logged against client-billable engagements.

Bench hours (`bench_hours`) is available April capacity not consumed by any hours logged that month, billable or otherwise.

For each resource-project assignment active at any point during April, report `allocation_accuracy_pct`: 100 minus the absolute value of (actual hours − planned hours) as a percentage of planned hours, where planned hours are based on whichever allocation was in effect on each date that month. When an allocation has been revised, the revision most recently on record for a given date is the one in effect for that date — an earlier revision remains in effect for any date it covers that the later one does not. If planned hours resolve to zero, report 100 when no actual hours were logged that period and 0 otherwise.

Save `/workspace/resource_summary.csv` sorted by `resource_id`, with one row for every resource in `resources.csv` (including any with no logged hours that month), columns: `resource_id`, `role`, `region`, `net_capacity_hours`, `billable_hours`, `all_hours_logged`, `billable_utilization_pct`, `bench_hours`. Round hours to 2 decimal places and percentages to 2 decimal places.

Save `/workspace/assignment_accuracy.csv` sorted by `resource_id` then `project_id`, with one row per resource-project assignment active at any point during April, columns: `resource_id`, `project_id`, `resolved_planned_hours`, `actual_hours_logged`, `allocation_accuracy_pct`. Round the same way.

Save `/workspace/summary.json` with keys: `resource_count` (integer), `mean_billable_utilization_pct`, `total_bench_hours`, `mean_allocation_accuracy_pct` — all rounded to 2 decimal places.
