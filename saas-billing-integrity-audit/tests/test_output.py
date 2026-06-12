"""
saas-billing-integrity-audit — verifier tests.

Reasoning challenges tested:
  1. Amendment credit gap (trap 1): billing system uses monthly proration for all
     credits; daily_prorate amendments are under-credited by the partial-month
     fraction.  Agents that sum credited_amount without re-computing the partial
     fraction will under-report the gap.
  2. Usage overage (trap 2): billing periods reset on the contract start day-of-month
     (anniversary), not the calendar 1st.  cumulative_units_ytd is a red herring
     (runs from Jan 1).  Agents grouping by calendar month will get a different
     (higher) overage because they merge days from two anniversary periods into one
     calendar bucket, artificially inflating some periods.
  3. Reseller revenue (trap 3): invoices record gross amounts; recognised revenue for
     reseller accounts is gross × reseller_margin_pct.  Agents that sum all
     usd_equivalent without applying the margin overstate reseller revenue by 3–5×.
  4. SLA credit stacking (trap 4): when multiple SLA metrics breach in the same
     (account, period), only the highest-severity credit applies.  Agents that sum
     credit_calculated across all rows in a period overcount.

  Data quality:
     - Ghost-voided invoices (void_reason populated, status ≠ 'void') must be excluded.
     - Consolidated invoice children (parent_invoice_id populated) must be excluded.
     - is_test_account=True accounts must be excluded throughout.
     - is_test_usage=True usage records must be excluded.
"""

import json
import math
from calendar import monthrange
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

WORKSPACE_DIR = Path("/workspace") if Path("/workspace").exists() else Path(__file__).parent.parent
DATA_DIR = (
    WORKSPACE_DIR / "data"
    if (WORKSPACE_DIR / "data").exists()
    else Path(__file__).parent.parent / "environment" / "data"
)

AUDIT_FILE   = WORKSPACE_DIR / "billing_audit.csv"
SUMMARY_FILE = WORKSPACE_DIR / "summary.json"

AUDIT_COLS = [
    "discrepancy_type", "account_id", "contract_id", "reference_id",
    "period", "billed_amount_usd", "correct_amount_usd", "discrepancy_usd",
]
BUCKET_KEYS = [
    "discrepancy_bucket_1_usd",
    "discrepancy_bucket_2_usd",
    "discrepancy_bucket_3_usd",
    "discrepancy_bucket_4_usd",
]
SUMMARY_KEYS = set(BUCKET_KEYS) | {
    "total_discrepancy_usd",
    "accounts_with_discrepancies",
    "valid_invoice_count",
}
DISCREPANCY_TYPES = {
    "amendment_undercredit",
    "unbilled_overage",
    "reseller_overstatement",
    "sla_credit_overcount",
}


@pytest.fixture(scope="module")
def ground_truth():
    accts = pd.read_csv(DATA_DIR / "accounts.csv")
    amds  = pd.read_csv(DATA_DIR / "amendments.csv")
    usage = pd.read_csv(DATA_DIR / "usage_records.csv")
    inv   = pd.read_csv(DATA_DIR / "invoices.csv")
    sla   = pd.read_csv(DATA_DIR / "sla_records.csv")
    prods = pd.read_csv(DATA_DIR / "products.csv")

    test_ids = set(accts.loc[accts["is_test_account"] == True, "account_id"])

    # ── valid invoices (no H1 filter — count covers full dataset) ─────────────
    inv_v = inv[~inv["account_id"].isin(test_ids)].copy()
    inv_v = inv_v[inv_v["status"] != "void"]
    inv_v = inv_v[inv_v["void_reason"].isna()]
    inv_v = inv_v[inv_v["parent_invoice_id"].isna()]
    valid_count = int(len(inv_v))

    # ── Trap 1: amendment credit gap ──────────────────────────────────────────
    daily_red = amds[
        (amds["amendment_type"] == "reduction") &
        (amds["credit_method"] == "daily_prorate") &
        (~amds["account_id"].isin(test_ids))
    ].copy()
    gaps = []
    for _, r in daily_red.iterrows():
        eff = date.fromisoformat(str(r["effective_date"]))
        red = float(r["old_mrr"]) - float(r["new_mrr"])
        pd_  = monthrange(eff.year, eff.month)[1]
        rem  = (date(eff.year, eff.month, pd_) - eff).days + 1
        gaps.append(round(red * (rem / pd_), 2))
    amd_gap = float(round(sum(gaps), 2))

    # ── Trap 2: unbilled usage overage (anniversary periods) ──────────────────
    df_u = usage[
        (usage["is_test_usage"] == False) &
        (~usage["account_id"].isin(test_ids))
    ].copy()
    ov_rates = prods.set_index("sku")["overage_rate"].to_dict()
    g2 = (
        df_u.groupby(["contract_id", "sku", "billing_period_id"])
        .agg(tot=("units_consumed", "sum"), incl=("units_included_monthly", "first"),
             acc=("account_id", "first"))
        .reset_index()
    )
    g2["ov"]     = (g2["tot"] - g2["incl"]).clip(lower=0)
    g2["rate"]   = g2["sku"].map(ov_rates).fillna(0.0)
    g2["ov_usd"] = (g2["ov"] * g2["rate"]).round(2)
    use_overage = float(round(g2["ov_usd"].sum(), 2))
    use_accts   = set(g2.loc[g2["ov_usd"] > 0, "acc"])

    # ── Trap 3: reseller revenue overstatement ─────────────────────────────────
    resellers = accts[accts["is_reseller"] == True][["account_id", "reseller_margin_pct"]].copy()
    resellers["reseller_margin_pct"] = pd.to_numeric(
        resellers["reseller_margin_pct"], errors="coerce"
    )
    res_invs = inv_v[inv_v["account_id"].isin(resellers["account_id"])]
    gross_by = (
        res_invs.groupby("account_id")["usd_equivalent"]
        .sum()
        .reset_index()
        .rename(columns={"usd_equivalent": "gross_usd"})
        .merge(resellers, on="account_id")
    )
    gross_by["net_usd"] = (gross_by["gross_usd"] * gross_by["reseller_margin_pct"]).round(2)
    gross_by["over"]    = (gross_by["gross_usd"] - gross_by["net_usd"]).round(2)
    res_over       = float(round(gross_by["over"].sum(), 2))
    reseller_accts = set(resellers["account_id"])

    # ── Trap 4: SLA credit stacking ───────────────────────────────────────────
    df_sla = sla[~sla["account_id"].isin(test_ids)].copy()
    g4 = (
        df_sla.groupby(["account_id", "period_start"])
        .agg(tot=("credit_calculated", "sum"), mx=("credit_calculated", "max"),
             n=("credit_calculated", "count"))
        .reset_index()
    )
    multi         = g4[g4["n"] >= 2]
    sla_overcount = float(round((multi["tot"] - multi["mx"]).sum(), 2))
    sla_accts     = set(multi["account_id"])

    # ── aggregate ─────────────────────────────────────────────────────────────
    amd_accts      = set(daily_red["account_id"])
    all_disc_accts = amd_accts | reseller_accts | sla_accts | use_accts
    total_disc     = float(round(amd_gap + sla_overcount + res_over + use_overage, 2))

    return {
        "valid_invoice_count":                valid_count,
        "amendment_credit_gap_usd":           amd_gap,
        "unbilled_usage_overage_usd":         use_overage,
        "reseller_revenue_overstatement_usd": res_over,
        "sla_credit_overcount_usd":           sla_overcount,
        "total_discrepancy_usd":              total_disc,
        "accounts_with_discrepancies":        int(len(all_disc_accts)),
        "test_ids":                           test_ids,
        "reseller_account_ids":               reseller_accts,
    }


@pytest.fixture(scope="module")
def agent_summary():
    assert SUMMARY_FILE.exists(), f"summary.json not found at {SUMMARY_FILE}"
    with open(SUMMARY_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def agent_audit():
    assert AUDIT_FILE.exists(), f"billing_audit.csv not found at {AUDIT_FILE}"
    return pd.read_csv(AUDIT_FILE)


# ── test_case_01: anti-cheat sentinels ────────────────────────────────────────

def test_case_01_input_sentinels():
    accounts   = pd.read_csv(DATA_DIR / "accounts.csv")
    invoices   = pd.read_csv(DATA_DIR / "invoices.csv")
    amendments = pd.read_csv(DATA_DIR / "amendments.csv")
    sla        = pd.read_csv(DATA_DIR / "sla_records.csv")
    usage      = pd.read_csv(DATA_DIR / "usage_records.csv")

    assert len(accounts)   == 500,   "accounts.csv must not be modified"
    assert len(invoices)   == 3924,  "invoices.csv must not be modified"
    assert len(amendments) == 63,    "amendments.csv must not be modified"
    assert len(sla)        == 710,   "sla_records.csv must not be modified"
    assert len(usage)      == 16833, "usage_records.csv must not be modified"

    assert accounts["is_test_account"].sum() == 20, \
        "Expected exactly 20 test accounts in accounts.csv"


# ── test_case_02: billing_audit.csv structure ─────────────────────────────────

def test_case_02_audit_structure(agent_audit, ground_truth):
    for col in AUDIT_COLS:
        assert col in agent_audit.columns, f"billing_audit.csv missing column: {col}"

    found_types = set(agent_audit["discrepancy_type"].unique())
    assert DISCREPANCY_TYPES.issubset(found_types), \
        f"Missing discrepancy types: {DISCREPANCY_TYPES - found_types}"

    assert (agent_audit["discrepancy_usd"] >= 0).all(), \
        "discrepancy_usd must be non-negative for all rows"

    test_ids  = ground_truth["test_ids"]
    test_rows = agent_audit[agent_audit["account_id"].isin(test_ids)]
    assert len(test_rows) == 0, \
        f"billing_audit.csv contains {len(test_rows)} rows from test accounts"


# ── test_case_03: summary.json structure ──────────────────────────────────────

def test_case_03_summary_structure(agent_summary):
    assert set(agent_summary.keys()) == SUMMARY_KEYS, \
        f"summary.json key mismatch. Extra: {set(agent_summary) - SUMMARY_KEYS}, " \
        f"Missing: {SUMMARY_KEYS - set(agent_summary)}"

    for key in BUCKET_KEYS + ["total_discrepancy_usd"]:
        assert isinstance(agent_summary[key], float), \
            f"summary.json['{key}'] must be a float, got {type(agent_summary[key])}"

    assert isinstance(agent_summary["accounts_with_discrepancies"], int), \
        "accounts_with_discrepancies must be int"
    assert isinstance(agent_summary["valid_invoice_count"], int), \
        "valid_invoice_count must be int"


# ── test_case_04: valid invoice count ─────────────────────────────────────────

def test_case_04_valid_invoice_count(agent_summary, ground_truth):
    expected = ground_truth["valid_invoice_count"]
    got      = agent_summary["valid_invoice_count"]
    # QC NOTE: exact assertion; both ghost-voided and consolidated-child exclusions
    # produce a deterministic result from the fixed SEED=42 dataset.
    assert got == expected, \
        f"valid_invoice_count: expected {expected}, got {got}. " \
        f"Check: status='void', void_reason populated (ghost-voided), " \
        f"parent_invoice_id populated (consolidated children), and test account exclusions."


# ── test_case_05: amendment credit gap (trap 1) ───────────────────────────────

def test_case_05_amendment_credit_gap(agent_audit, ground_truth):
    expected = ground_truth["amendment_credit_gap_usd"]
    got = float(round(
        agent_audit[agent_audit["discrepancy_type"] == "amendment_undercredit"][
            "discrepancy_usd"
        ].sum(),
        2,
    ))
    assert math.isclose(got, expected, abs_tol=50.0), \
        f"amendment_undercredit total: expected ~{expected:.2f}, got {got:.2f}. " \
        f"daily_prorate amendments are under-credited by reduction × " \
        f"(remaining_days_in_month / days_in_month); credited_amount omits that fraction."


# ── test_case_06: SLA credit overcount (trap 4) ───────────────────────────────

def test_case_06_sla_credit_overcount(agent_audit, ground_truth):
    expected = ground_truth["sla_credit_overcount_usd"]
    got = float(round(
        agent_audit[agent_audit["discrepancy_type"] == "sla_credit_overcount"][
            "discrepancy_usd"
        ].sum(),
        2,
    ))
    assert math.isclose(got, expected, abs_tol=200.0), \
        f"sla_credit_overcount total: expected ~{expected:.2f}, got {got:.2f}. " \
        f"When multiple metrics breach in the same (account, period), only the " \
        f"highest-severity credit applies — not the sum."


# ── test_case_07: reseller revenue overstatement (trap 3) ─────────────────────

def test_case_07_reseller_overstatement(agent_audit, ground_truth):
    expected = ground_truth["reseller_revenue_overstatement_usd"]
    got = float(round(
        agent_audit[agent_audit["discrepancy_type"] == "reseller_overstatement"][
            "discrepancy_usd"
        ].sum(),
        2,
    ))
    assert math.isclose(got, expected, abs_tol=500.0), \
        f"reseller_overstatement total: expected ~{expected:.2f}, got {got:.2f}. " \
        f"Recognised revenue = gross × reseller_margin_pct; " \
        f"summing usd_equivalent without applying the margin overstates revenue."


# ── test_case_08: unbilled usage overage (trap 2) ─────────────────────────────

def test_case_08_unbilled_usage_overage(agent_audit, ground_truth):
    expected = ground_truth["unbilled_usage_overage_usd"]
    got = float(round(
        agent_audit[agent_audit["discrepancy_type"] == "unbilled_overage"][
            "discrepancy_usd"
        ].sum(),
        2,
    ))
    # abs_tol=500 distinguishes the correct anniversary-period answer (~26821)
    # from the calendar-month grouping error (~30413).
    assert math.isclose(got, expected, abs_tol=500.0), \
        f"unbilled_overage total: expected ~{expected:.2f}, got {got:.2f}. " \
        f"Overage must be computed per billing_period_id (anniversary window), " \
        f"not per calendar month. cumulative_units_ytd is a YTD running total, " \
        f"not a per-period ceiling."


# ── test_case_09: summary.json totals and bucket consistency ──────────────────

def test_case_09_totals(agent_summary, agent_audit, ground_truth):
    exp_total = ground_truth["total_discrepancy_usd"]
    exp_accts = ground_truth["accounts_with_discrepancies"]
    got_total = agent_summary["total_discrepancy_usd"]
    got_accts = agent_summary["accounts_with_discrepancies"]

    assert math.isclose(got_total, exp_total, abs_tol=1500.0), \
        f"total_discrepancy_usd: expected ~{exp_total:.2f}, got {got_total:.2f}"

    assert abs(got_accts - exp_accts) <= 5, \
        f"accounts_with_discrepancies: expected {exp_accts}, got {got_accts}"

    # The 4 buckets must sum to total_discrepancy_usd
    bucket_sum = sum(agent_summary[k] for k in BUCKET_KEYS)
    assert math.isclose(bucket_sum, got_total, abs_tol=1.0), \
        f"discrepancy buckets sum to {bucket_sum:.2f} but total_discrepancy_usd is {got_total:.2f}"

    # Each bucket must correspond to one discrepancy_type in billing_audit.csv —
    # verify the multiset of bucket values matches the multiset of per-type sums.
    type_totals = sorted(
        round(float(v), 2)
        for v in agent_audit.groupby("discrepancy_type")["discrepancy_usd"].sum()
    )
    bucket_vals = sorted(round(agent_summary[k], 2) for k in BUCKET_KEYS)
    for tv, bv in zip(type_totals, bucket_vals):
        assert math.isclose(tv, bv, abs_tol=500.0), \
            f"Bucket values {bucket_vals} do not match per-type sums {type_totals}. " \
            f"Each bucket must equal the total discrepancy_usd for one discrepancy_type."


# ── test_case_10: reseller rows individually correct ─────────────────────────

def test_case_10_reseller_rows(agent_audit, ground_truth):
    """
    For each reseller account the audit rows must correctly report
    billed_amount_usd (gross) and correct_amount_usd (net = gross × margin).
    Tests that the agent applied per-account margins, not a global average.
    """
    reseller_ids = ground_truth["reseller_account_ids"]
    res_rows = agent_audit[
        agent_audit["discrepancy_type"] == "reseller_overstatement"
    ].copy()

    assert len(res_rows) >= len(reseller_ids) - 2, \
        f"Expected ~{len(reseller_ids)} reseller rows, got {len(res_rows)}"

    unknown = set(res_rows["account_id"]) - reseller_ids
    assert len(unknown) == 0, \
        f"reseller_overstatement rows for non-reseller accounts: {unknown}"

    for _, row in res_rows.iterrows():
        expected_disc = round(row["billed_amount_usd"] - row["correct_amount_usd"], 2)
        assert math.isclose(row["discrepancy_usd"], expected_disc, abs_tol=1.0), \
            f"Row {row['reference_id']}: discrepancy_usd {row['discrepancy_usd']} " \
            f"≠ billed − correct = {expected_disc}"

    valid_ratio = res_rows[res_rows["correct_amount_usd"] > 0].copy()
    valid_ratio["ratio"] = valid_ratio["billed_amount_usd"] / valid_ratio["correct_amount_usd"]
    pct_above = (valid_ratio["ratio"] > 1.5).mean()
    assert pct_above >= 0.85, \
        f"Only {pct_above:.0%} of reseller rows show gross/net > 1.5×; " \
        f"reseller_margin_pct may not have been applied correctly."
