"""
April 2024 Resource Allocation Report.

projects.csv carries an engagement_type per project (client_billable,
internal, practice_development); billable_utilization_pct only counts hours
logged against client_billable projects, which requires joining timesheets
to projects -- engagement_type isn't present in timesheets.csv itself.
bench_hours is recomputed from ALL April hours logged (any engagement_type),
not the billable-filtered frame used for utilization.

time_off.csv reduces available capacity regardless of leave_type -- both
individual PTO and the firm-wide company_holiday rows are netted the same
way.

assignment_revisions.csv can carry more than one revision row per
(resource_id, project_id) pair when planned allocation is amended
mid-assignment. Some amendments are submitted with an effective date range
that overlaps the original revision's range rather than starting where it
left off. The governing revision for any single date is whichever revision
covering that date has the latest submitted_date -- so the resolved
allocation timeline for a pair has to be built one business day at a time,
not by simply keeping the single most-recently-submitted row for the whole
assignment (that would silently drop the earlier revision even for days it
still legitimately governs).

assignment_revisions.csv also carries approval_status. A revision that was
never authorized (approval_status == "rejected") must be excluded before
resolving which revision governs a date -- the prior authorized revision
continues governing undisturbed. instruction.md signals this with the word
"authorized" in the planned-hours definition; it never names the column.

projects.csv also carries region, and project_id is NOT globally unique --
it is only unique WITHIN a region. Every resource is staffed exclusively
within their own region's project pool (resources.csv carries each
resource's region), so joining timesheets to projects requires first
attaching each resource's region and then merging on (region, project_id)
together -- merging on project_id alone silently matches a colliding code
against every region that happens to reuse it, duplicating that timesheet
row once per spurious match and inflating all_hours_logged (and therefore
understating bench_hours) for the resources whose project code happens to
collide.

regional_holidays.csv lists non-working days observed by EMEA and APAC
(AMER has none beyond the firm-wide time_off.csv holiday). "Available April
capacity" is prorated over business days without specifying which calendar
-- net capacity must subtract these region-specific dates too, combined
into the same deduped set of covered days as time_off.csv (a resource's own
region determines which regional holidays apply to them).

A resource has zero available capacity on any day covered by time_off.csv
or their region's regional_holidays.csv -- that governs planned hours in
allocation_accuracy_pct too, not just net_capacity_hours: a resource can't
be planned to deliver a percentage of their capacity on a day they have no
capacity at all. The same per-resource unavailable-days set feeds both
net_capacity_hours and resolve_governing_revision_hours. instruction.md
makes this explicit ("...in effect on each date that month when the
resource had available capacity to work") -- added 2026-07-22 after a real
eval round showed 2/3 independent models (both Gemini runs) already
reasoning their way to this consistency on their own, while the ground
truth had never enforced it; without the clarification this was a genuine
three-way ambiguity (no exclusion / holidays-only / holidays+time_off), not
a designed trap.

projects.csv also carries contract_type and contracted_hour_cap. A minority
of client_billable projects are "fixed_fee" rather than
"time_and_materials", with a contracted_hour_cap below the project's own
natural April hours total (summed across every resource on it) -- the
practice can't bill beyond the contracted ceiling even though the hours
were genuinely logged against a client_billable engagement. billable_hours
must be capped at the PROJECT level (group by project, not by resource)
and any shortfall applied proportionally back across every resource who
logged hours on that project -- not per-resource, not first-come-first-
served by date.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")

APRIL_START = pd.Timestamp("2024-04-01")
APRIL_END = pd.Timestamp("2024-04-30")


def load_data():
    resources = pd.read_csv(DATA_DIR / "resources.csv")
    projects = pd.read_csv(DATA_DIR / "projects.csv")
    revisions = pd.read_csv(
        DATA_DIR / "assignment_revisions.csv",
        parse_dates=["effective_start_date", "effective_end_date", "submitted_date"],
    )
    timesheets = pd.read_csv(DATA_DIR / "timesheets.csv", parse_dates=["week_ending_date"])
    time_off = pd.read_csv(DATA_DIR / "time_off.csv", parse_dates=["start_date", "end_date"])
    regional_holidays = pd.read_csv(DATA_DIR / "regional_holidays.csv", parse_dates=["holiday_date"])
    return resources, projects, revisions, timesheets, time_off, regional_holidays


def apply_fixed_fee_caps(april_ts, projects):
    """billable_hours for a fixed_fee project is capped at its
    contracted_hour_cap, summed across every resource who logged hours
    against it -- if the project-wide total exceeds the cap, each
    resource's own billable contribution is scaled down proportionally so
    the sum across all of them equals the cap exactly."""
    billable = april_ts[april_ts["engagement_type"] == "client_billable"].copy()
    caps = projects.loc[
        projects["contract_type"] == "fixed_fee", ["project_id", "region", "contracted_hour_cap"]
    ]
    billable = billable.merge(caps, on=["project_id", "region"], how="left")

    project_totals = billable.groupby(["region", "project_id"])["hours_logged"].transform("sum")
    over_cap = billable["contracted_hour_cap"].notna() & (project_totals > billable["contracted_hour_cap"])
    ratio = np.where(over_cap, billable["contracted_hour_cap"] / project_totals, 1.0)
    billable["billable_hours"] = billable["hours_logged"] * ratio
    return billable


def scope_timesheets_to_april(timesheets, projects, resources):
    """Every week in this dataset falls inside a single calendar month, so
    scoping by week_ending_date alone is unambiguous.

    project_id is only unique within a region, so the project join must go
    through each resource's own region -- merging on project_id alone would
    silently match a colliding code against every region that reuses it."""
    april = timesheets[
        (timesheets["week_ending_date"] >= APRIL_START) & (timesheets["week_ending_date"] <= APRIL_END)
    ]
    april = april.merge(resources[["resource_id", "region"]], on="resource_id", how="left")
    return april.merge(projects[["project_id", "region", "engagement_type"]], on=["region", "project_id"], how="left")


def compute_unavailable_days(resources, time_off, regional_holidays):
    """Per resource, the set of April business days on which the resource
    has zero available capacity -- covered by time_off.csv or by a
    regional_holidays.csv row for their own region. Shared by both
    net_capacity_hours and resolve_governing_revision_hours: a resource
    can't be planned to work on a day they have no capacity at all."""
    unavailable = {}
    for _, r in resources.iterrows():
        resource_id, region = r["resource_id"], r["region"]
        covered_days = set()
        rows = time_off[time_off["resource_id"] == resource_id]
        for _, row in rows.iterrows():
            start, end = max(row["start_date"], APRIL_START), min(row["end_date"], APRIL_END)
            if start > end:
                continue
            covered_days.update(pd.bdate_range(start, end))
        for _, row in regional_holidays[regional_holidays["region"] == region].iterrows():
            d = row["holiday_date"]
            if APRIL_START <= d <= APRIL_END:
                covered_days.add(d)
        unavailable[resource_id] = covered_days
    return unavailable


def compute_net_capacity(resources, unavailable_days):
    business_days = np.busday_count(APRIL_START.date(), (APRIL_END + pd.Timedelta(days=1)).date())
    gross_capacity = business_days / 5 * resources["weekly_capacity_hours"]
    net = gross_capacity - resources["resource_id"].map(
        lambda rid: len(unavailable_days[rid]) * 8.0
    )
    return net


def build_resource_summary(resources, april_ts, unavailable_days, projects):
    out = resources.copy()
    out["net_capacity_hours"] = compute_net_capacity(resources, unavailable_days)

    all_hours = april_ts.groupby("resource_id")["hours_logged"].sum()
    capped_billable = apply_fixed_fee_caps(april_ts, projects)
    billable_hours = capped_billable.groupby("resource_id")["billable_hours"].sum()
    out["all_hours_logged"] = out["resource_id"].map(all_hours).fillna(0.0)
    out["billable_hours"] = out["resource_id"].map(billable_hours).fillna(0.0)
    out["billable_utilization_pct"] = out["billable_hours"] / out["net_capacity_hours"] * 100
    out["bench_hours"] = (out["net_capacity_hours"] - out["all_hours_logged"]).clip(lower=0.0)

    for col in ["net_capacity_hours", "billable_hours", "all_hours_logged",
                "billable_utilization_pct", "bench_hours"]:
        out[col] = out[col].round(2)

    return out[["resource_id", "role", "region", "net_capacity_hours",
                "billable_hours", "all_hours_logged", "billable_utilization_pct",
                "bench_hours"]].sort_values("resource_id").reset_index(drop=True)


def resolve_governing_revision_hours(pair_revisions, window_start, window_end, unavailable_days):
    """Build the resolved planned-hours timeline for one (resource, project)
    pair, one business day at a time.

    For each day, the governing revision is whichever revision covering that
    day has the latest submitted_date. A later revision only supersedes an
    earlier one for the days its own effective range actually covers -- days
    outside that range are still governed by whichever revision covers them.
    A resource has no planned hours on a day they have zero available
    capacity (time_off.csv or a regional holiday for their region).
    """
    total = 0.0
    for day in pd.bdate_range(window_start, window_end):
        if day in unavailable_days:
            continue
        covering = pair_revisions[
            (pair_revisions["effective_start_date"] <= day) & (pair_revisions["effective_end_date"] >= day)
        ]
        if covering.empty:
            continue
        winner = covering.loc[covering["submitted_date"].idxmax()]
        total += winner["planned_allocation_pct"] / 100.0 * 8.0
    return total


def build_assignment_accuracy(revisions, april_ts, unavailable_days_by_resource):
    authorized = revisions[revisions["approval_status"] == "approved"]
    pairs = authorized.groupby(["resource_id", "project_id"]).agg(
        pair_start=("effective_start_date", "min"), pair_end=("effective_end_date", "max")
    ).reset_index()
    active = pairs[(pairs["pair_start"] <= APRIL_END) & (pairs["pair_end"] >= APRIL_START)]

    rows = []
    for _, pair in active.iterrows():
        pair_revisions = authorized[
            (authorized["resource_id"] == pair["resource_id"]) & (authorized["project_id"] == pair["project_id"])
        ]
        window_start = max(pair["pair_start"], APRIL_START)
        window_end = min(pair["pair_end"], APRIL_END)
        unavailable_days = unavailable_days_by_resource[pair["resource_id"]]
        resolved = resolve_governing_revision_hours(pair_revisions, window_start, window_end, unavailable_days)

        actual = april_ts[
            (april_ts["resource_id"] == pair["resource_id"]) & (april_ts["project_id"] == pair["project_id"])
        ]["hours_logged"].sum()

        if resolved > 0:
            accuracy = 100.0 - abs(resolved - actual) / resolved * 100.0
        else:
            accuracy = 100.0 if actual == 0 else 0.0
        accuracy = max(0.0, accuracy)

        rows.append(dict(
            resource_id=pair["resource_id"], project_id=pair["project_id"],
            resolved_planned_hours=round(resolved, 2), actual_hours_logged=round(float(actual), 2),
            allocation_accuracy_pct=round(accuracy, 2),
        ))

    return pd.DataFrame(rows).sort_values(["resource_id", "project_id"]).reset_index(drop=True)


def main():
    resources, projects, revisions, timesheets, time_off, regional_holidays = load_data()

    april_ts = scope_timesheets_to_april(timesheets, projects, resources)
    unavailable_days = compute_unavailable_days(resources, time_off, regional_holidays)

    resource_summary = build_resource_summary(resources, april_ts, unavailable_days, projects)
    resource_summary.to_csv(WORKSPACE_DIR / "resource_summary.csv", index=False)

    assignment_accuracy = build_assignment_accuracy(revisions, april_ts, unavailable_days)
    assignment_accuracy.to_csv(WORKSPACE_DIR / "assignment_accuracy.csv", index=False)

    summary = {
        "resource_count": int(len(resource_summary)),
        "mean_billable_utilization_pct": float(round(resource_summary["billable_utilization_pct"].mean(), 2)),
        "total_bench_hours": float(round(resource_summary["bench_hours"].sum(), 2)),
        "mean_allocation_accuracy_pct": float(round(assignment_accuracy["allocation_accuracy_pct"].mean(), 2)),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
