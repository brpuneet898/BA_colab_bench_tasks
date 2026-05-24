"""
Unit tests for the Creative CTR Attribution task (v2 — raw event data).

Hidden credit-assignment rule (from generate_data_ctr.py; agents do not see this):
    1. Stable scroll segment: avg_scroll_speed <= 50.0 px/s
    2. Per-creative stable_vis_ms = total overlap (ms) between the creative's
       visibility intervals and stable scroll segments on that pageview.
    3. Per-creative stable_avg_vp = weighted-mean viewport_pct in those overlaps.
    4. Eligible: refresh_count == 0  AND  is_preload == 0
    5. Qualified (billable): eligible + stable_vis_ms >= 300 + stable_avg_vp >= 50.0
    6. Attention score = stable_vis_ms * stable_avg_vp
    7. Credit -> qualified creative with max score; tie-break lowest creative_id.

Why the task is hard:
    The impression-level data does NOT contain stable-window features.
    The agent must figure out how to combine scroll_segments and
    visibility_intervals to derive the attention signal. Without this,
    naive heuristics on aggregate impression features achieve ~55-60%
    accuracy, well below the 95% test threshold.
"""

import json
import pandas as pd
import numpy as np
import pytest
from pathlib import Path

WORKSPACE = Path("/workspace")
DATA = WORKSPACE / "data"

TRAIN_PATH = DATA / "impressions_train.csv"
EVAL_IMP = DATA / "impressions_eval.csv"
EVAL_SCROLL = DATA / "scroll_segments_eval.csv"
EVAL_VIS = DATA / "visibility_intervals_eval.csv"

CREDITED_PATH = WORKSPACE / "credited_clicks_eval.csv"
CTR_PATH = WORKSPACE / "creative_ctr_summary.csv"
VARS_PATH = Path("/logs/verifier/notebook_variables.json")

SPEED_THRESH = 50.0
VIS_THRESH = 300
VP_THRESH = 50.0
ACCURACY_MIN = 0.95
CTR_TOL = 0.03


# ── ground-truth computation ────────────────────────────────────────────────


def _stable_metrics(pv_id, cid, vis_df, scroll_df):
    """Compute stable-window visibility for one creative on one pageview."""
    ivs = vis_df[(vis_df["pageview_id"] == pv_id) & (vis_df["creative_id"] == cid)]
    segs = scroll_df[
        (scroll_df["pageview_id"] == pv_id)
        & (scroll_df["avg_scroll_speed"] <= SPEED_THRESH)
    ]
    total_ms = 0
    w_vp = 0.0
    for _, iv in ivs.iterrows():
        for _, ss in segs.iterrows():
            lo = max(iv["interval_start_ms"], ss["segment_start_ms"])
            hi = min(iv["interval_end_ms"], ss["segment_end_ms"])
            o = max(0, hi - lo)
            if o > 0:
                total_ms += o
                w_vp += o * iv["avg_viewport_pct"]
    avg_vp = w_vp / total_ms if total_ms > 0 else 0.0
    return total_ms, avg_vp


def _ground_truth(imp_df, vis_df, scroll_df):
    """Apply the hidden rule to eval data and return credited clicks + CTR."""
    click_pvs = imp_df[imp_df["click_occurred"] == 1]

    credited = []
    for pv_id in click_pvs["pageview_id"].unique():
        pv = click_pvs[click_pvs["pageview_id"] == pv_id]
        elig = pv[(pv["refresh_count"] == 0) & (pv["is_preload"] == 0)]
        cands = []
        for _, r in elig.iterrows():
            sm, sv = _stable_metrics(pv_id, r["creative_id"], vis_df, scroll_df)
            if sm >= VIS_THRESH and sv >= VP_THRESH:
                cands.append((sm * sv, r["creative_id"]))
        if cands:
            cands.sort(key=lambda x: (-x[0], x[1]))
            credited.append({"pageview_id": pv_id, "credited_creative_id": cands[0][1]})

    credited_df = pd.DataFrame(credited)

    billable_rows = []
    for pv_id in imp_df["pageview_id"].unique():
        pv = imp_df[imp_df["pageview_id"] == pv_id]
        elig = pv[(pv["refresh_count"] == 0) & (pv["is_preload"] == 0)]
        for _, r in elig.iterrows():
            sm, sv = _stable_metrics(pv_id, r["creative_id"], vis_df, scroll_df)
            if sm >= VIS_THRESH and sv >= VP_THRESH:
                billable_rows.append({"creative_id": r["creative_id"]})

    billable_df = pd.DataFrame(billable_rows)
    all_cre = sorted(imp_df["creative_id"].unique())
    b_counts = billable_df.groupby("creative_id").size().reindex(all_cre, fill_value=0)
    c_counts = (
        credited_df.groupby("credited_creative_id").size().reindex(all_cre, fill_value=0)
    )
    summary = pd.DataFrame(
        {
            "creative_id": all_cre,
            "billable_impressions": b_counts.values,
            "credited_clicks": c_counts.values,
        }
    )
    summary["ctr"] = np.where(
        summary["billable_impressions"] > 0,
        summary["credited_clicks"] / summary["billable_impressions"],
        0.0,
    )
    total_billable = int(b_counts.sum())
    return credited_df, summary, total_billable


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def eval_data():
    """Load all eval data files."""
    assert EVAL_IMP.exists(), f"Missing {EVAL_IMP}"
    assert EVAL_SCROLL.exists(), f"Missing {EVAL_SCROLL}"
    assert EVAL_VIS.exists(), f"Missing {EVAL_VIS}"
    return (
        pd.read_csv(EVAL_IMP),
        pd.read_csv(EVAL_VIS),
        pd.read_csv(EVAL_SCROLL),
    )


@pytest.fixture(scope="module")
def ground_truth(eval_data):
    """Compute ground-truth credited clicks and CTR."""
    imp, vis, scr = eval_data
    return _ground_truth(imp, vis, scr)


@pytest.fixture(scope="module")
def notebook_vars():
    """Load notebook variables."""
    if not VARS_PATH.exists():
        pytest.skip("notebook_variables.json not found")
    with open(VARS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def sub_credited():
    """Load agent's credited-clicks output."""
    assert CREDITED_PATH.exists(), f"Missing {CREDITED_PATH}"
    return pd.read_csv(CREDITED_PATH)


@pytest.fixture(scope="module")
def sub_ctr():
    """Load agent's CTR summary output."""
    assert CTR_PATH.exists(), f"Missing {CTR_PATH}"
    return pd.read_csv(CTR_PATH)


# ── tests ────────────────────────────────────────────────────────────────────


def test_n_train_impressions(notebook_vars):
    """Verify the agent loaded training data correctly."""
    assert "n_train_impressions" in notebook_vars, (
        "Notebook must define n_train_impressions (int)"
    )
    expected = len(pd.read_csv(TRAIN_PATH))
    assert notebook_vars["n_train_impressions"] == expected, (
        f"Expected {expected}, got {notebook_vars['n_train_impressions']}"
    )


def test_credited_clicks_exists():
    """Verify the credited clicks file was created."""
    assert CREDITED_PATH.exists(), "credited_clicks_eval.csv is missing"


def test_credited_clicks_columns(sub_credited):
    """Verify credited clicks file has required columns."""
    for c in ["pageview_id", "credited_creative_id"]:
        assert c in sub_credited.columns, f"Missing column: {c}"


def test_credited_clicks_accuracy(sub_credited, ground_truth):
    """Compare agent's click attribution against deterministic ground truth."""
    gt, _, _ = ground_truth
    merged = pd.merge(gt, sub_credited, on="pageview_id", suffixes=("_true", "_pred"), how="inner")
    if len(merged) == 0:
        pytest.fail("No matching pageview_ids")
    acc = (merged["credited_creative_id_true"] == merged["credited_creative_id_pred"]).mean()
    assert acc >= ACCURACY_MIN, (
        f"Attribution accuracy {acc:.2%} below required {ACCURACY_MIN:.0%}"
    )


def test_ctr_summary_exists():
    """Verify CTR summary file was created."""
    assert CTR_PATH.exists(), "creative_ctr_summary.csv is missing"


def test_ctr_summary_columns(sub_ctr):
    """Verify CTR summary has required columns."""
    for c in ["creative_id", "billable_impressions", "credited_clicks", "ctr"]:
        assert c in sub_ctr.columns, f"Missing column: {c}"


def test_ctr_summary_values(sub_ctr, ground_truth):
    """Compare agent's CTR values against ground truth."""
    _, gt_sum, _ = ground_truth
    merged = pd.merge(
        gt_sum[["creative_id", "ctr"]], sub_ctr[["creative_id", "ctr"]],
        on="creative_id", suffixes=("_true", "_pred"), how="inner",
    )
    if len(merged) == 0:
        pytest.fail("No matching creative_ids")
    max_diff = (merged["ctr_true"] - merged["ctr_pred"]).abs().max()
    assert max_diff <= CTR_TOL, (
        f"Max CTR deviation {max_diff:.4f} exceeds tolerance {CTR_TOL}"
    )


def test_total_billable_impressions(notebook_vars, ground_truth):
    """Verify total billable impression count."""
    assert "total_billable_impressions_eval" in notebook_vars, (
        "Notebook must define total_billable_impressions_eval (int)"
    )
    _, _, gt_total = ground_truth
    assert notebook_vars["total_billable_impressions_eval"] == gt_total, (
        f"Expected {gt_total}, got {notebook_vars['total_billable_impressions_eval']}"
    )
