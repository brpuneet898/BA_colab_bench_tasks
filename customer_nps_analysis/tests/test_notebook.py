import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

WORKSPACE = Path("/workspace")
DATA_DIR = WORKSPACE / "data"
VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")

CONVERGENCE = 1e-9
WINDOW_DAYS = 90
DECAY_DAYS = 30
MAX_SWEEPS = 100000


def _weighted_median(values, weights):
    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    total = sum(w for _, w in pairs)
    if total == 0:
        return float("nan")
    cum = 0.0
    half = total / 2.0
    for v, w in pairs:
        cum += w
        if cum >= half:
            return float(v)
    return float(pairs[-1][0])


def _segment_metrics(responses, customers, targets, segment, latest):
    cutoff = latest - pd.Timedelta(days=WINDOW_DAYS)
    seg_customers = customers[customers["segment"] == segment].copy()
    complete = seg_customers.dropna(subset=["age_band", "region"]).copy()
    keep_ids = set(complete["customer_id"])

    qualifying = responses[
        (responses["flag"] == "verified")
        & (responses["survey_type"] == "relationship")
        & (responses["response_date"] >= cutoff)
        & (responses["score"] != 7)
        & (responses["customer_id"].isin(keep_ids))
    ].copy()
    qualifying["delta_days"] = (latest - qualifying["response_date"]).dt.days
    qualifying["w_resp"] = qualifying["weight"] * np.exp(
        -qualifying["delta_days"] / DECAY_DAYS
    )

    distinct_dates = qualifying.groupby("customer_id")["response_date"].nunique()
    span_days = (
        qualifying.groupby("customer_id")["response_date"].max()
        - qualifying.groupby("customer_id")["response_date"].min()
    ).dt.days
    earliest = qualifying.groupby("customer_id")["response_date"].min()
    tenure_cutoff = latest - pd.Timedelta(days=30)
    stable_ids = set(
        distinct_dates[
            (distinct_dates >= 2)
            & (span_days >= 14)
            & (earliest <= tenure_cutoff)
        ].index
    )
    qualifying = qualifying[qualifying["customer_id"].isin(stable_ids)]

    rep_rows = []
    for cid, grp in qualifying.groupby("customer_id"):
        scores = grp["score"].tolist()
        wts = grp["w_resp"].tolist()
        rep = _weighted_median(scores, wts)
        if len(scores) >= 2 and all(int(s) == 6 for s in scores):
            rep = 8.0
        rep_rows.append((cid, rep))
    rep_df = pd.DataFrame(rep_rows, columns=["customer_id", "repr_score"])

    panel = complete.merge(rep_df, on="customer_id", how="inner").reset_index(drop=True)
    n = len(panel)
    if n == 0:
        return {
            "panel_size": 0,
            "nps": 0.0,
            "promoter_count": 0,
            "detractor_count": 0,
        }

    seg_targets = targets[targets["segment"] == segment]
    age_targets = (
        seg_targets[seg_targets["variable"] == "age_band"]
        .set_index("level")["target_proportion"]
        .to_dict()
    )
    region_targets = (
        seg_targets[seg_targets["variable"] == "region"]
        .set_index("level")["target_proportion"]
        .to_dict()
    )

    panel["w"] = 1.0
    for _ in range(MAX_SWEEPS):
        before = panel["w"].copy()
        for level, share in age_targets.items():
            mask = panel["age_band"] == level
            current = panel.loc[mask, "w"].sum()
            if current > 0:
                panel.loc[mask, "w"] *= (share * n) / current
        for level, share in region_targets.items():
            mask = panel["region"] == level
            current = panel.loc[mask, "w"].sum()
            if current > 0:
                panel.loc[mask, "w"] *= (share * n) / current
        if (panel["w"] - before).abs().max() < CONVERGENCE:
            break

    cap_mask = panel["w"] > 1.5
    if cap_mask.any():
        amount_shaved = float((panel.loc[cap_mask, "w"] - 1.5).sum())
        uncapped_sum = float(panel.loc[~cap_mask, "w"].sum())
        if uncapped_sum > 0:
            panel.loc[~cap_mask, "w"] *= (uncapped_sum + amount_shaved) / uncapped_sum
        panel.loc[cap_mask, "w"] = 1.5

    total_w = panel["w"].sum()
    promoter_w = panel.loc[panel["repr_score"] >= 9, "w"].sum()
    detractor_w = panel.loc[panel["repr_score"] <= 6, "w"].sum()
    promoter_count = int((panel["repr_score"] >= 9).sum())
    detractor_count = int((panel["repr_score"] <= 6).sum())
    if total_w == 0:
        return {
            "panel_size": n,
            "nps": 0.0,
            "promoter_count": promoter_count,
            "detractor_count": detractor_count,
        }
    return {
        "panel_size": n,
        "nps": round((promoter_w - detractor_w) / total_w * 100, 2),
        "promoter_count": promoter_count,
        "detractor_count": detractor_count,
    }


@pytest.fixture(scope="module")
def notebook_variables():
    assert VARIABLES_PATH.exists(), "notebook_variables.json not found"
    with open(VARIABLES_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def ground_truth():
    responses = pd.read_csv(DATA_DIR / "responses.csv")
    customers = pd.read_csv(DATA_DIR / "customers.csv")
    targets = pd.read_csv(DATA_DIR / "targets.csv")

    responses["response_date"] = pd.to_datetime(responses["response_date"])
    latest = responses["response_date"].max()

    return {
        "corp": _segment_metrics(responses, customers, targets, "corp", latest),
        "smb": _segment_metrics(responses, customers, targets, "smb", latest),
    }


def test_variables_exist(notebook_variables):
    for name in [
        "customer_nps_corp",
        "customer_nps_smb",
        "panel_size_corp",
        "panel_size_smb",
        "promoter_count_corp",
        "promoter_count_smb",
        "detractor_count_corp",
        "detractor_count_smb",
    ]:
        assert name in notebook_variables, f"missing variable: {name}"


def test_promoter_count_corp(notebook_variables, ground_truth):
    assert int(notebook_variables["promoter_count_corp"]) == ground_truth["corp"]["promoter_count"]


def test_promoter_count_smb(notebook_variables, ground_truth):
    assert int(notebook_variables["promoter_count_smb"]) == ground_truth["smb"]["promoter_count"]


def test_detractor_count_corp(notebook_variables, ground_truth):
    assert int(notebook_variables["detractor_count_corp"]) == ground_truth["corp"]["detractor_count"]


def test_detractor_count_smb(notebook_variables, ground_truth):
    assert int(notebook_variables["detractor_count_smb"]) == ground_truth["smb"]["detractor_count"]


def test_panel_size_corp(notebook_variables, ground_truth):
    assert int(notebook_variables["panel_size_corp"]) == ground_truth["corp"]["panel_size"]


def test_panel_size_smb(notebook_variables, ground_truth):
    assert int(notebook_variables["panel_size_smb"]) == ground_truth["smb"]["panel_size"]


def test_customer_nps_corp(notebook_variables, ground_truth):
    assert (
        abs(notebook_variables["customer_nps_corp"] - ground_truth["corp"]["nps"])
        < 0.01
    )


def test_customer_nps_smb(notebook_variables, ground_truth):
    assert (
        abs(notebook_variables["customer_nps_smb"] - ground_truth["smb"]["nps"])
        < 0.01
    )
