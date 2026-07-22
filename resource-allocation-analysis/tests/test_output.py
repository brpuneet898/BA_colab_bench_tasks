"""
Tests for the April 2024 resource-allocation report.

Reasoning challenges this suite verifies (none of these are named in
instruction.md -- each is discoverable by inspecting the raw data):

1. Billable scoping -- billable_utilization_pct must count only hours logged
   against projects.csv rows where engagement_type == "client_billable".
   Billability is not present in timesheets.csv itself; it requires a join
   to projects.csv. A subset of resources also log real hours against
   internal/practice_development engagements alongside client work.

2. Raw-vs-filtered re-derivation -- bench_hours (idle capacity) must be
   computed from ALL April hours logged, not the billable-filtered subset
   used for (1). Reusing the filtered frame silently overstates bench_hours.

3. Capacity netting -- net_capacity_hours must subtract every row in
   time_off.csv overlapping April for that resource, regardless of
   leave_type (individual PTO and a firm-wide holiday use different
   leave_type values but both reduce capacity the same way).

4. Revision resolution -- assignment_revisions.csv allows multiple revision
   rows per (resource_id, project_id) when planned allocation is amended
   mid-assignment. Some of these amendments are submitted with an effective
   date range that OVERLAPS the original revision's range rather than
   starting where it left off -- the original revision still legitimately
   governs the days before the correction took effect. The correct
   resolution for any date is: among all revisions for that pair covering
   that date, take the one with the latest submitted_date. Naively keeping
   only the single most-recently-submitted row per assignment discards the
   earlier revision entirely and extends the corrected rate backward over
   days it never covered.

5. Authorization gate -- assignment_revisions.csv also carries
   approval_status. A revision with approval_status == "rejected" was
   proposed but never authorized -- it must be excluded before resolving
   which revision governs a date, leaving the prior authorized revision in
   force for the entire period the rejected one claims to cover.
   instruction.md signals this with a single word ("authorized") inside the
   existing planned-hours sentence; it never names approval_status or
   describes a filter. A model that resolves recency (4) without also
   filtering to approved revisions lets a never-authorized revision govern
   real dates.

6. Region-scoped project code collision (join-structure) -- projects.csv
   carries a region column, and project_id is only unique WITHIN a region:
   a minority of project codes are reused independently across two or more
   regions (a shared numbering pool that predates each region's own PSA
   instance). Every resource is staffed exclusively within their own
   region's project pool, so the data itself is never ambiguous, but the
   correct join from timesheets to projects requires first attaching each
   resource's own region (from resources.csv) and merging on (region,
   project_id) together. Merging on project_id alone silently matches a
   colliding code against every region that reuses it, duplicating that
   timesheet row once per spurious match and inflating all_hours_logged
   (and understating bench_hours) for the resources whose project code
   happens to collide.

7. Regional holiday capacity netting (calendar/group-scoped netting, a
   different skill from (6)'s join-key integrity) -- regional_holidays.csv
   lists non-working days observed by EMEA and APAC (AMER has none beyond
   the firm-wide time_off.csv holiday). "Available April capacity" is
   prorated over business days without specifying which calendar --
   net_capacity_hours must subtract these region-specific dates too,
   combined into the same deduped set of covered days as time_off.csv. A
   model that computes gross capacity from a single global business-day
   count and only nets time_off.csv silently overstates capacity for every
   EMEA/APAC resource.

Ground truth is computed inline below, directly from
/workspace/data/*.csv -- never from a pre-baked constant.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

WORKSPACE_DIR = Path("/workspace") if Path("/workspace").exists() else Path(__file__).parent.parent
DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
    else Path(__file__).parent.parent / "environment" / "data"

APRIL_START = pd.Timestamp("2024-04-01")
APRIL_END = pd.Timestamp("2024-04-30")

RESOURCE_SUMMARY_COLS = {
    "resource_id", "role", "region", "net_capacity_hours",
    "billable_hours", "all_hours_logged", "billable_utilization_pct", "bench_hours",
}
ASSIGNMENT_ACCURACY_COLS = {
    "resource_id", "project_id", "resolved_planned_hours",
    "actual_hours_logged", "allocation_accuracy_pct",
}


@pytest.fixture(scope="module")
def ground_truth():
    resources = pd.read_csv(DATA_DIR / "resources.csv")
    projects = pd.read_csv(DATA_DIR / "projects.csv")
    revisions = pd.read_csv(DATA_DIR / "assignment_revisions.csv")
    timesheets = pd.read_csv(DATA_DIR / "timesheets.csv")
    time_off = pd.read_csv(DATA_DIR / "time_off.csv")
    regional_holidays = pd.read_csv(DATA_DIR / "regional_holidays.csv")

    revisions = revisions.copy()
    revisions["eff_start"] = pd.to_datetime(revisions.effective_start_date)
    revisions["eff_end"] = pd.to_datetime(revisions.effective_end_date)
    revisions["sub"] = pd.to_datetime(revisions.submitted_date)

    timesheets = timesheets.copy()
    timesheets["week_ending_date"] = pd.to_datetime(timesheets.week_ending_date)
    # Every week in this dataset falls cleanly inside a single calendar
    # month (April 2024 starts on a Monday), so scoping to April is
    # unambiguous. project_id is only unique WITHIN a region, so the
    # project join must go through each resource's own region first.
    april_ts = timesheets[
        (timesheets.week_ending_date >= APRIL_START) & (timesheets.week_ending_date <= APRIL_END)
    ].merge(resources[["resource_id", "region"]], on="resource_id", how="left") \
     .merge(projects[["project_id", "region", "engagement_type"]], on=["region", "project_id"], how="left")

    time_off = time_off.copy()
    time_off["start_date"] = pd.to_datetime(time_off.start_date)
    time_off["end_date"] = pd.to_datetime(time_off.end_date)

    regional_holidays = regional_holidays.copy()
    regional_holidays["holiday_date"] = pd.to_datetime(regional_holidays.holiday_date)

    gross_capacity = (
        np.busday_count(APRIL_START.date(), (APRIL_END + pd.Timedelta(days=1)).date()) / 5 * 40
    )

    def covered_hours(rid, region):
        rows = time_off[time_off.resource_id == rid]
        covered_days = set()
        for _, r in rows.iterrows():
            s, e = max(r.start_date, APRIL_START), min(r.end_date, APRIL_END)
            if s > e:
                continue
            covered_days.update(pd.bdate_range(s, e))
        for _, r in regional_holidays[regional_holidays.region == region].iterrows():
            d = r.holiday_date
            if APRIL_START <= d <= APRIL_END:
                covered_days.add(d)
        return len(covered_days) * 8.0

    out = resources.copy()
    out["net_capacity_hours"] = out.apply(
        lambda r: gross_capacity - covered_hours(r.resource_id, r.region), axis=1
    )

    all_hours = april_ts.groupby("resource_id").hours_logged.sum()
    billable_hours = (
        april_ts[april_ts.engagement_type == "client_billable"]
        .groupby("resource_id").hours_logged.sum()
    )
    out["all_hours_logged"] = out.resource_id.map(all_hours).fillna(0.0)
    out["billable_hours"] = out.resource_id.map(billable_hours).fillna(0.0)
    out["billable_utilization_pct"] = (out.billable_hours / out.net_capacity_hours * 100).round(2)
    out["bench_hours"] = (out.net_capacity_hours - out.all_hours_logged).clip(lower=0.0).round(2)

    # Revision resolution, April-scoped, per (resource_id, project_id) pair.
    # Only authorized (approved) revisions can govern a date -- a rejected
    # revision was proposed but never took effect.
    authorized = revisions[revisions.approval_status == "approved"]
    pairs = authorized.groupby(["resource_id", "project_id"]).agg(
        pair_start=("eff_start", "min"), pair_end=("eff_end", "max")
    ).reset_index()
    active_pairs = pairs[(pairs.pair_start <= APRIL_END) & (pairs.pair_end >= APRIL_START)].copy()

    def resolved_hours(rid, pid, window_start, window_end):
        g = authorized[(authorized.resource_id == rid) & (authorized.project_id == pid)]
        total = 0.0
        for d in pd.bdate_range(window_start, window_end):
            covering = g[(g.eff_start <= d) & (g.eff_end >= d)]
            if covering.empty:
                continue
            winner = covering.loc[covering["sub"].idxmax()]
            total += winner.planned_allocation_pct / 100.0 * 8.0
        return total

    acc_rows = []
    for _, row in active_pairs.iterrows():
        window_start = max(row.pair_start, APRIL_START)
        window_end = min(row.pair_end, APRIL_END)
        resolved = resolved_hours(row.resource_id, row.project_id, window_start, window_end)
        actual = april_ts[
            (april_ts.resource_id == row.resource_id) & (april_ts.project_id == row.project_id)
        ].hours_logged.sum()
        accuracy = 100.0 - abs(resolved - actual) / resolved * 100.0 if resolved > 0 else (
            100.0 if actual == 0 else 0.0
        )
        acc_rows.append(dict(
            resource_id=row.resource_id, project_id=row.project_id,
            resolved_planned_hours=round(resolved, 2), actual_hours_logged=round(float(actual), 2),
            allocation_accuracy_pct=round(max(0.0, accuracy), 2),
        ))
    accuracy_df = pd.DataFrame(acc_rows)

    internal_mix_ids = sorted(
        set(april_ts[april_ts.engagement_type == "client_billable"].resource_id)
        & set(april_ts[april_ts.engagement_type != "client_billable"].resource_id)
    )
    zero_timesheet_ids = sorted(set(resources.resource_id) - set(april_ts.resource_id))

    # Resources whose April timesheets reference a project_id that is reused
    # by more than one region -- a naive project_id-only merge (dropping the
    # region key) would duplicate their rows for that assignment.
    region_counts_by_pid = projects.groupby("project_id").region.nunique()
    colliding_project_ids = set(region_counts_by_pid[region_counts_by_pid > 1].index)
    collision_resource_ids = sorted(set(
        april_ts[april_ts.project_id.isin(colliding_project_ids)].resource_id
    ))

    rejected_pairs = sorted(
        set(zip(revisions[revisions.approval_status == "rejected"].resource_id,
                revisions[revisions.approval_status == "rejected"].project_id))
        & set(zip(active_pairs.resource_id, active_pairs.project_id))
    )

    # Resources in a region with any regional holiday -- a naive capacity
    # calculation that only nets time_off.csv overstates net_capacity_hours
    # for these resources.
    holiday_regions = set(regional_holidays.region.unique())
    holiday_affected_ids = sorted(set(resources[resources.region.isin(holiday_regions)].resource_id))

    return dict(
        resource_summary=out, assignment_accuracy=accuracy_df,
        internal_mix_ids=internal_mix_ids, zero_timesheet_ids=zero_timesheet_ids,
        collision_resource_ids=collision_resource_ids, holiday_affected_ids=holiday_affected_ids,
        projects=projects, time_off=time_off, revisions=revisions, rejected_pairs=rejected_pairs,
        regional_holidays=regional_holidays,
    )


def _load_csv(name):
    path = WORKSPACE_DIR / name
    assert path.exists(), f"Expected output file missing: {name}"
    return pd.read_csv(path)


def test_case_01_output_files_exist():
    for name in ["resource_summary.csv", "assignment_accuracy.csv", "summary.json"]:
        assert (WORKSPACE_DIR / name).exists(), f"Missing required output: {name}"


def test_case_02_resource_summary_structure(ground_truth):
    df = _load_csv("resource_summary.csv")
    missing = RESOURCE_SUMMARY_COLS - set(df.columns)
    assert not missing, f"resource_summary.csv missing columns: {missing}"
    assert len(df) == len(ground_truth["resource_summary"]), (
        f"resource_summary.csv must have one row per resource "
        f"({len(ground_truth['resource_summary'])}), got {len(df)}"
    )
    assert set(df.resource_id) == set(ground_truth["resource_summary"].resource_id)


def test_case_03_assignment_accuracy_structure(ground_truth):
    df = _load_csv("assignment_accuracy.csv")
    missing = ASSIGNMENT_ACCURACY_COLS - set(df.columns)
    assert not missing, f"assignment_accuracy.csv missing columns: {missing}"
    expected_n = len(ground_truth["assignment_accuracy"])
    assert len(df) == expected_n, (
        f"assignment_accuracy.csv must have exactly one row per (resource_id, project_id) "
        f"pair with an assignment overlapping April ({expected_n}), got {len(df)}. "
        f"A row count of more/fewer usually means April-overlap filtering was done wrong."
    )


def test_case_04_billable_utilization_domain_hook(ground_truth):
    """engagement_type lives only in projects.csv -- billable_utilization_pct
    must be scoped to client_billable hours, not all hours logged. Excludes
    the regional-holiday cohort (test_12) so a failure here is attributable
    specifically to the engagement_type filter, not net_capacity_hours."""
    df = _load_csv("resource_summary.csv").set_index("resource_id")
    gt = ground_truth["resource_summary"].set_index("resource_id")
    holiday_ids = set(ground_truth["holiday_affected_ids"])
    ids = [r for r in ground_truth["internal_mix_ids"] if r not in holiday_ids]
    assert len(ids) >= 50, "sanity: expected a meaningful internal-mix cohort outside the holiday cohort"

    deltas = (df.loc[ids, "billable_utilization_pct"] - gt.loc[ids, "billable_utilization_pct"]).abs()
    n_within = (deltas <= 2.0).sum()
    assert n_within / len(ids) >= 0.85, (
        f"billable_utilization_pct wrong for {len(ids) - n_within}/{len(ids)} resources that mix "
        f"client-billable and internal/practice_development work -- check that only "
        f"engagement_type == 'client_billable' hours are counted in the numerator."
    )


def test_case_05_bench_hours_reredivation(ground_truth):
    """bench_hours must be recomputed from ALL logged hours, not the
    billable-filtered frame used for billable_utilization_pct. Excludes the
    region-collision cohort (test_11) and regional-holiday cohort (test_12)
    so a failure here is attributable specifically to reusing the filtered
    frame, not the project join or net_capacity_hours."""
    df = _load_csv("resource_summary.csv").set_index("resource_id")
    gt = ground_truth["resource_summary"].set_index("resource_id")
    exclude = set(ground_truth["collision_resource_ids"]) | set(ground_truth["holiday_affected_ids"])
    ids = [r for r in ground_truth["internal_mix_ids"] if r not in exclude]
    assert len(ids) >= 50, "sanity: expected a meaningful internal-mix cohort outside the collision/holiday cohorts"

    deltas = (df.loc[ids, "bench_hours"] - gt.loc[ids, "bench_hours"]).abs()
    n_within = (deltas <= 2.0).sum()
    assert n_within / len(ids) >= 0.85, (
        f"bench_hours wrong for {len(ids) - n_within}/{len(ids)} internal-mix resources -- "
        f"likely reusing the billable-filtered frame instead of recomputing from all hours."
    )

    zero_ids = [r for r in ground_truth["zero_timesheet_ids"] if r not in exclude]
    gt_bench = gt.loc[zero_ids, "bench_hours"]
    actual_bench = df.loc[zero_ids, "bench_hours"]
    assert (actual_bench - gt_bench).abs().le(2.0).all(), (
        "resources with zero April timesheet rows must show bench_hours "
        "approximately equal to their full net capacity."
    )


def test_case_06_capacity_netting(ground_truth):
    """net_capacity_hours must subtract every time_off.csv row overlapping
    April, regardless of leave_type (including the firm-wide holiday), AND
    every applicable regional_holidays.csv row for the resource's region
    (test_12 isolates the latter specifically; this is the general,
    all-resources check on the same output column)."""
    df = _load_csv("resource_summary.csv").set_index("resource_id").sort_index()
    gt = ground_truth["resource_summary"].set_index("resource_id").sort_index()

    deltas = (df["net_capacity_hours"] - gt["net_capacity_hours"]).abs()
    n_within = (deltas <= 0.5).sum()
    assert n_within / len(gt) >= 0.90, (
        f"net_capacity_hours wrong for {len(gt) - n_within}/{len(gt)} resources -- "
        f"every resource loses at least the firm-wide holiday from time_off.csv, "
        f"and a subset also carry individual PTO rows."
    )


def test_case_07_revision_resolution_coverage(ground_truth):
    """The governing revision for any date is the one with the latest
    submitted_date among revisions whose effective range covers that date --
    not simply the row with the latest submitted_date for the assignment."""
    df = _load_csv("assignment_accuracy.csv").set_index(["resource_id", "project_id"]).sort_index()
    gt = ground_truth["assignment_accuracy"].set_index(["resource_id", "project_id"]).sort_index()
    common = df.index.intersection(gt.index)
    assert len(common) == len(gt), "assignment_accuracy.csv is missing rows present in the ground truth"

    deltas = (df.loc[common, "allocation_accuracy_pct"] - gt.loc[common, "allocation_accuracy_pct"]).abs()
    n_within = (deltas <= 1.5).sum()
    assert n_within / len(common) >= 0.90, (
        f"allocation_accuracy_pct wrong for {len(common) - n_within}/{len(common)} "
        f"(resource, project) pairs -- check how the governing revision is picked "
        f"when a pair has more than one revision row."
    )


def test_case_08_summary_json_aggregates(ground_truth):
    """Portfolio-level cross-check on summary.json -- catches drift that
    individual-row tolerance checks could average out, and confirms all
    four promised keys are present and correctly aggregated."""
    with open(WORKSPACE_DIR / "summary.json") as f:
        summary = json.load(f)
    required_keys = {"resource_count", "mean_billable_utilization_pct", "total_bench_hours", "mean_allocation_accuracy_pct"}
    missing = required_keys - set(summary)
    assert not missing, f"summary.json missing keys: {missing}"

    assert summary["resource_count"] == len(ground_truth["resource_summary"])

    expected_util = ground_truth["resource_summary"].billable_utilization_pct.mean()
    assert abs(summary["mean_billable_utilization_pct"] - expected_util) <= 2.0, (
        f"summary.json mean_billable_utilization_pct = {summary['mean_billable_utilization_pct']}, "
        f"expected ~{expected_util:.2f}"
    )

    expected_bench = ground_truth["resource_summary"].bench_hours.sum()
    assert abs(summary["total_bench_hours"] - expected_bench) <= 20.0, (
        f"summary.json total_bench_hours = {summary['total_bench_hours']}, expected ~{expected_bench:.2f}"
    )

    expected_accuracy = ground_truth["assignment_accuracy"].allocation_accuracy_pct.mean()
    assert abs(summary["mean_allocation_accuracy_pct"] - expected_accuracy) <= 2.0, (
        f"summary.json mean_allocation_accuracy_pct = {summary['mean_allocation_accuracy_pct']}, "
        f"expected ~{expected_accuracy:.2f}"
    )


def test_case_09_revision_authorization_gate(ground_truth):
    """A revision with approval_status == 'rejected' was never authorized --
    it must be excluded when resolving the governing revision for a date,
    leaving the prior authorized revision in force for the whole period the
    rejected one claims to cover. Isolated from test_07 to attribute
    failures specifically to the authorization filter, not general
    date-recency resolution."""
    df = _load_csv("assignment_accuracy.csv").set_index(["resource_id", "project_id"]).sort_index()
    gt = ground_truth["assignment_accuracy"].set_index(["resource_id", "project_id"]).sort_index()
    rejected_pairs = ground_truth["rejected_pairs"]
    assert len(rejected_pairs) >= 50, "sanity: expected a meaningful rejected-amendment cohort in the fixture"

    common = [p for p in rejected_pairs if p in df.index]
    assert len(common) == len(rejected_pairs), (
        "assignment_accuracy.csv is missing rows for pairs with a rejected amendment"
    )

    deltas = (df.loc[common, "allocation_accuracy_pct"] - gt.loc[common, "allocation_accuracy_pct"]).abs()
    n_within = (deltas <= 1.5).sum()
    assert n_within / len(common) >= 0.85, (
        f"allocation_accuracy_pct wrong for {len(common) - n_within}/{len(common)} (resource, project) "
        f"pairs with a rejected amendment revision -- a rejected revision was never authorized and must "
        f"not govern any date, even though it has the latest submitted_date and its effective range "
        f"claims to cover part of April."
    )


def test_case_10_anti_cheat_sentinels(ground_truth):
    """Structural sentinels on the immutable source data -- guard against
    outputs that happen to pass value checks without genuinely resolving
    the underlying joins."""
    projects = ground_truth["projects"]
    counts = projects.engagement_type.value_counts()
    assert counts.get("client_billable") == 450
    assert counts.get("internal") == 105
    assert counts.get("practice_development") == 90

    time_off = ground_truth["time_off"]
    lt_counts = time_off.leave_type.value_counts()
    assert lt_counts.get("company_holiday") == 1500
    assert lt_counts.get("pto") == 300

    assert len(ground_truth["zero_timesheet_ids"]) == 133

    revisions = ground_truth["revisions"]
    approval_counts = revisions.approval_status.value_counts()
    assert approval_counts.get("rejected", 0) >= 50
    assert approval_counts.get("approved", 0) > approval_counts.get("rejected", 0) * 3

    region_counts_by_pid = projects.groupby("project_id").region.nunique()
    n_colliding_project_ids = int((region_counts_by_pid > 1).sum())
    assert n_colliding_project_ids >= 15, (
        "sanity: expected a meaningful number of project_id values reused across regions"
    )

    regional_holidays = ground_truth["regional_holidays"]
    assert set(regional_holidays.region.unique()) == {"EMEA", "APAC"}
    assert len(regional_holidays) >= 4


def test_case_11_region_scoped_project_code_collision(ground_truth):
    """project_id is only unique WITHIN a region -- a minority of codes are
    reused independently across two or more regions. The project join must
    go through each resource's own region (from resources.csv); joining on
    project_id alone duplicates the affected resources' rows for that
    assignment, inflating all_hours_logged."""
    df = _load_csv("resource_summary.csv").set_index("resource_id")
    gt = ground_truth["resource_summary"].set_index("resource_id")
    ids = ground_truth["collision_resource_ids"]
    assert len(ids) >= 30, "sanity: expected a meaningful region-collision cohort in the fixture"

    deltas = (df.loc[ids, "all_hours_logged"] - gt.loc[ids, "all_hours_logged"]).abs()
    n_within = (deltas <= 2.0).sum()
    assert n_within / len(ids) >= 0.85, (
        f"all_hours_logged wrong for {len(ids) - n_within}/{len(ids)} resources whose April assignment "
        f"uses a project_id that is reused by another region -- the project join must go through each "
        f"resource's own region (resources.csv), not project_id alone, or rows get duplicated."
    )


def test_case_12_regional_holiday_capacity(ground_truth):
    """EMEA and APAC each observe regional holidays not shared by AMER
    (regional_holidays.csv). "Available April capacity" is prorated over
    business days without specifying which calendar -- net_capacity_hours
    must subtract these region-specific dates in addition to time_off.csv,
    or capacity (and everything derived from it) is silently overstated for
    every EMEA/APAC resource."""
    df = _load_csv("resource_summary.csv").set_index("resource_id")
    gt = ground_truth["resource_summary"].set_index("resource_id")
    ids = ground_truth["holiday_affected_ids"]
    assert len(ids) >= 100, "sanity: expected a large EMEA/APAC cohort in the fixture"

    deltas = (df.loc[ids, "net_capacity_hours"] - gt.loc[ids, "net_capacity_hours"]).abs()
    n_within = (deltas <= 1.0).sum()
    assert n_within / len(ids) >= 0.85, (
        f"net_capacity_hours wrong for {len(ids) - n_within}/{len(ids)} EMEA/APAC resources -- "
        f"regional_holidays.csv lists additional non-working days for these regions that must "
        f"reduce available April capacity alongside time_off.csv."
    )
