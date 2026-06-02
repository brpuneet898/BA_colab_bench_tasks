import json
import os
import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

if os.path.exists('/workspace/data'):
    DATA_DIR      = Path('/workspace/data')
    WORKSPACE_DIR = Path('/workspace')
else:
    DATA_DIR      = Path(__file__).parent.parent / 'environment' / 'data'
    WORKSPACE_DIR = Path(__file__).parent.parent

VARIABLES_PATH = Path(os.environ.get('VARIABLES_PATH', '/logs/verifier/notebook_variables.json'))

# DST spring-forward: 2024-03-10 02:00 ET → 03:00 ET = 2024-03-10 07:00 UTC
DST_BOUNDARY_UTC = pd.Timestamp('2024-03-10 07:00:00')


@pytest.fixture(scope='module')
def notebook_vars():
    if not VARIABLES_PATH.exists():
        return None
    return json.load(open(VARIABLES_PATH))


@pytest.fixture(scope='module')
def raw_data():
    shifts        = pd.read_csv(DATA_DIR / 'shifts.csv',
                                 parse_dates=['shift_start', 'shift_end'])
    employees     = pd.read_csv(DATA_DIR / 'employees.csv')
    depts         = pd.read_csv(DATA_DIR / 'departments.csv')
    ot_policy     = pd.read_csv(DATA_DIR / 'ot_policy.csv')
    reassignments = pd.read_csv(DATA_DIR / 'reassignments.csv')
    budget        = pd.read_csv(DATA_DIR / 'weekly_budget.csv')
    return shifts, employees, depts, ot_policy, reassignments, budget


# ── Anti-cheat sentinels ──────────────────────────────────────────────────────

def test_input_shifts_row_count(raw_data):
    shifts, *_ = raw_data
    assert len(shifts) == 35_783, "Input shifts.csv must not be modified."

def test_contractors_present(raw_data):
    _, employees, *_ = raw_data
    assert (employees['employee_type'] == 'contractor').sum() >= 100, \
        "At least 100 contractor employees must be present."

def test_contractor_rate_scale(raw_data):
    _, employees, *_ = raw_data
    ct_max = employees.loc[employees['employee_type'] == 'contractor', 'hourly_rate'].max()
    ft_max = employees.loc[employees['employee_type'] == 'full_time',  'hourly_rate'].max()
    assert ct_max > ft_max * 50, \
        "Contractor rates must be in US cents (much larger than FT dollar rates)."

def test_cross_week_shifts_present(raw_data):
    shifts, *_ = raw_data
    def week_start_of(ts):
        return (ts - pd.to_timedelta(ts.dt.weekday, unit='D')).dt.normalize()
    off    = pd.to_timedelta(np.where(shifts['shift_end'] < DST_BOUNDARY_UTC, 5, 4), unit='h')
    se_et  = shifts['shift_end'] - off
    crosses = week_start_of(shifts['shift_start']) != week_start_of(se_et)
    assert crosses.sum() >= 1_000, "At least 1000 cross-week shifts must be present."

def test_ot_policy_exceptions_present(raw_data):
    _, _, _, ot_policy, *_ = raw_data
    thresholds = ot_policy.set_index('department_id')['weekly_ot_threshold'].to_dict()
    assert thresholds.get('D05') == 36.0, "D05 must have a 36h OT threshold."
    assert thresholds.get('D08') == 44.0, "D08 must have a 44h OT threshold."

def test_reassignments_present(raw_data):
    *_, reassignments, _ = raw_data
    assert len(reassignments) >= 200, "At least 200 reassignment records must be present."


# ── Output file structure ─────────────────────────────────────────────────────

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

def test_variance_report_sort_order():
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    expected = report.sort_values(['department_id', 'week_start']).reset_index(drop=True)
    assert (list(report['department_id']) == list(expected['department_id']) and
            list(report['week_start'])    == list(expected['week_start'])), \
        "Report must be sorted by department_id then week_start."

def test_variance_formula_all_rows():
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    for _, row in report.iterrows():
        expected = round(float(row['actual_cost']) - float(row['budgeted_cost']), 2)
        assert abs(float(row['variance']) - expected) < 0.02, \
            f"variance wrong for {row['department_id']}/{row['week_start']}."


# ── Ground-truth fixture ──────────────────────────────────────────────────────

def week_start_of(ts_series):
    return (ts_series - pd.to_timedelta(ts_series.dt.weekday, unit='D')).dt.normalize()


@pytest.fixture(scope='module')
def ground_truth(raw_data):
    shifts, employees, depts, ot_policy, reassignments, budget = raw_data

    # Trap 2: convert contractor rates from US cents to USD
    emp = employees.copy()
    mask = emp['employee_type'] == 'contractor'
    emp.loc[mask, 'hourly_rate'] = emp.loc[mask, 'hourly_rate'] / 100.0

    # Trap 1: convert shift_end from UTC to ET (DST-aware)
    sh = shifts.copy()
    off = pd.to_timedelta(np.where(sh['shift_end'] < DST_BOUNDARY_UTC, 5, 4), unit='h')
    sh['shift_end_et'] = sh['shift_end'] - off

    # Split cross-week shifts at Monday boundary (using ET times)
    crosses = week_start_of(sh['shift_start']) != week_start_of(sh['shift_end_et'])
    normal  = sh[~crosses].copy()
    normal['seg_start'] = normal['shift_start']
    normal['seg_end']   = normal['shift_end_et']

    xw = sh[crosses].copy()
    xw['boundary'] = week_start_of(xw['shift_end_et'])
    s1 = xw.copy(); s1['seg_start'] = xw['shift_start'];  s1['seg_end'] = xw['boundary']
    s2 = xw.copy(); s2['seg_start'] = xw['boundary'];     s2['seg_end'] = xw['shift_end_et']

    segs = pd.concat([normal, s1, s2], ignore_index=True)
    segs['hours']      = (segs['seg_end'] - segs['seg_start']).dt.total_seconds() / 3600
    segs['week_start'] = week_start_of(segs['seg_start']).dt.strftime('%Y-%m-%d')

    wh = (segs.groupby(['employee_id', 'week_start'])['hours']
          .sum().reset_index().rename(columns={'hours': 'total_hours'}))

    # Trap 4: dept-specific OT threshold
    wh = wh.merge(emp[['employee_id', 'department_id', 'hourly_rate']], on='employee_id')
    wh = wh.merge(ot_policy, on='department_id')
    wh['regular_hours'] = np.minimum(wh['total_hours'], wh['weekly_ot_threshold'])
    wh['ot_hours']      = np.maximum(wh['total_hours'] - wh['weekly_ot_threshold'], 0.0)
    wh['actual_cost']   = (wh['regular_hours'] * wh['hourly_rate']
                           + wh['ot_hours'] * wh['hourly_rate'] * 1.5)

    # Trap 3: reassignment cost allocation
    rc = reassignments.merge(emp[['employee_id', 'hourly_rate']], on='employee_id')
    rc['reass_cost'] = rc['reassigned_hours'] * rc['hourly_rate']

    home_ded = (rc.groupby(['employee_id', 'week_start'])['reass_cost']
                .sum().reset_index().rename(columns={'reass_cost': 'home_deduction'}))
    wh = wh.merge(home_ded, on=['employee_id', 'week_start'], how='left')
    wh['home_deduction'] = wh['home_deduction'].fillna(0.0)
    wh['home_cost']      = wh['actual_cost'] - wh['home_deduction']

    home_agg = (wh.groupby(['department_id', 'week_start'])
                .agg(actual_cost=('home_cost', 'sum'), ot_hours=('ot_hours', 'sum'))
                .reset_index())
    recv_agg = (rc.groupby(['to_dept_id', 'week_start'])['reass_cost']
                .sum().reset_index()
                .rename(columns={'to_dept_id': 'department_id', 'reass_cost': 'recv_cost'}))

    dw = home_agg.merge(recv_agg, on=['department_id', 'week_start'], how='left')
    dw['recv_cost']   = dw['recv_cost'].fillna(0.0)
    dw['actual_cost'] = dw['actual_cost'] + dw['recv_cost']
    dw = dw.drop(columns=['recv_cost'])

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


# ── Notebook variable presence ────────────────────────────────────────────────

def test_notebook_variables_exist(notebook_vars):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    for v in ['total_budgeted_cost', 'total_actual_cost', 'total_variance',
              'total_overtime_hours', 'over_budget_week_count']:
        assert v in notebook_vars, f"Missing notebook variable: {v}"


# ── Notebook variable correctness ─────────────────────────────────────────────

def test_total_overtime_hours(notebook_vars, ground_truth):
    """Primary trap: wrong if timezone not converted before computing hours."""
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = float(notebook_vars['total_overtime_hours'])
    expected = ground_truth['total_overtime_hours']
    assert abs(actual - expected) <= 1.0, \
        f"total_overtime_hours: got {actual}, expected {expected} (±1.0)"

def test_total_actual_cost(notebook_vars, ground_truth):
    """Affected by Traps 1 (timezone) and 2 (contractor cents)."""
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = float(notebook_vars['total_actual_cost'])
    expected = ground_truth['total_actual_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_actual_cost: got {actual}, expected {expected} (±1.0)"

def test_total_budgeted_cost(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = float(notebook_vars['total_budgeted_cost'])
    expected = ground_truth['total_budgeted_cost']
    assert abs(actual - expected) <= 1.0, \
        f"total_budgeted_cost: got {actual}, expected {expected} (±1.0)"

def test_total_variance(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual   = float(notebook_vars['total_variance'])
    expected = ground_truth['total_variance']
    assert abs(actual - expected) <= 1.0, \
        f"total_variance: got {actual}, expected {expected} (±1.0)"

def test_over_budget_week_count(notebook_vars, ground_truth):
    """Affected by all 4 traps via per-dept actual_cost."""
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    assert notebook_vars['over_budget_week_count'] == ground_truth['over_budget_week_count'], \
        (f"over_budget_week_count: got {notebook_vars['over_budget_week_count']}, "
         f"expected {ground_truth['over_budget_week_count']}")


# ── Row-level CSV spot checks ─────────────────────────────────────────────────

def test_budgeted_cost_formula_all_rows(raw_data):
    _, _, _, _, _, budget = raw_data
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    check = report.merge(budget[['department_id', 'week_start', 'budgeted_hours', 'avg_hourly_rate']],
                         on=['department_id', 'week_start'])
    for _, row in check.iterrows():
        expected = round(float(row['budgeted_hours']) * float(row['avg_hourly_rate']), 2)
        assert abs(float(row['budgeted_cost']) - expected) < 0.02, \
            f"budgeted_cost wrong for {row['department_id']}/{row['week_start']}."

def test_d05_actual_cost_reflects_36h_threshold(ground_truth):
    """D05 uses 36h OT threshold — actual_cost should reflect lower OT boundary."""
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    gt     = ground_truth['_report']
    for ws in gt['week_start'].unique():
        gt_row  = gt[(gt['department_id'] == 'D05') & (gt['week_start'] == ws)]
        rep_row = report[(report['department_id'] == 'D05') & (report['week_start'] == ws)]
        if len(gt_row) == 0 or len(rep_row) == 0:
            continue
        exp = float(round(gt_row['actual_cost'].iloc[0], 2))
        got = float(rep_row['actual_cost'].iloc[0])
        assert abs(got - exp) <= 1.0, \
            f"D05 actual_cost wrong for {ws}: got {got}, expected {exp}"

def test_reassignment_effect_on_dept_cost(ground_truth):
    """At least one receiving dept must have a higher actual_cost than home-only attribution."""
    report = pd.read_csv(WORKSPACE_DIR / 'payroll_variance_report.csv')
    gt     = ground_truth['_report']
    merged = report.merge(gt[['department_id', 'week_start', 'actual_cost']],
                          on=['department_id', 'week_start'],
                          suffixes=('_got', '_exp'))
    close_rows = (abs(merged['actual_cost_got'] - merged['actual_cost_exp']) <= 1.0).sum()
    assert close_rows >= 120, \
        f"Only {close_rows}/130 rows match ground truth — reassignment allocation likely wrong."
