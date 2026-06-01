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

ANNUAL_CATS = {'software_licenses', 'professional_services'}


@pytest.fixture(scope='module')
def notebook_vars():
    if not VARIABLES_PATH.exists():
        return None
    return json.load(open(VARIABLES_PATH))


@pytest.fixture(scope='module')
def raw_data():
    exp  = pd.read_csv(DATA_DIR / 'expenses.csv',
                        parse_dates=['expense_date', 'payment_date'])
    bud  = pd.read_csv(DATA_DIR / 'budgets.csv')
    dept = pd.read_csv(DATA_DIR / 'departments.csv')
    cats = pd.read_csv(DATA_DIR / 'categories.csv')
    return exp, bud, dept, cats


@pytest.fixture(scope='module')
def ground_truth(raw_data):
    exp_raw, bud, _, cats = raw_data

    cat_name_map = dict(zip(cats['category_id'], cats['category_name']))

    bud = bud.copy()
    bud['category_name'] = bud['category_id'].map(cat_name_map)
    bud['monthly_budget_usd'] = np.where(
        bud['category_name'].isin(ANNUAL_CATS),
        np.round(bud['budgeted_amount'] / 12, 2),
        bud['budgeted_amount']
    )

    exp = exp_raw[exp_raw['is_approved']].copy()
    exp['budget_month'] = exp['expense_date'].dt.to_period('M').astype(str)

    actual = (
        exp.groupby(['department_id', 'category_id', 'budget_month'])['amount_usd']
        .sum().reset_index().rename(columns={'amount_usd': 'actual_spend_usd'})
    )

    report = bud.merge(actual, on=['department_id', 'category_id', 'budget_month'],
                       how='outer')
    report['actual_spend_usd']   = report['actual_spend_usd'].fillna(0.0)
    report['monthly_budget_usd'] = report['monthly_budget_usd'].fillna(0.0)

    total_budgeted  = float(round(report['monthly_budget_usd'].sum(), 2))
    total_actual    = float(round(report['actual_spend_usd'].sum(),   2))
    total_variance  = float(round(total_actual - total_budgeted,      2))

    dept_q1 = report.groupby('department_id').agg(
        q1_budget=('monthly_budget_usd', 'sum'),
        q1_actual=('actual_spend_usd',   'sum')
    ).reset_index()
    over_count = int((dept_q1['q1_actual'] > dept_q1['q1_budget']).sum())

    sw_budget = float(round(
        bud[bud['category_name'] == 'software_licenses']['monthly_budget_usd'].sum(), 2
    ))

    march_actual = float(round(
        report.loc[report['budget_month'] == '2024-03', 'actual_spend_usd'].sum(), 2
    ))

    unapproved = int((~exp_raw['is_approved']).sum())

    return {
        'total_budgeted_usd':              total_budgeted,
        'total_actual_usd':                total_actual,
        'total_variance_usd':              total_variance,
        'over_budget_department_count':    over_count,
        'software_licenses_q1_budget_usd': sw_budget,
        'march_actual_spend':              march_actual,
        'unapproved_expense_count':        unapproved,
    }


# ── Anti-cheat sentinels ─────────────────────────────────────────────────────

def test_input_expenses_row_count(raw_data):
    exp, *_ = raw_data
    assert len(exp) == 3_330, "Input expenses.csv must not be modified."

def test_unapproved_rows_present(raw_data):
    exp, *_ = raw_data
    assert int((~exp['is_approved']).sum()) == 189, \
        "Unapproved expense rows must not be removed from expenses.csv."

def test_cross_month_accrual_entries_present(raw_data):
    exp, *_ = raw_data
    cross = ((exp['expense_date'].dt.month == 3) &
             (exp['payment_date'].dt.month == 4)).sum()
    assert cross >= 300, \
        "Cross-month accrual entries (March expense_date / April payment_date) must be present."

def test_annual_budget_values_present(raw_data):
    _, bud, _, cats = raw_data
    cat_name_map = dict(zip(cats['category_id'], cats['category_name']))
    bud = bud.copy()
    bud['category_name'] = bud['category_id'].map(cat_name_map)
    sw_amounts = bud[bud['category_name'] == 'software_licenses']['budgeted_amount']
    assert (sw_amounts > 50_000).all(), \
        "software_licenses budgeted_amount must contain annual contract values (>$50K)."


# ── Output file structure ─────────────────────────────────────────────────────

def test_variance_report_exists():
    assert (WORKSPACE_DIR / 'variance_report.csv').exists(), \
        "variance_report.csv not found in workspace."

def test_variance_report_columns():
    df = pd.read_csv(WORKSPACE_DIR / 'variance_report.csv')
    required = {'department_id', 'department_name', 'category_id', 'category_name',
                'budget_month', 'monthly_budget_usd', 'actual_spend_usd',
                'variance_usd', 'variance_pct'}
    assert required <= set(df.columns), \
        f"variance_report.csv is missing columns: {required - set(df.columns)}"

def test_variance_report_row_count():
    df = pd.read_csv(WORKSPACE_DIR / 'variance_report.csv')
    assert len(df) == 270, \
        "variance_report.csv must have one row per department–category–month (15×6×3=270)."


# ── Notebook variable presence ────────────────────────────────────────────────

def test_notebook_variables_exist(notebook_vars):
    if notebook_vars is None:
        pytest.fail('notebook_variables.json not found — notebook must export all required variables.')
    required = [
        'total_budgeted_usd', 'total_actual_usd', 'total_variance_usd',
        'over_budget_department_count', 'software_licenses_q1_budget_usd',
        'march_actual_spend', 'unapproved_expense_count',
    ]
    for var in required:
        assert var in notebook_vars, f"Missing notebook variable: {var}"


# ── Correctness tests ─────────────────────────────────────────────────────────

def test_total_budgeted_usd(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['total_budgeted_usd']
    expected = ground_truth['total_budgeted_usd']
    assert abs(actual - expected) <= 1.0, \
        f"total_budgeted_usd: got {actual}, expected {expected} (±1.0)."

def test_software_licenses_q1_budget_usd(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['software_licenses_q1_budget_usd']
    expected = ground_truth['software_licenses_q1_budget_usd']
    assert abs(actual - expected) <= 1.0, \
        f"software_licenses_q1_budget_usd: got {actual}, expected {expected} (±1.0)."

def test_total_actual_usd(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['total_actual_usd']
    expected = ground_truth['total_actual_usd']
    assert abs(actual - expected) <= 1.0, \
        f"total_actual_usd: got {actual}, expected {expected} (±1.0)."

def test_march_actual_spend(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['march_actual_spend']
    expected = ground_truth['march_actual_spend']
    assert abs(actual - expected) <= 1.0, \
        f"march_actual_spend: got {actual}, expected {expected} (±1.0)."

def test_total_variance_usd(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['total_variance_usd']
    expected = ground_truth['total_variance_usd']
    assert abs(actual - expected) <= 1.0, \
        f"total_variance_usd: got {actual}, expected {expected} (±1.0)."

def test_over_budget_department_count(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['over_budget_department_count']
    expected = ground_truth['over_budget_department_count']
    assert actual == expected, \
        f"over_budget_department_count: got {actual}, expected {expected}."

def test_unapproved_expense_count(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json absent')
    actual = notebook_vars['unapproved_expense_count']
    expected = ground_truth['unapproved_expense_count']
    assert actual == expected, \
        f"unapproved_expense_count: got {actual}, expected {expected}."
