import json
import os
import pytest
import pandas as pd
import numpy as np
from pathlib import Path

if os.path.exists('/workspace/data'):
    DATA_DIR      = Path('/workspace/data')
    WORKSPACE_DIR = Path('/workspace')
else:
    DATA_DIR      = Path(__file__).parent.parent / 'environment' / 'data'
    WORKSPACE_DIR = Path(__file__).parent.parent

VARIABLES_PATH = Path(os.environ.get('VARIABLES_PATH',
                                      '/logs/verifier/notebook_variables.json'))

WEEKLY_OT_THRESHOLD = 40.0


@pytest.fixture(scope='module')
def notebook_vars():
    if not VARIABLES_PATH.exists():
        return None
    return json.load(open(VARIABLES_PATH))


@pytest.fixture(scope='module')
def raw_data():
    shifts    = pd.read_csv(DATA_DIR / 'shifts.csv',       parse_dates=['shift_start', 'shift_end'])
    employees = pd.read_csv(DATA_DIR / 'employees.csv')
    depts     = pd.read_csv(DATA_DIR / 'departments.csv')
    budget    = pd.read_csv(DATA_DIR / 'weekly_budget.csv')
    return shifts, employees, depts, budget


def week_start_of(ts_series):
    return (ts_series - pd.to_timedelta(ts_series.dt.weekday, unit='D')).dt.normalize()


@pytest.fixture(scope='module')
def ground_truth(raw_data):
    shifts, employees, depts, budget = raw_data

    # Cross-week count
    s_wk = week_start_of(shifts['shift_start'])
    e_wk = week_start_of(shifts['shift_end'])
    cross_week_shift_count = int((s_wk != e_wk).sum())

    # Split cross-week shifts
    crosses = s_wk != e_wk
    normal  = shifts[~crosses].copy()
    normal['seg_start'] = normal['shift_start']
    normal['seg_end']   = normal['shift_end']

    xw = shifts[crosses].copy()
    xw['boundary'] = week_start_of(xw['shift_end'])

    seg1 = xw.copy(); seg1['seg_start'] = xw['shift_start']; seg1['seg_end'] = xw['boundary']
    seg2 = xw.copy(); seg2['seg_start'] = xw['boundary'];    seg2['seg_end'] = xw['shift_end']

    segs = pd.concat([normal, seg1, seg2], ignore_index=True)
    segs['hours']      = (segs['seg_end'] - segs['seg_start']).dt.total_seconds() / 3600
    segs['week_start'] = week_start_of(segs['seg_start']).dt.strftime('%Y-%m-%d')

    weekly_hrs = (segs.groupby(['employee_id', 'week_start'])['hours']
                  .sum().reset_index().rename(columns={'hours': 'total_hours'}))
    weekly_hrs['regular_hours'] = np.minimum(weekly_hrs['total_hours'], WEEKLY_OT_THRESHOLD)
    weekly_hrs['ot_hours']      = np.maximum(weekly_hrs['total_hours'] - WEEKLY_OT_THRESHOLD, 0.0)
    weekly_hrs = weekly_hrs.merge(employees[['employee_id', 'department_id', 'hourly_rate']],
                                   on='employee_id')
    weekly_hrs['actual_cost'] = (weekly_hrs['regular_hours'] * weekly_hrs['hourly_rate']
                                  + weekly_hrs['ot_hours'] * weekly_hrs['hourly_rate'] * 1.5)

    dept_week = (weekly_hrs.groupby(['department_id', 'week_start'])
                 .agg(actual_cost=('actual_cost', 'sum'), ot_hours=('ot_hours', 'sum'))
                 .reset_index())

    budget = budget.copy()
    budget['budgeted_cost'] = budget['budgeted_hours'] * budget['avg_hourly_rate']
    report = budget.merge(dept_week, on=['department_id', 'week_start'])

    return {
        'total_budgeted_cost':    float(round(report['budgeted_cost'].sum(), 2)),
        'total_actual_cost':      float(round(report['actual_cost'].sum(), 2)),
        'total_variance':         float(round(report['actual_cost'].sum() - report['budgeted_cost'].sum(), 2)),
        'total_overtime_hours':   float(round(dept_week['ot_hours'].sum(), 2)),
        'over_budget_week_count': int((report['actual_cost'] > report['budgeted_cost']).sum()),
        'cross_week_shift_count': cross_week_shift_count,
        '_report':                report,
        '_dept_week':             dept_week,
        '_weekly_hrs':            weekly_hrs,
    }


# ── Anti-cheat sentinels ─────────────────────────────────────────────────────

def test_input_shifts_row_count(raw_data):
    shifts, *_ = raw_data
    assert len(shifts) == 23_995, "Input shifts.csv must not be modified."

def test_cross_week_shifts_present(raw_data):
    shifts, *_ = raw_data
    s_wk = week_start_of(shifts['shift_start'])
    e_wk = week_start_of(shifts['shift_end'])
    assert int((s_wk != e_wk).sum()) >= 400, \
        "Cross-week shifts (spanning Sunday-Monday boundary) must be present in shifts.csv."

def test_cross_week_employees_present(raw_data):
    shifts, employees, *_ = raw_data
    s_wk = week_start_of(shifts['shift_start'])
    e_wk = week_start_of(shifts['shift_end'])
    cross_emps = shifts[s_wk != e_wk]['employee_id'].nunique()
    assert cross_emps >= 50, \
        "At least 50 distinct employees must have cross-week shifts."


# ── Output file structure ────────────────────────────────────────────────────

def test_variance_report_exists():
    assert (WORKSPACE_DIR / 'payroll_variance_report.csv').exists()

def test_variance_report_columns():
    df = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    required = {'department_id', 'department_name', 'week_start',
                'budgeted_cost', 'actual_cost', 'variance'}
    assert required <= set(df.columns), f"Missing columns: {required - set(df.columns)}"

def test_variance_report_row_count():
    df = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    assert len(df) == 130, "Must have 130 rows (10 departments × 13 weeks)."


# ── Row-level CSV validation ─────────────────────────────────────────────────

def test_variance_report_d01_week1_spot_check(ground_truth):
    """D01 week 2024-01-01: verifies correct OT calculation and week-boundary split."""
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    gt     = ground_truth['_report']

    expected_actual   = float(round(
        gt[(gt['department_id'] == 'D01') & (gt['week_start'] == '2024-01-01')]['actual_cost'].iloc[0], 2))
    expected_budgeted = float(round(
        gt[(gt['department_id'] == 'D01') & (gt['week_start'] == '2024-01-01')]['budgeted_cost'].iloc[0], 2))

    row = report[(report['department_id'] == 'D01') & (report['week_start'] == '2024-01-01')].iloc[0]

    assert abs(float(row['actual_cost'])   - expected_actual)   <= 0.10, \
        f"D01 w1 actual_cost: got {row['actual_cost']}, expected {expected_actual}."
    assert abs(float(row['budgeted_cost']) - expected_budgeted) <= 0.10, \
        f"D01 w1 budgeted_cost: got {row['budgeted_cost']}, expected {expected_budgeted}."
    assert abs(float(row['variance']) - round(expected_actual - expected_budgeted, 2)) <= 0.10

def test_variance_formula():
    """variance = actual_cost - budgeted_cost for every row."""
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    for _, row in report.iterrows():
        expected = round(float(row['actual_cost']) - float(row['budgeted_cost']), 2)
        assert abs(float(row['variance']) - expected) < 0.02, \
            f"variance mismatch for {row['department_id']}/{row['week_start']}."

def test_budgeted_cost_formula_all_rows(raw_data):
    """budgeted_cost must equal budgeted_hours × avg_hourly_rate for every row."""
    _, _, _, budget = raw_data
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    check = report.merge(
        budget[['department_id', 'week_start', 'budgeted_hours', 'avg_hourly_rate']],
        on=['department_id', 'week_start']
    )
    for _, row in check.iterrows():
        expected = round(float(row['budgeted_hours']) * float(row['avg_hourly_rate']), 2)
        assert abs(float(row['budgeted_cost']) - expected) < 0.02, \
            f"budgeted_cost mismatch for {row['department_id']}/{row['week_start']}: " \
            f"got {row['budgeted_cost']}, expected {expected}."

def test_variance_report_sort_order():
    """Report must be sorted by department_id then week_start."""
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    expected_order = report.sort_values(['department_id', 'week_start']).reset_index(drop=True)
    assert list(report['department_id']) == list(expected_order['department_id']) and \
           list(report['week_start'])    == list(expected_order['week_start']), \
        "payroll_variance_report.csv must be sorted by department_id then week_start."


# ── Notebook variable presence ────────────────────────────────────────────────

def test_notebook_variables_exist(notebook_vars):
    if notebook_vars is None:
        pytest.fail('notebook_variables.json not found.')
    required = ['total_budgeted_cost', 'total_actual_cost', 'total_variance',
                'total_overtime_hours', 'over_budget_week_count', 'cross_week_shift_count']
    for var in required:
        assert var in notebook_vars, f"Missing variable: {var}"


# ── Notebook variable correctness ─────────────────────────────────────────────

def test_cross_week_shift_count(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    assert notebook_vars['cross_week_shift_count'] == ground_truth['cross_week_shift_count'], \
        f"cross_week_shift_count: got {notebook_vars['cross_week_shift_count']}, " \
        f"expected {ground_truth['cross_week_shift_count']}."

def test_total_overtime_hours(notebook_vars, ground_truth):
    """Directly tests whether cross-week shifts were correctly split at the week boundary."""
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = notebook_vars['total_overtime_hours']
    expected = ground_truth['total_overtime_hours']
    assert abs(actual - expected) <= 1.0, \
        f"total_overtime_hours: got {actual}, expected {expected} (±1.0)."

def test_total_actual_cost(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = notebook_vars['total_actual_cost']
    expected = ground_truth['total_actual_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_actual_cost: got {actual}, expected {expected} (±1.0)."

def test_total_budgeted_cost(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = notebook_vars['total_budgeted_cost']
    expected = ground_truth['total_budgeted_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_budgeted_cost: got {actual}, expected {expected} (±1.0)."

def test_total_variance(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = notebook_vars['total_variance']
    expected = ground_truth['total_variance']
    assert abs(actual - expected) <= 1.0, \
        f"total_variance: got {actual}, expected {expected} (±1.0)."

def test_over_budget_week_count(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    assert notebook_vars['over_budget_week_count'] == ground_truth['over_budget_week_count'], \
        f"over_budget_week_count: got {notebook_vars['over_budget_week_count']}, " \
        f"expected {ground_truth['over_budget_week_count']}."
