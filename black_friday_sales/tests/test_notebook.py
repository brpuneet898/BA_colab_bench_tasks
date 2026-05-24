import json
import pandas as pd
import numpy as np
import pytest
from pathlib import Path
import os

# ============================================================
# Paths
# ============================================================

WORKSPACE = Path("/workspace")
DATA_DIR = WORKSPACE / "data"
SUMMARY_PATH = WORKSPACE / "promo_summary.csv"
VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")

ORDERS_PATH = DATA_DIR / "orders.csv"
RETURNS_PATH = DATA_DIR / "returns.csv"


# ============================================================
# Reference Pipeline (Ground Truth Computation)
# ============================================================

def _compute_ground_truth():
    """Computes the exact answers directly from the raw data."""
    # Trap 1 Fix: keep_default_na=False prevents "NA" (North America) from becoming NaN
    orders = pd.read_csv(ORDERS_PATH, keep_default_na=False)
    returns = pd.read_csv(RETURNS_PATH)
    
    orders['order_date'] = pd.to_datetime(orders['order_date'])
    returns['return_date'] = pd.to_datetime(returns['return_date'])
    
    # Filter for Cyber Week Promo
    promo_orders = orders[(orders['order_date'] >= '2025-11-24') & (orders['order_date'] <= '2025-11-30')].copy()
    total_promo_orders = int(len(promo_orders))
    
    # Trap 2 Fix: Left merge to catch returns that happened AFTER November
    promo_perf = promo_orders.merge(returns, on='order_id', how='left')
    promo_perf['refund_amount'] = promo_perf['refund_amount'].fillna(0)
    
    # Calculate Net Profit: (Revenue - COGS) - Refunds
    total_revenue = promo_perf['revenue'].sum()
    total_cogs = promo_perf['cogs'].sum()
    total_refunds = promo_perf['refund_amount'].sum()
    net_profit = (total_revenue - total_cogs) - total_refunds
    
    # Calculate Return Rate
    num_returns = promo_perf['return_id'].notna().sum()
    return_rate = num_returns / total_promo_orders if total_promo_orders > 0 else 0.0
    
    return {
        "total_promo_orders": total_promo_orders,
        "global_promo_net_profit": round(net_profit, 2),
        "promo_return_rate": round(return_rate, 4)
    }

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def ground_truth():
    """Compute all ground-truth values before running tests."""
    assert ORDERS_PATH.exists(), f"Raw data file missing: {ORDERS_PATH}"
    assert RETURNS_PATH.exists(), f"Raw data file missing: {RETURNS_PATH}"
    return _compute_ground_truth()

@pytest.fixture(scope="module")
def notebook_vars():
    """Load notebook variables produced by the harness."""
    if not VARIABLES_PATH.exists():
        pytest.skip("notebook_variables.json not found (requires harness execution)")
    with open(VARIABLES_PATH) as f:
        return json.load(f)

@pytest.fixture(scope="module")
def summary_df():
    """Load the agent's summary CSV."""
    assert SUMMARY_PATH.exists(), f"promo_summary.csv not found at {SUMMARY_PATH}"
    return pd.read_csv(SUMMARY_PATH)

# ============================================================
# Tests — Fail-Fast Structural Checks
# ============================================================

def test_variables_json_exists():
    """Verify the variable extraction harness ran successfully."""
    assert VARIABLES_PATH.exists(), "notebook_variables.json was not created. The agent may have syntax errors."

def test_summary_csv_exists():
    """Verify the agent exported the required CSV file."""
    assert SUMMARY_PATH.exists(), "Agent failed to save promo_summary.csv to the workspace."

def test_required_variables_present(notebook_vars):
    """Verify all explicitly requested variables are in the JSON."""
    required_vars = ["total_promo_orders", "global_promo_net_profit", "promo_return_rate"]
    for var in required_vars:
        assert var in notebook_vars, f"Missing required top-level variable: {var}"

def test_summary_csv_schema(summary_df):
    """Verify the CSV contains the exact required columns."""
    expected_cols = ["total_promo_orders", "global_promo_net_profit", "promo_return_rate"]
    for col in expected_cols:
        assert col in summary_df.columns, f"Missing column in summary CSV: {col}"

def test_summary_csv_shape(summary_df):
    """Verify the CSV has exactly one row of metrics."""
    assert len(summary_df) == 1, f"Expected exactly 1 row in summary CSV, got {len(summary_df)}."

# ============================================================
# Tests — Value Validation & Trap Checks
# ============================================================

def test_total_promo_orders_val(notebook_vars, summary_df, ground_truth):
    """Verify the total number of orders placed during Cyber Week."""
    expected = ground_truth["total_promo_orders"]
    
    # Check JSON
    actual_var = int(notebook_vars["total_promo_orders"])
    assert actual_var == expected, (
        f"JSON total_promo_orders: expected {expected}, got {actual_var}. "
        "Did the agent filter dates correctly (Nov 24 - Nov 30) or did they drop 'NA' strings?"
    )
    
    # Check CSV
    actual_csv = int(summary_df["total_promo_orders"].iloc[0])
    assert actual_csv == expected, f"CSV total_promo_orders: expected {expected}, got {actual_csv}."

def test_global_net_profit_val(notebook_vars, summary_df, ground_truth):
    """
    Verify the final net profit. 
    This test will fail if the agent drops the North American market 
    (the 'NA' string trap) or misses the December returns (Temporal trap).
    """
    expected = ground_truth["global_promo_net_profit"]
    
    # Check JSON
    actual_var = float(notebook_vars["global_promo_net_profit"])
    assert np.isclose(actual_var, expected, atol=0.1), (
        f"JSON global_promo_net_profit: expected {expected}, got {actual_var}. "
        "TRAP DETECTED: If the actual value is much lower, the agent likely dropped 'NA' regions. "
        "If the actual value is slightly higher, the agent missed the December returns for November orders."
    )
    
    # Check CSV
    actual_csv = float(summary_df["global_promo_net_profit"].iloc[0])
    assert np.isclose(actual_csv, expected, atol=0.1), (
        f"CSV global_promo_net_profit: expected {expected}, got {actual_csv}."
    )

def test_promo_return_rate_val(notebook_vars, summary_df, ground_truth):
    """
    Verify the return rate.
    This explicitly tests the downstream Temporal Return logic.
    """
    expected = ground_truth["promo_return_rate"]
    
    # Check JSON
    actual_var = float(notebook_vars["promo_return_rate"])
    assert np.isclose(actual_var, expected, atol=0.001), (
        f"JSON promo_return_rate: expected {expected}, got {actual_var}. "
        "TRAP DETECTED: The agent likely filtered returns.csv by November dates instead "
        "of merging on order_id to find downstream returns in December."
    )
    
    # Check CSV
    actual_csv = float(summary_df["promo_return_rate"].iloc[0])
    assert np.isclose(actual_csv, expected, atol=0.001), (
        f"CSV promo_return_rate: expected {expected}, got {actual_csv}."
    )

def test_variable_csv_consistency(notebook_vars, summary_df):
    """Ensure the values in the JSON match the values in the CSV."""
    assert np.isclose(float(notebook_vars["global_promo_net_profit"]), float(summary_df["global_promo_net_profit"].iloc[0]), atol=0.01), \
        "Mismatch between global_promo_net_profit in notebook variables and CSV."
    
    assert np.isclose(float(notebook_vars["promo_return_rate"]), float(summary_df["promo_return_rate"].iloc[0]), atol=0.0001), \
        "Mismatch between promo_return_rate in notebook variables and CSV."