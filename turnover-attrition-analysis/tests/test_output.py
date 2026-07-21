"""Tests for the turnover-attrition-analysis sample.

Ground truth is computed inline (an independent re-implementation, never
imported from solution/solve.py) from the immutable input data. It checks
four reasoning traps, none of which are named in instruction.md:

  1. Spell grain -- employee_id repeats for the ~30 rehired employees.
     A model that collapses to one row per employee_id (a natural EDA
     reflex, and what pandas' `drop_duplicates()`/`groupby().first()`
     defaults do given the row order in employees.csv) silently drops the
     employee's current, active spell.
  2. Point-in-time department attribution -- an employee's department on a
     given date must be reconstructed from department_transfers.csv, not
     read off the static home_department_id column.
  3. Leave interval containment -- "on leave as of date D" means D falls
     inside a specific [leave_start, leave_end] window, not merely that
     some leave_start occurred before D. The dataset deliberately contains
     more employees whose leave had already concluded before the report
     cutoff than employees genuinely on leave at cutoff.
  4. Point-in-time worker_category scope -- worker_category is likewise a
     starting value, not a static flag: intern/contractor spells that later
     convert to regular_full_time/regular_part_time (recorded in
     worker_category_changes.csv) must be scoped in or out as of the date
     being evaluated, mirroring trap 2's resolution pattern but on a
     second, independent attribute.

Traps 2 and 4 both feed the same output fields (headcount_jun30,
avg_headcount, the separations counts), so a submission that mishandles
either one can fail the same downstream tests -- that's intentional
compounding, not test contamination: getting these fields right requires
both to be resolved correctly. What keeps the traps independently provable
is the *data*, not the tests: converted spells never carry transfer or
leave records, so each trap has its own naive-baseline sufficiency check
(see test_case_05 and test_case_10) proving it alone moves the dataset by
a large margin, regardless of how the other traps are handled.
"""
from pathlib import Path

import pandas as pd
import pytest

WORKSPACE_DIR = Path("/workspace") if Path("/workspace").exists() else Path(__file__).parent.parent
DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
    else Path(__file__).parent.parent / "environment" / "data"

Q2_START = pd.Timestamp("2024-04-01")
CUTOFF = pd.Timestamp("2024-06-30")
MONTH_ENDS = [pd.Timestamp("2024-04-30"), pd.Timestamp("2024-05-31"), pd.Timestamp("2024-06-30")]
REGULAR_CATEGORIES = {"regular_full_time", "regular_part_time"}
VOLUNTARY_REASONS = {"resignation", "retirement"}
INVOLUNTARY_REASONS = {"involuntary_termination", "layoff"}

REPORT_COLUMNS = [
    "department_id", "department_name", "avg_headcount", "headcount_jun30",
    "working_headcount_jun30", "voluntary_separations", "involuntary_separations",
    "voluntary_turnover_rate", "involuntary_turnover_rate",
]
SUMMARY_KEYS = [
    "total_avg_headcount", "total_voluntary_separations", "total_involuntary_separations",
    "overall_voluntary_turnover_rate", "overall_involuntary_turnover_rate",
]


@pytest.fixture(scope="module")
def ground_truth():
    departments = pd.read_csv(DATA_DIR / "departments.csv")
    employees = pd.read_csv(DATA_DIR / "employees.csv", parse_dates=["hire_date", "term_date"])
    transfers = pd.read_csv(DATA_DIR / "department_transfers.csv", parse_dates=["transfer_date"])
    category_changes = pd.read_csv(DATA_DIR / "worker_category_changes.csv", parse_dates=["change_date"])
    leaves = pd.read_csv(DATA_DIR / "leave_records.csv", parse_dates=["leave_start", "leave_end"])

    dept_ids = departments["department_id"].tolist()

    transfer_lookup = {}
    for sid, g in transfers.groupby("spell_id"):
        transfer_lookup[sid] = list(
            g.sort_values("transfer_date")[["transfer_date", "new_department_id"]].itertuples(index=False, name=None)
        )

    category_lookup = {}
    for sid, g in category_changes.groupby("spell_id"):
        category_lookup[sid] = list(
            g.sort_values("change_date")[["change_date", "new_worker_category"]].itertuples(index=False, name=None)
        )

    leave_lookup = {}
    for sid, g in leaves.groupby("spell_id"):
        leave_lookup[sid] = list(g[["leave_start", "leave_end"]].itertuples(index=False, name=None))

    def dept_as_of(row, date):
        d = row.home_department_id
        for t_date, new_dept in transfer_lookup.get(row.spell_id, []):
            if t_date <= date:
                d = new_dept
            else:
                break
        return d

    def category_as_of(row, date):
        c = row.worker_category
        for c_date, new_cat in category_lookup.get(row.spell_id, []):
            if c_date <= date:
                c = new_cat
            else:
                break
        return c

    def on_leave(spell_id, date):
        for start, end in leave_lookup.get(spell_id, []):
            if start <= date <= end:
                return True
        return False

    def active_on(row, date):
        if row.hire_date > date:
            return False
        if pd.notna(row.term_date) and row.term_date <= date:
            return False
        return True

    # --- True ground truth: department AND worker_category both resolved
    # as of the relevant date, for every spell (no upfront category filter). ---
    month_end_hc = {d: {m: 0 for m in MONTH_ENDS} for d in dept_ids}
    # naive_dept_hc: static home_department_id, but TRUE (as-of) worker_category
    # -- isolates the department-attribution trap (2) from the category trap (4).
    naive_dept_hc_jun30 = {d: 0 for d in dept_ids}
    # naive_category_hc: TRUE (as-of) department, but STATIC worker_category
    # -- isolates the category-scope trap (4) from the department trap (2).
    naive_category_hc_jun30 = {d: 0 for d in dept_ids}
    for row in employees.itertuples(index=False):
        for m in MONTH_ENDS:
            if not active_on(row, m):
                continue
            if category_as_of(row, m) not in REGULAR_CATEGORIES:
                continue
            month_end_hc[dept_as_of(row, m)][m] += 1
        if active_on(row, CUTOFF):
            true_regular_jun30 = category_as_of(row, CUTOFF) in REGULAR_CATEGORIES
            if true_regular_jun30:
                naive_dept_hc_jun30[row.home_department_id] += 1
            if row.worker_category in REGULAR_CATEGORIES:
                naive_category_hc_jun30[dept_as_of(row, CUTOFF)] += 1

    avg_headcount = {d: sum(month_end_hc[d].values()) / len(MONTH_ENDS) for d in dept_ids}
    headcount_jun30 = {d: month_end_hc[d][CUTOFF] for d in dept_ids}

    working_headcount_jun30 = {d: 0 for d in dept_ids}
    naive_working_hc_jun30 = {d: 0 for d in dept_ids}  # naive: leave_start <= cutoff only
    for row in employees.itertuples(index=False):
        if not active_on(row, CUTOFF) or category_as_of(row, CUTOFF) not in REGULAR_CATEGORIES:
            continue
        d = dept_as_of(row, CUTOFF)
        if not on_leave(row.spell_id, CUTOFF):
            working_headcount_jun30[d] += 1
        naive_leave_flag = any(start <= CUTOFF for start, end in leave_lookup.get(row.spell_id, []))
        if not naive_leave_flag:
            naive_working_hc_jun30[d] += 1

    voluntary_sep = {d: 0 for d in dept_ids}
    involuntary_sep = {d: 0 for d in dept_ids}
    # naive_category separations: TRUE department, STATIC worker_category
    # -- isolates trap 4 the same way naive_category_hc_jun30 does above.
    naive_category_voluntary_sep = {d: 0 for d in dept_ids}
    naive_category_involuntary_sep = {d: 0 for d in dept_ids}
    for row in employees.itertuples(index=False):
        if pd.isna(row.term_date) or not (Q2_START <= row.term_date <= CUTOFF):
            continue
        d = dept_as_of(row, row.term_date)
        is_true_regular = category_as_of(row, row.term_date) in REGULAR_CATEGORIES
        is_naive_regular = row.worker_category in REGULAR_CATEGORIES
        if is_true_regular:
            if row.term_reason in VOLUNTARY_REASONS:
                voluntary_sep[d] += 1
            elif row.term_reason in INVOLUNTARY_REASONS:
                involuntary_sep[d] += 1
        if is_naive_regular:
            if row.term_reason in VOLUNTARY_REASONS:
                naive_category_voluntary_sep[d] += 1
            elif row.term_reason in INVOLUNTARY_REASONS:
                naive_category_involuntary_sep[d] += 1

    rate = {
        d: (
            round(voluntary_sep[d] / avg_headcount[d] * 100, 2) if avg_headcount[d] > 0 else 0.0,
            round(involuntary_sep[d] / avg_headcount[d] * 100, 2) if avg_headcount[d] > 0 else 0.0,
        )
        for d in dept_ids
    }

    total_avg_headcount = sum(avg_headcount.values())
    total_headcount_jun30 = sum(headcount_jun30.values())
    total_voluntary = sum(voluntary_sep.values())
    total_involuntary = sum(involuntary_sep.values())
    overall_voluntary_rate = round(total_voluntary / total_avg_headcount * 100, 2)
    overall_involuntary_rate = round(total_involuntary / total_avg_headcount * 100, 2)

    # rehires: how many active-at-cutoff spells belong to an employee_id
    # that also has an earlier (terminated) spell. Rehire spells are always
    # created regular_full_time/regular_part_time, so they never intersect
    # the category-conversion trap.
    dupe_ids = employees["employee_id"].value_counts()
    dupe_ids = set(dupe_ids[dupe_ids > 1].index)
    active_rehire_spells = sum(
        1 for row in employees.itertuples(index=False)
        if row.employee_id in dupe_ids and active_on(row, CUTOFF)
        and category_as_of(row, CUTOFF) in REGULAR_CATEGORIES
    )
    n_on_leave_at_cutoff = sum(1 for sid in employees["spell_id"] if on_leave(sid, CUTOFF))
    n_already_returned = sum(
        1 for sid in leaves["spell_id"].unique()
        if not on_leave(sid, CUTOFF) and any(s <= CUTOFF for s, e in leave_lookup.get(sid, []))
    )

    n_conversions = len(category_changes)
    n_converted_active_jun30 = sum(
        1 for row in category_changes.itertuples(index=False)
        if pd.isna(employees.set_index("spell_id").loc[row.spell_id, "term_date"])
    )

    return {
        "dept_ids": dept_ids,
        "avg_headcount": avg_headcount,
        "headcount_jun30": headcount_jun30,
        "naive_dept_hc_jun30": naive_dept_hc_jun30,
        "naive_category_hc_jun30": naive_category_hc_jun30,
        "working_headcount_jun30": working_headcount_jun30,
        "naive_working_hc_jun30": naive_working_hc_jun30,
        "voluntary_sep": voluntary_sep,
        "involuntary_sep": involuntary_sep,
        "naive_category_voluntary_sep": naive_category_voluntary_sep,
        "naive_category_involuntary_sep": naive_category_involuntary_sep,
        "rate": rate,
        "total_avg_headcount": total_avg_headcount,
        "total_headcount_jun30": total_headcount_jun30,
        "total_voluntary": total_voluntary,
        "total_involuntary": total_involuntary,
        "overall_voluntary_rate": overall_voluntary_rate,
        "overall_involuntary_rate": overall_involuntary_rate,
        "n_rehire_pairs": len(dupe_ids),
        "active_rehire_spells": active_rehire_spells,
        "n_on_leave_at_cutoff": n_on_leave_at_cutoff,
        "n_already_returned": n_already_returned,
        "n_conversions": n_conversions,
        "n_converted_active_jun30": n_converted_active_jun30,
    }


@pytest.fixture(scope="module")
def submission():
    report_path = WORKSPACE_DIR / "department_turnover_report.csv"
    summary_path = WORKSPACE_DIR / "summary.json"
    report = pd.read_csv(report_path) if report_path.exists() else None
    import json
    summary = json.load(open(summary_path)) if summary_path.exists() else None
    return report, summary


def test_case_01_output_files_exist():
    assert (WORKSPACE_DIR / "department_turnover_report.csv").exists()
    assert (WORKSPACE_DIR / "summary.json").exists()


def test_case_02_report_structure(submission, ground_truth):
    report, _ = submission
    assert report is not None
    assert list(report.columns) == REPORT_COLUMNS
    assert len(report) == len(ground_truth["dept_ids"])
    assert set(report["department_id"]) == set(ground_truth["dept_ids"])


def test_case_03_summary_structure(submission):
    _, summary = submission
    assert summary is not None
    for key in SUMMARY_KEYS:
        assert key in summary


def test_case_04_spell_grain_and_conservation(submission, ground_truth):
    """Trap 1: company-wide sums, computed without regard to which department
    each spell lands in, isolate whether rehire spells were silently dropped
    (or any other row loss) from department-attribution errors -- trap 2 is
    purely redistributive across departments and does not move these totals.
    Trap 4 does move these totals (a missed worker_category conversion
    changes which spells are in scope at all, not just which department
    they land in), so this also catches trap 4."""
    report, _ = submission
    assert ground_truth["active_rehire_spells"] >= 15

    total_avg = report["avg_headcount"].sum()
    total_hc = report["headcount_jun30"].sum()
    assert abs(total_avg - ground_truth["total_avg_headcount"]) <= 3.0
    assert abs(total_hc - ground_truth["total_headcount_jun30"]) <= 3


def test_case_05_department_attribution(submission, ground_truth):
    """Trap 2: per-department headcount_jun30 must reflect the transfer-
    reconstructed department, not the static home_department_id. The
    sufficiency check below (mean_naive_delta) isolates trap 2 from trap 4
    by holding worker_category resolution correct in its naive baseline, to
    prove trap 2 alone moves this dataset enough regardless of trap 4. The
    pass/fail comparison against `true_hc` is against the fully-resolved
    value, so a submission that mishandles either trap 2 or trap 4 (they
    share this output field) can fail here -- that's intentional: getting
    headcount_jun30 right requires both to be resolved correctly."""
    report, _ = submission
    true_hc = ground_truth["headcount_jun30"]
    naive_hc = ground_truth["naive_dept_hc_jun30"]
    mean_naive_delta = sum(abs(true_hc[d] - naive_hc[d]) for d in ground_truth["dept_ids"]) / len(ground_truth["dept_ids"])
    assert mean_naive_delta >= 1.5, "dataset does not exercise this trap enough"

    within = 0
    for _, row in report.iterrows():
        d = row["department_id"]
        if abs(row["headcount_jun30"] - true_hc[d]) <= 1:
            within += 1
    assert within >= 8, f"only {within}/10 departments within tolerance on headcount_jun30"


def test_case_06_leave_working_headcount(submission, ground_truth):
    """Trap 3 (same-axis false friend): working_headcount_jun30 must exclude
    only employees on leave AT the cutoff date (interval containment), not
    everyone who ever started a leave before the cutoff."""
    assert ground_truth["n_on_leave_at_cutoff"] >= 15
    assert ground_truth["n_already_returned"] >= 10

    report, _ = submission
    true_delta = {
        d: ground_truth["headcount_jun30"][d] - ground_truth["working_headcount_jun30"][d]
        for d in ground_truth["dept_ids"]
    }
    within = 0
    for _, row in report.iterrows():
        d = row["department_id"]
        submitted_delta = row["headcount_jun30"] - row["working_headcount_jun30"]
        if abs(submitted_delta - true_delta[d]) <= 1:
            within += 1
    assert within >= 8, f"only {within}/10 departments within tolerance on the leave delta"


def test_case_07_separations_by_reason(submission, ground_truth):
    report, _ = submission
    within_vol, within_inv = 0, 0
    for _, row in report.iterrows():
        d = row["department_id"]
        if abs(row["voluntary_separations"] - ground_truth["voluntary_sep"][d]) <= 1:
            within_vol += 1
        if abs(row["involuntary_separations"] - ground_truth["involuntary_sep"][d]) <= 1:
            within_inv += 1
    assert within_vol >= 8, f"only {within_vol}/10 departments within tolerance on voluntary_separations"
    assert within_inv >= 8, f"only {within_inv}/10 departments within tolerance on involuntary_separations"


def test_case_08_turnover_rates(submission, ground_truth):
    report, _ = submission
    within_vol, within_inv = 0, 0
    for _, row in report.iterrows():
        d = row["department_id"]
        true_vol_rate, true_inv_rate = ground_truth["rate"][d]
        if abs(row["voluntary_turnover_rate"] - true_vol_rate) <= 3.0:
            within_vol += 1
        if abs(row["involuntary_turnover_rate"] - true_inv_rate) <= 3.0:
            within_inv += 1
    assert within_vol >= 8, f"only {within_vol}/10 departments within tolerance on voluntary_turnover_rate"
    assert within_inv >= 8, f"only {within_inv}/10 departments within tolerance on involuntary_turnover_rate"


def test_case_09_summary_json_aggregates(submission, ground_truth):
    _, summary = submission
    assert abs(summary["total_avg_headcount"] - ground_truth["total_avg_headcount"]) <= 5.0
    assert abs(summary["total_voluntary_separations"] - ground_truth["total_voluntary"]) <= 4
    assert abs(summary["total_involuntary_separations"] - ground_truth["total_involuntary"]) <= 4
    assert abs(summary["overall_voluntary_turnover_rate"] - ground_truth["overall_voluntary_rate"]) <= 2.0
    assert abs(summary["overall_involuntary_turnover_rate"] - ground_truth["overall_involuntary_rate"]) <= 2.0


def test_case_10_worker_category_scope(submission, ground_truth):
    """Trap 4: worker_category is a starting value, not a static flag. The
    sufficiency check below (mean_naive_delta) isolates trap 4 from trap 2
    by holding department resolution correct in its naive baseline, to
    prove trap 4 alone moves this dataset enough regardless of trap 2 --
    converted spells never carry transfer or leave records, so this
    isolation is clean at the data level. The pass/fail comparison against
    `true_hc` is against the fully-resolved value (same as test_case_05),
    so mishandling either trap can fail here; a submission that resolves
    department perfectly but reads worker_category straight off
    employees.csv still fails on the isolation check alone."""
    report, _ = submission
    assert ground_truth["n_conversions"] >= 100
    assert ground_truth["n_converted_active_jun30"] >= 20

    true_hc = ground_truth["headcount_jun30"]
    naive_hc = ground_truth["naive_category_hc_jun30"]
    mean_naive_delta = sum(abs(true_hc[d] - naive_hc[d]) for d in ground_truth["dept_ids"]) / len(ground_truth["dept_ids"])
    assert mean_naive_delta >= 3.0, "dataset does not exercise this trap enough"

    within_hc = 0
    for _, row in report.iterrows():
        d = row["department_id"]
        if abs(row["headcount_jun30"] - true_hc[d]) <= 2:
            within_hc += 1
    assert within_hc >= 8, f"only {within_hc}/10 departments within tolerance on headcount_jun30 (category scope)"

    within_sep = 0
    for _, row in report.iterrows():
        d = row["department_id"]
        vol_ok = abs(row["voluntary_separations"] - ground_truth["voluntary_sep"][d]) <= 1
        inv_ok = abs(row["involuntary_separations"] - ground_truth["involuntary_sep"][d]) <= 1
        if vol_ok and inv_ok:
            within_sep += 1
    assert within_sep >= 7, f"only {within_sep}/10 departments within tolerance on separations (category scope)"


def test_case_11_anti_cheat_sentinels(ground_truth):
    """Fixed structural facts about the immutable input data, from the
    deterministic seed -- guards against tampering with /workspace/data."""
    assert ground_truth["n_rehire_pairs"] == 630
    assert ground_truth["active_rehire_spells"] == 471
    assert ground_truth["n_on_leave_at_cutoff"] == 369
    assert ground_truth["n_already_returned"] == 956
    assert ground_truth["total_voluntary"] == 775
    assert ground_truth["total_involuntary"] == 572
    assert ground_truth["n_conversions"] == 165
    assert ground_truth["n_converted_active_jun30"] == 65
