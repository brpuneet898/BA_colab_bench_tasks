"""
Tests for the Q1 2024 EVM Cost Performance Report task.

Ground truth is recomputed from the canonical data files baked into the image.

Headroom mechanisms tested:

  Trap 1 — (project_id, work_package_id) composite key.
            work_package_id values WP-001 through WP-040 repeat independently
            across all five projects. Joining actuals or baselines on
            work_package_id alone creates phantom cross-project matches,
            inflating AC and EV by up to 5× per work package.

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
            valid, use the one with the most recent baseline_effective_from.

  Trap 4 — Subcontractor billing lag (billing_period_date vs entry_date).
            ~40% of March subcontract cost rows have entry_date in April or May
            2024. AC must be based on billing_period_date (the accounting period
            of the cost), not entry_date (when the invoice was posted). A model
            filtering on entry_date <= 2024-03-31 silently drops those rows,
            understating Q1 AC for subcontract-heavy work packages.
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
# Ground-truth helpers  (replicate oracle logic exactly)
# ---------------------------------------------------------------------------

def _get_applicable_baseline(baselines: pd.DataFrame) -> pd.DataFrame:
    """
    For each (project_id, work_package_id), return the baseline row whose
    baseline_effective_from is the most recent date on or before REPORTING_DATE.

    NaT in baseline_effective_to means the baseline has no expiry and is always
    valid after its baseline_effective_from. The comparison
    `REPORTING_DATE <= NaT` evaluates to False in pandas, so NaT must be
    handled explicitly: treat it as "always in effect after effective_from".
    """
    bl = baselines.copy()
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])

    open_ended = bl["baseline_effective_to"].isna()
    bl["baseline_effective_to"] = pd.to_datetime(bl["baseline_effective_to"])

    valid = bl[
        (bl["baseline_effective_from"] <= REPORTING_DATE) &
        (open_ended | (REPORTING_DATE <= bl["baseline_effective_to"]))
    ].copy()

    # Where multiple rows are valid, keep the most recently approved (highest effective_from)
    valid = valid.sort_values("baseline_effective_from", ascending=False)
    valid = valid.drop_duplicates(subset=["project_id", "work_package_id"], keep="first")
    return valid[["project_id", "work_package_id", "bac_usd"]]


def _compute_ac(actuals: pd.DataFrame) -> pd.DataFrame:
    """
    Sum cost_amount_usd per (project_id, work_package_id) where
    billing_period_date <= REPORTING_DATE.
    billing_period_date is the accounting period of the cost — not entry_date.
    """
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    in_period = actuals[actuals["billing_period_date"] <= REPORTING_DATE]
    ac = (
        in_period
        .groupby(["project_id", "work_package_id"], as_index=False)["cost_amount_usd"]
        .sum()
        .rename(columns={"cost_amount_usd": "ac_usd"})
    )
    return ac


def _compute_ev(row) -> float:
    """EV based on ev_technique: percent_complete (linear) or 0_100."""
    if row["ev_technique"] == "0_100":
        return float(row["bac_usd"]) if row["completion_status"] == "complete" else 0.0
    else:
        return (float(row["percent_complete"]) / 100.0) * float(row["bac_usd"])


def _build_expected(work_packages, baselines, actuals, progress, pv_schedule):
    bac_df = _get_applicable_baseline(baselines)
    ac_df  = _compute_ac(actuals)

    mar_progress = progress[progress["reporting_period"] == "2024-03"][
        ["project_id", "work_package_id", "percent_complete"]
    ]
    mar_pv = pv_schedule[pv_schedule["reporting_period"] == "2024-03"][
        ["project_id", "work_package_id", "cumulative_pv_usd"]
    ].rename(columns={"cumulative_pv_usd": "pv_usd"})

    df = (
        work_packages[["project_id", "work_package_id", "work_package_name",
                       "control_account_id", "ev_technique", "completion_status"]]
        .merge(bac_df,        on=["project_id", "work_package_id"], how="left")
        .merge(ac_df,         on=["project_id", "work_package_id"], how="left")
        .merge(mar_progress,  on=["project_id", "work_package_id"], how="left")
        .merge(mar_pv,        on=["project_id", "work_package_id"], how="left")
    )
    df["ac_usd"]          = df["ac_usd"].fillna(0.0)
    df["percent_complete"] = df["percent_complete"].fillna(0.0)
    df["pv_usd"]          = df["pv_usd"].fillna(0.0)

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

    df = df.sort_values(["project_id", "work_package_id"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_data():
    work_packages = pd.read_csv(DATA_DIR / "work_packages.csv")
    baselines     = pd.read_csv(DATA_DIR / "baselines.csv",
                                parse_dates=["baseline_effective_from"])
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
# Test 01 — Input sentinel
# ---------------------------------------------------------------------------

def test_01_input_sentinels(raw_data):
    """Verify input files were not tampered with and all trap signals are present."""
    wp, bl, ac, pr, pv = raw_data

    assert len(wp) == 200, f"work_packages.csv must have 200 rows, got {len(wp)}"
    assert wp["project_id"].nunique() == 5, "Expected 5 projects"

    # Trap 1 signal: WP-IDs repeat across projects
    wp_id_project_counts = wp.groupby("work_package_id")["project_id"].nunique()
    repeated = (wp_id_project_counts > 1).sum()
    assert repeated == 40, (
        f"Expected all 40 WP-IDs to appear in multiple projects (composite key trap), "
        f"got {repeated}"
    )

    # Trap 2 signal: active baselines have NaT effective_to
    nat_count = bl["baseline_effective_to"].isna().sum()
    assert nat_count == 200, (
        f"baselines.csv must have exactly 200 rows with NaT baseline_effective_to "
        f"(open-ended active baselines), got {nat_count}"
    )

    # Trap 3 signal: 10 WPs have dual baseline rows
    dual_count = (bl.groupby(["project_id", "work_package_id"]).size() > 1).sum()
    assert dual_count == 10, (
        f"baselines.csv must have exactly 10 dual-baseline work packages, got {dual_count}"
    )

    # Trap 4 signal: March subcontract rows with April+ entry_date
    ac_parsed = ac.copy()
    ac_parsed["billing_period_date"] = pd.to_datetime(ac_parsed["billing_period_date"])
    ac_parsed["entry_date"]          = pd.to_datetime(ac_parsed["entry_date"])
    march_sub = ac_parsed[
        (ac_parsed["cost_type"] == "subcontract") &
        (ac_parsed["billing_period_date"] == pd.Timestamp("2024-03-01"))
    ]
    lagged = march_sub[march_sub["entry_date"] > pd.Timestamp("2024-03-31")]
    assert len(lagged) >= 100, (
        f"actuals.csv must contain at least 100 March subcontract rows with "
        f"entry_date in April+ (billing lag trap), got {len(lagged)}"
    )

    assert len(pr) == 600, f"progress_entries.csv must have 600 rows, got {len(pr)}"


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
        f"evm_report.csv must have 200 rows (one per work package), got {len(agent_report)}"
    )

    required_keys = [
        "reporting_date", "total_bac_usd", "total_ev_usd", "total_ac_usd",
        "total_pv_usd", "portfolio_cpi", "portfolio_spi", "total_eac_usd",
        "overbudget_work_package_count", "behind_schedule_work_package_count",
    ]
    for key in required_keys:
        assert key in agent_summary, f"Missing key in summary.json: {key}"


# ---------------------------------------------------------------------------
# Test 03 — [Trap 1] AC per project (phantom joins inflate AC)
# ---------------------------------------------------------------------------

def test_03_trap1_ac_per_project(agent_report, expected):
    """
    Trap 1 — Joining actuals on work_package_id alone creates phantom cross-project
    cost matches, inflating AC by up to 5× per work package.

    Spot-checks total ac_usd for the 3 projects with the most actuals rows.
    A model with the correct (project_id, work_package_id) join key will match
    ground truth; a model with a work_package_id-only join will be significantly
    over by a factor approaching 5.
    """
    exp_by_project = expected.groupby("project_id")["ac_usd"].sum()
    spot = exp_by_project.nlargest(3).index.tolist()

    failures = []
    for pid in spot:
        exp_ac = float(exp_by_project[pid])
        act_ac = float(
            agent_report[agent_report["project_id"] == pid]["ac_usd"].sum()
        )
        if not math.isclose(act_ac, exp_ac, rel_tol=0.02):
            failures.append(
                f"{pid}: total ac_usd {act_ac:,.2f} != expected {exp_ac:,.2f} "
                f"(ratio {act_ac/max(exp_ac,1):.2f}×). "
                "Join actuals using (project_id, work_package_id) — "
                "work_package_id is not globally unique."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 04 — [Trap 1] EV per project (phantom BAC matches inflate EV)
# ---------------------------------------------------------------------------

def test_04_trap1_ev_per_project(agent_report, expected):
    """
    Trap 1 — A work_package_id-only join to baselines creates phantom BAC matches
    from other projects, inflating EV by up to 5× per work package.

    Spot-checks total ev_usd for the same 3 projects.
    """
    exp_by_project = expected.groupby("project_id")["ev_usd"].sum()
    spot = exp_by_project.nlargest(3).index.tolist()

    failures = []
    for pid in spot:
        exp_ev = float(exp_by_project[pid])
        act_ev = float(
            agent_report[agent_report["project_id"] == pid]["ev_usd"].sum()
        )
        if not math.isclose(act_ev, exp_ev, rel_tol=0.02):
            failures.append(
                f"{pid}: total ev_usd {act_ev:,.2f} != expected {exp_ev:,.2f}. "
                "EV depends on BAC from the correct project-scoped baseline. "
                "Use (project_id, work_package_id) as the join key."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 05 — [Trap 2] Portfolio EV must not collapse
# ---------------------------------------------------------------------------

def test_05_trap2_portfolio_ev_not_zero(agent_summary, expected):
    """
    Trap 2 — If NaT baseline_effective_to rows are silently excluded by a
    pandas date-range filter, all 200 work packages have no matched BAC and
    EV collapses to zero or NaN across the entire portfolio.

    Verifies that total_ev_usd is within 5% of the correct ground truth.
    """
    exp_total_ev = round(float(expected["ev_usd"].sum()), 2)
    act_total_ev = float(agent_summary["total_ev_usd"])

    assert act_total_ev > exp_total_ev * 0.10, (
        f"total_ev_usd {act_total_ev:,.2f} is implausibly low "
        f"(expected ~{exp_total_ev:,.2f}). "
        "NaT in baseline_effective_to must be treated as 'no expiry', not excluded. "
        "The pandas comparison `reporting_date <= NaT` returns False — "
        "handle NaT explicitly before applying the date-range filter."
    )
    assert math.isclose(act_total_ev, exp_total_ev, rel_tol=0.05), (
        f"total_ev_usd {act_total_ev:,.2f} != expected {exp_total_ev:,.2f}"
    )


# ---------------------------------------------------------------------------
# Test 06 — [Trap 2] BAC accuracy for single-baseline work packages
# ---------------------------------------------------------------------------

def test_06_trap2_bac_single_baseline_wps(agent_report, expected, raw_data):
    """
    Trap 2 — Single-baseline work packages have their active baseline_effective_to
    set to NaT. A pandas date filter of the form
    `reporting_date <= NaT` returns False, silently dropping these rows and
    leaving the work package with no BAC → ev_usd = 0 or NaN.

    Spot-checks bac_usd for 5 single-baseline work packages (those NOT in the
    dual-baseline set).
    """
    _, bl, _, _, _ = raw_data
    dual_keys = set(
        zip(*[bl.groupby(["project_id", "work_package_id"]).filter(lambda g: len(g) > 1)
              [col].tolist() for col in ["project_id", "work_package_id"]])
    )
    single_exp = expected[
        ~expected.apply(lambda r: (r["project_id"], r["work_package_id"]) in dual_keys, axis=1)
    ].head(5)

    failures = []
    for _, row in single_exp.iterrows():
        pid, wp_id = row["project_id"], row["work_package_id"]
        exp_bac = float(row["bac_usd"])
        act_rows = agent_report[
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
# Test 07 — [Trap 3] BAC for dual-baseline work packages
# ---------------------------------------------------------------------------

def test_07_trap3_bac_dual_baseline_wps(agent_report, expected, raw_data):
    """
    Trap 3 — 10 work packages have two baseline rows. The original (lower-BAC)
    row appears first in baselines.csv; the revised (higher-BAC) row appears
    second. A model iterating in CSV order without sorting by
    baseline_effective_from picks the original lower BAC.

    Verifies bac_usd for all 10 re-baselined work packages matches the revised
    (higher) BAC from the most recently approved baseline.
    """
    _, bl, _, _, _ = raw_data
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])

    # Identify the 10 dual-baseline WP keys
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
                "When multiple baselines are valid, apply the one with the most recent "
                "baseline_effective_from — the revised baseline supersedes the original."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 08 — [Trap 3] CPI and EAC cascade for dual-baseline WPs
# ---------------------------------------------------------------------------

def test_08_trap3_cpi_eac_cascade(agent_report, expected, raw_data):
    """
    Trap 3 — A wrong (lower) BAC from the original baseline propagates into EV,
    CPI (= EV/AC), and EAC (= BAC/CPI), making the work package appear more
    efficient than it is and understating the forecast final cost.

    Spot-checks cpi and eac_usd for the 5 dual-baseline WPs with the largest
    BAC revision (where the error is most visible).
    """
    _, bl, _, _, _ = raw_data
    bl["baseline_effective_from"] = pd.to_datetime(bl["baseline_effective_from"])

    dual_mask = bl.groupby(["project_id", "work_package_id"])["baseline_id"].transform("count") > 1
    dual_wps  = bl[dual_mask][["project_id", "work_package_id"]].drop_duplicates()

    exp_sub = expected.merge(dual_wps, on=["project_id", "work_package_id"])
    spot = exp_sub.nlargest(5, "bac_usd")[["project_id", "work_package_id"]].values.tolist()

    failures = []
    for pid, wp_id in spot:
        exp_row = expected[
            (expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)
        ].iloc[0]
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            continue
        act_row = act_rows.iloc[0]

        exp_cpi = exp_row["cpi"]
        if exp_cpi is not None and not pd.isna(exp_cpi):
            act_cpi_raw = act_row["cpi"]
            act_cpi = float(act_cpi_raw) if not pd.isna(act_cpi_raw) else None
            if act_cpi is not None and not math.isclose(act_cpi, float(exp_cpi), abs_tol=0.05):
                failures.append(
                    f"({pid}, {wp_id}): cpi {act_cpi:.4f} != expected {float(exp_cpi):.4f}. "
                    "Wrong BAC cascades into EV → wrong CPI."
                )

        exp_eac = float(exp_row["eac_usd"])
        act_eac = float(act_row["eac_usd"])
        if not math.isclose(act_eac, exp_eac, rel_tol=0.05):
            failures.append(
                f"({pid}, {wp_id}): eac_usd {act_eac:,.2f} != expected {exp_eac:,.2f}. "
                "Wrong BAC (from original baseline) cascades into EAC = BAC / CPI."
            )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 09 — [Trap 4] AC for subcontract-heavy work packages
# ---------------------------------------------------------------------------

def test_09_trap4_ac_subcontract_billing_lag(agent_report, expected, raw_data):
    """
    Trap 4 — ~40% of March subcontract cost rows have entry_date in April or
    May 2024. A model filtering actuals on entry_date <= 2024-03-31 misses
    these rows, understating Q1 AC for subcontract-heavy work packages.

    Identifies the 5 work packages with the most lagged March subcontract rows
    and verifies their ac_usd matches the ground truth (which uses billing_period_date).
    """
    _, _, actuals, _, _ = raw_data
    actuals = actuals.copy()
    actuals["billing_period_date"] = pd.to_datetime(actuals["billing_period_date"])
    actuals["entry_date"]          = pd.to_datetime(actuals["entry_date"])

    lagged = actuals[
        (actuals["cost_type"] == "subcontract") &
        (actuals["billing_period_date"] == pd.Timestamp("2024-03-01")) &
        (actuals["entry_date"] > pd.Timestamp("2024-03-31"))
    ]
    lagged_cost = (
        lagged
        .groupby(["project_id", "work_package_id"])["cost_amount_usd"]
        .sum()
        .sort_values(ascending=False)
    )
    spot = lagged_cost.index[:5].tolist()

    failures = []
    for pid, wp_id in spot:
        exp_ac = float(
            expected[(expected["project_id"] == pid) & (expected["work_package_id"] == wp_id)]
            ["ac_usd"].iloc[0]
        )
        act_rows = agent_report[
            (agent_report["project_id"] == pid) & (agent_report["work_package_id"] == wp_id)
        ]
        if act_rows.empty:
            failures.append(f"({pid}, {wp_id}): not found in evm_report.csv")
            continue
        act_ac = float(act_rows["ac_usd"].iloc[0])
        missed = float(lagged_cost.get((pid, wp_id), 0))
        if not math.isclose(act_ac, exp_ac, rel_tol=0.02):
            failures.append(
                f"({pid}, {wp_id}): ac_usd {act_ac:,.2f} != expected {exp_ac:,.2f} "
                f"(diff {act_ac - exp_ac:+,.2f}, lagged subcontract cost: {missed:,.2f}). "
                "Filter actuals on billing_period_date, not entry_date. "
                "Subcontract invoices are posted 30–60 days after the billing period."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 10 — [Trap 4 + All] Portfolio AC aggregate
# ---------------------------------------------------------------------------

def test_10_portfolio_ac_aggregate(agent_summary, expected):
    """
    Portfolio total_ac_usd in summary.json must include all costs where
    billing_period_date <= 2024-03-31, regardless of entry_date.

    This test is sensitive to all four traps:
    - Trap 1 phantom joins inflate AC
    - Trap 4 billing lag omission deflates AC
    - Traps 2 & 3 affect EV and CPI but not AC directly; portfolio_cpi is
      also checked here as a compound metric.
    """
    exp_total_ac = round(float(expected["ac_usd"].sum()), 2)
    act_total_ac = float(agent_summary["total_ac_usd"])

    assert math.isclose(act_total_ac, exp_total_ac, rel_tol=0.02), (
        f"total_ac_usd {act_total_ac:,.2f} != expected {exp_total_ac:,.2f} "
        f"(diff {act_total_ac - exp_total_ac:+,.2f}). "
        "Use billing_period_date (not entry_date) for period filtering, and "
        "(project_id, work_package_id) as the join key."
    )

    exp_ev = round(float(expected["ev_usd"].sum()), 2)
    exp_cpi = round(exp_ev / exp_total_ac, 4) if exp_total_ac > 0 else 0.0
    act_cpi = float(agent_summary["portfolio_cpi"])

    assert math.isclose(act_cpi, exp_cpi, abs_tol=0.05), (
        f"portfolio_cpi {act_cpi:.4f} != expected {exp_cpi:.4f}. "
        "CPI = total_ev / total_ac; errors in EV (Traps 2, 3) or AC (Traps 1, 4) both affect this."
    )
