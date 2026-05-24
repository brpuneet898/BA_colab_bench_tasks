"""
Tests for the direct-traffic-attribution task.
"""

import json
import importlib.util
from pathlib import Path

import pandas as pd


WORKSPACE_DIR = Path("/workspace")
VERIFIER_DIR = Path("/logs/verifier")
DATA_PATH = WORKSPACE_DIR / "data" / "customer_journey_test_case.csv"
VARIABLES_PATH = VERIFIER_DIR / "notebook_variables.json"
ENGINE_PATH = WORKSPACE_DIR / "attribution_engine.py"


def load_engine_module():
    assert ENGINE_PATH.exists(), "Expected /workspace/attribution_engine.py to be created"
    spec = importlib.util.spec_from_file_location("attribution_engine", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None, "Could not load attribution_engine module"
    spec.loader.exec_module(module)
    return module


def notebook_variables() -> dict:
    assert VARIABLES_PATH.exists(), "notebook_variables.json was not created in /logs/verifier"
    with open(VARIABLES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def canonicalize_result(obj):
    """
    Canonicalize nested dict/list structures so comparison is stable across
    harmless ordering differences.
    """
    if isinstance(obj, dict):
        return {k: canonicalize_result(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [canonicalize_result(x) for x in obj]
    return obj


def build_required_scenarios(base_df: pd.DataFrame) -> dict:
    df = base_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    conversions = df[(df["is_conversion"] == True) & (df["revenue"] > 0)].sort_values(
        ["timestamp", "user_id"]
    )
    assert len(conversions) >= 1, "Base dataset must contain at least one valid conversion"

    first_conv = conversions.iloc[0]
    conv_ts = first_conv["timestamp"]
    conv_user = first_conv["user_id"]

    scenarios = {}

    scenarios["base_case"] = df.copy()

    s2 = df.copy()
    email_click_mask = (s2["channel"] == "Email") & (s2["interaction_type"] == "click")
    assert email_click_mask.any(), "Base dataset must contain at least one Email click for scenario 2"
    latest_email_click_idx = s2.loc[email_click_mask, "timestamp"].idxmax()
    s2.loc[latest_email_click_idx, "timestamp"] = conv_ts - pd.Timedelta(days=3)
    scenarios["recent_email_click"] = s2

    s3 = df.copy()
    s3 = pd.concat(
        [
            s3,
            pd.DataFrame(
                [
                    {
                        "user_id": conv_user,
                        "timestamp": conv_ts - pd.Timedelta(hours=2),
                        "channel": "Paid Search",
                        "interaction_type": "click",
                        "campaign_name": "Brand_Rescue",
                        "revenue": 0.0,
                        "is_conversion": False,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    scenarios["paid_search_override"] = s3

    s4 = df.copy()
    tie_ts = conv_ts - pd.Timedelta(minutes=90)
    s4 = pd.concat(
        [
            s4,
            pd.DataFrame(
                [
                    {
                        "user_id": conv_user,
                        "timestamp": tie_ts,
                        "channel": "Affiliate",
                        "interaction_type": "click",
                        "campaign_name": "Partner_A",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": conv_user,
                        "timestamp": tie_ts,
                        "channel": "Paid Social",
                        "interaction_type": "click",
                        "campaign_name": "Social_A",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    scenarios["tie_break_same_timestamp"] = s4

    s5 = df.copy()
    s5 = pd.concat(
        [
            s5,
            pd.DataFrame(
                [
                    {
                        "user_id": conv_user,
                        "timestamp": conv_ts + pd.Timedelta(days=20),
                        "channel": "Direct Traffic",
                        "interaction_type": "visit",
                        "campaign_name": "Return_Visit",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": conv_user,
                        "timestamp": conv_ts + pd.Timedelta(days=25),
                        "channel": "Email",
                        "interaction_type": "click",
                        "campaign_name": "Winback_25D",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": conv_user,
                        "timestamp": conv_ts + pd.Timedelta(days=31),
                        "channel": "Display Ad",
                        "interaction_type": "impression",
                        "campaign_name": "Retargeting_Late",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": conv_user,
                        "timestamp": conv_ts + pd.Timedelta(days=32),
                        "channel": "Direct Traffic",
                        "interaction_type": "visit",
                        "campaign_name": "Organic_Return",
                        "revenue": 125.0,
                        "is_conversion": True,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    scenarios["second_conversion_extension"] = s5

    return scenarios


def build_hidden_probe_dataset(base_df: pd.DataFrame) -> pd.DataFrame:
    """
    A hidden-style robustness probe created inside the test so hardcoding the five
    public scenarios is not enough.
    """
    df = base_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    conversions = df[(df["is_conversion"] == True) & (df["revenue"] > 0)].sort_values(
        ["timestamp", "user_id"]
    )
    first_conv = conversions.iloc[0]
    conv_ts = first_conv["timestamp"]
    conv_user = first_conv["user_id"]

    probe = df.copy()

    probe = pd.concat([probe, probe.iloc[[0]].copy()], ignore_index=True)

    latest_tie_ts = conv_ts - pd.Timedelta(hours=1)
    probe = pd.concat(
        [
            probe,
            pd.DataFrame(
                [
                    {
                        "user_id": conv_user,
                        "timestamp": latest_tie_ts,
                        "channel": "Zeta",
                        "interaction_type": "click",
                        "campaign_name": "ZZZ",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": conv_user,
                        "timestamp": latest_tie_ts,
                        "channel": "Alpha",
                        "interaction_type": "click",
                        "campaign_name": "AAA",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )

    probe = pd.concat(
        [
            probe,
            pd.DataFrame(
                [
                    {
                        "user_id": "USR_X2",
                        "timestamp": conv_ts + pd.Timedelta(days=40),
                        "channel": "Display Ad",
                        "interaction_type": "impression",
                        "campaign_name": "Prospecting_X2",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": "USR_X2",
                        "timestamp": conv_ts + pd.Timedelta(days=41),
                        "channel": "Email",
                        "interaction_type": "click",
                        "campaign_name": "Winback_X2",
                        "revenue": 0.0,
                        "is_conversion": False,
                    },
                    {
                        "user_id": "USR_X2",
                        "timestamp": conv_ts + pd.Timedelta(days=45),
                        "channel": "Direct Traffic",
                        "interaction_type": "visit",
                        "campaign_name": "Organic_X2",
                        "revenue": 80.0,
                        "is_conversion": True,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )

    return probe


def test_required_artifacts_exist() -> None:
    assert DATA_PATH.exists(), "Expected /workspace/data/customer_journey_test_case.csv to exist"
    assert (WORKSPACE_DIR / "notebook.ipynb").exists(), "Expected /workspace/notebook.ipynb to exist"
    assert ENGINE_PATH.exists(), "Expected /workspace/attribution_engine.py to exist"
    assert VARIABLES_PATH.exists(), "Expected /logs/verifier/notebook_variables.json to exist"


def test_engine_function_exists() -> None:
    module = load_engine_module()
    assert hasattr(module, "attribute_conversions"), "attribute_conversions function not found"
    assert callable(module.attribute_conversions), "attribute_conversions must be callable"


def test_output_has_exact_top_level_schema() -> None:
    variables = notebook_variables()
    assert set(variables.keys()) == {"scenario_results"}, (
        f"Top-level JSON must contain exactly one key: scenario_results. Got {set(variables.keys())}"
    )
    assert isinstance(variables["scenario_results"], dict), "scenario_results must be a dict"


def test_required_scenarios_present() -> None:
    variables = notebook_variables()
    expected = {
        "base_case",
        "recent_email_click",
        "paid_search_override",
        "tie_break_same_timestamp",
        "second_conversion_extension",
    }
    actual = set(variables["scenario_results"].keys())
    assert actual == expected, f"scenario_results keys mismatch. Expected {expected}, got {actual}"


def test_public_scenarios_match_reference_engine() -> None:
    module = load_engine_module()
    base_df = pd.read_csv(DATA_PATH)
    scenarios = build_required_scenarios(base_df)
    produced = notebook_variables()["scenario_results"]

    for name, scenario_df in scenarios.items():
        expected = module.attribute_conversions(scenario_df.copy(), lookback_days=14)
        actual = produced[name]
        assert canonicalize_result(actual) == canonicalize_result(expected), (
            f"Scenario {name} does not match the engine output."
        )


def test_hidden_probe_dataset_matches_reference_engine() -> None:
    module = load_engine_module()
    base_df = pd.read_csv(DATA_PATH)
    probe_df = build_hidden_probe_dataset(base_df)

    result = module.attribute_conversions(probe_df.copy(), lookback_days=14)

    assert isinstance(result, dict), "Engine output must be a dictionary"
    assert "conversion_count" in result, "Engine output missing conversion_count"
    assert "total_revenue" in result, "Engine output missing total_revenue"
    assert "channel_totals" in result, "Engine output missing channel_totals"
    assert "per_conversion" in result, "Engine output missing per_conversion"

    assert result["conversion_count"] == 2, (
        f"Hidden probe should have exactly 2 conversions after construction. Got {result['conversion_count']}"
    )
    assert abs(float(result["total_revenue"]) - 230.0) < 1e-9, (
        f"Hidden probe total revenue should be 230.0. Got {result['total_revenue']}"
    )

    first_conv = sorted(result["per_conversion"], key=lambda x: (x["conversion_timestamp"], x["user_id"]))[0]
    assert first_conv["winning_channel"] == "Alpha", (
        f"Expected hidden probe tie-break winner to be Alpha. Got {first_conv['winning_channel']}"
    )

    second_conv = sorted(result["per_conversion"], key=lambda x: (x["conversion_timestamp"], x["user_id"]))[1]
    assert second_conv["winning_channel"] == "Email", (
        f"Expected second hidden conversion to be attributed to Email. Got {second_conv['winning_channel']}"
    )


def test_channel_totals_sum_to_total_revenue_for_all_public_scenarios() -> None:
    scenario_results = notebook_variables()["scenario_results"]
    for name, result in scenario_results.items():
        total = float(result["total_revenue"])
        channel_total_sum = sum(float(v) for v in result["channel_totals"].values())
        assert abs(total - channel_total_sum) < 1e-9, (
            f"Scenario {name}: channel_totals sum {channel_total_sum} does not equal total_revenue {total}"
        )


def test_per_conversion_rows_are_sorted() -> None:
    scenario_results = notebook_variables()["scenario_results"]
    for name, result in scenario_results.items():
        rows = result["per_conversion"]
        observed = [(r["conversion_timestamp"], r["user_id"]) for r in rows]
        expected = sorted(observed)
        assert observed == expected, f"Scenario {name}: per_conversion must be sorted by timestamp then user_id"


def test_excluded_counts_schema_and_integrity() -> None:
    scenario_results = notebook_variables()["scenario_results"]
    required_keys = {"conversion_row", "same_or_after_conversion", "non_click", "outside_lookback"}

    for name, result in scenario_results.items():
        for row in result["per_conversion"]:
            assert set(row["excluded_counts"].keys()) == required_keys, (
                f"Scenario {name}: excluded_counts keys mismatch for conversion {row}"
            )
            for k, v in row["excluded_counts"].items():
                assert isinstance(v, int), f"Scenario {name}: excluded_counts[{k}] must be int, got {type(v).__name__}"
                assert v >= 0, f"Scenario {name}: excluded_counts[{k}] must be non-negative"
            assert isinstance(row["eligible_click_count"], int), (
                f"Scenario {name}: eligible_click_count must be int"
            )
            assert row["eligible_click_count"] >= 0, (
                f"Scenario {name}: eligible_click_count must be non-negative"
            )
