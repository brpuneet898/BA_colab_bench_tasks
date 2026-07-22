"""Tests for the turnover-attrition-analysis sample.

Ground truth is computed inline (an independent re-implementation, never
imported from solution/solve.py) from the immutable input data.

Traps 1-4 are a competence floor, not the headroom bet -- they all share one
mechanical shape (reconstruct a value as of a date from a change log, check
interval containment, don't collapse a non-unique key) that frontier models
handle as a drilled reflex:

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

Traps 5-7 are a domain-knowledge-gap family (a logged row that never took
effect) rather than the resolve-as-of-date family above. The as-of
resolution logic traps 2/4 already require is unchanged -- these traps only
fail a submission that feeds that logic every logged row instead of the
ones that are actually authoritative.

  5. Secondments are not transfers -- department_transfers.csv rows carry a
     transfer_type. A temporary_secondment does not change a spell's HR
     department of record; only permanent rows may update it.
  6. Unapproved category changes never took effect -- worker_category_changes.csv
     rows carry an approval_status. A reclassification that was rejected or
     is still pending never happened; only approved rows are real.
  7. term_reason is a closed two-set match, not a complement -- a small,
     isolated cohort of already-regular employees separates in Q2 2024 with
     term_reason=intercompany_transfer (an internal legal-entity move, not a
     departure). Nothing in instruction.md needed to change for this: the
     separation definition is already exhaustive, so this only punishes
     "voluntary vs. everything else terminated" complement logic in place of
     two explicit membership checks.

Trap 8 is a composite-key trap, structurally different from all of the
above: department_id is unique only WITHIN a business_unit_id.
departments.csv has one row per department per business unit; two codes
(Finance, Legal) are shared-services functions that kept their
pre-acquisition codes and collide across both units -- the same code is two
genuinely different real departments. The report's own grouping key is
therefore (business_unit_id, department_id), never department_id alone. A
submission that groups or joins on department_id alone merges two real
departments into one bucket (wrong row count, and for the two shared codes,
a merged/duplicated headcount).

Traps 2, 4, 5, 6 and 8 all land on the same output fields (headcount_jun30,
avg_headcount, the separations counts), so a submission that mishandles any
one of them can fail the same downstream tests -- that's intentional
compounding, not test contamination: getting these fields right requires
all of them resolved correctly. What keeps each trap independently provable
is the *data*, not the tests: converted/secondment/non-approved/intercompany
spells don't overlap each other's affected rows, and the shared department
codes are populated on both sides independently of every other mechanism --
so each trap has its own naive-baseline sufficiency check (see test_case_05,
test_case_07, test_case_10, test_case_12) proving it alone moves the dataset
by a large margin, regardless of how the other traps are handled.
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
    "business_unit_id", "department_id", "department_name", "avg_headcount", "headcount_jun30",
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

    dept_keys = list(departments[["business_unit_id", "department_id"]].itertuples(index=False, name=None))
    dept_id_counts = departments["department_id"].value_counts()
    shared_department_ids = sorted(dept_id_counts[dept_id_counts > 1].index)

    def build_lookup(df, date_col, value_col):
        lookup = {}
        for sid, g in df.groupby("spell_id"):
            lookup[sid] = list(g.sort_values(date_col)[[date_col, value_col]].itertuples(index=False, name=None))
        return lookup

    # TRUE: only rows that actually took effect (permanent transfers, approved changes).
    transfer_lookup = build_lookup(
        transfers[transfers["transfer_type"] == "permanent"], "transfer_date", "new_department_id"
    )
    category_lookup = build_lookup(
        category_changes[category_changes["approval_status"] == "approved"], "change_date", "new_worker_category"
    )
    # NAIVE (mechanism A/B baselines): every logged row treated as authoritative.
    all_transfer_lookup = build_lookup(transfers, "transfer_date", "new_department_id")
    all_category_lookup = build_lookup(category_changes, "change_date", "new_worker_category")

    leave_lookup = {}
    for sid, g in leaves.groupby("spell_id"):
        leave_lookup[sid] = list(g[["leave_start", "leave_end"]].itertuples(index=False, name=None))

    def resolve_as_of(spell_id, base_value, lookup, date):
        value = base_value
        for change_date, new_value in lookup.get(spell_id, []):
            if change_date <= date:
                value = new_value
            else:
                break
        return value

    def dept_as_of(row, date):
        return resolve_as_of(row.spell_id, row.home_department_id, transfer_lookup, date)

    def category_as_of(row, date):
        return resolve_as_of(row.spell_id, row.worker_category, category_lookup, date)

    def dept_as_of_all(row, date):
        return resolve_as_of(row.spell_id, row.home_department_id, all_transfer_lookup, date)

    def category_as_of_all(row, date):
        return resolve_as_of(row.spell_id, row.worker_category, all_category_lookup, date)

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

    # --- True ground truth, keyed by (business_unit_id, department_id) ---
    month_end_hc = {k: {m: 0 for m in MONTH_ENDS} for k in dept_keys}
    # naive_dept_hc: static home_department_id (paired with the spell's own,
    # unambiguous business_unit_id), TRUE (as-of, permanent-only) category
    # -- isolates trap 2 from traps 4/5/6.
    naive_dept_hc_jun30 = {k: 0 for k in dept_keys}
    # naive_category_hc: TRUE (as-of, permanent-only) department, STATIC
    # worker_category -- isolates trap 4 from traps 2/5/6.
    naive_category_hc_jun30 = {k: 0 for k in dept_keys}
    # naive_dept_secondment_hc: department resolved from EVERY logged
    # transfer (including temporary_secondment), true (approved-only)
    # category -- isolates trap 5 alone.
    naive_dept_secondment_hc_jun30 = {k: 0 for k in dept_keys}
    # naive_category_approval_hc: true (permanent-only) department, category
    # resolved from EVERY logged change (including non-approved) -- isolates
    # trap 6 alone.
    naive_category_approval_hc_jun30 = {k: 0 for k in dept_keys}
    # naive_dept_only_hc: department_id ALONE (ignoring business_unit_id),
    # summed across whichever business unit(s) actually use that code --
    # isolates trap 8 (composite key) alone. For the 8 exclusive department
    # codes this equals the single true value; for the 2 shared codes it
    # silently merges two real departments' headcounts together.
    naive_dept_only_hc_jun30 = {d: 0 for d in departments["department_id"].unique()}

    for row in employees.itertuples(index=False):
        for m in MONTH_ENDS:
            if not active_on(row, m):
                continue
            if category_as_of(row, m) not in REGULAR_CATEGORIES:
                continue
            dept = dept_as_of(row, m)
            month_end_hc[(row.business_unit_id, dept)][m] += 1
        if active_on(row, CUTOFF):
            true_regular_jun30 = category_as_of(row, CUTOFF) in REGULAR_CATEGORIES
            true_dept_jun30 = dept_as_of(row, CUTOFF)
            if true_regular_jun30:
                naive_dept_hc_jun30[(row.business_unit_id, row.home_department_id)] += 1
                naive_dept_secondment_hc_jun30[(row.business_unit_id, dept_as_of_all(row, CUTOFF))] += 1
                naive_dept_only_hc_jun30[true_dept_jun30] += 1
            if row.worker_category in REGULAR_CATEGORIES:
                naive_category_hc_jun30[(row.business_unit_id, dept_as_of(row, CUTOFF))] += 1
            if category_as_of_all(row, CUTOFF) in REGULAR_CATEGORIES:
                naive_category_approval_hc_jun30[(row.business_unit_id, dept_as_of(row, CUTOFF))] += 1

    avg_headcount = {k: sum(month_end_hc[k].values()) / len(MONTH_ENDS) for k in dept_keys}
    headcount_jun30 = {k: month_end_hc[k][CUTOFF] for k in dept_keys}

    working_headcount_jun30 = {k: 0 for k in dept_keys}
    naive_working_hc_jun30 = {k: 0 for k in dept_keys}  # naive: leave_start <= cutoff only
    for row in employees.itertuples(index=False):
        if not active_on(row, CUTOFF) or category_as_of(row, CUTOFF) not in REGULAR_CATEGORIES:
            continue
        key = (row.business_unit_id, dept_as_of(row, CUTOFF))
        if not on_leave(row.spell_id, CUTOFF):
            working_headcount_jun30[key] += 1
        naive_leave_flag = any(start <= CUTOFF for start, end in leave_lookup.get(row.spell_id, []))
        if not naive_leave_flag:
            naive_working_hc_jun30[key] += 1

    voluntary_sep = {k: 0 for k in dept_keys}
    involuntary_sep = {k: 0 for k in dept_keys}
    # naive_category separations: TRUE department, STATIC worker_category
    # -- isolates trap 4 the same way naive_category_hc_jun30 does above.
    naive_category_voluntary_sep = {k: 0 for k in dept_keys}
    naive_category_involuntary_sep = {k: 0 for k in dept_keys}
    # naive_complement separations (trap 7): term_reason matched as "voluntary
    # if in VOLUNTARY_REASONS, else involuntary" instead of two explicit
    # membership checks -- lumps intercompany_transfer in with involuntary.
    naive_complement_involuntary_sep = {k: 0 for k in dept_keys}
    for row in employees.itertuples(index=False):
        if pd.isna(row.term_date) or not (Q2_START <= row.term_date <= CUTOFF):
            continue
        key = (row.business_unit_id, dept_as_of(row, row.term_date))
        is_true_regular = category_as_of(row, row.term_date) in REGULAR_CATEGORIES
        is_naive_regular = row.worker_category in REGULAR_CATEGORIES
        if is_true_regular:
            if row.term_reason in VOLUNTARY_REASONS:
                voluntary_sep[key] += 1
            elif row.term_reason in INVOLUNTARY_REASONS:
                involuntary_sep[key] += 1
            if row.term_reason not in VOLUNTARY_REASONS:
                naive_complement_involuntary_sep[key] += 1
        if is_naive_regular:
            if row.term_reason in VOLUNTARY_REASONS:
                naive_category_voluntary_sep[key] += 1
            elif row.term_reason in INVOLUNTARY_REASONS:
                naive_category_involuntary_sep[key] += 1

    rate = {
        k: (
            round(voluntary_sep[k] / avg_headcount[k] * 100, 2) if avg_headcount[k] > 0 else 0.0,
            round(involuntary_sep[k] / avg_headcount[k] * 100, 2) if avg_headcount[k] > 0 else 0.0,
        )
        for k in dept_keys
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

    n_secondment_transfers = int((transfers["transfer_type"] == "temporary_secondment").sum())
    n_non_approved_changes = int((category_changes["approval_status"] != "approved").sum())
    n_intercompany_transfers = int((employees["term_reason"] == "intercompany_transfer").sum())

    return {
        "dept_keys": dept_keys,
        "shared_department_ids": shared_department_ids,
        "avg_headcount": avg_headcount,
        "headcount_jun30": headcount_jun30,
        "naive_dept_hc_jun30": naive_dept_hc_jun30,
        "naive_category_hc_jun30": naive_category_hc_jun30,
        "naive_dept_secondment_hc_jun30": naive_dept_secondment_hc_jun30,
        "naive_category_approval_hc_jun30": naive_category_approval_hc_jun30,
        "naive_dept_only_hc_jun30": naive_dept_only_hc_jun30,
        "working_headcount_jun30": working_headcount_jun30,
        "naive_working_hc_jun30": naive_working_hc_jun30,
        "voluntary_sep": voluntary_sep,
        "involuntary_sep": involuntary_sep,
        "naive_category_voluntary_sep": naive_category_voluntary_sep,
        "naive_category_involuntary_sep": naive_category_involuntary_sep,
        "naive_complement_involuntary_sep": naive_complement_involuntary_sep,
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
        "n_secondment_transfers": n_secondment_transfers,
        "n_non_approved_changes": n_non_approved_changes,
        "n_intercompany_transfers": n_intercompany_transfers,
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
    """Also covers trap 8 (composite key): the report's row set is
    (business_unit_id, department_id) pairs, not department_id alone --
    a submission that groups by department_id only collapses two of the
    12 rows into fewer, wrong ones."""
    report, _ = submission
    assert report is not None
    assert list(report.columns) == REPORT_COLUMNS
    assert len(report) == len(ground_truth["dept_keys"])
    submitted_keys = set(zip(report["business_unit_id"], report["department_id"]))
    assert submitted_keys == set(ground_truth["dept_keys"])


def test_case_03_summary_structure(submission):
    _, summary = submission
    assert summary is not None
    for key in SUMMARY_KEYS:
        assert key in summary


def test_case_04_spell_grain_and_conservation(submission, ground_truth):
    """Trap 1: company-wide sums, computed without regard to which
    (business_unit_id, department_id) bucket each spell lands in, isolate
    whether rehire spells were silently dropped (or any other row loss)
    from attribution errors -- traps 2 and 8 are purely redistributive
    across buckets and do not move these totals. Trap 4 does move these
    totals (a missed worker_category conversion changes which spells are
    in scope at all), so this also catches trap 4."""
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
    by holding worker_category resolution correct in its naive baseline.

    Also covers trap 8 (composite key): departments.csv reuses two
    department_id codes (Finance, Legal) across both business units for
    genuinely different real departments. naive_dept_only_hc_jun30 shows
    what a department_id-only group/join produces -- for the shared codes
    it silently sums two different departments' headcounts together, far
    off from either business unit's real, individual number.

    The pass/fail comparison against `true_hc` is against the fully-resolved
    value, so a submission that mishandles trap 2, 4, 5, 6 or 8 (they share
    this output field) can fail here -- that's intentional: getting
    headcount_jun30 right requires all of them resolved correctly."""
    report, _ = submission
    true_hc = ground_truth["headcount_jun30"]
    naive_hc = ground_truth["naive_dept_hc_jun30"]
    mean_naive_delta = sum(abs(true_hc[k] - naive_hc[k]) for k in ground_truth["dept_keys"]) / len(ground_truth["dept_keys"])
    assert mean_naive_delta >= 1.5, "dataset does not exercise this trap enough"

    naive_merged = ground_truth["naive_dept_only_hc_jun30"]
    shared_deltas = [
        abs(naive_merged[k[1]] - true_hc[k])
        for k in ground_truth["dept_keys"] if k[1] in ground_truth["shared_department_ids"]
    ]
    mean_shared_delta = sum(shared_deltas) / len(shared_deltas)
    assert mean_shared_delta >= 100, "shared department codes are not populated enough on both sides"

    within = 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key in true_hc and abs(row["headcount_jun30"] - true_hc[key]) <= 1:
            within += 1
    assert within >= 9, f"only {within}/12 (business_unit_id, department_id) rows within tolerance on headcount_jun30"


def test_case_06_leave_working_headcount(submission, ground_truth):
    """Trap 3 (same-axis false friend): working_headcount_jun30 must exclude
    only employees on leave AT the cutoff date (interval containment), not
    everyone who ever started a leave before the cutoff."""
    assert ground_truth["n_on_leave_at_cutoff"] >= 15
    assert ground_truth["n_already_returned"] >= 10

    report, _ = submission
    true_delta = {
        k: ground_truth["headcount_jun30"][k] - ground_truth["working_headcount_jun30"][k]
        for k in ground_truth["dept_keys"]
    }
    within = 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key not in true_delta:
            continue
        submitted_delta = row["headcount_jun30"] - row["working_headcount_jun30"]
        if abs(submitted_delta - true_delta[key]) <= 1:
            within += 1
    assert within >= 9, f"only {within}/12 rows within tolerance on the leave delta"


def test_case_07_separations_by_reason(submission, ground_truth):
    """Also covers trap 7: term_reason is a closed two-value-per-bucket set.
    A small, isolated cohort separates in Q2 2024 with
    term_reason=intercompany_transfer, which matches neither the voluntary
    nor the involuntary set and must be excluded from both -- the existing
    per-row tolerance checks below already catch a submission that uses
    "voluntary vs. everything else terminated" complement logic instead of
    two explicit membership checks (it inflates involuntary_separations).
    The sufficiency check proves that complement-logic mistake alone would
    move the dataset by a large margin."""
    assert ground_truth["n_intercompany_transfers"] >= 30

    naive_inv = ground_truth["naive_complement_involuntary_sep"]
    true_inv = ground_truth["involuntary_sep"]
    mean_naive_delta = sum(abs(true_inv[k] - naive_inv[k]) for k in ground_truth["dept_keys"]) / len(ground_truth["dept_keys"])
    assert mean_naive_delta >= 1.5, "dataset does not exercise the term_reason exhaustiveness trap enough"

    report, _ = submission
    within_vol, within_inv = 0, 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key not in true_inv:
            continue
        if abs(row["voluntary_separations"] - ground_truth["voluntary_sep"][key]) <= 1:
            within_vol += 1
        if abs(row["involuntary_separations"] - true_inv[key]) <= 1:
            within_inv += 1
    assert within_vol >= 9, f"only {within_vol}/12 rows within tolerance on voluntary_separations"
    assert within_inv >= 9, f"only {within_inv}/12 rows within tolerance on involuntary_separations"


def test_case_08_turnover_rates(submission, ground_truth):
    report, _ = submission
    within_vol, within_inv = 0, 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key not in ground_truth["rate"]:
            continue
        true_vol_rate, true_inv_rate = ground_truth["rate"][key]
        if abs(row["voluntary_turnover_rate"] - true_vol_rate) <= 3.0:
            within_vol += 1
        if abs(row["involuntary_turnover_rate"] - true_inv_rate) <= 3.0:
            within_inv += 1
    assert within_vol >= 9, f"only {within_vol}/12 rows within tolerance on voluntary_turnover_rate"
    assert within_inv >= 9, f"only {within_inv}/12 rows within tolerance on involuntary_turnover_rate"


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
    by holding department resolution correct in its naive baseline --
    converted spells never carry transfer or leave records, so this
    isolation is clean at the data level. The pass/fail comparison against
    `true_hc` is against the fully-resolved value (same as test_case_05),
    so mishandling any of traps 2/4/5/6/8 can fail here; a submission that
    resolves department perfectly but reads worker_category straight off
    employees.csv still fails on the isolation check alone."""
    report, _ = submission
    assert ground_truth["n_conversions"] >= 100
    assert ground_truth["n_converted_active_jun30"] >= 20

    true_hc = ground_truth["headcount_jun30"]
    naive_hc = ground_truth["naive_category_hc_jun30"]
    mean_naive_delta = sum(abs(true_hc[k] - naive_hc[k]) for k in ground_truth["dept_keys"]) / len(ground_truth["dept_keys"])
    assert mean_naive_delta >= 3.0, "dataset does not exercise this trap enough"

    within_hc = 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key in true_hc and abs(row["headcount_jun30"] - true_hc[key]) <= 2:
            within_hc += 1
    assert within_hc >= 9, f"only {within_hc}/12 rows within tolerance on headcount_jun30 (category scope)"

    within_sep = 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key not in ground_truth["voluntary_sep"]:
            continue
        vol_ok = abs(row["voluntary_separations"] - ground_truth["voluntary_sep"][key]) <= 1
        inv_ok = abs(row["involuntary_separations"] - ground_truth["involuntary_sep"][key]) <= 1
        if vol_ok and inv_ok:
            within_sep += 1
    assert within_sep >= 8, f"only {within_sep}/12 rows within tolerance on separations (category scope)"


def test_case_11_anti_cheat_sentinels(ground_truth):
    """Fixed structural facts about the immutable input data, from the
    deterministic seed -- guards against tampering with /workspace/data."""
    assert ground_truth["n_rehire_pairs"] == 630
    assert ground_truth["active_rehire_spells"] == 479
    assert ground_truth["n_on_leave_at_cutoff"] == 439
    assert ground_truth["n_already_returned"] == 905
    assert ground_truth["total_voluntary"] == 795
    assert ground_truth["total_involuntary"] == 566
    assert ground_truth["n_conversions"] == 169
    assert ground_truth["n_converted_active_jun30"] == 75
    assert ground_truth["n_secondment_transfers"] == 384
    assert ground_truth["n_non_approved_changes"] == 58
    assert ground_truth["n_intercompany_transfers"] == 45
    assert ground_truth["shared_department_ids"] == ["DEPT04", "DEPT09"]
    assert len(ground_truth["dept_keys"]) == 12


def test_case_12_authoritative_records(submission, ground_truth):
    """Traps 5/6: department_transfers.csv and worker_category_changes.csv
    each include rows that were logged but never took effect (a temporary
    secondment doesn't change the HR department of record; a category
    change that was rejected or is still pending never happened). The
    as-of resolution logic traps 2/4 already require is unchanged here --
    these traps only fail a submission that feeds that logic every logged
    row instead of filtering to the ones that are actually authoritative
    first. Each mechanism gets its own isolated naive-baseline sufficiency
    check, holding the other axis at its TRUE (correctly filtered)
    resolution."""
    assert ground_truth["n_secondment_transfers"] >= 200
    assert ground_truth["n_non_approved_changes"] >= 15

    true_hc = ground_truth["headcount_jun30"]

    naive_secondment = ground_truth["naive_dept_secondment_hc_jun30"]
    mean_delta_a = sum(
        abs(true_hc[k] - naive_secondment[k]) for k in ground_truth["dept_keys"]
    ) / len(ground_truth["dept_keys"])
    assert mean_delta_a >= 1.5, "dataset does not exercise the secondment mechanism enough"

    naive_approval = ground_truth["naive_category_approval_hc_jun30"]
    mean_delta_b = sum(
        abs(true_hc[k] - naive_approval[k]) for k in ground_truth["dept_keys"]
    ) / len(ground_truth["dept_keys"])
    assert mean_delta_b >= 1.0, "dataset does not exercise the approval-status mechanism enough"

    report, _ = submission
    within = 0
    for _, row in report.iterrows():
        key = (row["business_unit_id"], row["department_id"])
        if key in true_hc and abs(row["headcount_jun30"] - true_hc[key]) <= 2:
            within += 1
    assert within >= 9, f"only {within}/12 rows within tolerance on headcount_jun30 (authoritative-record filtering)"
