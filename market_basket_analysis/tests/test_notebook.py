"""
Unit tests for Market Basket Association Analysis benchmark.

Seven traps embedded in the data:

  1. Return transactions (quantity < 0)
     Transaction log includes all POS events, including returns.
     Returns must be excluded — a returned item was removed from a basket,
     not purchased.

  2. Item ID remapping after POS migration
     Item IDs reset at migration date 2024-07-01.
     The same product carries a different item_id before and after migration.
     Post-migration rows have item_name suffixed with '[v2]' in items.csv.
     canonical_item_id is the only stable product identifier.

  3. Promotional bundle transactions (transaction_type == 'promo_bundle')
     Pre-assembled promo gift baskets artificially inflate item co-occurrence.
     Only organic customer purchasing behavior should be analysed.

  4. Split baskets (basket_id spans multiple transaction_ids)
     Carts exceeding the system item limit were split across multiple
     transaction_ids at checkout.  basket_id is the correct unit of analysis,
     not transaction_id.

  5. Duplicate rows from POS double-logging
     Some transaction rows were logged twice during the POS migration.
     Exact duplicate rows must be removed before analysis.

  6. Basket contamination (basket_id spans both regular and promo_bundle types)
     Some basket_ids contain both organic customer purchases and promotional
     bundle items under the same basket_id.  A naive row-filter removes the
     bundle rows but keeps the regular items — which were influenced by the
     bundle placement and are not purely organic.  The entire basket must be
     excluded when any promo_bundle row is detected for that basket_id.

  7. Within-basket return netting
     Some items are purchased and returned within the same basket_id (customer
     changed their mind at checkout).  Filtering quantity < 0 removes the
     return row but leaves the original purchase row as a ghost.  Quantities
     must be netted per (basket_id, canonical_item_id); items with net <= 0
     are not actually purchased and must be excluded from basket composition.
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ============================================================
# Paths
# ============================================================
if Path("/workspace/data").exists():
    DATA_DIR  = Path("/workspace/data")
    WORKSPACE = Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR  = Path("../environment/data")
    WORKSPACE = Path("..")
elif Path("environment/data").exists():
    DATA_DIR  = Path("environment/data")
    WORKSPACE = Path(".")
else:
    DATA_DIR  = Path("data")
    WORKSPACE = Path(".")

VARIABLES_PATH    = Path(os.environ.get("VARIABLES_PATH", "/logs/verifier/notebook_variables.json"))
RULES_PATH        = WORKSPACE / "association_rules.csv"
TRANSACTIONS_PATH = DATA_DIR  / "transactions.csv"
ITEMS_PATH        = DATA_DIR  / "items.csv"

MIN_SUPPORT    = 0.03
MIN_CONFIDENCE = 0.30

REQUIRED_COLUMNS = ["antecedent", "consequent", "support", "confidence", "lift"]


# ============================================================
# Reference Pipeline
# ============================================================

def _compute_ground_truth():
    mlxtend = pytest.importorskip("mlxtend")
    from mlxtend.frequent_patterns  import apriori, association_rules
    from mlxtend.preprocessing      import TransactionEncoder

    txn   = pd.read_csv(TRANSACTIONS_PATH)
    items = pd.read_csv(ITEMS_PATH)

    raw_row_count = len(txn)
    return_count  = int((txn["quantity"] < 0).sum())   # Trap 1 + Trap 7 (in-basket returns)

    # --- Trap 1 + 7: remove all negative-quantity rows ---
    clean = txn[txn["quantity"] > 0].copy()

    # --- Trap 3 + 6: basket-level contamination exclusion ---
    contaminated_ids = set(txn[txn["transaction_type"] == "promo_bundle"]["basket_id"].unique())
    contaminated_basket_count = int(len(contaminated_ids))
    clean = clean[~clean["basket_id"].isin(contaminated_ids)]

    # --- Trap 5: deduplicate ---
    clean = clean.drop_duplicates()

    # --- Trap 4: count split baskets ---
    split_count = int(
        clean.groupby("basket_id")["transaction_id"].nunique().gt(1).sum()
    )

    # --- Trap 2: resolve canonical item ID ---
    clean = clean.merge(
        items[["item_id", "canonical_item_id"]],
        on="item_id",
        how="left",
    )
    clean = clean.dropna(subset=["canonical_item_id"])
    clean["canonical_item_id"] = clean["canonical_item_id"].astype(int)

    canonical_names = (
        items[items["item_id"] == items["canonical_item_id"]]
        .set_index("canonical_item_id")["item_name"]
    )

    # --- Trap 7: within-basket return netting ---
    # Net quantities must be computed from ALL rows (including returns that were
    # removed by the qty > 0 filter) to detect ghost purchases.
    txn_canonical = txn.merge(
        items[["item_id", "canonical_item_id"]],
        on="item_id",
        how="left",
    ).dropna(subset=["canonical_item_id"])
    txn_canonical["canonical_item_id"] = txn_canonical["canonical_item_id"].astype(int)
    txn_nc = txn_canonical[~txn_canonical["basket_id"].isin(contaminated_ids)]
    net_qty = txn_nc.groupby(["basket_id", "canonical_item_id"])["quantity"].sum()
    valid_pairs = net_qty[net_qty > 0].reset_index()[["basket_id", "canonical_item_id"]]
    clean = clean.merge(valid_pairs, on=["basket_id", "canonical_item_id"])

    # --- Trap 4: group by basket_id, not transaction_id ---
    baskets = (
        clean.groupby("basket_id")["canonical_item_id"]
        .apply(lambda x: sorted(set(x)))
        .reset_index()
    )
    baskets = baskets[baskets["canonical_item_id"].apply(len) >= 2]
    valid_basket_count = int(len(baskets))

    # --- Association rules ---
    te     = TransactionEncoder()
    matrix = te.fit_transform(baskets["canonical_item_id"].tolist())
    df_enc = pd.DataFrame(matrix, columns=te.columns_)
    df_enc = df_enc.rename(
        columns={cid: canonical_names.get(cid, str(cid)) for cid in te.columns_}
    )

    freq = apriori(df_enc, min_support=MIN_SUPPORT, use_colnames=True)
    try:
        rules = association_rules(
            freq, metric="confidence", min_threshold=MIN_CONFIDENCE,
            num_itemsets=len(freq)
        )
    except TypeError:
        rules = association_rules(
            freq, metric="confidence", min_threshold=MIN_CONFIDENCE
        )

    rules = rules[
        (rules["antecedents"].apply(len) == 1) &
        (rules["consequents"].apply(len) == 1)
    ].copy()
    rules["antecedent"] = rules["antecedents"].apply(lambda x: next(iter(x)))
    rules["consequent"]  = rules["consequents"].apply(lambda x: next(iter(x)))
    rules = (
        rules[["antecedent", "consequent", "support", "confidence", "lift"]]
        .sort_values(["lift", "antecedent"], ascending=[False, True])
        .reset_index(drop=True)
    )

    return {
        "raw_row_count":                raw_row_count,
        "return_rows_removed":          return_count,
        "contaminated_baskets_removed": contaminated_basket_count,
        "split_basket_count":           split_count,
        "valid_basket_count":           valid_basket_count,
        "rules":                        rules,
    }


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def ground_truth():
    return _compute_ground_truth()


@pytest.fixture(scope="module")
def notebook_vars():
    if not VARIABLES_PATH.exists():
        pytest.skip("notebook_variables.json not found")
    with open(VARIABLES_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def rules_df():
    if not RULES_PATH.exists():
        pytest.fail(f"association_rules.csv not found at {RULES_PATH}")
    return pd.read_csv(RULES_PATH)


# ============================================================
# Validation Variable Tests
# ============================================================

def test_raw_row_count(notebook_vars, ground_truth):
    """Verify transactions.csv was fully loaded."""
    assert "raw_row_count" in notebook_vars
    assert notebook_vars["raw_row_count"] == ground_truth["raw_row_count"]


def test_return_rows_removed(notebook_vars, ground_truth):
    """Verify return transactions (quantity < 0) were identified and removed."""
    assert "return_rows_removed" in notebook_vars
    expected = ground_truth["return_rows_removed"]
    actual   = notebook_vars["return_rows_removed"]
    assert actual == expected, (
        f"Expected return_rows_removed={expected}, got {actual}. "
        "Count all rows with quantity < 0, including those sharing a basket_id "
        "with regular purchase rows."
    )


def test_contaminated_baskets_removed(notebook_vars, ground_truth):
    """Verify basket_ids containing promotional bundle transactions were excluded."""
    assert "contaminated_baskets_removed" in notebook_vars, (
        "Variable 'contaminated_baskets_removed' not found. "
        "This should be the number of basket_ids (not rows) excluded because "
        "they contained at least one promotional bundle transaction."
    )
    expected = ground_truth["contaminated_baskets_removed"]
    actual   = notebook_vars["contaminated_baskets_removed"]
    assert actual == expected, (
        f"Expected contaminated_baskets_removed={expected}, got {actual}. "
        "Count distinct basket_ids that have any promo_bundle transaction — "
        "not the number of promo_bundle rows. Some basket_ids contain both "
        "regular and bundle transactions under the same basket_id."
    )


def test_split_basket_count(notebook_vars, ground_truth):
    """Verify split baskets (basket_id spanning multiple transaction_ids) were detected."""
    assert "split_basket_count" in notebook_vars
    expected = ground_truth["split_basket_count"]
    actual   = notebook_vars["split_basket_count"]
    assert actual == expected, (
        f"Expected split_basket_count={expected}, got {actual}. "
        "Count basket_ids associated with more than one transaction_id "
        "after excluding contaminated baskets."
    )


def test_valid_basket_count(notebook_vars, ground_truth):
    """Verify the number of baskets passed to the Apriori algorithm is correct."""
    assert "valid_basket_count" in notebook_vars, (
        "Variable 'valid_basket_count' not found. "
        "This should be the number of baskets with at least 2 distinct products "
        "that were passed to the Apriori algorithm."
    )
    expected = ground_truth["valid_basket_count"]
    actual   = notebook_vars["valid_basket_count"]
    assert actual == expected, (
        f"Expected valid_basket_count={expected}, got {actual}. "
        "Ensure contaminated baskets are fully excluded, within-basket returns "
        "are netted before grouping, and only baskets with ≥2 distinct canonical "
        "products are counted."
    )


# ============================================================
# Output Structure Tests
# ============================================================

def test_rules_file_exists():
    """Verify association_rules.csv was saved."""
    assert RULES_PATH.exists(), (
        f"association_rules.csv not found at {RULES_PATH}"
    )


def test_rules_columns(rules_df):
    """Verify required columns are present."""
    for col in REQUIRED_COLUMNS:
        assert col in rules_df.columns, f"Missing required column: {col}"


def test_rules_not_empty(rules_df):
    """Verify the output contains association rules."""
    assert len(rules_df) > 0, "association_rules.csv is empty"


def test_rules_sorted_by_lift(rules_df):
    """Output must be sorted descending by lift."""
    vals = rules_df["lift"].values
    assert np.all(vals[:-1] >= vals[1:]), (
        "association_rules.csv must be sorted descending by lift"
    )


# ============================================================
# Rule Quality Tests
# ============================================================

def test_top_rule_identity(rules_df, ground_truth):
    """Top rule antecedent and consequent match reference."""
    ref    = ground_truth["rules"].iloc[0]
    actual = rules_df.sort_values("lift", ascending=False).iloc[0]

    assert actual["antecedent"] == ref["antecedent"], (
        f"Top rule antecedent: expected '{ref['antecedent']}', "
        f"got '{actual['antecedent']}'. "
        "Ensure rules are sorted by lift descending, with ties broken "
        "alphabetically by antecedent. Also check that canonical_item_id "
        "is used for product grouping, contaminated baskets are fully excluded, "
        "and within-basket returns are netted."
    )
    assert actual["consequent"] == ref["consequent"], (
        f"Top rule consequent: expected '{ref['consequent']}', "
        f"got '{actual['consequent']}'."
    )


def test_top_rule_lift(rules_df, ground_truth):
    """Top rule lift is approximately correct."""
    ref_lift    = float(ground_truth["rules"].iloc[0]["lift"])
    actual_lift = float(
        rules_df.sort_values("lift", ascending=False).iloc[0]["lift"]
    )
    assert abs(actual_lift - ref_lift) < 1.0, (
        f"Top rule lift: expected ~{ref_lift:.3f}, got {actual_lift:.3f}. "
        "Inflated lift indicates remapped items were not resolved via "
        "canonical_item_id, or contaminated baskets were not fully excluded."
    )


def test_pbjelly_rule_support(rules_df, ground_truth):
    """Peanut Butter → Jelly rule has approximately correct support."""
    ref_rules = ground_truth["rules"]
    ref_row   = ref_rules[
        (ref_rules["antecedent"] == "Peanut Butter") &
        (ref_rules["consequent"] == "Jelly")
    ]
    if ref_row.empty:
        pytest.skip("Peanut Butter → Jelly not in reference rules")

    expected_support = float(ref_row.iloc[0]["support"])

    actual_row = rules_df[
        (rules_df["antecedent"] == "Peanut Butter") &
        (rules_df["consequent"] == "Jelly")
    ]
    assert not actual_row.empty, (
        "Peanut Butter → Jelly rule not found. "
        "Check canonical_item_id resolution and that support threshold is met."
    )
    actual_support = float(actual_row.iloc[0]["support"])
    assert abs(actual_support - expected_support) < 0.015, (
        f"Peanut Butter → Jelly support: expected ~{expected_support:.4f}, "
        f"got {actual_support:.4f}. "
        "Inflated support suggests within-basket returns were not netted: "
        "some Peanut Butter purchases were returned in the same basket, "
        "and the ghost purchase rows remain after a simple quantity < 0 filter."
    )
