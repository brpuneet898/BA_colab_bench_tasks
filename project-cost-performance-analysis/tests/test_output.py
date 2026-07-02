"""
Tests for the Q1 2024 EVM Cost Performance Report task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — Progress revision: preliminary vs confirmed report  [PHANTOM DUPLICATE]
            15 WPs have two progress_entries rows for "2024-03":
            preliminary (lower percent_complete, submitted 5 days earlier, FIRST in CSV)
            and confirmed (actual percent_complete, SECOND in CSV).
            A naive merge gives 215 rows for 200 WPs; drop_duplicates(keep='first')
            silently picks the preliminary value.  Correct: sort by submitted_date
            descending before deduplicating on (project_id, work_package_id).

  Trap 2 — Dual PV schedule for re-baselined WPs (CSV order != schedule precedence).
            10 dual-baseline WPs have two planned value schedule rows per month:
            original schedule (lower PV, FIRST in CSV) and revised schedule
            (higher PV, SECOND in CSV).

  Trap 3 — Dual-baseline renegotiation (CSV order != effective-date precedence).
            10 WPs have two approved baseline rows; original lower-BAC written FIRST.

  Trap 4 — Commitment exclusion from Actual Cost  [DOMAIN REASONING]
            actuals.csv carries transaction_type: "actual" rows are incurred costs
            (include in AC); "commitment" rows are purchase orders not yet invoiced
            (exclude from AC).  A model summing all cost_amount_usd without filtering
            transaction_type will overstate AC, distorting CPI and EAC portfolio-wide.
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

def _get_applicable_baseline(baselines):
    bl = baselines.copy()
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])
    open_ended = bl["baseline_effective_to"].isna()
    bl["baseline_effective_to"] = pd.to_datetime(bl["baseline_effective_to"])
    valid = bl[
        (bl["baseline_effective_from"] <= REPORTING_DATE) &
        (open_ended | (REPORTING_DATE <= bl["baseline_effective_to"]))
    ].copy()
    valid = valid.sort_values("baseline_effective_from", ascending=False)
    valid = valid.drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    return valid[["project_id", "work_package_id", "bac_usd"]]


def _compute_ac(actuals):
    ac = actuals.copy()
    ac["billing_period_date"] = pd.to_datetime(ac["billing_period_date"])
    in_period = ac[
        (ac["billing_period_date"] <= REPORTING_DATE) &
        (ac["transaction_type"] != "commitment")
    ]
    return (
        in_period
        .groupby(["project_id", "work_package_id"], as_index=False)["cost_amount_usd"]
        .sum()
        .rename(columns={"cost_amount_usd": "ac_usd"})
    )


def _get_march_progress(progress):
    return (
        progress[progress["reporting_period"] == "2024-03"]
        .sort_values("submitted_date", ascending=False)
        .drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
        [["project_id", "work_package_id", "percent_complete"]]
    )


def _compute_ev(row):
    if row["ev_technique"] == "0_100":
        comp_date = pd.to_datetime(row["completion_date"])
        if pd.notna(comp_date) and comp_date <= REPORTING_DATE:
            return float(row["bac_usd"])
        return 0.0
    return (float(row["percent_complete"]) / 100.0) * float(row["bac_usd"])


def _get_march_pv(pv_schedule):
    pv = pv_schedule.copy()
    pv["schedule_effective_from"] = pd.to_datetime(pv["schedule_effective_from"])
    return (
        pv[pv["reporting_period"] == "2024-03"]
        .sort_values("schedule_effective_from", ascending=False)
        .drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
        [["project_id", "work_package_id", "cumulative_pv_usd"]]
        .rename(columns={"cumulative_pv_usd": "pv_usd"})
    )


def _build_expected(work_packages, baselines, actuals, progress, pv_schedule):
    bac_df  = _get_applicable_baseline(baselines)
    ac_df   = _compute_ac(actuals)
    prog_df = _get_march_progress(progress)
    mar_pv  = _get_march_pv(pv_schedule)

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
    df["cpi"] = df.apply(
        lambda r: round(r["ev_usd"] / r["ac_usd"], 4) if r["ac_usd"] > 0 else None, axis=1)
    df["spi"] = df.apply(
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
    """Verify canonical inputs and all four trap signals are present."""
    wp, bl, ac, pr, pv = raw_data
    assert len(wp) == 200
    assert wp["project_id"].nunique() == 5

    nat_count = bl["baseline_effective_to"].isna().sum()
    assert nat_count == 200, f"Expected 200 NaT baseline_effective_to, got {nat_count}"

    dual_bl = (bl.groupby(["project_id", "work_package_id"]).size() > 1).sum()
    assert dual_bl == 10, f"Trap 3: expected 10 dual-baseline WPs, got {dual_bl}"

    dual_pv = (
        pv.groupby(["project_id", "work_package_id", "reporting_period"]).size() > 1
    ).sum()
    assert dual_pv >= 10, (
        f"Trap 2: expected >=10 WP-period combos with dual PV schedules, got {dual_pv}"
    )

    dual_prog = (
        pr[pr["reporting_period"] == "2024-03"]
        .groupby(["project_id", "work_package_id"]).size().gt(1).sum()
    )
    assert dual_prog >= 10, (
        f"Trap 1: expected >=10 WPs with dual March progress entries, got {dual_prog}"
    )

    commitment_count = (ac["transaction_type"] == "commitment").sum()
    assert commitment_count >= 500, (
        f"Trap 4: expected >=500 commitment rows in actuals, got {commitment_count}"
    )


# ---------------------------------------------------------------------------
# Test 02 — Output structure
# ---------------------------------------------------------------------------

def test_02_output_structure(agent_report, agent_summary):
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
        assert col in agent_report.columns, f"Missing column: {col}"
    assert len(agent_report) == 200

    required_keys = [
        "reporting_date", "total_bac_usd", "total_ev_usd", "total_ac_usd",
        "total_pv_usd", "portfolio_cpi", "portfolio_spi", "total_eac_usd",
        "overbudget_work_package_count", "behind_schedule_work_package_count",
    ]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — [Trap 1] Confirmed percent_complete used, not preliminary
# ---------------------------------------------------------------------------

def test_03_trap1_confirmed_progress_used(agent_report, expected, raw_data):
    """
    Trap 1 — 15 WPs have two progress entries for "2024-03": a preliminary
    estimate (lower percent_complete, written FIRST in CSV, submitted_date 5
    days earlier) and the confirmed final report (written SECOND).  A naive
    merge gives 215 rows; drop_duplicates(keep='first') silently picks the
    preliminary value.  Correct: sort by submitted_date descending before
    deduplicating so the confirmed (most recently submitted) value is used.
    """
    _, _, _, progress, _ = raw_data
    mar = progress[progress["reporting_period"] == "2024-03"]
    dual_mask = mar.groupby(["project_id", "work_package_id"])["entry_id"].transform("count") > 1
    dual_wps  = mar[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    failures = []
    for _, key in dual_wps.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]

        exp_pct = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["percent_complete"].iloc[0]
        )
        prelim_rows = mar[
            (mar["project_id"] == pid) & (mar["work_package_id"] == wp_id) &
            (mar["report_type"] == "preliminary")
        ]
        prelim_pct = float(prelim_rows["percent_complete"].iloc[0]) if not prelim_rows.empty else None

        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found in agent report")
            continue

        act_pct = float(act["percent_complete"].iloc[0])
        if prelim_pct is not None and math.isclose(act_pct, prelim_pct, abs_tol=0.5):
            failures.append(
                f"({pid}, {wp_id}): percent_complete={act_pct} matches preliminary "
                f"({prelim_pct}); expected confirmed value {exp_pct}. "
                "Sort by submitted_date descending before drop_duplicates."
            )
        elif not math.isclose(act_pct, exp_pct, abs_tol=0.5):
            failures.append(
                f"({pid}, {wp_id}): percent_complete={act_pct}, expected {exp_pct}."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — [Trap 1] Wrong progress cascades into EV
# ---------------------------------------------------------------------------

def test_04_trap1_ev_uses_confirmed_progress(agent_report, expected, raw_data):
    """
    Trap 1 — Using the preliminary (lower) percent_complete understates EV for
    affected WPs.  Verify ev_usd matches the oracle for all dual-progress WPs.
    """
    _, _, _, progress, _ = raw_data
    mar = progress[progress["reporting_period"] == "2024-03"]
    dual_mask = mar.groupby(["project_id", "work_package_id"])["entry_id"].transform("count") > 1
    dual_wps  = mar[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    failures = []
    for _, key in dual_wps.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_ev = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ev_usd"].iloc[0]
        )
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            continue
        act_ev = float(act["ev_usd"].iloc[0])
        if not math.isclose(act_ev, exp_ev, rel_tol=0.03):
            failures.append(
                f"({pid}, {wp_id}): ev_usd={act_ev:,.2f}, expected {exp_ev:,.2f}. "
                "Verify confirmed percent_complete is used."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — [Trap 2/3] Portfolio EV must not collapse to near zero
# ---------------------------------------------------------------------------

def test_05_portfolio_ev_not_zero(agent_summary, expected):
    """Baseline NaT effective_to must be treated as no expiry (not excluded)."""
    exp_ev = round(float(expected["ev_usd"].sum()), 2)
    act_ev = float(agent_summary["total_ev_usd"])
    assert act_ev > exp_ev * 0.10, (
        f"total_ev_usd {act_ev:,.2f} implausibly low (~{exp_ev:,.2f} expected). "
        "pandas `reporting_date <= NaT` returns False — handle NaT explicitly."
    )
    assert math.isclose(act_ev, exp_ev, rel_tol=0.05), (
        f"total_ev_usd {act_ev:,.2f} != expected {exp_ev:,.2f}"
    )


# ---------------------------------------------------------------------------
# Test 06 — [Trap 2] Dual-baseline WPs must use the revised PV schedule
# ---------------------------------------------------------------------------

def test_06_trap2_pv_dual_schedule_revised_used(agent_report, expected, raw_data):
    """
    Trap 2 — The 10 re-baselined WPs have two PV schedule rows per reporting
    period: original schedule (lower cumulative_pv_usd, FIRST in CSV) and revised
    schedule (higher cumulative_pv_usd, SECOND in CSV).  A naive merge creates
    phantom duplicate rows; drop_duplicates(keep='first') silently picks the
    original (wrong) PV.  Correct: sort by schedule_effective_from descending,
    then dedup on (project_id, work_package_id), keep='first'.
    """
    _, bl, _, _, pv = raw_data
    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    failures = []
    for _, key in dual_wps.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_pv = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["pv_usd"].iloc[0]
        )
        orig_rows = pv[
            (pv["project_id"] == pid) & (pv["work_package_id"] == wp_id) &
            (pv["reporting_period"] == "2024-03") & (pv["schedule_revision"] == "original")
        ]
        orig_pv = float(orig_rows["cumulative_pv_usd"].iloc[0]) if not orig_rows.empty else None

        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_pv = float(act["pv_usd"].iloc[0])
        if orig_pv is not None and math.isclose(act_pv, orig_pv, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): pv_usd={act_pv:,.2f} matches original schedule "
                f"({orig_pv:,.2f}); expected revised schedule PV {exp_pv:,.2f}. "
                "Sort by schedule_effective_from descending before deduplicating."
            )
        elif not math.isclose(act_pv, exp_pv, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): pv_usd={act_pv:,.2f}, expected {exp_pv:,.2f}."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 07 — [Trap 3] Dual-baseline WPs use the revised (higher) BAC
# ---------------------------------------------------------------------------

def test_07_trap3_bac_dual_baseline_wps(agent_report, expected, raw_data):
    """Trap 3 — Original lower-BAC row is written first; revised row must win."""
    _, bl, _, _, _ = raw_data
    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    failures = []
    for _, key in dual_wps.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_bac = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["bac_usd"].iloc[0]
        )
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_bac = float(act["bac_usd"].iloc[0])
        if not math.isclose(act_bac, exp_bac, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): bac_usd {act_bac:,.2f} != expected {exp_bac:,.2f}."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — [Trap 3] Wrong BAC cascades into EAC
# ---------------------------------------------------------------------------

def test_08_trap3_eac_cascade(agent_report, expected, raw_data):
    """Trap 3 — Wrong (lower) BAC cascades into wrong EV -> wrong CPI and EAC."""
    _, bl, _, _, _ = raw_data
    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()
    exp_sub   = expected.merge(dual_wps, on=["project_id", "work_package_id"])
    spot      = exp_sub.nlargest(5, "bac_usd")[["project_id", "work_package_id"]].values.tolist()

    failures = []
    for pid, wp_id in spot:
        exp_eac = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["eac_usd"].iloc[0]
        )
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            continue
        act_eac = float(act["eac_usd"].iloc[0])
        if not math.isclose(act_eac, exp_eac, rel_tol=0.05):
            failures.append(
                f"({pid}, {wp_id}): eac_usd {act_eac:,.2f} != expected {exp_eac:,.2f}."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 09 — [Trap 4] AC excludes commitment rows
# ---------------------------------------------------------------------------

def test_09_trap4_ac_excludes_commitments(agent_report, expected, raw_data):
    """
    Trap 4 — actuals.csv carries transaction_type: "actual" rows are incurred
    costs (include in AC); "commitment" rows are PO obligations not yet invoiced
    (exclude from AC).  For WPs where commitment rows carry a non-trivial amount,
    the agent's ac_usd must match the oracle (which excludes commitments), not
    the inflated total that includes them.
    """
    _, _, actuals, _, _ = raw_data
    ac = actuals.copy()
    ac["billing_period_date"] = pd.to_datetime(ac["billing_period_date"])
    in_period = ac[ac["billing_period_date"] <= REPORTING_DATE]

    commitment_by_wp = (
        in_period[in_period["transaction_type"] == "commitment"]
        .groupby(["project_id", "work_package_id"])["cost_amount_usd"]
        .sum()
        .reset_index()
        .rename(columns={"cost_amount_usd": "commitment_usd"})
    )
    # Focus on WPs where the commitment load is material (>$5k)
    material = commitment_by_wp[commitment_by_wp["commitment_usd"] > 5_000].head(15)

    failures = []
    for _, key in material.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_ac  = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ac_usd"].iloc[0]
        )
        inflated_ac = exp_ac + float(key["commitment_usd"])

        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_ac = float(act["ac_usd"].iloc[0])
        if math.isclose(act_ac, inflated_ac, rel_tol=0.02):
            failures.append(
                f"({pid}, {wp_id}): ac_usd={act_ac:,.2f} matches inflated total "
                f"(incurred {exp_ac:,.2f} + commitments {key['commitment_usd']:,.2f}). "
                "Exclude transaction_type='commitment' rows from AC."
            )
        elif not math.isclose(act_ac, exp_ac, rel_tol=0.03):
            failures.append(
                f"({pid}, {wp_id}): ac_usd={act_ac:,.2f}, expected {exp_ac:,.2f}."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 10 — [Trap 4] Portfolio CPI distorted by included commitments
# ---------------------------------------------------------------------------

def test_10_trap4_portfolio_cpi_not_distorted(agent_summary, expected, raw_data):
    """
    Trap 4 — Including commitment rows inflates total_ac_usd portfolio-wide,
    understating portfolio CPI.
    """
    _, _, actuals, _, _ = raw_data
    ac = actuals.copy()
    ac["billing_period_date"] = pd.to_datetime(ac["billing_period_date"])
    in_period = ac[ac["billing_period_date"] <= REPORTING_DATE]

    total_commitments = float(
        in_period[in_period["transaction_type"] == "commitment"]["cost_amount_usd"].sum()
    )
    assert total_commitments > 500_000, (
        f"Total commitment amount {total_commitments:,.2f} too small — trap may be missing."
    )

    exp_ac  = round(float(expected["ac_usd"].sum()), 2)
    exp_ev  = round(float(expected["ev_usd"].sum()), 2)
    exp_cpi = round(exp_ev / exp_ac, 4) if exp_ac > 0 else 0.0

    act_ac  = float(agent_summary["total_ac_usd"])
    act_cpi = float(agent_summary["portfolio_cpi"])

    assert math.isclose(act_ac, exp_ac, rel_tol=0.03), (
        f"total_ac_usd {act_ac:,.2f} != expected {exp_ac:,.2f} "
        f"(portfolio commitments total {total_commitments:,.2f}). "
        "Exclude transaction_type='commitment' rows from AC."
    )
    assert math.isclose(act_cpi, exp_cpi, abs_tol=0.05), (
        f"portfolio_cpi {act_cpi:.4f} != expected {exp_cpi:.4f}."
    )
