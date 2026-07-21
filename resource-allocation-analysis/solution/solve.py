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

projects.csv also carries billing_type. A minority of engagement_type ==
"client_billable" projects have billing_type == "pro_bono" -- a real client
engagement the practice does not bill for. billable_utilization_pct must
exclude their hours even though engagement_type alone says "client_billable"
-- instruction.md signals this by defining the metric as hours "the
practice actually bills the client for", not as hours logged against
"client-billable engagements"; it never names billing_type.
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
    return resources, projects, revisions, timesheets, time_off


def scope_timesheets_to_april(timesheets, projects):
    """Every week in this dataset falls inside a single calendar month, so
    scoping by week_ending_date alone is unambiguous."""
    april = timesheets[
        (timesheets["week_ending_date"] >= APRIL_START) & (timesheets["week_ending_date"] <= APRIL_END)
    ]
    return april.merge(projects[["project_id", "engagement_type", "billing_type"]], on="project_id", how="left")


def compute_net_capacity(resources, time_off):
    business_days = np.busday_count(APRIL_START.date(), (APRIL_END + pd.Timedelta(days=1)).date())
    gross_capacity = business_days / 5 * resources["weekly_capacity_hours"]

    def time_off_hours(resource_id):
        rows = time_off[time_off["resource_id"] == resource_id]
        covered_days = set()
        for _, row in rows.iterrows():
            start, end = max(row["start_date"], APRIL_START), min(row["end_date"], APRIL_END)
            if start > end:
                continue
            covered_days.update(pd.bdate_range(start, end))
        return len(covered_days) * 8.0

    net = gross_capacity - resources["resource_id"].apply(time_off_hours)
    return net


def build_resource_summary(resources, april_ts, time_off):
    out = resources.copy()
    out["net_capacity_hours"] = compute_net_capacity(resources, time_off)

    all_hours = april_ts.groupby("resource_id")["hours_logged"].sum()
    billable_hours = (
        april_ts[(april_ts["engagement_type"] == "client_billable") & (april_ts["billing_type"] == "standard")]
        .groupby("resource_id")["hours_logged"].sum()
    )
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


def resolve_governing_revision_hours(pair_revisions, window_start, window_end):
    """Build the resolved planned-hours timeline for one (resource, project)
    pair, one business day at a time.

    For each day, the governing revision is whichever revision covering that
    day has the latest submitted_date. A later revision only supersedes an
    earlier one for the days its own effective range actually covers -- days
    outside that range are still governed by whichever revision covers them.
    """
    total = 0.0
    for day in pd.bdate_range(window_start, window_end):
        covering = pair_revisions[
            (pair_revisions["effective_start_date"] <= day) & (pair_revisions["effective_end_date"] >= day)
        ]
        if covering.empty:
            continue
        winner = covering.loc[covering["submitted_date"].idxmax()]
        total += winner["planned_allocation_pct"] / 100.0 * 8.0
    return total


def build_assignment_accuracy(revisions, april_ts):
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
        resolved = resolve_governing_revision_hours(pair_revisions, window_start, window_end)

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
    resources, projects, revisions, timesheets, time_off = load_data()

    april_ts = scope_timesheets_to_april(timesheets, projects)

    resource_summary = build_resource_summary(resources, april_ts, time_off)
    resource_summary.to_csv(WORKSPACE_DIR / "resource_summary.csv", index=False)

    assignment_accuracy = build_assignment_accuracy(revisions, april_ts)
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
