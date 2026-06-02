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
    shifts, employees, _, budget = raw_data

    crosses = week_start_of(shifts['shift_start']) != week_start_of(shifts['shift_end'])
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

    wh = (segs.groupby(['employee_id', 'week_start'])['hours']
          .sum().reset_index().rename(columns={'hours': 'total_hours'}))
    wh['regular_hours'] = np.minimum(wh['total_hours'], WEEKLY_OT_THRESHOLD)
    wh['ot_hours']      = np.maximum(wh['total_hours'] - WEEKLY_OT_THRESHOLD, 0.0)
    wh = wh.merge(employees[['employee_id', 'department_id', 'hourly_rate']], on='employee_id')
    wh['actual_cost'] = (wh['regular_hours'] * wh['hourly_rate']
                         + wh['ot_hours'] * wh['hourly_rate'] * 1.5)

    dw = wh.groupby(['department_id', 'week_start']).agg(
        actual_cost=('actual_cost', 'sum'), ot_hours=('ot_hours', 'sum')).reset_index()

    budget = budget.copy()
    budget['budgeted_cost'] = budget['budgeted_hours'] * budget['avg_hourly_rate']
    report = budget.merge(dw, on=['department_id', 'week_start'])

    return {
        'total_budgeted_cost':    float(round(report['budgeted_cost'].sum(), 2)),
        'total_actual_cost':      float(round(report['actual_cost'].sum(), 2)),
        'total_variance':         float(round(report['actual_cost'].sum() - report['budgeted_cost'].sum(), 2)),
        'total_overtime_hours':   float(round(dw['ot_hours'].sum(), 2)),
        'over_budget_week_count': int((report['actual_cost'] > report['budgeted_cost']).sum()),
        '_report': report, '_dw': dw, '_wh': wh,
    }


# ── Anti-cheat sentinels ─────────────────────────────────────────────────────

def test_input_shifts_row_count(raw_data):
    shifts, *_ = raw_data
    assert len(shifts) == 23_995, "Input shifts.csv must not be modified."

def test_cross_week_shifts_present(raw_data):
    shifts, *_ = raw_data
    crosses = (week_start_of(shifts['shift_start']) != week_start_of(shifts['shift_end'])).sum()
    assert crosses >= 400, "Cross-week shifts must be present in shifts.csv."

def test_cross_week_employees_present(raw_data):
    shifts, *_ = raw_data
    crosses_mask = week_start_of(shifts['shift_start']) != week_start_of(shifts['shift_end'])
    assert shifts[crosses_mask]['employee_id'].nunique() >= 50, \
        "At least 50 distinct employees must have cross-week shifts."


# ── Output file structure ────────────────────────────────────────────────────

def test_variance_report_exists():
    assert (WORKSPACE_DIR / 'payroll_variance_report.csv').exists()

def test_variance_report_columns():
    df = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    required = {'department_id', 'department_name', 'week_start',
                'budgeted_cost', 'actual_cost', 'variance'}
    assert required <= set(df.columns), f"Missing: {required - set(df.columns)}"

def test_variance_report_row_count():
    df = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    assert len(df) == 130, "Must have 130 rows (10 departments × 13 weeks)."


# ── Row-level CSV validation ─────────────────────────────────────────────────

def test_variance_report_sort_order():
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    expected = report.sort_values(['department_id', 'week_start']).reset_index(drop=True)
    assert list(report['department_id']) == list(expected['department_id']) and \
           list(report['week_start'])    == list(expected['week_start']), \
        "Report must be sorted by department_id then week_start."

def test_budgeted_cost_formula_all_rows(raw_data):
    _, _, _, budget = raw_data
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    check = report.merge(budget[['department_id','week_start','budgeted_hours','avg_hourly_rate']],
                         on=['department_id','week_start'])
    for _, row in check.iterrows():
        expected = round(float(row['budgeted_hours']) * float(row['avg_hourly_rate']), 2)
        assert abs(float(row['budgeted_cost']) - expected) < 0.02, \
            f"budgeted_cost wrong for {row['department_id']}/{row['week_start']}."

def test_variance_formula_all_rows():
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    for _, row in report.iterrows():
        expected = round(float(row['actual_cost']) - float(row['budgeted_cost']), 2)
        assert abs(float(row['variance']) - expected) < 0.02, \
            f"variance wrong for {row['department_id']}/{row['week_start']}."

def test_d01_week1_spot_check(ground_truth):
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    gt_row = ground_truth['_report']
    exp_actual   = float(round(gt_row[(gt_row['department_id']=='D01') & (gt_row['week_start']=='2024-01-01')]['actual_cost'].iloc[0], 2))
    exp_budgeted = float(round(gt_row[(gt_row['department_id']=='D01') & (gt_row['week_start']=='2024-01-01')]['budgeted_cost'].iloc[0], 2))
    row = report[(report['department_id']=='D01') & (report['week_start']=='2024-01-01')].iloc[0]
    assert abs(float(row['actual_cost'])   - exp_actual)   <= 0.10
    assert abs(float(row['budgeted_cost']) - exp_budgeted) <= 0.10


# ── Notebook variable presence ────────────────────────────────────────────────

def test_notebook_variables_exist(notebook_vars):
    if notebook_vars is None:
        pytest.fail('notebook_variables.json not found.')
    for v in ['total_budgeted_cost','total_actual_cost','total_variance',
              'total_overtime_hours','over_budget_week_count']:
        assert v in notebook_vars, f"Missing: {v}"


# ── Notebook variable correctness ─────────────────────────────────────────────

def test_total_overtime_hours(notebook_vars, ground_truth):
    """Primary trap test: wrong if cross-week shifts not split at Monday boundary."""
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual, expected = notebook_vars['total_overtime_hours'], ground_truth['total_overtime_hours']
    assert abs(actual - expected) <= 1.0, \
        f"total_overtime_hours: got {actual}, expected {expected} (±1.0)."

def test_total_actual_cost(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual, expected = notebook_vars['total_actual_cost'], ground_truth['total_actual_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_actual_cost: got {actual}, expected {expected} (±1.0)."

def test_total_budgeted_cost(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual, expected = notebook_vars['total_budgeted_cost'], ground_truth['total_budgeted_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_budgeted_cost: got {actual}, expected {expected} (±1.0)."

def test_total_variance(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual, expected = notebook_vars['total_variance'], ground_truth['total_variance']
    assert abs(actual - expected) <= 1.0, \
        f"total_variance: got {actual}, expected {expected} (±1.0)."

def test_over_budget_week_count(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    assert notebook_vars['over_budget_week_count'] == ground_truth['over_budget_week_count'], \
        f"over_budget_week_count: got {notebook_vars['over_budget_week_count']}, expected {ground_truth['over_budget_week_count']}."
