"""
Q3-Q4 2023 Signup Cohort Month-3 Retention Report.

customer_id is not the join key for cohort assignment on its own: a customer
who churns and later restarts their subscription gets a brand-new signup_id
row in signups.csv, dated whenever they came back. A customer's cohort is
fixed by their earliest signup across the ENTIRE file (not just rows inside
the analysis window) -- resolving every customer_id down to its minimum
signup_date collapses these reactivations to their original cohort instead
of double-counting them as new members of a later cohort.

signup_id itself is also not guaranteed to be one row: some signup records
were corrected after entry, so a signup_id can appear more than once, an
earlier (stale) row followed by the current one. Each signup_id must be
resolved to its single most-recent row (by created_at) before the
first-signup-per-customer step above, or stale duplicate rows corrupt it.

Month-3 retention counts only genuine product usage. activity_logs.csv also
carries system_generated rows (webhook deliveries, scheduled syncs,
provisioning callbacks) logged against the customer that are not real
engagement -- only event_category == "user_action" rows count. Activity is
further limited to rows logged into the system (created_at) by the report's
cutoff date, not just rows whose event_date falls in the window.
"""

import json
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

WINDOW_START = pd.Timestamp("2023-07-01")
WINDOW_END = pd.Timestamp("2023-12-31")
REPORT_AS_OF = pd.Timestamp("2024-04-10")


def load_data():
    signups = pd.read_csv(DATA_DIR / "signups.csv", parse_dates=["signup_date", "created_at"])
    activity = pd.read_csv(DATA_DIR / "activity_logs.csv", parse_dates=["event_date", "created_at"])
    return signups, activity


def resolve_current_signups(signups):
    """Collapse amended signup_id rows to their single most-recent version."""
    # BOTTLENECK: idxmax() ties break to the first matching row in file order.
    # Amended rows are always written with a strictly earlier created_at than
    # the row they precede, so no signup_id has a genuine created_at tie here.
    idx = signups.groupby("signup_id")["created_at"].idxmax()
    return signups.loc[idx].reset_index(drop=True)


def true_first_signup(resolved_signups):
    """Each customer's cohort is fixed by the earliest signup across their
    entire history, collapsing reactivations into their original cohort."""
    return resolved_signups.groupby("customer_id")["signup_date"].min().reset_index()


def build_cohorts(first_signup):
    cohort = first_signup[
        (first_signup["signup_date"] >= WINDOW_START) & (first_signup["signup_date"] <= WINDOW_END)
    ].copy()
    cohort["cohort_period"] = cohort["signup_date"].dt.to_period("M")
    cohort["cohort_month"] = cohort["cohort_period"].astype(str)
    # BOTTLENECK: adding 3 to a monthly Period rolls the year over correctly
    # (e.g. 2023-11 + 3 -> 2024-02). Three of the six cohort months land in
    # 2024 this way; a bare `month + 3` integer add would silently produce an
    # invalid or wrong-year month for those.
    m3_period = cohort["cohort_period"] + 3
    cohort["m3_start"] = m3_period.dt.start_time
    cohort["m3_end"] = m3_period.dt.end_time
    return cohort


def mark_retained(cohort, activity):
    """A customer counts as retained only if a genuine user_action event,
    already logged into the system by the report cutoff, falls inside their
    month-3 window."""
    qualifying = activity[
        (activity["event_category"] == "user_action") & (activity["created_at"] <= REPORT_AS_OF)
    ]
    merged = cohort.merge(qualifying[["customer_id", "event_date"]], on="customer_id", how="left")
    in_window = (merged["event_date"] >= merged["m3_start"]) & (merged["event_date"] <= merged["m3_end"])
    retained_customers = set(merged.loc[in_window.fillna(False), "customer_id"])

    cohort = cohort.copy()
    cohort["retained_month_3"] = cohort["customer_id"].isin(retained_customers)
    return cohort


def build_report(cohort):
    agg = cohort.groupby("cohort_month").agg(
        cohort_size=("customer_id", "nunique"),
        retained_month_3=("retained_month_3", "sum"),
    ).reset_index()
    agg["retained_month_3"] = agg["retained_month_3"].astype(int)
    agg["retention_rate_month_3"] = (agg["retained_month_3"] / agg["cohort_size"]).round(4)
    return agg.sort_values("cohort_month").reset_index(drop=True)


def main():
    signups, activity = load_data()

    resolved_signups = resolve_current_signups(signups)
    first_signup = true_first_signup(resolved_signups)
    cohort = build_cohorts(first_signup)
    cohort = mark_retained(cohort, activity)
    report = build_report(cohort)

    report.to_csv(WORKSPACE_DIR / "cohort_retention_report.csv", index=False)

    total_customers = int(report["cohort_size"].sum())
    total_retained = int(report["retained_month_3"].sum())

    summary = {
        "total_cohort_customers": total_customers,
        "total_retained_month_3": total_retained,
        "overall_retention_rate_month_3": float(round(total_retained / total_customers, 4)),
        "best_performing_cohort": str(report.loc[report["retention_rate_month_3"].idxmax(), "cohort_month"]),
        "worst_performing_cohort": str(report.loc[report["retention_rate_month_3"].idxmin(), "cohort_month"]),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
