We need the April 2024 monthly resource allocation report for the consulting practice. For each resource, show billable utilization, idle (bench) capacity, and how close actual hours were to the allocation on record, for each of their project assignments that month. Input files are in /workspace/data/, just use the files named below and nothing else.

resources.csv has each resource’s role, region and standard weekly capacity. projects.csv tells you which project belongs to which client. assignment_revisions.csv has each resource’s planned allocation to a project. timesheets.csv has the actual hours a resource logged against a project, by week. time_off.csv has approved leave that reduces a resource’s available capacity for the days it covers.

Compute the following for each resource, for April 2024.

Available April capacity is the resource’s standard weekly capacity prorated over Aprils business days, reduced by any approved time off during the month.

Billable utilization (billable_utilization_pct) is the percent of available April capacity that was consumed by hours logged against client billable engagements.

Bench hours (bench_hours) is available April capacity not consumed by any hours logged that month, billable or otherwise. A resource can’t have negative bench hours, if hours logged meet or go over available capacity, bench hours is 0.

For each resource-project assignment active at any point during April, report allocation_accuracy_pct: 100 minus the absolute value of (actual hours minus planned hours) as a percent of planned hours, where planned hours are based on whichever authorized allocation was in effect on each date that month. When an allocation has been revised, the revision most recently on record for a given date is the one in effect for that date, an earlier revision remains in effect for any date it covers that the later one does not. If planned hours resolve to zero, report 100 when no actual hours were logged that period, and 0 otherwise.

Save /workspace/resource_summary.csv sorted by resource_id, with one row for every resource in resources.csv (include any with no logged hours that month too), columns: resource_id, role, region, net_capacity_hours, billable_hours, all_hours_logged, billable_utilization_pct, bench_hours. Round hours to 2 decimal places, round percentages to 2 decimal places as well.

Save /workspace/assignment_accuracy.csv sorted by resource_id then project_id, one row per resource-project assignment active at any point during April, columns: resource_id, project_id, resolved_planned_hours, actual_hours_logged, allocation_accuracy_pct. Round the same way.

Save /workspace/summary.json with keys: resource_count (integer), mean_billable_utilization_pct, total_bench_hours, mean_allocation_accuracy_pct, all rounded to 2 decimal places.
