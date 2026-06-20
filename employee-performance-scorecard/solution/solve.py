"""
Q1 2024 Employee Performance Scorecard.

Exploratory checks on the raw data revealed several characteristics that
shaped the implementation:

1.  employee_id values are not globally unique — each business unit assigns its
    own sequence (EMP-001 through EMP-040), so the same string appears in all
    four units.  Every join between tables requires the composite key
    (business_unit_id, employee_id).

2.  Roughly 32 employees carry no termination_date (NaT) because they are still
    active.  pandas evaluates any date comparison against NaT as False, so the
    Q1 eligibility filter must treat NaT as "no expiry" with an explicit null
    check: hire_date <= Q1_END AND (termination_date IS NULL OR
    termination_date >= Q1_START).

3.  Twenty employees have two review records — an initial rating and a manager
    revision with a later review_date.  Deduplication to the row with the
    maximum review_date per (business_unit_id, employee_id) is required before
    any downstream calculation.

4.  rating_scales.csv holds two effective-date versions of the rating-to-score
    mapping.  Reviews conducted before 2024-02-01 use the old scale
    (Exceeds=5.0, Needs Improvement=1.0); later reviews use the recalibrated
    scale (Exceeds=4.0, Needs Improvement=2.0).  A plain merge on rating alone
    produces two candidate rows per review; the correct row is the one whose
    effective_from is the most recent date on or before the review_date.

5.  employee_goals.csv is keyed by (department_id, employee_id), not
    (business_unit_id, employee_id).  Joining goals to employees requires
    bridging through department_metadata.csv to recover business_unit_id.
    A naive join on employee_id alone produces 4× cross-BU phantom duplicates,
    mixing the goals of unrelated employees who share the same employee_id string.

6.  Employees with two or more Q1 PIP events are subject to a composite score
    cap at 2.5.  Employees with exactly one Q1 event are not — so the count is
    filtered to Q1 dates before grouping.
"""

import json
import os
import pandas as pd
from pathlib import Path

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
DATA_DIR      = Path(os.environ.get("DATA_DIR", str(WORKSPACE_DIR / "data")))

SCORECARD_PATH = WORKSPACE_DIR / "performance_scorecard.csv"
SUMMARY_PATH   = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END   = pd.Timestamp("2024-03-31")


def load_data():
    employees  = pd.read_csv(DATA_DIR / "employees.csv",
                             parse_dates=["hire_date", "termination_date"])
    reviews    = pd.read_csv(DATA_DIR / "performance_reviews.csv",
                             parse_dates=["review_date"])
    scales     = pd.read_csv(DATA_DIR / "rating_scales.csv",
                             parse_dates=["effective_from"])
    goals      = pd.read_csv(DATA_DIR / "employee_goals.csv")
    pip_events = pd.read_csv(DATA_DIR / "pip_events.csv",
                             parse_dates=["event_date"])
    dept_meta  = pd.read_csv(DATA_DIR / "department_metadata.csv")
    return employees, reviews, scales, goals, pip_events, dept_meta


def filter_active_employees(employees):
    """Keep employees active at any point in Q1 2024.

    NaT termination_date means the employee is still employed (open-ended).
    pandas `termination_date >= Q1_START` evaluates NaT as False, so the
    open-ended case must be handled with an explicit isna() check.
    """
    open_ended = employees["termination_date"].isna()
    return employees[
        (employees["hire_date"] <= Q1_END) &
        (open_ended | (employees["termination_date"] >= Q1_START))
    ].copy()


def get_canonical_reviews(reviews):
    """Return the latest review per (business_unit_id, employee_id).

    Twenty employees have an initial review and a later manager revision.
    Sorting ascending then keeping the last row gives the revision.
    """
    return (
        reviews
        .sort_values("review_date")
        .drop_duplicates(subset=["business_unit_id", "employee_id"], keep="last")
        .reset_index(drop=True)
    )


def attach_rating_scores(canonical_reviews, scales):
    """SCD join: attach the score from the most recently effective rating row
    on or before each employee's review_date.

    Steps:
      1. Cross-join reviews to scales on rating (produces two rows per review).
      2. Filter to scale rows where effective_from <= review_date.
      3. Keep the row with the highest effective_from per employee.
    """
    merged = canonical_reviews.merge(scales, on="rating", how="left")
    valid  = merged[merged["effective_from"] <= merged["review_date"]].copy()
    # BOTTLENECK: idxmax on effective_from picks the most recently effective row
    best_idx = valid.groupby(["business_unit_id", "employee_id"])["effective_from"].idxmax()
    return (
        valid.loc[best_idx]
        [["business_unit_id", "employee_id", "review_date", "rating", "score"]]
        .rename(columns={"score": "rating_score"})
        .reset_index(drop=True)
    )


def compute_goal_completion(goals, dept_meta):
    """Weighted average completion pct per (business_unit_id, employee_id).

    Goals are keyed by (department_id, employee_id).  Bridge through
    department_metadata to recover business_unit_id before grouping.
    """
    goals_with_bu = goals.merge(
        dept_meta[["department_id", "business_unit_id"]],
        on="department_id", how="left",
    )
    g = goals_with_bu.assign(
        weighted=goals_with_bu["weight"] * goals_with_bu["completion_pct"]
    )
    agg = (
        g.groupby(["business_unit_id", "employee_id"])
        .agg(total_weighted=("weighted", "sum"), total_weight=("weight", "sum"))
    )
    agg["goal_completion_pct"] = (agg["total_weighted"] / agg["total_weight"]).round(2)
    return agg[["goal_completion_pct"]].reset_index()


def count_q1_pip_events(pip_events):
    """Count PIP events per employee strictly within Q1 2024."""
    pip_q1 = pip_events[
        (pip_events["event_date"] >= Q1_START) &
        (pip_events["event_date"] <= Q1_END)
    ]
    counts = (
        pip_q1
        .groupby(["business_unit_id", "employee_id"])
        .size()
        .reset_index()
        .rename(columns={0: "pip_event_count"})
    )
    return counts


def build_scorecard(active, rated_reviews, goal_scores, pip_counts):
    df = active.merge(
        rated_reviews[["business_unit_id", "employee_id",
                       "review_date", "rating", "rating_score"]],
        on=["business_unit_id", "employee_id"], how="left",
    )
    df = df.merge(goal_scores,  on=["business_unit_id", "employee_id"], how="left")
    df = df.merge(pip_counts,   on=["business_unit_id", "employee_id"], how="left")

    df["pip_event_count"]    = df["pip_event_count"].fillna(0).astype(int)
    df["goal_completion_pct"] = df["goal_completion_pct"].fillna(0.0)

    goal_score = df["goal_completion_pct"] / 100.0 * 5.0
    df["composite_score"] = (0.6 * df["rating_score"] + 0.4 * goal_score).round(4)

    # BOTTLENECK: cap applies only when Q1 PIP count >= 2, not for a single event
    cap_mask = df["pip_event_count"] >= 2
    df.loc[cap_mask, "composite_score"] = (
        df.loc[cap_mask, "composite_score"].clip(upper=2.5)
    )

    df["bonus_eligible"] = (
        (df["composite_score"] >= 3.5) & (df["pip_event_count"] == 0)
    )

    df["review_date"] = df["review_date"].dt.strftime("%Y-%m-%d")

    out_cols = [
        "employee_id", "business_unit_id", "name", "role_level",
        "review_date", "rating", "rating_score",
        "goal_completion_pct", "composite_score",
        "pip_event_count", "bonus_eligible",
    ]
    return (
        df[out_cols]
        .sort_values(["business_unit_id", "employee_id"])
        .reset_index(drop=True)
    )


def build_summary(scorecard):
    bu_means = scorecard.groupby("business_unit_id")["composite_score"].mean()
    return {
        "total_employees_assessed": int(len(scorecard)),
        "bonus_eligible_count":     int(scorecard["bonus_eligible"].sum()),
        "mean_composite_score":     float(round(scorecard["composite_score"].mean(), 4)),
        "bu_with_highest_mean_score": str(bu_means.idxmax()),
        "employees_on_pip":         int((scorecard["pip_event_count"] >= 1).sum()),
    }


def main():
    employees, reviews, scales, goals, pip_events, dept_meta = load_data()

    active          = filter_active_employees(employees)
    canon_reviews   = get_canonical_reviews(reviews)
    rated_reviews   = attach_rating_scores(canon_reviews, scales)
    goal_scores     = compute_goal_completion(goals, dept_meta)
    pip_counts      = count_q1_pip_events(pip_events)

    scorecard = build_scorecard(active, rated_reviews, goal_scores, pip_counts)
    summary   = build_summary(scorecard)

    scorecard.to_csv(SCORECARD_PATH, index=False)
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {SCORECARD_PATH}  ({len(scorecard)} rows)")
    print(f"Wrote {SUMMARY_PATH}")
    print(f"  total_employees_assessed  : {summary['total_employees_assessed']}")
    print(f"  bonus_eligible_count      : {summary['bonus_eligible_count']}")
    print(f"  mean_composite_score      : {summary['mean_composite_score']:.4f}")
    print(f"  bu_with_highest_mean_score: {summary['bu_with_highest_mean_score']}")
    print(f"  employees_on_pip          : {summary['employees_on_pip']}")


if __name__ == "__main__":
    main()
