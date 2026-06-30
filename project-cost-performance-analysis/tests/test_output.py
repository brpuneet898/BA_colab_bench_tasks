"""
Tests for the Q1 2024 EVM Cost Performance Report task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — Bidirectional completion_date vs. completion_status confusion.
            (a) 8 WPs: completion_status="complete" but completion_date AFTER
                2024-03-31. EV must be 0.
            (b) 5 WPs: completion_status="in_progress" but completion_date ON
                OR BEFORE 2024-03-31. EV must equal full BAC.
            A model checking completion_status instead of completion_date fails
            in both directions.

  Trap 2 — Open-ended baselines (NaT baseline_effective_to).
            pandas `reporting_date <= NaT` returns False, silently excluding
            all 200 active baselines. NaT must be treated as "no expiry".

  Trap 3 — Dual-baseline renegotiation (CSV order != effective-date precedence).
            10 WPs have two baseline rows; original lower-BAC written FIRST.
            Correct rule: most recently approved baseline_effective_from wins.

  Trap 4 — Cost reversals inflate AC when negatives are filtered.
            ~25 WPs have negative cost_amount_usd rows (credit memos).
            groupby().sum() correctly nets them; filtering cost > 0 overcounts AC.
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
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    in_period = actuals[actuals["billing_period_date"] <= REPORTING_DATE]
    return (
        in_period
        .groupby(["project_id", "work_package_id"], as_index=False)["cost_amount_usd"]
        .sum()
        .rename(columns={"cost_amount_usd": "ac_usd"})
    )


def _get_march_progress(progress):
    mar = progress[progress["reporting_period"] == "2024-03"]
    return mar[["project_id", "work_package_id", "percent_complete"]]


def _compute_ev(row):
    if row["ev_technique"] == "0_100":
        comp_date = pd.to_datetime(row["completion_date"])
        if pd.notna(comp_date) and comp_date <= REPORTING_DATE:
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

    wp = wp.copy()
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])

    future = wp[
        (wp["ev_technique"] == "0_100") & (wp["completion_status"] == "complete") &
        (wp["completion_date"] > REPORTING_DATE)
    ]
    assert len(future) >= 6, f"Trap 1a: expected >=6 future-complete 0_100 WPs, got {len(future)}"

    early = wp[
        (wp["ev_technique"] == "0_100") & (wp["completion_status"] == "in_progress") &
        (wp["completion_date"].notna()) & (wp["completion_date"] <= REPORTING_DATE)
    ]
    assert len(early) >= 3, f"Trap 1b: expected >=3 early-closed 0_100 WPs, got {len(early)}"

    nat_count = bl["baseline_effective_to"].isna().sum()
    assert nat_count == 200, f"Trap 2: expected 200 NaT baseline_effective_to, got {nat_count}"

    dual_count = (bl.groupby(["project_id", "work_package_id"]).size() > 1).sum()
    assert dual_count == 10, f"Trap 3: expected 10 dual-baseline WPs, got {dual_count}"

    neg_rows = (ac["cost_amount_usd"] < 0).sum()
    assert neg_rows >= 40, f"Trap 4: expected >=40 negative cost rows, got {neg_rows}"


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
# Test 03 — [Trap 1a] Future-complete 0_100 WPs must have ev_usd = 0
# ---------------------------------------------------------------------------

def test_03_trap1a_future_complete_ev_is_zero(agent_report, raw_data):
    """
    Trap 1a — 0_100 WPs marked "complete" with completion_date AFTER 2024-03-31
    were not done at the reporting date. ev_usd must be 0, not bac_usd.

    A model using completion_status=="complete" directly overcounts EV.
    """
    wp, _, _, _, _ = raw_data
    wp = wp.copy()
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])
    future_wps = wp[
        (wp["ev_technique"] == "0_100") & (wp["completion_status"] == "complete") &
        (wp["completion_date"] > REPORTING_DATE)
    ]
    failures = []
    for _, row in future_wps.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_ev = float(act["ev_usd"].iloc[0])
        if not math.isclose(act_ev, 0.0, abs_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): ev_usd={act_ev:,.2f}, expected 0.00. "
                f"completion_date={row['completion_date'].date()} is after 2024-03-31."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — [Trap 1b] Early-closed 0_100 WPs must have ev_usd = bac_usd
# ---------------------------------------------------------------------------

def test_04_trap1b_early_closed_ev_equals_bac(agent_report, expected, raw_data):
    """
    Trap 1b — 0_100 WPs with completion_status="in_progress" but completion_date
    on or before 2024-03-31 were formally closed in Q1. ev_usd must equal bac_usd.

    A model treating in_progress as ev=0 for 0_100 undercounts EV for these WPs.
    """
    wp, _, _, _, _ = raw_data
    wp = wp.copy()
    wp["completion_date"] = pd.to_datetime(wp["completion_date"])
    early_wps = wp[
        (wp["ev_technique"] == "0_100") & (wp["completion_status"] == "in_progress") &
        (wp["completion_date"].notna()) & (wp["completion_date"] <= REPORTING_DATE)
    ]
    failures = []
    for _, row in early_wps.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        exp_ev = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ev_usd"].iloc[0]
        )
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_ev = float(act["ev_usd"].iloc[0])
        if not math.isclose(act_ev, exp_ev, rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): ev_usd={act_ev:,.2f}, expected {exp_ev:,.2f} (=bac_usd). "
                f"completion_status='in_progress' but completion_date="
                f"{row['completion_date'].date()} <= 2024-03-31. "
                "Use completion_date, not completion_status, to determine 0_100 EV."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — [Trap 2] Portfolio EV must not collapse to near zero
# ---------------------------------------------------------------------------

def test_05_trap2_portfolio_ev_not_zero(agent_summary, expected):
    """Trap 2 — NaT baseline_effective_to must be treated as no expiry."""
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
# Test 06 — [Trap 2] BAC accuracy for single-baseline WPs
# ---------------------------------------------------------------------------

def test_06_trap2_bac_single_baseline_wps(agent_report, expected, raw_data):
    """Trap 2 — Single-baseline WPs have NaT effective_to; must not be excluded."""
    _, bl, _, _, _ = raw_data
    dual_keys = set(
        map(tuple,
            bl.groupby(["project_id", "work_package_id"])
              .filter(lambda g: len(g) > 1)[["project_id", "work_package_id"]]
              .drop_duplicates().values.tolist()
        )
    )
    single_exp = expected[
        ~expected.apply(
            lambda r: (r["project_id"], r["work_package_id"]) in dual_keys, axis=1)
    ].head(5)

    failures = []
    for _, row in single_exp.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_bac = float(act["bac_usd"].iloc[0])
        if act_bac <= 0 or pd.isna(act_bac):
            failures.append(f"({pid}, {wp_id}): bac_usd={act_bac} — baseline excluded.")
        elif not math.isclose(act_bac, float(row["bac_usd"]), rel_tol=0.01):
            failures.append(
                f"({pid}, {wp_id}): bac_usd {act_bac:,.2f} != expected {row['bac_usd']:,.2f}"
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
                f"({pid}, {wp_id}): bac_usd {act_bac:,.2f} != expected {exp_bac:,.2f}. "
                "Use most recently approved baseline_effective_from."
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
# Test 09 — [Trap 4] AC for reversal WPs must include negative transactions
# ---------------------------------------------------------------------------

def test_09_trap4_ac_includes_reversals(agent_report, expected, raw_data):
    """
    Trap 4 — ~25 WPs have negative cost_amount_usd rows (credit memos / adjustments).
    Net AC = gross charges + reversals. Filtering cost_amount_usd > 0 overcounts AC.
    """
    _, _, actuals, _, _ = raw_data
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    q1 = actuals[actuals["billing_period_date"] <= REPORTING_DATE]

    reversal_wps = (
        q1[q1["cost_amount_usd"] < 0][["project_id", "work_package_id"]]
        .drop_duplicates()
    )
    spot = reversal_wps.head(10)
    failures = []
    for _, key in spot.iterrows():
        pid, wp_id = key["project_id"], key["work_package_id"]
        exp_ac = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ac_usd"].iloc[0]
        )
        act = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act.empty:
            failures.append(f"({pid}, {wp_id}): not found")
            continue
        act_ac = float(act["ac_usd"].iloc[0])
        if not math.isclose(act_ac, exp_ac, rel_tol=0.03):
            gross = float(q1[
                (q1["project_id"] == pid) & (q1["work_package_id"] == wp_id) &
                (q1["cost_amount_usd"] > 0)
            ]["cost_amount_usd"].sum())
            failures.append(
                f"({pid}, {wp_id}): ac_usd={act_ac:,.2f}, expected {exp_ac:,.2f}. "
                f"Gross (positives only)={gross:,.2f}. "
                "Sum ALL cost_amount_usd — negative rows are reversals, not errors."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 10 — [Trap 4] Portfolio CPI distorted by filtered reversals
# ---------------------------------------------------------------------------

def test_10_trap4_portfolio_cpi_not_distorted(agent_summary, expected, raw_data):
    """
    Trap 4 — Filtering negatives inflates total AC portfolio-wide, lowering CPI.
    """
    _, _, actuals, _, _ = raw_data
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    q1 = actuals[actuals["billing_period_date"] <= REPORTING_DATE]

    neg_total = float(q1[q1["cost_amount_usd"] < 0]["cost_amount_usd"].sum())
    assert neg_total < -50_000, (
        f"Total reversal amount {neg_total:,.2f} too small — trap data may be missing."
    )

    exp_ac  = round(float(expected["ac_usd"].sum()), 2)
    exp_ev  = round(float(expected["ev_usd"].sum()), 2)
    exp_cpi = round(exp_ev / exp_ac, 4) if exp_ac > 0 else 0.0

    act_ac  = float(agent_summary["total_ac_usd"])
    act_cpi = float(agent_summary["portfolio_cpi"])

    assert math.isclose(act_ac, exp_ac, rel_tol=0.03), (
        f"total_ac_usd {act_ac:,.2f} != expected {exp_ac:,.2f} "
        f"(diff {act_ac - exp_ac:+,.2f}). "
        "Include all cost_amount_usd — reversals reduce net AC."
    )
    assert math.isclose(act_cpi, exp_cpi, abs_tol=0.05), (
        f"portfolio_cpi {act_cpi:.4f} != expected {exp_cpi:.4f}."
    )
