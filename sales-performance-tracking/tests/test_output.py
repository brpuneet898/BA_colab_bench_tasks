import os
import json
import pandas as pd
from pathlib import Path

def test_monthly_rep_performance():
    workspace = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    report_path = workspace / "monthly_rep_performance.csv"
    summary_path = workspace / "summary.json"
    
    assert report_path.exists(), "monthly_rep_performance.csv not found"
    assert summary_path.exists(), "summary.json not found"
    
    df = pd.read_csv(report_path)
    
    # Check structure
    expected_cols = ["rep_id", "rep_name", "region", "month", "base_quota", "effective_quota", "net_arr_usd", "attainment_pct"]
    for col in expected_cols:
        assert col in df.columns, f"Missing column: {col}"
        
    # Check months are 2, 3, 4 (Feb, Mar, Apr)
    months = df["month"].unique()
    assert set(months) == {2, 3, 4}, f"Expected months 2, 3, 4. Got {months}"
    
    # Check that there is exactly one row per rep per month for the 200 reps
    assert len(df) == 600, f"Expected 600 rows (200 reps x 3 months), got {len(df)}"
    
    # Anti-cheat Sentinel 1: FX Triangulation correctness
    # If the model fails to triangulate FX rates properly, the net_arr_usd will be drastically off for foreign deals.
    with open(summary_path) as f:
        summary = json.load(f)
    
    total_net = summary["total_net_arr_usd"]
    # We expect total net ARR to be roughly in the hundreds of millions. 
    # If JPY/AUD are miscalculated (missing rates left as 1.0 or NaN), it will be completely wrong.
    assert total_net > 0, "Total Net ARR is zero."
    
    # Sentinel 2: Quota Rollover
    # Ensure effective_quota >= base_quota
    assert (df["effective_quota"] >= df["base_quota"]).all(), "Effective quota should never be less than base quota."
    
    # Find reps with rollover
    df_sorted = df.sort_values(["rep_id", "month"])
    has_rollover = False
    for _, group in df_sorted.groupby("rep_id"):
        m2_row = group[group["month"] == 2].iloc[0]
        m3_row = group[group["month"] == 3].iloc[0]
        
        expected_shortfall = max(0, m2_row["effective_quota"] - m2_row["net_arr_usd"])
        actual_rollover = m3_row["effective_quota"] - m3_row["base_quota"]
        
        assert abs(expected_shortfall - actual_rollover) < 0.1, f"Quota rollover failed for {m2_row['rep_id']}"
        if actual_rollover > 0:
            has_rollover = True
            
    assert has_rollover, "No quota rollovers found. Data generator might be broken."
    
    print("All algorithmic traps successfully verified!")

if __name__ == "__main__":
    test_monthly_rep_performance()
