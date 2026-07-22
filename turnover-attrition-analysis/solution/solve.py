#!/usr/bin/env python3
"""Oracle solution for the turnover-attrition-analysis sample.

Business rules implemented:
  - `employees.csv` is one row per employment spell; the same employee_id can
    have more than one spell (a rehire). Every spell is treated independently
    -- spell_id is the grain, never employee_id.
  - A spell's department and worker_category are each a starting value
    (home_department_id, worker_category) plus a change log
    (department_transfers.csv, worker_category_changes.csv). Either
    attribute "as of" a date is the most recent logged change with a
    change/transfer date <= that date, else the starting value -- but only
    over the change rows that actually took effect: a transfer_type of
    temporary_secondment doesn't change the department of record, and an
    approval_status other than approved means the category change never
    happened.
  - A spell is part of headcount on a date if hire_date <= date and
    (term_date is null or term_date > date), AND its worker_category as of
    that date is regular_full_time or regular_part_time. A separation is
    attributed to the quarter containing its term_date (inclusive of both
    endpoints), scoped by worker_category as of that same term_date.
  - A spell is "on leave" on a date if that date falls within
    [leave_start, leave_end] for any of its leave_records.csv rows.
  - A spell's business_unit_id is fixed for its whole life. departments.csv
    has one row per department per business unit -- department_id alone is
    not a unique key there, so every department bucket is
    (business_unit_id, department_id), never department_id alone.
"""
import json
from pathlib import Path

import pandas as pd

WORKSPACE_DIR = Path("/workspace") if Path("/workspace").exists() else Path(__file__).parent.parent
DATA_DIR = WORKSPACE_DIR / "data" if (WORKSPACE_DIR / "data").exists() \
    else Path(__file__).parent.parent / "environment" / "data"

Q2_START = pd.Timestamp("2024-04-01")
CUTOFF = pd.Timestamp("2024-06-30")
MONTH_ENDS = [pd.Timestamp("2024-04-30"), pd.Timestamp("2024-05-31"), pd.Timestamp("2024-06-30")]

REGULAR_CATEGORIES = {"regular_full_time", "regular_part_time"}
VOLUNTARY_REASONS = {"resignation", "retirement"}
INVOLUNTARY_REASONS = {"involuntary_termination", "layoff"}


def load_data():
    departments = pd.read_csv(DATA_DIR / "departments.csv")
    employees = pd.read_csv(DATA_DIR / "employees.csv", parse_dates=["hire_date", "term_date"])
    transfers = pd.read_csv(DATA_DIR / "department_transfers.csv", parse_dates=["transfer_date"])
    category_changes = pd.read_csv(DATA_DIR / "worker_category_changes.csv", parse_dates=["change_date"])
    leaves = pd.read_csv(DATA_DIR / "leave_records.csv", parse_dates=["leave_start", "leave_end"])
    return departments, employees, transfers, category_changes, leaves


def build_change_lookup(df, date_col, value_col):
    """spell_id -> list of (date, new_value) sorted ascending."""
    lookup = {}
    for spell_id, grp in df.groupby("spell_id"):
        lookup[spell_id] = list(
            grp.sort_values(date_col)[[date_col, value_col]].itertuples(index=False, name=None)
        )
    return lookup


def value_as_of(spell_id, base_value, lookup, date):
    value = base_value
    for change_date, new_value in lookup.get(spell_id, []):
        if change_date <= date:
            value = new_value
        else:
            break
    return value


def build_leave_lookup(leaves):
    lookup = {}
    for spell_id, grp in leaves.groupby("spell_id"):
        lookup[spell_id] = list(grp[["leave_start", "leave_end"]].itertuples(index=False, name=None))
    return lookup


def is_on_leave(spell_id, leave_lookup, date):
    for start, end in leave_lookup.get(spell_id, []):
        if start <= date <= end:
            return True
    return False


def is_active_on(spell, date):
    if spell.hire_date > date:
        return False
    if pd.notna(spell.term_date) and spell.term_date <= date:
        return False
    return True


def build_report(employees, transfer_lookup, category_lookup, leave_lookup, departments):
    dept_keys = list(departments[["business_unit_id", "department_id"]].itertuples(index=False, name=None))
    month_end_headcounts = {k: {m: 0 for m in MONTH_ENDS} for k in dept_keys}
    total_month_end_headcounts = {m: 0 for m in MONTH_ENDS}

    for spell in employees.itertuples(index=False):
        for m in MONTH_ENDS:
            if not is_active_on(spell, m):
                continue
            category = value_as_of(spell.spell_id, spell.worker_category, category_lookup, m)
            if category not in REGULAR_CATEGORIES:
                continue
            dept = value_as_of(spell.spell_id, spell.home_department_id, transfer_lookup, m)
            key = (spell.business_unit_id, dept)
            month_end_headcounts[key][m] += 1
            total_month_end_headcounts[m] += 1

    avg_headcount = {
        k: sum(month_end_headcounts[k].values()) / len(MONTH_ENDS) for k in dept_keys
    }
    total_avg_headcount = sum(total_month_end_headcounts.values()) / len(MONTH_ENDS)

    headcount_jun30 = {k: month_end_headcounts[k][CUTOFF] for k in dept_keys}
    working_headcount_jun30 = {k: 0 for k in dept_keys}
    for spell in employees.itertuples(index=False):
        if not is_active_on(spell, CUTOFF):
            continue
        category = value_as_of(spell.spell_id, spell.worker_category, category_lookup, CUTOFF)
        if category not in REGULAR_CATEGORIES:
            continue
        if is_on_leave(spell.spell_id, leave_lookup, CUTOFF):
            continue
        dept = value_as_of(spell.spell_id, spell.home_department_id, transfer_lookup, CUTOFF)
        working_headcount_jun30[(spell.business_unit_id, dept)] += 1

    voluntary_separations = {k: 0 for k in dept_keys}
    involuntary_separations = {k: 0 for k in dept_keys}
    for spell in employees.itertuples(index=False):
        if pd.isna(spell.term_date):
            continue
        if not (Q2_START <= spell.term_date <= CUTOFF):
            continue
        category = value_as_of(spell.spell_id, spell.worker_category, category_lookup, spell.term_date)
        if category not in REGULAR_CATEGORIES:
            continue
        dept = value_as_of(spell.spell_id, spell.home_department_id, transfer_lookup, spell.term_date)
        key = (spell.business_unit_id, dept)
        if spell.term_reason in VOLUNTARY_REASONS:
            voluntary_separations[key] += 1
        elif spell.term_reason in INVOLUNTARY_REASONS:
            involuntary_separations[key] += 1

    rows = []
    for dept_row in departments.itertuples(index=False):
        key = (dept_row.business_unit_id, dept_row.department_id)
        ah = avg_headcount[key]
        vol = voluntary_separations[key]
        inv = involuntary_separations[key]
        rows.append(
            {
                "business_unit_id": dept_row.business_unit_id,
                "department_id": dept_row.department_id,
                "department_name": dept_row.department_name,
                "avg_headcount": round(ah, 2),
                "headcount_jun30": headcount_jun30[key],
                "working_headcount_jun30": working_headcount_jun30[key],
                "voluntary_separations": vol,
                "involuntary_separations": inv,
                "voluntary_turnover_rate": round(vol / ah * 100, 2) if ah > 0 else 0.0,
                "involuntary_turnover_rate": round(inv / ah * 100, 2) if ah > 0 else 0.0,
            }
        )

    report_df = pd.DataFrame(rows).sort_values(["business_unit_id", "department_id"]).reset_index(drop=True)

    total_voluntary = sum(voluntary_separations.values())
    total_involuntary = sum(involuntary_separations.values())
    summary = {
        "total_avg_headcount": round(total_avg_headcount, 2),
        "total_voluntary_separations": total_voluntary,
        "total_involuntary_separations": total_involuntary,
        "overall_voluntary_turnover_rate": round(total_voluntary / total_avg_headcount * 100, 2)
        if total_avg_headcount > 0 else 0.0,
        "overall_involuntary_turnover_rate": round(total_involuntary / total_avg_headcount * 100, 2)
        if total_avg_headcount > 0 else 0.0,
    }
    return report_df, summary


def main():
    departments, employees, transfers, category_changes, leaves = load_data()
    permanent_transfers = transfers[transfers["transfer_type"] == "permanent"]
    approved_changes = category_changes[category_changes["approval_status"] == "approved"]
    transfer_lookup = build_change_lookup(permanent_transfers, "transfer_date", "new_department_id")
    category_lookup = build_change_lookup(approved_changes, "change_date", "new_worker_category")
    leave_lookup = build_leave_lookup(leaves)

    report_df, summary = build_report(employees, transfer_lookup, category_lookup, leave_lookup, departments)

    report_df.to_csv(WORKSPACE_DIR / "department_turnover_report.csv", index=False)
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
