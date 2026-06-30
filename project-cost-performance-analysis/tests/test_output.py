"""
Tests for the Q1 2024 EVM Cost Performance Report task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — 0/100 WPs with post-reporting completion_date.
            work_packages.csv includes a completion_date column recording when
            each WP was formally closed. Eight work packages using the 0_100
            technique have completion_status="complete" (current state at data
            export) but completion_date AFTER 2024-03-31. EV for a 0_100 WP
            equals the full BAC only if the WP was completed BY the reporting
            date. A model evaluating completion_status=="complete" without
            checking completion_date <= 2024-03-31 overcounts EV for those WPs.

  Trap 2 — Open-ended baselines (NaT baseline_effective_to).
            Every currently active baseline has no expiry date —
            baseline_effective_to is blank (NaT). A pandas date comparison of
            the form `reporting_date <= NaT` returns False, silently excluding
            all 200 current baselines. The correct implementation treats NaT as
            "no expiry": the baseline remains valid indefinitely after its
            baseline_effective_from.

  Trap 3 — Dual-baseline renegotiation (CSV order ≠ effective-date precedence).
            Ten work packages have two baseline rows: the original lower-BAC
            baseline written first in the CSV, and the revised higher-BAC
            baseline written second. After correctly handling Trap 2, both rows
            match the reporting date filter. A model iterating in file order
            picks the lower BAC. The correct rule: where multiple baselines are
            valid, use the one with the most recently approved baseline_effective_from.

  Trap 4 — Progress revisions: multiple submissions per WP/period.
            Thirty work packages have two progress_entries rows for
            reporting_period="2024-03": the initial estimate (written FIRST in
            CSV, submitted_date 2024-03-28) and a manager's post-close revision
            (written SECOND, submitted_date 2024-04-05, different percent_complete).
            Two failure modes:
              (a) Fan-out: merging without deduplication produces 230 March rows
                  instead of 200, inflating portfolio EV silently.
              (b) Wrong dedup: drop_duplicates(keep="first") picks the initial
                  estimate rather than the revision (latest submitted_date).
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

REPORT_PATH  = WORKSPACE_DIR / "evm_report.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

REPORTING_DATE = pd.Timestamp("2024-03-31")

# ---------------------------------------------------------------------------
# Ground-truth helpers
# ---------------------------------------------------------------------------

def _get_applicable_baseline(baselines: pd.DataFrame) -> pd.DataFrame:
    """
    For each (project_id, work_package_id), return the baseline in effect on
    REPORTING_DATE with the most recently approved baseline_effective_from.

    NaT in baseline_effective_to means no expiry — treated as always valid.
    """
    bl = baselines.copy()
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])
    open_ended = bl["baseline_effective_to"].isna()
    bl["baseline_effective_to"]   = pd.to_datetime(bl["baseline_effective_to"])

    valid = bl[
        (bl["baseline_effective_from"] <= REPORTING_DATE) &
        (open_ended | (REPORTING_DATE <= bl["baseline_effective_to"]))
    ].copy()

    valid = valid.sort_values("baseline_effective_from", ascending=False)
    valid = valid.drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    return valid[["project_id", "work_package_id", "bac_usd"]]


def _compute_ac(actuals: pd.DataFrame) -> pd.DataFrame:
    """Sum cost_amount_usd per (project_id, work_package_id) where billing_period_date <= REPORTING_DATE."""
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    in_period = actuals[actuals["billing_period_date"] <= REPORTING_DATE]
    return (
        in_period
        .groupby(["project_id", "work_package_id"], as_index=False)["cost_amount_usd"]
        .sum()
        .rename(columns={"cost_amount_usd": "ac_usd"})
    )


def _get_march_progress(progress: pd.DataFrame) -> pd.DataFrame:
    """
    Return the most recently submitted percent_complete per WP for 2024-03.
    Revisions (submitted_date 2024-04-05) supersede initial estimates.
    """
    mar = progress[progress["reporting_period"] == "2024-03"].copy()
    mar["submitted_date"] = pd.to_datetime(mar["submitted_date"])
    mar = (
        mar
        .sort_values("submitted_date", ascending=False)
        .drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    )
    return mar[["project_id", "work_package_id", "percent_complete"]]


def _compute_ev(row) -> float:
    """EV per ev_technique; for 0_100 checks completion_date against REPORTING_DATE."""
    if row["ev_technique"] == "0_100":
        comp_date = pd.to_datetime(row["completion_date"])
        if row["completion_status"] == "complete" and pd.notna(comp_date) and comp_date <= REPORTING_DATE:
            return float(row["bac_usd"])
        return 0.0
    return (float(row["percent_complete"]) / 100.0) * float(row["bac_usd"])


def _build_expected(work_packages, baselines, actuals, progress, pv_schedule):
    bac_df  = _get_applicable_baseline(baselines)
    ac_df   = _compute_ac(actuals)
    prog_df = _get_march_progress(progress)
    mar_pv  = pv_schedule[pv_schedule["reporting_period"] == "2024-03"][
        ["project_id", "work_package_id", "cumulative_pv_usd"]
    ].rename(columns={"cumulative_pv_usd": "pv_usd"})

    df = (
        work_packages[["project_id", "work_package_id", "work_package_name",
                       "control_account_id", "ev_technique", "completion_status",
                       "completion_date"]]
        .merge(bac_df,   on=["project_id", "work_package_id"], how="left")
        .merge(ac_df,    on=["project_id", "work_package_id"], how="left")
        .merge(prog_df,  on=["project_id", "work_package_id"], how="left")
        .merge(mar_pv,   on=["project_id", "work_package_id"], how="left")
    )
    df["ac_usd"]           = df["ac_usd"].fillna(0.0)
    df["percent_complete"] = df["percent_complete"].fillna(0.0)
    df["pv_usd"]           = df["pv_usd"].fillna(0.0)
    df["completion_date"]  = pd.to_datetime(df["completion_date"])

    df["ev_usd"] = df.apply(_compute_ev, axis=1)
    df["cv_usd"] = df["ev_usd"] - df["ac_usd"]
    df["sv_usd"] = df["ev_usd"] - df["pv_usd"]
    df["cpi"]    = df.apply(
        lambda r: round(r["ev_usd"] / r["ac_usd"], 4) if r["ac_usd"] > 0 else None, axis=1)
    df["spi"]    = df.apply(
        lambda r: round(r["ev_usd"] / r["pv_usd"], 4) if r["pv_usd"] > 0 else None, axis=1)
    df["eac_usd"] = df.apply(
        lambda r: round(r["bac_usd"] / r["cpi"], 2)
        if (r["cpi"] is not None and r["cpi"] > 0) else round(r["bac_usd"], 2), axis=1)
    for col in ["bac_usd", "pv_usd", "ev_usd", "ac_usd", "cv_usd", "sv_usd"]:
        df[col] = df[col].round(2)
    df["etc_usd"] = (df["eac_usd"] - df["ac_usd"]).round(2)
    df["vac_usd"] = (df["bac_usd"] - df["eac_usd"]).round(2)

    return df.sort_values(["project_id", "work_package_id"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_data():
    work_packages = pd.read_csv(DATA_DIR / "work_packages.csv")
    baselines     = pd.read_csv(DATA_DIR / "baselines.csv")
    actuals       = pd.read_csv(DATA_DIR / "actuals.csv")
    progress      = pd.read_csv(DATA_DIR / "progress_entries.csv")
    pv_schedule   = pd.read_csv(DATA_DIR / "planned_value_schedule.csv")
    return work_packages, baselines, actuals, progress, pv_schedule


@pytest.fixture(scope="module")
def expected(raw_data):
    wp, bl, ac, pr, pv = raw_data
    return _build_expected(wp, bl, ac, pr, pv)


@pytest.fixture(scope="module")
def agent_report():
    return pd.read_csv(REPORT_PATH) if REPORT_PATH.exists() else None


@pytest.fixture(scope="module")
def agent_summary():
    if not SUMMARY_PATH.exists():
        return None
    with open(SUMMARY_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 01 — Input sentinels
# ---------------------------------------------------------------------------

def test_01_input_sentinels(raw_data):
    """Verify input files are canonical and all four trap signals are present."""
    wp, bl, ac, pr, pv = raw_data

    assert len(wp) == 200, f"work_packages.csv must have 200 rows, got {len(wp)}"
    assert wp["project_id"].nunique() == 5

    # Trap 1 signal: some 0_100 "complete" WPs have completion_date > reporting date
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])
    future_complete = wp[
        (wp["ev_technique"] == "0_100") &
        (wp["completion_status"] == "complete") &
        (wp["completion_date"] > REPORTING_DATE)
    ]
    assert len(future_complete) >= 6, (
        f"Expected at least 6 future-complete 0_100 WPs, got {len(future_complete)}"
    )

    # Trap 2 signal: active baselines have NaT effective_to
    nat_count = bl["baseline_effective_to"].isna().sum()
    assert nat_count == 200, (
        f"baselines.csv must have 200 NaT baseline_effective_to rows, got {nat_count}"
    )

    # Trap 3 signal: 10 WPs have dual baseline rows
    dual_count = (bl.groupby(["project_id", "work_package_id"]).size() > 1).sum()
    assert dual_count == 10, (
        f"Expected 10 dual-baseline WPs, got {dual_count}"
    )

    # Trap 4 signal: 30 WPs have a revised March progress entry (submitted_date in April)
    mar_pr = pr[pr["reporting_period"] == "2024-03"]
    dup_count = (mar_pr.groupby(["project_id", "work_package_id"]).size() > 1).sum()
    assert dup_count >= 25, (
        f"Expected at least 25 WPs with revised March progress entries, got {dup_count}"
    )
    april_revisions = mar_pr[pd.to_datetime(mar_pr["submitted_date"]) > REPORTING_DATE]
    assert len(april_revisions) >= 25, (
        f"Expected at least 25 post-close progress revisions, got {len(april_revisions)}"
    )


# ---------------------------------------------------------------------------
# Test 02 — Output structure
# ---------------------------------------------------------------------------

def test_02_output_structure(agent_report, agent_summary):
    """Both output files exist with correct schema and row count."""
    assert REPORT_PATH.exists(),  "evm_report.csv not found in /workspace"
    assert SUMMARY_PATH.exists(), "summary.json not found in /workspace"
    assert agent_report is not None

    required_cols = [
        "project_id", "work_package_id", "work_package_name", "control_account_id",
        "ev_technique", "completion_status", "percent_complete",
        "bac_usd", "pv_usd", "ev_usd", "ac_usd",
        "cv_usd", "sv_usd", "cpi", "spi", "eac_usd", "etc_usd", "vac_usd",
    ]
    for col in required_cols:
        assert col in agent_report.columns, f"Missing column in evm_report.csv: {col}"

    assert len(agent_report) == 200, (
        f"evm_report.csv must have 200 rows, got {len(agent_report)}"
    )

    required_keys = [
        "reporting_date", "total_bac_usd", "total_ev_usd", "total_ac_usd",
        "total_pv_usd", "portfolio_cpi", "portfolio_spi", "total_eac_usd",
        "overbudget_work_package_count", "behind_schedule_work_package_count",
    ]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — [Trap 1] Future-complete 0_100 WPs must have ev_usd = 0
# ---------------------------------------------------------------------------

def test_03_trap1_future_complete_ev_is_zero(agent_report, raw_data):
    """
    Trap 1 — 0_100 WPs with completion_date AFTER 2024-03-31 were not done
    at the reporting date. ev_usd must be 0, not bac_usd.

    A model checking completion_status=="complete" without comparing
    completion_date to the reporting date assigns full BAC as EV for these WPs.
    """
    wp, _, _, _, _ = raw_data
    wp = wp.copy()
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])

    future_complete = wp[
        (wp["ev_technique"] == "0_100") &
        (wp["completion_status"] == "complete") &
        (wp["completion_date"] > REPORTING_DATE)
    ]

    failures = []
    for _, row in future_complete.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            failures.append(f"({pid}, {wp_id}): not found in evm_report.csv")
            continue
        act_ev = float(act_rows["ev_usd"].iloc[0])
        if not math.isclose(act_ev, 0.0, abs_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): ev_usd = {act_ev:,.2f}, expected 0.00. "
                f"completion_date = {row['completion_date'].date()} is after 2024-03-31. "
                "For 0_100 technique, EV = BAC only if the WP was completed BY the reporting date — "
                "check completion_date, not just completion_status."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — [Trap 1] On-time 0_100 completions must have ev_usd = bac_usd
# ---------------------------------------------------------------------------

def test_04_trap1_timely_complete_ev_equals_bac(agent_report, expected, raw_data):
    """
    Trap 1 regression — 0_100 WPs with completion_date ON OR BEFORE 2024-03-31
    must have ev_usd = bac_usd. A model that applies the date check too
    aggressively (e.g., ev = 0 for all 0_100 WPs) fails here.
    """
    wp, _, _, _, _ = raw_data
    wp = wp.copy()
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])

    timely = wp[
        (wp["ev_technique"] == "0_100") &
        (wp["completion_status"] == "complete") &
        (wp["completion_date"] <= REPORTING_DATE)
    ].head(5)

    failures = []
    for _, row in timely.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        exp_ev = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ev_usd"].iloc[0]
        )
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            continue
        act_ev = float(act_rows["ev_usd"].iloc[0])
        if not math.isclose(act_ev, exp_ev, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): ev_usd = {act_ev:,.2f}, expected {exp_ev:,.2f}. "
                f"completion_date = {row['completion_date'].date()} is on or before 2024-03-31 — "
                "ev_usd should equal bac_usd."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — [Trap 2] Portfolio EV must not collapse to near zero
# ---------------------------------------------------------------------------

def test_05_trap2_portfolio_ev_not_zero(agent_summary, expected):
    """
    Trap 2 — If NaT baseline_effective_to rows are silently excluded by a
    pandas date-range filter, all 200 WPs have no matched BAC and EV collapses.
    """
    exp_total_ev = round(float(expected["ev_usd"].sum()), 2)
    act_total_ev = float(agent_summary["total_ev_usd"])

    assert act_total_ev > exp_total_ev * 0.10, (
        f"total_ev_usd {act_total_ev:,.2f} is implausibly low (expected ~{exp_total_ev:,.2f}). "
        "NaT in baseline_effective_to must be treated as 'no expiry', not excluded. "
        "The pandas comparison `reporting_date <= NaT` returns False — handle NaT explicitly."
    )
    assert math.isclose(act_total_ev, exp_total_ev, rel_tol=0.05), (
        f"total_ev_usd {act_total_ev:,.2f} != expected {exp_total_ev:,.2f}"
    )


# ---------------------------------------------------------------------------
# Test 06 — [Trap 2] BAC accuracy for single-baseline work packages
# ---------------------------------------------------------------------------

def test_06_trap2_bac_single_baseline_wps(agent_report, expected, raw_data):
    """
    Trap 2 — Single-baseline WPs have NaT baseline_effective_to.
    If silently excluded, bac_usd is zero or missing.
    """
    _, bl, _, _, _ = raw_data
    dual_keys = set(
        zip(*[bl.groupby(["project_id", "work_package_id"]).filter(lambda g: len(g) > 1)
              [col].tolist() for col in ["project_id", "work_package_id"]])
    )
    single_exp = expected[
        ~expected.apply(
            lambda r: (r["project_id"], r["work_package_id"]) in dual_keys, axis=1)
    ].head(5)

    failures = []
    for _, row in single_exp.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        exp_bac    = float(row["bac_usd"])
        act_rows   = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            failures.append(f"({pid}, {wp_id}): not found in evm_report.csv")
            continue
        act_bac = float(act_rows["bac_usd"].iloc[0])
        if act_bac <= 0 or pd.isna(act_bac):
            failures.append(
                f"({pid}, {wp_id}): bac_usd = {act_bac}, expected {exp_bac:,.2f}. "
                "Open-ended baselines (NaT baseline_effective_to) must not be excluded."
            )
        elif not math.isclose(act_bac, exp_bac, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): bac_usd {act_bac:,.2f} != expected {exp_bac:,.2f}"
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 07 — [Trap 3] BAC for dual-baseline work packages uses revised baseline
# ---------------------------------------------------------------------------

def test_07_trap3_bac_dual_baseline_wps(agent_report, expected, raw_data):
    """
    Trap 3 — 10 WPs have two baseline rows. The original (lower-BAC) row is
    written first in baselines.csv. A model iterating in CSV order without
    sorting by baseline_effective_from picks the lower BAC.
    """
    _, bl, _, _, _ = raw_data
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])
    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    failures = []
    for _, key in dual_wps.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_bac = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["bac_usd"].iloc[0]
        )
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            failures.append(f"({pid}, {wp_id}): not found in evm_report.csv")
            continue
        act_bac = float(act_rows["bac_usd"].iloc[0])
        if not math.isclose(act_bac, exp_bac, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): bac_usd {act_bac:,.2f} != expected {exp_bac:,.2f}. "
                "Where multiple baselines are valid, use the one with the most recently "
                "approved baseline_effective_from — the revised baseline supersedes the original."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — [Trap 3] CPI and EAC cascade for dual-baseline WPs
# ---------------------------------------------------------------------------

def test_08_trap3_cpi_eac_cascade(agent_report, expected, raw_data):
    """
    Trap 3 — Wrong (lower) BAC cascades into EV → wrong CPI and EAC.
    Spot-checks the 5 dual-baseline WPs with the largest BAC revision.
    """
    _, bl, _, _, _ = raw_data
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])
    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    exp_sub = expected.merge(dual_wps, on=["project_id", "work_package_id"])
    spot    = exp_sub.nlargest(5, "bac_usd")[["project_id", "work_package_id"]].values.tolist()

    failures = []
    for pid, wp_id in spot:
        exp_row  = expected[
            (expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)
        ].iloc[0]
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            continue
        act_row = act_rows.iloc[0]

        exp_eac = float(exp_row["eac_usd"])
        act_eac = float(act_row["eac_usd"])
        if not math.isclose(act_eac, exp_eac, rel_tol=0.05):
            failures.append(
                f"({pid}, {wp_id}): eac_usd {act_eac:,.2f} != expected {exp_eac:,.2f}. "
                "Wrong BAC (from original baseline) cascades into EAC = BAC / CPI."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 09 — [Trap 4] EV for revised WPs reflects the post-close revision
# ---------------------------------------------------------------------------

def test_09_trap4_ev_uses_revision_not_initial(agent_report, expected, raw_data):
    """
    Trap 4 — 30 WPs have two March progress entries: initial (CSV-first) and
    revision (CSV-second, submitted_date 2024-04-05). The correct EV uses the
    revision's percent_complete, not the initial estimate.

    A model that does drop_duplicates(keep='first') gets the initial estimate.
    """
    _, _, _, progress, _ = raw_data
    mar = progress[progress["reporting_period"] == "2024-03"].copy()
    mar["submitted_date"] = pd.to_datetime(mar["submitted_date"])

    # Find WPs with a post-close revision (April submitted_date)
    revised_wps = mar[mar["submitted_date"] > REPORTING_DATE][
        ["project_id", "work_package_id"]
    ].drop_duplicates()

    spot = revised_wps.head(10)
    failures = []
    for _, key in spot.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_ev = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ev_usd"].iloc[0]
        )
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            failures.append(f"({pid}, {wp_id}): not found in evm_report.csv")
            continue
        act_ev = float(act_rows["ev_usd"].iloc[0])
        if not math.isclose(act_ev, exp_ev, rel_tol=0.03):
            # Show initial vs revision to help diagnose
            wps_entries = mar[
                (mar["project_id"] == pid) & (mar["work_package_id"] == wp_id)
            ].sort_values("submitted_date")
            initial_pct  = float(wps_entries.iloc[0]["percent_complete"])
            revision_pct = float(wps_entries.iloc[-1]["percent_complete"])
            failures.append(
                f"({pid}, {wp_id}): ev_usd {act_ev:,.2f} != expected {exp_ev:,.2f}. "
                f"Initial submission: {initial_pct}%, "
                f"Post-close revision: {revision_pct}% (submitted 2024-04-05). "
                "Sort progress entries by submitted_date and use the most recent."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 10 — [Trap 4] Portfolio total_ev_usd must not be fan-out inflated
# ---------------------------------------------------------------------------

def test_10_trap4_portfolio_ev_not_fanout(agent_summary, expected, raw_data):
    """
    Trap 4 — Merging progress_entries without deduplication creates 230 March
    rows instead of 200, silently inflating portfolio EV by ~15%.

    Verifies total_ev_usd and portfolio_cpi in summary.json are within
    tolerance of the correct ground truth.
    """
    _, _, _, progress, _ = raw_data
    mar = progress[progress["reporting_period"] == "2024-03"]
    n_march_rows = len(mar)
    assert n_march_rows == 230, (  # 200 original + 30 revisions
        f"Expected 230 March progress rows (200 original + 30 revisions), got {n_march_rows}. "
        "This sentinel verifies the trap data is intact."
    )

    exp_total_ev = round(float(expected["ev_usd"].sum()), 2)
    act_total_ev = float(agent_summary["total_ev_usd"])

    # A fan-out inflates EV by roughly the fraction of duplicated rows
    assert math.isclose(act_total_ev, exp_total_ev, rel_tol=0.04), (
        f"total_ev_usd {act_total_ev:,.2f} != expected {exp_total_ev:,.2f} "
        f"(diff {act_total_ev - exp_total_ev:+,.2f}). "
        "Merging progress_entries without deduplication creates fan-out — "
        "deduplicate on (project_id, work_package_id) keeping the latest submitted_date."
    )

    exp_ac   = round(float(expected["ac_usd"].sum()), 2)
    exp_cpi  = round(exp_total_ev / exp_ac, 4) if exp_ac > 0 else 0.0
    act_cpi  = float(agent_summary["portfolio_cpi"])
    assert math.isclose(act_cpi, exp_cpi, abs_tol=0.05), (
        f"portfolio_cpi {act_cpi:.4f} != expected {exp_cpi:.4f}. "
        "Errors in EV (Traps 1, 4) or AC (Trap 2) both affect this metric."
    )
