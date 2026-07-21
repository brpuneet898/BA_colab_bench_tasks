#!/usr/bin/env python3
"""Oracle solution for the turnover-attrition-analysis sample.

Business rules implemented:
  - `employees.csv` is one row per employment spell; the same employee_id can
    have more than one spell (a rehire). Every spell is treated independently
    -- spell_id is the grain, never employee_id.
  - A spell's department on any given date is its home_department_id updated
    by every department_transfers.csv row for that spell with
    transfer_date <= that date, taking the most recent one.
  - A spell is part of headcount on a date if hire_date <= date and
    (term_date is null or term_date > date). A separation is attributed to
    the quarter containing its term_date (inclusive of both endpoints).
  - A spell is "on leave" on a date if that date falls within
    [leave_start, leave_end] for any of its leave_records.csv rows.
  - Only worker_category in {regular_full_time, regular_part_time} counts
    toward this report.
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
    leaves = pd.read_csv(DATA_DIR / "leave_records.csv", parse_dates=["leave_start", "leave_end"])
    return departments, employees, transfers, leaves


def build_department_lookup(transfers):
    """spell_id -> list of (transfer_date, new_department_id) sorted ascending."""
    lookup = {}
    for spell_id, grp in transfers.groupby("spell_id"):
        lookup[spell_id] = list(
            grp.sort_values("transfer_date")[["transfer_date", "new_department_id"]].itertuples(index=False, name=None)
        )
    return lookup


def department_as_of(spell, transfer_lookup, date):
    department = spell.home_department_id
    for t_date, new_dept in transfer_lookup.get(spell.spell_id, []):
        if t_date <= date:
            department = new_dept
        else:
            break
    return department


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


def build_report(employees, transfer_lookup, leave_lookup, departments):
    regular = employees[employees["worker_category"].isin(REGULAR_CATEGORIES)].copy()

    dept_ids = departments["department_id"].tolist()
    month_end_headcounts = {d: {m: 0 for m in MONTH_ENDS} for d in dept_ids}
    total_month_end_headcounts = {m: 0 for m in MONTH_ENDS}

    for spell in regular.itertuples(index=False):
        for m in MONTH_ENDS:
            if is_active_on(spell, m):
                dept = department_as_of(spell, transfer_lookup, m)
                month_end_headcounts[dept][m] += 1
                total_month_end_headcounts[m] += 1

    avg_headcount = {
        d: sum(month_end_headcounts[d].values()) / len(MONTH_ENDS) for d in dept_ids
    }
    total_avg_headcount = sum(total_month_end_headcounts.values()) / len(MONTH_ENDS)

    headcount_jun30 = {d: month_end_headcounts[d][CUTOFF] for d in dept_ids}
    working_headcount_jun30 = {d: 0 for d in dept_ids}
    for spell in regular.itertuples(index=False):
        if is_active_on(spell, CUTOFF) and not is_on_leave(spell.spell_id, leave_lookup, CUTOFF):
            dept = department_as_of(spell, transfer_lookup, CUTOFF)
            working_headcount_jun30[dept] += 1

    voluntary_separations = {d: 0 for d in dept_ids}
    involuntary_separations = {d: 0 for d in dept_ids}
    for spell in regular.itertuples(index=False):
        if pd.isna(spell.term_date):
            continue
        if not (Q2_START <= spell.term_date <= CUTOFF):
            continue
        dept = department_as_of(spell, transfer_lookup, spell.term_date)
        if spell.term_reason in VOLUNTARY_REASONS:
            voluntary_separations[dept] += 1
        elif spell.term_reason in INVOLUNTARY_REASONS:
            involuntary_separations[dept] += 1

    rows = []
    for d in dept_ids:
        name = departments.loc[departments["department_id"] == d, "department_name"].iloc[0]
        ah = avg_headcount[d]
        vol = voluntary_separations[d]
        inv = involuntary_separations[d]
        rows.append(
            {
                "department_id": d,
                "department_name": name,
                "avg_headcount": round(ah, 2),
                "headcount_jun30": headcount_jun30[d],
                "working_headcount_jun30": working_headcount_jun30[d],
                "voluntary_separations": vol,
                "involuntary_separations": inv,
                "voluntary_turnover_rate": round(vol / ah * 100, 2) if ah > 0 else 0.0,
                "involuntary_turnover_rate": round(inv / ah * 100, 2) if ah > 0 else 0.0,
            }
        )

    report_df = pd.DataFrame(rows).sort_values("department_id").reset_index(drop=True)

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
    departments, employees, transfers, leaves = load_data()
    transfer_lookup = build_department_lookup(transfers)
    leave_lookup = build_leave_lookup(leaves)

    report_df, summary = build_report(employees, transfer_lookup, leave_lookup, departments)

    report_df.to_csv(WORKSPACE_DIR / "department_turnover_report.csv", index=False)
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
