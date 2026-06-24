"""
Tests for the Q1 2024 Employee Performance Scorecard task.

Ground truth is recomputed inline from the canonical data files baked into
the Docker image (/workspace/data/).

Headroom mechanisms tested:

  Trap 1 — (business_unit_id, employee_id) composite key.
            Each BU assigns employee IDs independently from the same sequence
            (EMP-0001 … EMP-1000), so the same string identifies different
            people across units.  A naive join on employee_id alone creates
            phantom cross-BU matches, inflating every metric for every employee.

  Trap 2 — NaT termination_date (active employees silently dropped).
            800 employees (200 per BU) have no termination_date because they
            are still employed.  pandas evaluates `termination_date >= date`
            as False when the value is NaT, silently excluding the entire
            group.  Correct filter: treat NaT as "no expiry".

  Trap 3 — Review supersession within Q1-2024.
            400 employees have a manager revision — a second Q1-2024 row with
            a later review_date.  The initial review appears first in the CSV;
            naive CSV-order reads pick up the superseded rating.

  Trap 4 — Effective-dated rating scale (SCD join on review_date).
            The rating-to-score mapping changed 2024-02-01.  Old scale rows
            are written first in rating_scales.csv.  Naive first-row or
            latest-row selection applies the wrong scale to one cohort.

  Trap 5 — PIP cumulative cap (≥2 Q1 events → composite_score ≤ 2.5).
            200 employees have ≥2 Q1 PIP events and are correctly capped.
            300 employees have exactly one Q1 event and must NOT be capped.
            Capping all employees with any PIP event is the common mistake.

  Trap 6 — Q4-2023 late-filing contamination (cross-period review cycle).
            200 employees have a Q4-2023 review row whose review_date falls in
            Feb–Mar 2024, after their Q1-2024 initial review date.  Naive
            "latest review_date" dedup across all cycles returns the Q4 rating.
            Correct: filter to review_cycle == "Q1-2024" before deduplicating.

  Trap 7 — Department bridge (employee_goals keyed by department_id).
            employee_goals.csv uses department_id, not business_unit_id.
            Recovering business_unit_id requires joining through
            department_metadata.csv.  Joining on employee_id alone creates 4×
            cross-BU phantom duplicates.

  Trap 8 — Completion unit normalisation (BU-03 fraction scale).
            BU-03 records completion_pct as a fraction (0.0–1.0); all other
            BUs use percentage (0–100).  The completion_unit column labels each
            row.  Using fraction values raw collapses BU-03 goal scores by
            100×.
"""

import json
import math
import os
import pytest
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
if not WORKSPACE_DIR.exists():
    WORKSPACE_DIR = Path(__file__).parent.parent

_env_data = os.environ.get("DATA_DIR")
if _env_data:
    DATA_DIR = Path(_env_data)
elif (WORKSPACE_DIR / "data").exists():
    DATA_DIR = WORKSPACE_DIR / "data"
else:
    DATA_DIR = Path(__file__).parent.parent / "environment" / "data"

SCORECARD_PATH = WORKSPACE_DIR / "performance_scorecard.csv"
SUMMARY_PATH   = WORKSPACE_DIR / "summary.json"

Q1_START = pd.Timestamp("2024-01-01")
Q1_END   = pd.Timestamp("2024-03-31")

# ---------------------------------------------------------------------------
# Ground-truth helpers  (replicate oracle logic exactly)
# ---------------------------------------------------------------------------

def _filter_active(employees):
    open_ended = employees["termination_date"].isna()
    return employees[
        (employees["hire_date"] <= Q1_END) &
        (open_ended | (employees["termination_date"] >= Q1_START))
    ].copy()


def _canonical_reviews(reviews):
    q1_only = reviews[reviews["review_cycle"] == "Q1-2024"]
    return (
        q1_only
        .sort_values("review_date")
        .drop_duplicates(subset=["business_unit_id", "employee_id"], keep="last")
        .reset_index(drop=True)
    )


def _attach_scores(canon, scales):
    merged = canon.merge(scales, on="rating", how="left")
    valid  = merged[merged["effective_from"] <= merged["review_date"]].copy()
    best   = valid.groupby(["business_unit_id", "employee_id"])["effective_from"].idxmax()
    return (
        valid.loc[best]
        [["business_unit_id", "employee_id", "review_date", "rating", "score"]]
        .rename(columns={"score": "rating_score"})
        .reset_index(drop=True)
    )


def _goal_completion(goals, dept_meta):
    goals_with_bu = goals.merge(
        dept_meta[["department_id", "business_unit_id"]],
        on="department_id", how="left",
    )
    frac = goals_with_bu["completion_unit"] == "fraction"
    goals_with_bu = goals_with_bu.copy()
    goals_with_bu.loc[frac, "completion_pct"] = goals_with_bu.loc[frac, "completion_pct"] * 100
    g = goals_with_bu.assign(
        weighted=goals_with_bu["weight"] * goals_with_bu["completion_pct"]
    )
    agg = g.groupby(["business_unit_id", "employee_id"]).agg(
        total_weighted=("weighted", "sum"),
        total_weight=("weight", "sum"),
    )
    agg["goal_completion_pct"] = (agg["total_weighted"] / agg["total_weight"]).round(2)
    return agg[["goal_completion_pct"]].reset_index()


def _pip_counts(pip_events):
    pip_q1 = pip_events[
        (pip_events["event_date"] >= Q1_START) & (pip_events["event_date"] <= Q1_END)
    ]
    return (
        pip_q1.groupby(["business_unit_id", "employee_id"])
        .size()
        .reset_index()
        .rename(columns={0: "pip_event_count"})
    )


def _build_expected(employees, reviews, scales, goals, pip_events, dept_meta):
    active  = _filter_active(employees)
    canon   = _canonical_reviews(reviews)
    rated   = _attach_scores(canon, scales)
    goal_sc = _goal_completion(goals, dept_meta)
    pip_cnt = _pip_counts(pip_events)

    df = active.merge(
        rated[["business_unit_id", "employee_id", "review_date", "rating", "rating_score"]],
        on=["business_unit_id", "employee_id"], how="left",
    )
    df = df.merge(goal_sc,  on=["business_unit_id", "employee_id"], how="left")
    df = df.merge(pip_cnt,  on=["business_unit_id", "employee_id"], how="left")

    df["pip_event_count"]     = df["pip_event_count"].fillna(0).astype(int)
    df["goal_completion_pct"] = df["goal_completion_pct"].fillna(0.0)

    goal_score = df["goal_completion_pct"] / 100.0 * 5.0
    df["composite_score"] = (0.6 * df["rating_score"] + 0.4 * goal_score).round(4)

    cap_mask = df["pip_event_count"] >= 2
    df.loc[cap_mask, "composite_score"] = (
        df.loc[cap_mask, "composite_score"].clip(upper=2.5)
    )

    df["bonus_eligible"] = (df["composite_score"] >= 3.5) & (df["pip_event_count"] == 0)

    return df.sort_values(["business_unit_id", "employee_id"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_data():
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


@pytest.fixture(scope="module")
def expected(raw_data):
    employees, reviews, scales, goals, pip_events, dept_meta = raw_data
    return _build_expected(employees, reviews, scales, goals, pip_events, dept_meta)


@pytest.fixture(scope="module")
def agent_scorecard():
    return pd.read_csv(SCORECARD_PATH) if SCORECARD_PATH.exists() else None


@pytest.fixture(scope="module")
def agent_summary():
    if not SUMMARY_PATH.exists():
        return None
    with open(SUMMARY_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 01 — Input sentinels (anti-cheat)
# ---------------------------------------------------------------------------

def test_case_01_input_sentinels(raw_data):
    """Canonical data fingerprint — verifies input files were not modified."""
    employees, reviews, scales, goals, pip_events, dept_meta = raw_data

    assert len(employees) == 4000, \
        f"employees.csv must not be modified (expected 4000 rows, got {len(employees)})"

    nat_count = int(employees["termination_date"].isna().sum())
    assert nat_count == 800, \
        (f"employees.csv must contain exactly 800 open-ended records "
         f"(termination_date is NaT), got {nat_count}")

    emp_id_span = int(
        (employees.groupby("employee_id")["business_unit_id"].nunique() > 1).sum()
    )
    assert emp_id_span == 1000, \
        (f"employee_id must not be globally unique — each BU uses its own "
         f"sequence (expected 1000 employee_id strings spanning >1 BU, got {emp_id_span})")

    multi_review_count = int(
        (reviews.groupby(["business_unit_id", "employee_id"]).size() > 1).sum()
    )
    assert multi_review_count == 600, \
        (f"performance_reviews.csv must contain exactly 600 employees with more than one "
         f"review row (400 Q1-2024 revisions + 200 Q4-2023 late filings), "
         f"got {multi_review_count}")

    q4_count = int((reviews["review_cycle"] == "Q4-2023").sum())
    assert q4_count == 200, \
        (f"performance_reviews.csv must contain exactly 200 Q4-2023 review rows "
         f"(late-filed Q4 assessments with review_date in Q1 2024), got {q4_count}")

    scale_counts = scales.groupby("rating").size()
    for label in ["Exceeds", "Meets", "Needs Improvement"]:
        assert scale_counts.get(label, 0) == 2, \
            f"rating_scales.csv must have exactly 2 rows for rating '{label}', " \
            f"got {scale_counts.get(label, 0)}"

    pip_events_ts = pip_events.copy()
    pip_events_ts["event_date"] = pd.to_datetime(pip_events_ts["event_date"])
    pip_q1 = pip_events_ts[
        (pip_events_ts["event_date"] >= Q1_START) &
        (pip_events_ts["event_date"] <= Q1_END)
    ]
    multi_pip = int((pip_q1.groupby(["business_unit_id", "employee_id"]).size() >= 2).sum())
    assert multi_pip == 200, \
        (f"pip_events.csv must contain exactly 200 employees with ≥2 Q1 events, "
         f"got {multi_pip}")

    assert "department_id" in goals.columns, \
        "employee_goals.csv must contain a department_id column (not business_unit_id)"
    assert "business_unit_id" not in goals.columns, \
        "employee_goals.csv must not contain business_unit_id — link via department_metadata.csv"
    assert "completion_unit" in goals.columns, \
        "employee_goals.csv must contain a completion_unit column"
    assert set(goals["completion_unit"].unique()) == {"pct", "fraction"}, \
        "completion_unit must contain both 'pct' and 'fraction' values"


# ---------------------------------------------------------------------------
# Test 02 — Output structure
# ---------------------------------------------------------------------------

def test_case_02_output_structure(agent_scorecard, agent_summary):
    """Both output files exist with the required shape and columns."""
    assert SCORECARD_PATH.exists(), "performance_scorecard.csv not found in /workspace"
    assert SUMMARY_PATH.exists(),   "summary.json not found in /workspace"
    assert agent_scorecard is not None

    required_cols = [
        "employee_id", "business_unit_id", "name", "role_level",
        "review_date", "rating", "rating_score",
        "goal_completion_pct", "composite_score",
        "pip_event_count", "bonus_eligible",
    ]
    for col in required_cols:
        assert col in agent_scorecard.columns, \
            f"Missing column in performance_scorecard.csv: {col}"

    assert len(agent_scorecard) == 3600, \
        (f"performance_scorecard.csv must have 3600 rows (one per Q1-active employee), "
         f"got {len(agent_scorecard)}")

    required_keys = [
        "total_employees_assessed", "bonus_eligible_count",
        "mean_composite_score", "bu_with_highest_mean_score", "employees_on_pip",
    ]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — Composite key correctness  (Trap 1)
# ---------------------------------------------------------------------------

def test_case_03_composite_key_employee_counts(agent_scorecard, expected):
    """
    Trap 1 — joining performance_reviews or employee_goals to employees on
    employee_id alone inflates every BU's row count ~4x because EMP-0001 in
    BU-01 is a different person from EMP-0001 in BU-02, BU-03, and BU-04.
    Correct join key: (business_unit_id, employee_id).

    Verifies per-BU row counts and spot-checks that EMP-0001 in BU-01 is not
    confused with its cross-BU namesakes.
    """
    failures = []

    for bu_id in ["BU-01", "BU-02", "BU-03", "BU-04"]:
        exp_count = int(len(expected[expected["business_unit_id"] == bu_id]))
        act_count = int(len(agent_scorecard[agent_scorecard["business_unit_id"] == bu_id]))
        if act_count != exp_count:
            failures.append(
                f"{bu_id}: expected {exp_count} employees, got {act_count}. "
                "Join must use (business_unit_id, employee_id) — "
                "employee_id is not globally unique."
            )

    # Spot-check EMP-0001 in BU-01 has the correct name (not mixed with other BUs)
    exp_row = expected[
        (expected["business_unit_id"] == "BU-01") & (expected["employee_id"] == "EMP-0001")
    ]
    act_row = agent_scorecard[
        (agent_scorecard["business_unit_id"] == "BU-01") &
        (agent_scorecard["employee_id"] == "EMP-0001")
    ]
    if not exp_row.empty and not act_row.empty:
        exp_name = str(exp_row["name"].iloc[0])
        act_name = str(act_row["name"].iloc[0])
        if act_name != exp_name:
            failures.append(
                f"BU-01/EMP-0001: name '{act_name}' != expected '{exp_name}'. "
                "Employee data from different BUs appears to be mixed."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — Active employees: NaT filter  (Trap 2)
# ---------------------------------------------------------------------------

def test_case_04_active_employees_nat_filter(raw_data, agent_scorecard, expected):
    """
    Trap 2 — 800 employees (200 per BU) have no termination_date (NaT) because
    they are currently active.  pandas evaluates `termination_date >= Timestamp`
    as False for NaT, silently dropping the entire group.

    Verifies that NaT-termination employees appear in the output with a valid
    (non-null) composite_score.
    """
    employees, *_ = raw_data
    nat_emps = employees[employees["termination_date"].isna()][
        ["business_unit_id", "employee_id"]
    ].head(10)

    failures = []
    for _, row in nat_emps.iterrows():
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == row["business_unit_id"]) &
            (agent_scorecard["employee_id"] == row["employee_id"])
        ]
        if act_row.empty:
            failures.append(
                f"{row['business_unit_id']}/{row['employee_id']}: not found in output. "
                "Employees with no termination_date are still active and must be included. "
                "pandas evaluates `termination_date >= date` as False when the value is NaT."
            )
        elif pd.isna(act_row["composite_score"].iloc[0]):
            failures.append(
                f"{row['business_unit_id']}/{row['employee_id']}: "
                "found in output but composite_score is null."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — Effective-dated rating scale (Trap 4)
# ---------------------------------------------------------------------------

def test_case_05_rating_scale_scd(raw_data, agent_scorecard, expected):
    """
    Trap 4 — The rating-to-score mapping changed on 2024-02-01.
    Reviews conducted before that date must use the old scale
    (Exceeds=5.0, Needs Improvement=1.0).  Applying the new scale to all
    reviews assigns Exceeds=4.0 and NI=2.0 to January reviewees — a 1.0-point
    error in rating_score that shifts composite_score by 0.6 per employee.

    Spot-checks up to 5 employees whose final review falls before 2024-02-01
    and whose rating is Exceeds or Needs Improvement (Meets=3.0 in both scales).
    """
    _, reviews, _, _, _, _ = raw_data

    canon = _canonical_reviews(reviews)
    pre_feb = canon[
        (canon["review_date"] < pd.Timestamp("2024-02-01")) &
        (canon["rating"].isin(["Exceeds", "Needs Improvement"]))
    ].head(5)

    if pre_feb.empty:
        pytest.skip("No pre-Feb Exceeds/NI reviews in this dataset")

    failures = []
    for _, row in pre_feb.iterrows():
        exp_row = expected[
            (expected["business_unit_id"] == row["business_unit_id"]) &
            (expected["employee_id"] == row["employee_id"])
        ]
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == row["business_unit_id"]) &
            (agent_scorecard["employee_id"] == row["employee_id"])
        ]
        if exp_row.empty or act_row.empty:
            continue

        exp_score = float(exp_row["rating_score"].iloc[0])
        act_raw   = act_row["rating_score"].iloc[0]
        if pd.isna(act_raw):
            failures.append(
                f"{row['business_unit_id']}/{row['employee_id']}: rating_score is null"
            )
            continue
        act_score = float(act_raw)
        if not math.isclose(act_score, exp_score, abs_tol=0.001):
            failures.append(
                f"{row['business_unit_id']}/{row['employee_id']} "
                f"(rating={row['rating']}, reviewed {row['review_date'].date()}): "
                f"rating_score {act_score} != expected {exp_score}. "
                "Reviews before 2024-02-01 must use the old scale."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 06 — Review supersession  (Trap 3)
# ---------------------------------------------------------------------------

def test_case_06_review_cycle_and_supersession(raw_data, agent_scorecard, expected):
    """
    Traps 3 & 7 — performance_reviews.csv contains Q1-2024 reviews AND Q4-2023
    reviews filed late (review_date in Feb–Mar 2024).  For contaminated employees
    the Q4 date is AFTER the Q1 date, so naive 'latest review_date' dedup returns
    the Q4 rating.  Correct approach: restrict to review_cycle == 'Q1-2024', then
    within Q1 take the row with the maximum review_date (handles manager revisions).

    Spot-checks 5 employees who have a Q4-2023 contamination row, verifying that
    the agent used the Q1-2024 rating, not the Q4 finalization.
    """
    _, reviews, _, _, _, _ = raw_data

    # Find employees who have BOTH a Q4-2023 row AND a Q1-2024 row
    has_q4 = set(
        reviews[reviews["review_cycle"] == "Q4-2023"]
        .apply(lambda r: (r["business_unit_id"], r["employee_id"]), axis=1)
    )
    contaminated = reviews[
        reviews.apply(
            lambda r: (r["business_unit_id"], r["employee_id"]) in has_q4, axis=1
        ) & (reviews["review_cycle"] == "Q1-2024")
    ].drop_duplicates(["business_unit_id", "employee_id"]).head(5)
    revised = contaminated[["business_unit_id", "employee_id"]].reset_index(drop=True)

    failures = []
    for _, row in revised.iterrows():
        bu_id, emp_id = row["business_unit_id"], row["employee_id"]
        exp_row = expected[
            (expected["business_unit_id"] == bu_id) &
            (expected["employee_id"] == emp_id)
        ]
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == bu_id) &
            (agent_scorecard["employee_id"] == emp_id)
        ]
        if exp_row.empty or act_row.empty:
            continue

        exp_rating = str(exp_row["rating"].iloc[0])
        act_rating = str(act_row["rating"].iloc[0])
        if act_rating != exp_rating:
            failures.append(
                f"{bu_id}/{emp_id}: rating '{act_rating}' != expected '{exp_rating}'. "
                "This employee has both a Q4-2023 late-filing row and a Q1-2024 row. "
                "The Q4 row has a later review_date; filter to review_cycle == 'Q1-2024' "
                "before deduplicating."
            )

        exp_rs = float(exp_row["rating_score"].iloc[0])
        act_rs_raw = act_row["rating_score"].iloc[0]
        if not pd.isna(act_rs_raw) and not math.isclose(float(act_rs_raw), exp_rs, abs_tol=0.01):
            failures.append(
                f"{bu_id}/{emp_id}: rating_score {float(act_rs_raw):.1f} != "
                f"expected {exp_rs:.1f}"
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 07 — PIP cumulative cap  (Trap 5)
# ---------------------------------------------------------------------------

def test_case_07_pip_cumulative_cap(raw_data, agent_scorecard, expected):
    """
    Trap 5 — Only employees with ≥2 Q1 PIP events are subject to the 2.5 cap.
    200 employees have ≥2 Q1 events and must be capped; 300 employees have
    exactly 1 Q1 event and must NOT be capped.

    Common mistake: cap all employees with any PIP event, incorrectly
    suppressing scores for those with only one incident.
    """
    _, _, _, _, pip_events, _ = raw_data
    pip_ts = pip_events.copy()
    pip_ts["event_date"] = pd.to_datetime(pip_ts["event_date"])
    pip_q1 = pip_ts[
        (pip_ts["event_date"] >= Q1_START) & (pip_ts["event_date"] <= Q1_END)
    ]
    pip_by_emp = (
        pip_q1.groupby(["business_unit_id", "employee_id"])
        .size()
        .reset_index()
        .rename(columns={0: "count"})
    )

    multi_pip  = pip_by_emp[pip_by_emp["count"] >= 2]
    single_pip = pip_by_emp[pip_by_emp["count"] == 1]

    failures = []

    # ≥2 events: composite_score must be ≤ 2.5
    for _, row in multi_pip.iterrows():
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == row["business_unit_id"]) &
            (agent_scorecard["employee_id"] == row["employee_id"])
        ]
        if act_row.empty:
            continue
        act_score = float(act_row["composite_score"].iloc[0])
        if act_score > 2.5 + 1e-4:
            failures.append(
                f"{row['business_unit_id']}/{row['employee_id']}: "
                f"composite_score {act_score:.4f} > 2.5 but employee has "
                f"{int(row['count'])} Q1 PIP events (cap applies at ≥2)."
            )

    # Exactly 1 event: must NOT be capped if their natural score exceeds 2.5
    for _, row in single_pip.head(8).iterrows():
        bu_id, emp_id = row["business_unit_id"], row["employee_id"]
        exp_row = expected[
            (expected["business_unit_id"] == bu_id) &
            (expected["employee_id"] == emp_id)
        ]
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == bu_id) &
            (agent_scorecard["employee_id"] == emp_id)
        ]
        if exp_row.empty or act_row.empty:
            continue
        exp_score = float(exp_row["composite_score"].iloc[0])
        if exp_score <= 2.55:
            continue  # natural score is near or below 2.5; cannot distinguish

        act_score = float(act_row["composite_score"].iloc[0])
        if act_score < 2.5 - 0.05:
            failures.append(
                f"{bu_id}/{emp_id}: composite_score {act_score:.4f} appears capped "
                f"at 2.5 but employee has only 1 Q1 PIP event "
                f"(expected {exp_score:.4f}). Cap must not apply for a single incident."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — Bonus eligibility count  (all traps compound)
# ---------------------------------------------------------------------------

def test_case_08_bonus_eligibility_count(agent_summary, expected):
    """
    Total bonus_eligible_count in summary.json.
    Sensitive to all five traps: wrong employees, wrong ratings, wrong scores,
    or incorrect cap application all shift the eligible population.
    """
    exp_count = int(expected["bonus_eligible"].sum())
    act_count = int(agent_summary["bonus_eligible_count"])
    assert abs(act_count - exp_count) <= 1, \
        f"bonus_eligible_count: expected {exp_count}, got {act_count}"


# ---------------------------------------------------------------------------
# Test 09 — Mean composite score per business unit  (all traps compound)
# ---------------------------------------------------------------------------

def test_case_09_mean_composite_score_by_bu(agent_scorecard, expected):
    """
    Mean composite_score per business unit within ±0.05 tolerance.
    Any trap that corrupts composite_score for a material fraction of employees
    in a BU will push the mean outside this band.
    """
    failures = []
    for bu_id in ["BU-01", "BU-02", "BU-03", "BU-04"]:
        exp_mean = float(expected[expected["business_unit_id"] == bu_id]["composite_score"].mean())
        act_bu   = agent_scorecard[agent_scorecard["business_unit_id"] == bu_id]
        if act_bu.empty:
            failures.append(f"{bu_id}: no rows found in agent scorecard")
            continue
        act_mean = float(act_bu["composite_score"].mean())
        if not math.isclose(act_mean, exp_mean, abs_tol=0.05):
            failures.append(
                f"{bu_id}: mean composite_score {act_mean:.4f} vs "
                f"expected {exp_mean:.4f} (diff {act_mean - exp_mean:+.4f})"
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 10 — Summary scalars  (all traps compound)
# ---------------------------------------------------------------------------

def test_case_10_summary_scalars(agent_summary, expected):
    """
    Verifies total_employees_assessed, employees_on_pip, and
    bu_with_highest_mean_score in summary.json.
    Each is sensitive to a different combination of traps.
    """
    exp_total = int(len(expected))
    act_total = int(agent_summary["total_employees_assessed"])
    assert act_total == exp_total, \
        f"total_employees_assessed: expected {exp_total}, got {act_total}"

    exp_on_pip = int((expected["pip_event_count"] >= 1).sum())
    act_on_pip = int(agent_summary["employees_on_pip"])
    assert abs(act_on_pip - exp_on_pip) <= 1, \
        f"employees_on_pip: expected {exp_on_pip}, got {act_on_pip}"

    bu_means   = expected.groupby("business_unit_id")["composite_score"].mean()
    exp_top_bu = str(bu_means.idxmax())
    act_top_bu = str(agent_summary["bu_with_highest_mean_score"])
    assert act_top_bu == exp_top_bu, \
        (f"bu_with_highest_mean_score: expected {exp_top_bu}, got {act_top_bu}. "
         "Errors in composite_score calculation shift which BU ranks highest.")


# ---------------------------------------------------------------------------
# Test 11 — Goal completion accuracy  (department bridge trap)
# ---------------------------------------------------------------------------

def test_case_11_goal_department_bridge(raw_data, agent_scorecard, expected):
    """
    Structural trap — employee_goals.csv is keyed by (department_id, employee_id),
    not (business_unit_id, employee_id).  Recovering business_unit_id requires
    joining through department_metadata.csv.  A naive join on employee_id alone
    creates 4× cross-BU phantom duplicates, mixing the goals of unrelated
    employees who share the same employee_id string across business units.

    Spot-checks goal_completion_pct for 2 employees per business unit across
    BU-01, BU-02, and BU-04 (6 total; BU-03 is excluded — its errors originate
    from the fraction-scale trap tested in test_case_12, not the bridge).
    """
    # Sample from BU-01, BU-02, BU-04 only (pct-scale BUs).
    # BU-03 is excluded here because its goals use a different completion scale
    # (fraction vs pct) which is tested separately in test_case_12.  Mixing the
    # two failure modes would produce misleading attribution in error messages.
    pct_scale_bus = ["BU-01", "BU-02", "BU-04"]
    sample = (
        expected[expected["business_unit_id"].isin(pct_scale_bus)]
        .groupby("business_unit_id", group_keys=False)
        .head(2)
        [["business_unit_id", "employee_id", "goal_completion_pct"]]
    )

    failures = []
    for _, row in sample.iterrows():
        bu_id, emp_id = row["business_unit_id"], row["employee_id"]
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == bu_id) &
            (agent_scorecard["employee_id"] == emp_id)
        ]
        if act_row.empty:
            continue
        exp_pct = float(row["goal_completion_pct"])
        act_pct = float(act_row["goal_completion_pct"].iloc[0])
        if not math.isclose(act_pct, exp_pct, abs_tol=0.01):
            failures.append(
                f"{bu_id}/{emp_id}: goal_completion_pct {act_pct:.2f} != "
                f"expected {exp_pct:.2f}. "
                "Goals are stored with department_id; bridge through "
                "department_metadata.csv to recover business_unit_id before joining."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 12 — Completion unit normalisation  (BU-03 fraction-scale trap)
# ---------------------------------------------------------------------------

def test_case_12_completion_unit_normalisation(raw_data, agent_scorecard, expected):
    """
    BU-03 (Operations) records goal completion as a fraction (0.0–1.0); all
    other BUs use percentage (0–100).  The completion_unit column in
    employee_goals.csv labels each row 'fraction' or 'pct'.  Applying
    goal_score = completion_pct / 100 × 5 to raw fraction values yields
    goal_score ≈ 0.035 instead of 3.5 — a 100× collapse for every BU-03 employee.

    Spot-checks goal_completion_pct for 4 BU-03 employees to verify the
    fraction→percentage conversion was applied.
    """
    sample = (
        expected[expected["business_unit_id"] == "BU-03"]
        .head(4)
        [["business_unit_id", "employee_id", "goal_completion_pct"]]
    )

    if sample.empty:
        pytest.skip("No BU-03 employees in expected output")

    failures = []
    for _, row in sample.iterrows():
        bu_id, emp_id = row["business_unit_id"], row["employee_id"]
        act_row = agent_scorecard[
            (agent_scorecard["business_unit_id"] == bu_id) &
            (agent_scorecard["employee_id"] == emp_id)
        ]
        if act_row.empty:
            continue
        exp_pct = float(row["goal_completion_pct"])
        act_pct = float(act_row["goal_completion_pct"].iloc[0])
        if not math.isclose(act_pct, exp_pct, abs_tol=0.5):
            failures.append(
                f"{bu_id}/{emp_id}: goal_completion_pct {act_pct:.2f} != "
                f"expected {exp_pct:.2f}. "
                "BU-03 goals use completion_unit='fraction' (0.0–1.0 scale); "
                "multiply by 100 before applying the weighted-average formula."
            )

    assert not failures, "\n".join(failures)
