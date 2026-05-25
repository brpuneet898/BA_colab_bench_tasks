"""Verifier tests for churn-cohort-analysis.

Expected values are computed inline from the canonical data files so that
reviewers can verify correctness by reading this file alone.
"""
import json
import math
from itertools import combinations
from pathlib import Path

import pandas as pd
import pytest

VERIFIER_DIR = Path("/logs/verifier")
VARIABLES_PATH = VERIFIER_DIR / "notebook_variables.json"

# Robust data path resolution: container path first, then local dev fallbacks
for _candidate in [Path("/workspace/data"), Path("../environment/data"), Path("environment/data")]:
    if _candidate.exists():
        DATA_DIR = _candidate
        break
else:
    DATA_DIR = Path("/workspace/data")

REFERENCE_DATE = pd.Timestamp("2025-09-30")
ENGAGEMENT = {"feature_use", "page_view"}


@pytest.fixture(scope="module")
def expected():
    subs = pd.read_csv(DATA_DIR / "subscriptions.csv")
    act  = pd.read_csv(DATA_DIR / "activity.csv")
    subs["plan_start"] = pd.to_datetime(subs["plan_start"])
    subs["plan_end"]   = pd.to_datetime(subs["plan_end"], errors="coerce")
    act["event_date"]  = pd.to_datetime(act["event_date"])

    eng = act[act["event_type"].isin(ENGAGEMENT)]
    cohort_cutoff = REFERENCE_DATE - pd.Timedelta(days=60)

    engaged = set(eng["customer_id"].unique())
    tenured = set(subs.loc[subs["plan_start"] <= cohort_cutoff, "customer_id"])
    cohort  = engaged & tenured
    cohort_size = len(cohort)

    active_mask      = subs["plan_end"].isna() | (subs["plan_end"] > REFERENCE_DATE)
    active_customers = set(subs.loc[active_mask, "customer_id"])

    ended = subs[
        subs["customer_id"].isin(cohort - active_customers) &
        subs["plan_end"].notna() &
        (subs["plan_end"] <= REFERENCE_DATE)
    ].copy()
    merged    = ended.merge(eng, on="customer_id", how="left")
    in_period = merged[
        (merged["event_date"] >= merged["plan_start"]) &
        (merged["event_date"] < merged["plan_end"])
    ]
    last_ip = (
        in_period.groupby(["customer_id", "plan_start", "plan_end"], as_index=False)["event_date"]
        .max()
        .rename(columns={"event_date": "last_in_period_activity"})
    )
    df = ended.merge(last_ip, on=["customer_id", "plan_start", "plan_end"], how="left")
    df["inactivity_gap"]        = (df["plan_end"] - df["last_in_period_activity"]).dt.days
    df["subscription_duration"] = (df["plan_end"] - df["plan_start"]).dt.days

    long_tenure = df[df["last_in_period_activity"].notna() & (df["subscription_duration"] >= 90)]
    gaps = sorted(long_tenure["inactivity_gap"].astype(int).tolist())
    n, k = len(gaps), math.ceil(len(gaps) / 2)
    inactivity_threshold = float(gaps[k - 1])

    df["is_churned"] = df.apply(
        lambda r: pd.isna(r["last_in_period_activity"]) or r["inactivity_gap"] >= inactivity_threshold,
        axis=1,
    )
    churned_users = int(df.groupby("customer_id")["is_churned"].any().sum())
    churn_rate    = round(churned_users / cohort_size, 4)

    active_subs   = subs[active_mask].drop_duplicates("customer_id")
    sub_start_map = dict(zip(active_subs["customer_id"], active_subs["plan_start"]))
    last_eng      = eng.groupby("customer_id", as_index=False)["event_date"].max()
    last_map      = dict(zip(last_eng["customer_id"], last_eng["event_date"]))
    cutoff_7      = REFERENCE_DATE - pd.Timedelta(days=7)
    candidates    = [
        uid for uid in cohort
        if uid in active_customers
        and pd.notna(last_map.get(uid))
        and last_map[uid] <= cutoff_7
    ]
    labels     = {uid: {f"month:{sub_start_map[uid].to_period('M')}", f"dow:{last_map[uid].weekday()}"} for uid in candidates}
    stale_days = {uid: int((REFERENCE_DATE - last_map[uid]).days) for uid in candidates}
    target     = set().union(*labels.values()) if labels else set()

    high_risk_users: list[str] = []
    for size in range(len(candidates) + 1):
        choices = []
        for subset in combinations(sorted(candidates), size):
            covered = set().union(*(labels[uid] for uid in subset)) if subset else set()
            if covered == target:
                min_stale = min(stale_days[uid] for uid in subset) if subset else 0
                choices.append((-min_stale, list(subset)))
        if choices:
            high_risk_users = min(choices)[1]
            break

    return {
        "cohort_size":          cohort_size,
        "churned_users":        churned_users,
        "churn_rate":           churn_rate,
        "inactivity_threshold": inactivity_threshold,
        "high_risk_users":      sorted(high_risk_users),
    }


@pytest.fixture(scope="module")
def notebook_variables():
    if not VARIABLES_PATH.exists():
        pytest.skip("notebook_variables.json not found")
    with open(VARIABLES_PATH) as f:
        return json.load(f)


def test_variables_exist(notebook_variables, expected):
    for key in expected:
        assert key in notebook_variables, f"missing top-level variable: {key}"


def test_cohort_size(notebook_variables, expected):
    assert notebook_variables["cohort_size"] == expected["cohort_size"]


def test_churned_users(notebook_variables, expected):
    assert notebook_variables["churned_users"] == expected["churned_users"]


def test_churn_rate(notebook_variables, expected):
    assert abs(notebook_variables["churn_rate"] - expected["churn_rate"]) < 0.0001


def test_inactivity_threshold(notebook_variables, expected):
    assert abs(notebook_variables["inactivity_threshold"] - expected["inactivity_threshold"]) < 0.01


def test_high_risk_users(notebook_variables, expected):
    assert notebook_variables["high_risk_users"] == expected["high_risk_users"]
