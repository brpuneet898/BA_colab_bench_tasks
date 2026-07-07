"""
Tests for the Q3-Q4 2023 signup cohort Month-3 retention report.

Contract (instruction.md): the deliverable is
    /workspace/cohort_retention_report.csv — one row per cohort month
    /workspace/summary.json                — five scalar keys

Reasoning challenges this suite exercises:
  1. Reactivation identity — a customer who reactivates gets a brand-new
     signup_id row dated inside the analysis window; cohort assignment must
     resolve to the customer's earliest-ever signup, not double-count them
     as a new cohort member.
  2. Amendment resolution — some signup_id values appear more than once (a
     stale row plus a corrected current row); each signup_id must resolve to
     its single most-recent row (by created_at) before computing cohort
     membership.
  3. Domain-category gap — activity_logs.csv mixes genuine user_action rows
     with system_generated rows (webhook deliveries, scheduled syncs, ...)
     that are not real engagement; only user_action rows count toward
     retention.
  4. Ingestion-lag cutoff — activity must be logged into the system
     (created_at) by the report's 2024-04-10 cutoff, not just dated
     (event_date) inside the month-3 window.

test_case_01/02/03 are anti-tamper sentinels on the input data, not graded
reasoning. test_case_04-06 check output structure/schema. The remainder are
hard, ground-truth-derived checks tied to the four challenges above.
"""

import json
import pandas as pd
import pytest
from pathlib import Path

if Path("/workspace").exists():
    WORKSPACE_DIR = Path("/workspace")
else:
    WORKSPACE_DIR = Path(__file__).parent.parent

DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
           else Path(__file__).parent.parent / "environment" / "data"
REPORT_PATH = WORKSPACE_DIR / "cohort_retention_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

WINDOW_START = pd.Timestamp("2023-07-01")
WINDOW_END = pd.Timestamp("2023-12-31")
REPORT_AS_OF = pd.Timestamp("2024-04-10")

EXPECTED_COHORT_MONTHS = ["2023-07", "2023-08", "2023-09", "2023-10", "2023-11", "2023-12"]


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_case_01_input_data_not_tampered():
    """Sentinel: table row counts and event category mix must not be modified."""
    customers = pd.read_csv(DATA_DIR / "customers.csv")
    signups = pd.read_csv(DATA_DIR / "signups.csv")
    subscriptions = pd.read_csv(DATA_DIR / "subscriptions.csv")
    activity = pd.read_csv(DATA_DIR / "activity_logs.csv")

    assert len(customers) == 6_000, \
        f"customers.csv row count must not be modified (expected 6,000, got {len(customers)})."
    assert len(signups) == 6_705, \
        f"signups.csv row count must not be modified (expected 6,705, got {len(signups)})."
    assert len(subscriptions) == 6_344, \
        f"subscriptions.csv row count must not be modified (expected 6,344, got {len(subscriptions)})."
    assert len(activity) == 132_517, \
        f"activity_logs.csv row count must not be modified (expected 132,517, got {len(activity)})."

    cat_counts = activity["event_category"].value_counts()
    assert cat_counts.get("system_generated", 0) > 30_000, \
        f"Expected >30,000 system_generated rows, got {cat_counts.get('system_generated', 0)}."
    assert cat_counts.get("user_action", 0) > 80_000, \
        f"Expected >80,000 user_action rows, got {cat_counts.get('user_action', 0)}."


def test_case_02_signup_identity_sentinel():
    """Sentinel: signups.csv contains customers with more than one genuine
    signup_id (reactivations) and signup_id values with more than one row
    (amendments)."""
    signups = pd.read_csv(DATA_DIR / "signups.csv")

    distinct_signup_ids_per_customer = signups.groupby("customer_id")["signup_id"].nunique()
    assert (distinct_signup_ids_per_customer > 1).sum() > 250, \
        (f"Expected >250 customers with more than one distinct signup_id, got "
         f"{(distinct_signup_ids_per_customer > 1).sum()}. customer_id must not be "
         f"assumed to have exactly one signup event.")

    rows_per_signup_id = signups.groupby("signup_id").size()
    assert (rows_per_signup_id > 1).sum() > 250, \
        (f"Expected >250 signup_id values with more than one row, got "
         f"{(rows_per_signup_id > 1).sum()}. signup_id must not be assumed to be "
         f"one row per record.")
    assert signups["signup_status"].value_counts().get("Superseded", 0) > 250, \
        "Expected >250 'Superseded' rows in signups.csv."


def test_case_03_activity_log_sentinel():
    """Sentinel: activity_logs.csv mixes event categories and logging lag."""
    activity = pd.read_csv(DATA_DIR / "activity_logs.csv", parse_dates=["event_date", "created_at"])

    categories = set(activity["event_category"].unique())
    assert categories == {"user_action", "system_generated"}, \
        f"Expected exactly the categories user_action/system_generated, got {categories}."

    lag_days = (activity["created_at"] - activity["event_date"]).dt.days
    assert (lag_days > 15).sum() > 100, \
        (f"Expected >100 rows logged more than 15 days after event_date, got "
         f"{(lag_days > 15).sum()}. created_at must not be assumed to equal event_date.")


# ── Output file structure ─────────────────────────────────────────────────────

def test_case_04_report_exists_and_shape():
    assert REPORT_PATH.exists(), "cohort_retention_report.csv not found in /workspace."
    df = pd.read_csv(REPORT_PATH)
    required = {"cohort_month", "cohort_size", "retained_month_3", "retention_rate_month_3"}
    missing = required - set(df.columns)
    assert not missing, f"Missing columns: {missing}"
    assert len(df) == 6, f"Expected exactly 6 rows (one per cohort month), got {len(df)}."
    assert set(df["cohort_month"]) == set(EXPECTED_COHORT_MONTHS), \
        f"Expected cohort months {EXPECTED_COHORT_MONTHS}, got {sorted(df['cohort_month'])}."


def test_case_05_report_sort_order():
    df = pd.read_csv(REPORT_PATH)
    expected = df.sort_values("cohort_month").reset_index(drop=True)
    assert list(df["cohort_month"]) == list(expected["cohort_month"]), \
        "Report must be sorted by cohort_month ascending."


def test_case_06_summary_schema():
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace."
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    required = {"total_cohort_customers", "total_retained_month_3", "overall_retention_rate_month_3",
                "best_performing_cohort", "worst_performing_cohort"}
    missing = required - set(s.keys())
    assert not missing, f"Missing keys in summary.json: {missing}"
    assert isinstance(s["total_cohort_customers"], int)
    assert isinstance(s["total_retained_month_3"], int)
    assert isinstance(s["overall_retention_rate_month_3"], float)
    assert isinstance(s["best_performing_cohort"], str)
    assert isinstance(s["worst_performing_cohort"], str)


# ── Ground-truth fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth():
    signups = pd.read_csv(DATA_DIR / "signups.csv", parse_dates=["signup_date", "created_at"])
    activity = pd.read_csv(DATA_DIR / "activity_logs.csv", parse_dates=["event_date", "created_at"])

    # Amendments: each signup_id collapses to its single most-recent row.
    current_idx = signups.groupby("signup_id")["created_at"].idxmax()
    resolved = signups.loc[current_idx].reset_index(drop=True)

    # Reactivations: each customer's cohort is fixed by their earliest-ever signup.
    first_signup = resolved.groupby("customer_id")["signup_date"].min().reset_index()

    cohort = first_signup[
        (first_signup["signup_date"] >= WINDOW_START) & (first_signup["signup_date"] <= WINDOW_END)
    ].copy()
    cohort["cohort_period"] = cohort["signup_date"].dt.to_period("M")
    cohort["cohort_month"] = cohort["cohort_period"].astype(str)
    m3_period = cohort["cohort_period"] + 3
    cohort["m3_start"] = m3_period.dt.start_time
    cohort["m3_end"] = m3_period.dt.end_time

    # Retention: only user_action rows logged (created_at) by the cutoff count.
    qualifying = activity[
        (activity["event_category"] == "user_action") & (activity["created_at"] <= REPORT_AS_OF)
    ]
    merged = cohort.merge(qualifying[["customer_id", "event_date"]], on="customer_id", how="left")
    in_window = (merged["event_date"] >= merged["m3_start"]) & (merged["event_date"] <= merged["m3_end"])
    retained_customers = set(merged.loc[in_window.fillna(False), "customer_id"])
    cohort["retained_month_3"] = cohort["customer_id"].isin(retained_customers)

    report = cohort.groupby("cohort_month").agg(
        cohort_size=("customer_id", "nunique"),
        retained_month_3=("retained_month_3", "sum"),
    ).reset_index()
    report["retained_month_3"] = report["retained_month_3"].astype(int)
    report["retention_rate_month_3"] = (report["retained_month_3"] / report["cohort_size"]).round(4)
    report = report.sort_values("cohort_month").reset_index(drop=True)

    total_customers = int(report["cohort_size"].sum())
    total_retained = int(report["retained_month_3"].sum())

    return {
        "report": report.set_index("cohort_month"),
        "total_cohort_customers": total_customers,
        "total_retained_month_3": total_retained,
        "overall_retention_rate_month_3": float(round(total_retained / total_customers, 4)),
        "best_performing_cohort": str(report.loc[report["retention_rate_month_3"].idxmax(), "cohort_month"]),
        "worst_performing_cohort": str(report.loc[report["retention_rate_month_3"].idxmin(), "cohort_month"]),
    }


# ── Hard test 1: cohort size (reactivation + amendment resolution) ───────────

def test_case_07_total_cohort_customers_exact(ground_truth):
    """total_cohort_customers depends only on window scoping and resolving
    every customer to their earliest-ever, amendment-resolved signup; must
    match exactly. Treating each signup row as a new cohort member
    double-counts reactivating customers and overstates this figure."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["total_cohort_customers"] == ground_truth["total_cohort_customers"], \
        (f"total_cohort_customers: got {s['total_cohort_customers']}, "
         f"expected {ground_truth['total_cohort_customers']}.")


def test_case_08_cohort_size_per_month_exact(ground_truth):
    """Each cohort month's cohort_size must match exactly, not just the
    total. A partial fix (e.g. resolving amendments but not reactivations)
    could still land close on the overall total by coincidence; checking
    every month individually closes that gap."""
    report = pd.read_csv(REPORT_PATH).set_index("cohort_month")
    gt = ground_truth["report"]
    for month in gt.index:
        assert month in report.index, f"Missing cohort_month '{month}' in report."
        exp = int(gt.loc[month, "cohort_size"])
        got = int(report.loc[month, "cohort_size"])
        assert got == exp, f"cohort_size for {month}: got {got}, expected {exp}."


@pytest.fixture(scope="module")
def naive_cohort_size_per_month():
    """cohort_size per month if every signup row is grouped by its own raw
    signup_date, without resolving amendments or reactivations to a
    customer's earliest-ever signup."""
    signups = pd.read_csv(DATA_DIR / "signups.csv", parse_dates=["signup_date"])
    in_window = signups[(signups["signup_date"] >= WINDOW_START) & (signups["signup_date"] <= WINDOW_END)]
    naive = in_window.copy()
    naive["cohort_month"] = naive["signup_date"].dt.to_period("M").astype(str)
    return naive.groupby("cohort_month")["customer_id"].nunique().to_dict()


def test_case_09_cohort_size_rejects_naive_inflation(ground_truth, naive_cohort_size_per_month):
    """total_cohort_customers must not land near the naive figure obtained by
    grouping raw signup rows without resolving reactivations/amendments to
    a customer's earliest-ever signup (~14% higher than the correct total)."""
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    got = s["total_cohort_customers"]
    exp = ground_truth["total_cohort_customers"]
    naive_total = sum(naive_cohort_size_per_month.values())
    rel_err_from_naive = abs(got - naive_total) / naive_total
    assert rel_err_from_naive > 0.03, \
        (f"total_cohort_customers ({got}) is suspiciously close to the naive, "
         f"inflated total ({naive_total}); every customer must be resolved to "
         f"their earliest-ever, amendment-resolved signup before counting. "
         f"Correct total is {exp}.")


# ── Hard test 2: retention (domain-category gap + ingestion-lag cutoff) ──────

def test_case_10_retention_rate_within_tolerance(ground_truth):
    """Each cohort month's retention_rate_month_3 must be within +/-0.03 of
    ground truth.

    Counting any activity_logs row (including system_generated rows) as
    engagement, or ignoring the created_at cutoff, overstates every single
    cohort's rate by 0.09-0.22 -- well past this tolerance.
    """
    report = pd.read_csv(REPORT_PATH).set_index("cohort_month")
    gt = ground_truth["report"]
    for month in gt.index:
        assert month in report.index, f"Missing cohort_month '{month}' in report."
        exp = float(gt.loc[month, "retention_rate_month_3"])
        got = float(report.loc[month, "retention_rate_month_3"])
        assert abs(got - exp) <= 0.03, \
            f"retention_rate_month_3 for {month}: got {got}, expected {exp} (+/-0.03)."


def test_case_11_overall_retention_rejects_naive(ground_truth):
    """overall_retention_rate_month_3 must not land near the naive figure
    obtained by counting any activity_logs row (any event_category, no
    created_at cutoff) as engagement (~0.14 higher than the correct rate)."""
    signups = pd.read_csv(DATA_DIR / "signups.csv", parse_dates=["signup_date", "created_at"])
    activity = pd.read_csv(DATA_DIR / "activity_logs.csv", parse_dates=["event_date", "created_at"])

    current_idx = signups.groupby("signup_id")["created_at"].idxmax()
    resolved = signups.loc[current_idx].reset_index(drop=True)
    first_signup = resolved.groupby("customer_id")["signup_date"].min().reset_index()
    cohort = first_signup[
        (first_signup["signup_date"] >= WINDOW_START) & (first_signup["signup_date"] <= WINDOW_END)
    ].copy()
    cohort["cohort_period"] = cohort["signup_date"].dt.to_period("M")
    m3_period = cohort["cohort_period"] + 3
    cohort["m3_start"] = m3_period.dt.start_time
    cohort["m3_end"] = m3_period.dt.end_time

    merged = cohort.merge(activity[["customer_id", "event_date"]], on="customer_id", how="left")
    in_window = (merged["event_date"] >= merged["m3_start"]) & (merged["event_date"] <= merged["m3_end"])
    naive_retained = cohort["customer_id"].isin(set(merged.loc[in_window.fillna(False), "customer_id"])).sum()
    naive_rate = round(naive_retained / len(cohort), 4)

    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    got = s["overall_retention_rate_month_3"]
    exp = ground_truth["overall_retention_rate_month_3"]
    assert abs(got - naive_rate) > 0.03, \
        (f"overall_retention_rate_month_3 ({got}) is suspiciously close to the naive "
         f"rate ({naive_rate}); only event_category='user_action' rows logged "
         f"(created_at) by the report cutoff count as engagement. Correct rate is {exp}.")


# ── Medium test: best/worst cohort identifiers ────────────────────────────────

def test_case_12_best_worst_cohort(ground_truth):
    with open(SUMMARY_PATH) as f:
        s = json.load(f)
    assert s["best_performing_cohort"] == ground_truth["best_performing_cohort"], \
        (f"best_performing_cohort: got {s['best_performing_cohort']!r}, "
         f"expected {ground_truth['best_performing_cohort']!r}")
    assert s["worst_performing_cohort"] == ground_truth["worst_performing_cohort"], \
        (f"worst_performing_cohort: got {s['worst_performing_cohort']!r}, "
         f"expected {ground_truth['worst_performing_cohort']!r}")
