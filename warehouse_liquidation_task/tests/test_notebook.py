import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.optimize import milp, LinearConstraint, Bounds

WORKSPACE_DIR = Path("/workspace")
DATA_DIR = WORKSPACE_DIR / "data"
VARIABLES_PATH = Path("/logs/verifier/notebook_variables.json")

DATA_PATH = DATA_DIR / "Q3_Inventory_Sales_Data.csv"

ANCHOR_DATE = pd.Timestamp("2024-09-26")
RECENCY_CUTOFF = ANCHOR_DATE - pd.Timedelta(weeks=8)
SALES_COLUMNS = [f"Week_{i}" for i in range(1, 13)]


@pytest.fixture(scope="module")
def notebook_variables():
    assert VARIABLES_PATH.exists(), "notebook_variables.json not found"
    with open(VARIABLES_PATH, "r") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def raw_data():
    df = pd.read_csv(DATA_PATH)
    df["Launch_Date"] = pd.to_datetime(df["Launch_Date"])
    return df


@pytest.fixture(scope="module")
def outputs():
    liquidation_50_path = WORKSPACE_DIR / "liquidation_list_50.csv"
    liquidation_100_path = WORKSPACE_DIR / "liquidation_list_100.csv"
    final_report_path = WORKSPACE_DIR / "final_report.csv"

    assert liquidation_50_path.exists(), "liquidation_list_50.csv not found"
    assert liquidation_100_path.exists(), "liquidation_list_100.csv not found"
    assert final_report_path.exists(), "final_report.csv not found"

    liquidation_50 = pd.read_csv(liquidation_50_path)
    liquidation_100 = pd.read_csv(liquidation_100_path)
    final_report = pd.read_csv(final_report_path)

    return {
        "50": liquidation_50,
        "100": liquidation_100,
        "report": final_report,
    }


def normalized_avg_weekly_sales(row: pd.Series) -> float:
    sales = row[SALES_COLUMNS].to_numpy(dtype=float)

    weeks_since_launch = max(1, (ANCHOR_DATE - row["Launch_Date"]).days // 7)
    weeks_active = min(12, weeks_since_launch)

    active_sales = sales[-weeks_active:]

    if len(active_sales) > 1:
        stdev = np.std(active_sales, ddof=1)

        if stdev > 0:
            z_scores = np.abs((active_sales - np.mean(active_sales)) / stdev)
            anomaly_mask = z_scores >= 2.5

            if anomaly_mask.any():
                non_anomalous = active_sales[~anomaly_mask]

                if len(non_anomalous):
                    replacement_value = np.median(non_anomalous)
                else:
                    replacement_value = np.median(active_sales)

                active_sales = np.where(
                    anomaly_mask,
                    replacement_value,
                    active_sales,
                )

    return float(np.mean(active_sales))


@pytest.fixture(scope="module")
def enriched_data(raw_data):
    df = raw_data.copy()

    df["avg_weekly_sales"] = df.apply(normalized_avg_weekly_sales, axis=1)

    df["is_damaged"] = df["Condition"].eq("Damaged")

    df["is_dead"] = (
        (~df["is_damaged"])
        & ((df["Inventory_Qty"] / df["avg_weekly_sales"]) * 7 > 730)
    )

    df["is_protected"] = (
        df["Category"].eq("Home Automation")
        | (df["Launch_Date"] >= RECENCY_CUTOFF)
    )

    df["effective_margin"] = np.where(
        df["is_damaged"] | df["is_dead"],
        0.0,
        df["Inventory_Qty"] * df["Unit_Margin"],
    )

    df["effective_volume"] = (
        df["Inventory_Qty"] * df["Unit_Volume"]
    )

    return df


def build_ground_truth_solution(df: pd.DataFrame, target_count: int):
    eligible_indices = df.index[~df["is_protected"]].tolist()
    eligible_df = df.loc[eligible_indices].copy().reset_index(drop=True)
    if len(eligible_df) < target_count:
        raise pytest.fail(f"Only {len(eligible_df)} eligible SKUs, cannot build a {target_count}-SKU list.")

    damaged = eligible_df["is_damaged"].astype(int).to_numpy()
    dead = eligible_df["is_dead"].astype(int).to_numpy()

    margin = eligible_df["effective_margin"].to_numpy(dtype=float)
    volume = eligible_df["effective_volume"].to_numpy(dtype=float)

    dependency_edges = []

    sku_to_position = {
        sku: idx for idx, sku in enumerate(eligible_df["SKU"])
    }

    for _, row in eligible_df.iterrows():
        parent_sku = row["Requires_Base_SKU"]

        if pd.notna(parent_sku):
            parent_sku = str(parent_sku).strip()
            if parent_sku and parent_sku in sku_to_position:
                parent_idx = sku_to_position[parent_sku]
                child_idx = sku_to_position[row["SKU"]]
                dependency_edges.append((parent_idx, child_idx))

    def build_constraints(damaged_exact=None, dead_exact=None):
        rows = [np.ones(len(eligible_df))]
        lower = [target_count]
        upper = [target_count]

        if damaged_exact is not None:
            rows.append(damaged.astype(float))
            lower.append(damaged_exact)
            upper.append(damaged_exact)

        if dead_exact is not None:
            rows.append(dead.astype(float))
            lower.append(dead_exact)
            upper.append(dead_exact)

        for parent_idx, child_idx in dependency_edges:
            row = np.zeros(len(eligible_df))
            row[child_idx] = 1.0
            row[parent_idx] = -1.0
            rows.append(row)
            lower.append(-np.inf)
            upper.append(0.0)

        return LinearConstraint(
            np.vstack(rows),
            np.array(lower),
            np.array(upper),
        )


    def solve_problem(cost_vector, damaged_exact=None, dead_exact=None):
        result = milp(
            c=cost_vector,
            constraints=build_constraints(
                damaged_exact=damaged_exact,
                dead_exact=dead_exact,
            ),
            integrality=np.ones(len(eligible_df), dtype=int),
            bounds=Bounds(
                np.zeros(len(eligible_df)),
                np.ones(len(eligible_df)),
            ),
        )

        if result.status != 0:
            pytest.fail(
                f"MILP infeasible for target={target_count}, "
                f"damaged_exact={damaged_exact}, dead_exact={dead_exact}: {result.message}"
            )

        return result.x > 0.5

    damaged_solution = solve_problem(
        -damaged.astype(float)
    )

    optimal_damaged = int(
        damaged[damaged_solution].sum()
    )

    dead_solution = solve_problem(
        -dead.astype(float),
        damaged_exact=optimal_damaged,
    )

    optimal_dead = int(
        dead[dead_solution].sum()
    )

    density_solution = None
    density_value = 0.0

    for _ in range(60):
        density_solution = solve_problem(
            margin - density_value * volume,
            damaged_exact=optimal_damaged,
            dead_exact=optimal_dead,
        )

        total_margin = float(
            margin[density_solution].sum()
        )

        total_volume = float(
            volume[density_solution].sum()
        )

        updated_density = (
            total_margin / total_volume
            if total_volume > 0
            else 0.0
        )

        if abs(updated_density - density_value) < 1e-12:
            density_value = updated_density
            break

        density_value = updated_density

    chosen = eligible_df.loc[density_solution].copy()

    return {
        "chosen": chosen,
        "optimal_damaged": optimal_damaged,
        "optimal_dead": optimal_dead,
        "density": density_value,
        "margin": float(chosen["effective_margin"].sum()),
        "volume": float(chosen["effective_volume"].sum()),
    }


@pytest.fixture(scope="module")
def ground_truth(enriched_data):
    return {
        50: build_ground_truth_solution(enriched_data, 50),
        100: build_ground_truth_solution(enriched_data, 100),
    }


def validate_structure(df: pd.DataFrame, expected_rows: int):
    required_columns = [
        "Priority_Rank",
        "SKU",
        "Product_Name",
    ]

    for col in required_columns:
        assert col in df.columns, f"Missing column: {col}"

    assert len(df) == expected_rows, (
        f"Expected {expected_rows} rows, found {len(df)}"
    )

    assert df["Priority_Rank"].tolist() == list(range(1, expected_rows + 1)), (
        "Priority_Rank should be sequential starting from 1"
    )

    assert df["SKU"].is_unique, (
        "Duplicate SKUs found in liquidation output"
    )


@pytest.mark.parametrize("target", [50, 100])
def test_output_structure(outputs, target):
    validate_structure(outputs[str(target)], target)


@pytest.mark.parametrize("target", [50, 100])
def test_protected_items_not_selected(outputs, enriched_data, target):
    protected_skus = set(
        enriched_data.loc[
            enriched_data["is_protected"],
            "SKU",
        ]
    )

    selected = set(outputs[str(target)]["SKU"])

    overlap = selected & protected_skus

    assert not overlap, (
        f"Protected SKUs were selected: {sorted(overlap)}"
    )


@pytest.mark.parametrize("target", [50, 100])
def test_dependency_rules(outputs, enriched_data, target):
    selected = set(outputs[str(target)]["SKU"])

    dependency_rows = enriched_data[
        enriched_data["Requires_Base_SKU"].notna()
    ].copy()

    dependency_rows["Requires_Base_SKU"] = (
        dependency_rows["Requires_Base_SKU"].astype(str).str.strip()
    )

    # child -> parent
    for _, row in dependency_rows.iterrows():
        child = row["SKU"]
        parent = row["Requires_Base_SKU"]

        if child in selected and parent:
            assert parent in selected, (
                f"Dependent SKU {child} selected without required base SKU {parent}"
            )

    # base -> all dependents
    for base_sku in selected:
        children = set(
            dependency_rows.loc[
                dependency_rows["Requires_Base_SKU"].eq(base_sku),
                "SKU",
            ]
        )
        missing_children = children - selected
        assert not missing_children, (
            f"Base SKU {base_sku} selected without dependent SKUs: "
            f"{sorted(missing_children)}"
        )


@pytest.mark.parametrize("target", [50, 100])
def test_all_selected_items_are_eligible(outputs, enriched_data, target):
    eligible = set(
        enriched_data.loc[
            ~enriched_data["is_protected"],
            "SKU",
        ]
    )

    selected = set(outputs[str(target)]["SKU"])

    assert selected <= eligible, (
        "One or more selected SKUs were not eligible"
    )


@pytest.mark.parametrize("target", [50, 100])
def test_damaged_priority_is_optimal(outputs, enriched_data, ground_truth, target):
    selected = set(outputs[str(target)]["SKU"])

    damaged_selected = int(
        enriched_data[
            enriched_data["SKU"].isin(selected)
        ]["is_damaged"].sum()
    )

    expected = ground_truth[target]["optimal_damaged"]

    assert damaged_selected == expected, (
        f"Expected {expected} damaged SKUs but found {damaged_selected}. "
        "Likely failed to prioritize damaged inventory first."
    )


@pytest.mark.parametrize("target", [50, 100])
def test_dead_stock_priority_is_optimal(outputs, enriched_data, ground_truth, target):
    selected = set(outputs[str(target)]["SKU"])

    dead_selected = int(
        enriched_data[
            enriched_data["SKU"].isin(selected)
        ]["is_dead"].sum()
    )

    expected = ground_truth[target]["optimal_dead"]

    assert dead_selected == expected, (
        f"Expected {expected} dead-stock SKUs but found {dead_selected}. "
        "Likely failed to classify slow-moving inventory correctly."
    )


@pytest.mark.parametrize("target", [50, 100])
def test_margin_density_is_near_optimal(outputs, enriched_data, ground_truth, target):
    selected = set(outputs[str(target)]["SKU"])

    chosen = enriched_data[
        enriched_data["SKU"].isin(selected)
    ].copy()

    total_margin = float(chosen["effective_margin"].sum())
    total_volume = float(chosen["effective_volume"].sum())

    observed_density = (
        total_margin / total_volume
        if total_volume > 0
        else 0.0
    )

    optimal_density = ground_truth[target]["density"]

    assert abs(observed_density - optimal_density) <= 1e-6, (
        "Margin density deviates from optimal solution. "
        "Likely failed to normalize anomaly weeks or optimize correctly."
    )


@pytest.mark.parametrize("target", [50, 100])
def test_output_contains_mostly_damaged_then_dead(outputs, enriched_data, target):
    chosen = enriched_data[
        enriched_data["SKU"].isin(outputs[str(target)]["SKU"])
    ].copy()

    merged = outputs[str(target)].merge(
        chosen[["SKU", "is_damaged", "is_dead", "effective_volume"]],
        on="SKU",
        how="left",
    )

    merged["tier"] = np.select(
        [merged["is_damaged"], merged["is_dead"]],
        [0, 1],
        default=2,
    )

    # Tier order: damaged -> dead -> optimized
    assert merged["tier"].tolist() == sorted(merged["tier"].tolist()), (
        "Priority ordering violated. Damaged inventory should appear before dead stock, "
        "which should appear before optimized inventory."
    )

    # Tie-break inside each tier: total volume descending
    for tier in [0, 1, 2]:
        tier_df = merged[merged["tier"] == tier]
        if len(tier_df) > 1:
            assert tier_df["effective_volume"].tolist() == sorted(
                tier_df["effective_volume"].tolist(),
                reverse=True,
            ), (
                f"Tier {tier} is not sorted by total volume descending."
            )


@pytest.mark.parametrize("target", [50, 100])
def test_recent_launches_not_selected(outputs, enriched_data, target):
    recent_skus = set(
        enriched_data.loc[
            enriched_data["Launch_Date"] >= RECENCY_CUTOFF,
            "SKU",
        ]
    )

    selected = set(outputs[str(target)]["SKU"])

    assert not (selected & recent_skus), (
        "Recent-launch SKUs should not be liquidated."
    )


@pytest.mark.parametrize("target", [50, 100])
def test_home_automation_not_selected(outputs, enriched_data, target):
    home_skus = set(
        enriched_data.loc[
            enriched_data["Category"] == "Home Automation",
            "SKU",
        ]
    )

    selected = set(outputs[str(target)]["SKU"])

    assert not (selected & home_skus), (
        "Home Automation SKUs are protected inventory."
    )

def test_final_report_exists(outputs):
    report = outputs["report"]

    required_columns = [
        "reclaimed_space_100",
        "total_margin_density_100",
        "total_margin_100",
        "reclaimed_space_50",
        "total_margin_density_50",
        "total_margin_50",
    ]

    for col in required_columns:
        assert col in report.columns, f"Missing report column: {col}"


@pytest.mark.parametrize("target", [50, 100])
def test_final_report_metrics(outputs, ground_truth, target):
    report = outputs["report"].iloc[0]

    observed_space = float(report[f"reclaimed_space_{target}"])
    observed_margin = float(report[f"total_margin_{target}"])
    observed_density = float(report[f"total_margin_density_{target}"])

    expected = ground_truth[target]

    assert abs(observed_space - expected["volume"]) <= 1e-6

    assert abs(observed_margin - expected["margin"]) <= 1e-6

    assert abs(observed_density - expected["density"]) <= 1e-6


def test_solution_is_not_naive_average_based(outputs, enriched_data):
    selected = set(outputs["100"]["SKU"])

    chosen = enriched_data[
        enriched_data["SKU"].isin(selected)
    ].copy()

    raw_avg_sales = chosen[SALES_COLUMNS].mean(axis=1)
    normalized_avg_sales = chosen["avg_weekly_sales"]

    deviation = np.abs(raw_avg_sales - normalized_avg_sales)

    assert deviation.gt(2.0).any(), (
        "Selection appears to ignore anomaly normalization entirely."
    )


def test_solution_uses_dependency_reasoning(outputs, enriched_data):
    selected = set(outputs["100"]["SKU"])

    dependency_rows = enriched_data[
        enriched_data["Requires_Base_SKU"].astype(str).str.len() > 0
    ]

    linked_pairs = 0

    for _, row in dependency_rows.iterrows():
        child = row["SKU"]
        parent = str(row["Requires_Base_SKU"]).strip()

        if child in selected and parent in selected:
            linked_pairs += 1

    assert linked_pairs >= 5, (
        "Very few dependency-linked SKU chains were preserved. "
        "Likely ignored dependency constraints."
    )