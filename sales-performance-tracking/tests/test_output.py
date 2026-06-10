import json
import os
import pytest
import pandas as pd
from collections import defaultdict, deque
from pathlib import Path

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

REPORT_PATH  = WORKSPACE_DIR / "monthly_rep_performance.csv"
SUMMARY_PATH = WORKSPACE_DIR / "summary.json"

CUTOFF_DATE = pd.Timestamp("2024-04-05")

MONTH_BOUNDS = {
    2: (pd.Timestamp("2024-02-01"), pd.Timestamp("2024-02-29")),
    3: (pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-31")),
    4: (pd.Timestamp("2024-04-01"), pd.Timestamp("2024-04-30")),
}

# ─── Ground-truth helpers ─────────────────────────────────────────────────────

def _build_fx_graph(fx_rows):
    g = defaultdict(dict)
    for _, row in fx_rows.iterrows():
        g[row["base_currency"]][row["quote_currency"]] = float(row["rate"])
        g[row["quote_currency"]][row["base_currency"]] = 1.0 / float(row["rate"])
    return g


def _bfs_to_usd(graph, currency):
    if currency == "USD":
        return 1.0
    visited = {currency}
    q = deque([(currency, 1.0)])
    while q:
        curr, rate = q.popleft()
        for nxt, edge in graph.get(curr, {}).items():
            nr = rate * edge
            if nxt == "USD":
                return nr
            if nxt not in visited:
                visited.add(nxt)
                q.append((nxt, nr))
    return None


_fx_cache = {}

def _get_fx_rate(fx_rates, close_date, currency):
    if currency == "USD":
        return 1.0
    key = (close_date, currency)
    if key in _fx_cache:
        return _fx_cache[key]
    avail = fx_rates[fx_rates["date"] <= close_date]
    if avail.empty:
        _fx_cache[key] = 1.0
        return 1.0
    latest = avail["date"].max()
    graph  = _build_fx_graph(fx_rates[fx_rates["date"] == latest])
    val    = _bfs_to_usd(graph, currency)
    result = val if val is not None else 1.0
    _fx_cache[key] = result
    return result


def _best_multiplier(product_line, contract_months, deal_source):
    m = 1.0
    if product_line  == "Enterprise Suite": m = max(m, 1.5)
    if contract_months >= 24:              m = max(m, 1.3)
    if deal_source    == "Outbound":        m = max(m, 1.2)
    return m


def _resolve_attribution(deal_row, team, rep_region):
    ae  = team.get("Primary_AE")
    sdr = team.get("SDR")
    ov  = team.get("Overlay_Specialist")
    if not ae:
        return {}
    shares = {ae: 1.0}
    if sdr:
        shares[sdr] = 0.20
        shares[ae]  = 0.80
    if deal_row["product_line"] == "Enterprise Suite" and ov:
        shares[ov]  = 0.15
        shares[sdr] = 0.15 if sdr else 0.0
        shares[ae]  = 0.70 if sdr else 0.85
    if rep_region.get(ae) == "EMEA":
        scaled = {"R999": 0.05}
        for rep, s in shares.items():
            scaled[rep] = s * 0.95
        shares = scaled
    return shares


def _monthly_tranches(arr, close_month, cancel_month):
    third  = arr / 3.0
    months = [close_month, close_month + 1, close_month + 2]
    result = []
    for i, m in enumerate(months):
        if m < 2 or m > 4:
            continue
        tranche = third if i < 2 else (arr - 2 * third)
        if cancel_month < m:
            pass
        elif cancel_month == m:
            clawback = sum(
                (third if j < 2 else (arr - 2 * third))
                for j in range(i) if 2 <= months[j] <= 4
            )
            if clawback > 0:
                result.append((m, -clawback))
        else:
            result.append((m, tranche))
    return result


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def raw_data():
    return (
        pd.read_csv(DATA_DIR / "deals.csv",         parse_dates=["close_date"]),
        pd.read_csv(DATA_DIR / "reps.csv",          parse_dates=["hire_date"]),
        pd.read_csv(DATA_DIR / "account_teams.csv"),
        pd.read_csv(DATA_DIR / "quotas.csv",        parse_dates=["period_start"]),
        pd.read_csv(DATA_DIR / "cancellations.csv", parse_dates=["filed_date", "cancelled_date"]),
        pd.read_csv(DATA_DIR / "fx_rates.csv",      parse_dates=["date"]),
    )


@pytest.fixture(scope="module")
def report_df():
    return pd.read_csv(REPORT_PATH) if REPORT_PATH.exists() else None


@pytest.fixture(scope="module")
def summary_json():
    if not SUMMARY_PATH.exists():
        return None
    with open(SUMMARY_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def expected_report(raw_data):
    """Ground-truth report computed directly from raw input files."""
    deals, reps, account_teams, quotas, cancellations, fx_rates = raw_data

    rep_region = reps.set_index("rep_id")["region"].to_dict()

    teams = defaultdict(lambda: {"Primary_AE": None, "SDR": None, "Overlay_Specialist": None})
    for _, row in account_teams.iterrows():
        if row["role"] in ("Primary_AE", "SDR", "Overlay_Specialist"):
            teams[row["account_id"]][row["role"]] = row["rep_id"]

    won = deals[deals["stage"] == "closed_won"].copy()
    won["fx"]      = won.apply(lambda r: _get_fx_rate(fx_rates, r["close_date"], r["currency"]), axis=1)
    won["arr_usd"] = (won["total_contract_value"] / won["contract_months"] * 12.0
                      * won["fx"]
                      * won.apply(lambda r: _best_multiplier(r["product_line"], r["contract_months"], r["deal_source"]), axis=1))

    approved  = cancellations[cancellations["status"] == "approved"]
    pre       = approved[approved["filed_date"] <= CUTOFF_DATE].sort_values("filed_date")
    valid     = pre.drop_duplicates(["region", "deal_id"], keep="last")
    cancel_map = valid.set_index(["region", "deal_id"])["cancelled_date"].to_dict()

    ledger = defaultdict(float)
    for _, deal in won.iterrows():
        shares = _resolve_attribution(deal, teams[deal["account_id"]], rep_region)
        cd     = deal["close_date"].month
        cdate  = cancel_map.get((deal["region"], deal["deal_id"]))
        cm     = cdate.month if cdate else 99
        for rep, share in shares.items():
            if share > 0:
                for m, net in _monthly_tranches(deal["arr_usd"] * share, cd, cm):
                    ledger[(rep, m)] += net

    q1        = quotas[quotas["period_start"] == pd.Timestamp("2024-02-01")]
    rep_quota = q1.set_index("rep_id")["quota_usd"].to_dict()

    records = []
    for _, row in reps.iterrows():
        hire   = row["hire_date"]
        base_m = rep_quota.get(row["rep_id"], 0.0) / 3.0
        for m, (start, end) in MONTH_BOUNDS.items():
            days_in = (end - start).days + 1
            active  = 0 if hire > end else (days_in if hire <= start else (end - hire).days + 1)
            records.append({"rep_id": row["rep_id"], "rep_name": row["rep_name"],
                             "region": row["region"], "month": m,
                             "base_quota": base_m * (active / days_in)})

    monthly_q = pd.DataFrame(records)
    rep_ids   = set(reps["rep_id"])
    results   = []
    for rep_id, group in monthly_q.groupby("rep_id"):
        shortfall = 0.0
        for _, row in group.sort_values("month").iterrows():
            net   = ledger.get((rep_id, row["month"]), 0.0)
            eff_q = row["base_quota"] + shortfall
            shortfall = max(0.0, eff_q - net)
            att   = round(net / eff_q * 100, 2) if eff_q > 0 else 0.0
            results.append({"rep_id": rep_id, "rep_name": row["rep_name"],
                             "region": row["region"], "month": row["month"],
                             "base_quota": round(row["base_quota"], 2),
                             "effective_quota": round(eff_q, 2),
                             "net_arr_usd": round(net, 2), "attainment_pct": att})

    df = pd.DataFrame(results)
    return df[df["rep_id"].isin(rep_ids)].sort_values(["rep_id", "month"]).reset_index(drop=True)


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_01_files_and_schema(report_df, summary_json):
    """Both output files exist and the CSV has the required columns."""
    assert REPORT_PATH.exists(),  "monthly_rep_performance.csv not found"
    assert SUMMARY_PATH.exists(), "summary.json not found"
    assert report_df is not None
    for col in ["rep_id", "rep_name", "region", "month",
                "base_quota", "effective_quota", "net_arr_usd", "attainment_pct"]:
        assert col in report_df.columns, f"Missing column: {col}"
    assert "total_net_arr_usd"    in summary_json
    assert "total_base_quota_usd" in summary_json


def test_02_row_count(report_df):
    """Exactly 200 reps × 3 months = 600 rows, one per rep per month."""
    assert report_df is not None
    assert len(report_df) == 600, f"Expected 600 rows, got {len(report_df)}"
    assert set(report_df["month"].unique()) == {2, 3, 4}


def test_03_sentinel_input_data():
    """Input files must not be modified; key structural properties are anchored."""
    deals = pd.read_csv(DATA_DIR / "deals.csv")
    assert len(deals) == 5000, f"deals.csv must not be modified (expected 5000 rows, got {len(deals)})"

    at = pd.read_csv(DATA_DIR / "account_teams.csv")
    r081_ae = set(at[(at["rep_id"] == "R081") & (at["role"] == "Primary_AE")]["account_id"])
    assert {"ACC0760", "ACC0420", "ACC0979", "ACC0277", "ACC0230"}.issubset(r081_ae), \
        "account_teams.csv modified — R081 must be Primary_AE for the 5 APAC accounts"

    fx = pd.read_csv(DATA_DIR / "fx_rates.csv", parse_dates=["date"])
    assert (fx["date"].dt.day == 1).all(), \
        "fx_rates.csv modified — rates must be monthly (1st of each month) only"


def test_04_summary_totals(summary_json, expected_report):
    """total_net_arr_usd and total_base_quota_usd match ground truth.

    This test catches both FX as-of-date errors (Trap 1) and EMEA-AE attribution
    errors (Trap 2) since both distort the aggregate ARR.
    """
    assert summary_json is not None
    exp_arr   = round(expected_report["net_arr_usd"].sum(),   2)
    exp_quota = round(expected_report["base_quota"].sum(),    2)
    assert abs(summary_json["total_net_arr_usd"]    - exp_arr)   <= 0.5, \
        f"total_net_arr_usd {summary_json['total_net_arr_usd']:.2f} != expected {exp_arr:.2f}"
    assert abs(summary_json["total_base_quota_usd"] - exp_quota) <= 0.5, \
        f"total_base_quota_usd {summary_json['total_base_quota_usd']:.2f} != expected {exp_quota:.2f}"


def test_05_emea_ae_global_vp_attribution(report_df, expected_report):
    """Trap 2 — Global_VP 5% cut based on Primary_AE's home region, not deal region.

    R081 (EMEA rep) is Primary_AE for both EMEA accounts and 5 APAC accounts.
    The 5% cut must apply to *all* R081 Primary_AE deals regardless of which
    region's CRM recorded the deal. A model checking deal['region'] == 'EMEA'
    will skip the cut on APAC-region deals and give R081 too much ARR.
    """
    assert report_df is not None
    for month in [2, 3, 4]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R081") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R081") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R081 month {month}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Global_VP cut must trigger on Primary_AE's home region, not deal region."


def test_06_cancellation_clawback_in_cancelled_month(report_df, expected_report):
    """Trap 4 — clawback is recorded in cancelled_date month, not filed_date month.

    Deal (EMEA, 843): filed 2024-02-28, cancelled_date 2024-03-01.
    The clawback of the already-recognised February tranche must appear in
    Month 3 (March). A model using filed_date.month would shift it to Month 2,
    making R120's February too low and March too high.
    """
    assert report_df is not None
    for month in [2, 3]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R120") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R120") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R120 month {month}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Clawback must be recorded in cancelled_date month, not filed_date month."


def test_07_cancellation_composite_key(report_df, expected_report):
    """Trap 3 — cancellation key is (region, deal_id); deal_id alone is not unique.

    (APAC, deal_id=404) is cancelled. A separate deal with deal_id=404 exists in
    AMER (owned by R078) with no cancellation. A model matching cancellations by
    deal_id alone incorrectly voids R078's AMER deal.
    """
    assert report_df is not None
    for month in [2, 3, 4]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R078") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R078") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R078 month {month}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Cancellation (APAC, 404) must not affect the separate AMER deal with the same deal_id."


def test_08_new_hire_quota_proration(report_df, expected_report):
    """R040 hired 2024-02-21: active days in February = 9 (Feb 21–29, inclusive).
    base_quota = (quota_usd / 3) × (9 / 29). Off-by-one in active-day counting
    is a common failure.
    """
    assert report_df is not None
    exp = expected_report.loc[
        (expected_report["rep_id"] == "R040") & (expected_report["month"] == 2),
        "base_quota"].iloc[0]
    act = report_df.loc[
        (report_df["rep_id"] == "R040") & (report_df["month"] == 2),
        "base_quota"].iloc[0]
    assert abs(act - exp) <= 0.01, \
        f"R040 Feb base_quota: got {act:.2f}, expected {exp:.2f}. " \
        "Active days = (month_end - hire_date).days + 1 (inclusive of hire date)."


def test_09_quota_rollover(report_df):
    """Quota shortfall from month M is added to effective quota in month M+1.
    This must cascade: shortfall from month 2 adds to month 3's effective quota,
    and shortfall from month 3 adds to month 4's effective quota.
    """
    assert report_df is not None
    df = report_df.sort_values(["rep_id", "month"])
    has_rollover = False
    for rep_id, group in df.groupby("rep_id"):
        rows = {r["month"]: r for _, r in group.iterrows()}
        for m in [2, 3]:
            if m not in rows or m + 1 not in rows:
                continue
            shortfall    = max(0.0, rows[m]["effective_quota"] - rows[m]["net_arr_usd"])
            expected_eff = rows[m + 1]["base_quota"] + shortfall
            actual_eff   = rows[m + 1]["effective_quota"]
            assert abs(actual_eff - expected_eff) <= 0.02, \
                f"{rep_id} month {m+1}: effective_quota {actual_eff:.2f} != " \
                f"base_quota {rows[m+1]['base_quota']:.2f} + shortfall {shortfall:.2f} = {expected_eff:.2f}"
            if shortfall > 0:
                has_rollover = True
    assert has_rollover, "No quota shortfalls found — rollover logic was not exercised"


def test_10_fx_as_of_date_and_triangulation(report_df, expected_report, raw_data):
    """Trap 1 — FX rates are monthly; mid-month closes must use the most recent prior rate,
    and missing direct USD pairs require chaining through intermediate currencies.

    Deal (AMER, 1509) closes 2024-02-16 in CAD. No rate exists for Feb 16.
    The correct lookup uses the 2024-02-01 rate and chains: CAD→EUR→USD (2 hops).
    A model doing an exact-date lookup gets no match and falls back to 1.0, treating
    CAD as USD — overstating this deal's February tranche by ~$28,128 (~30%).
    R013 is the sole Primary_AE for this account with no SDR or Overlay, so the
    error flows directly into their Month 2 net_arr_usd.
    """
    assert report_df is not None
    _, _, _, _, _, fx_rates = raw_data

    # Verify the as-of rate for this specific deal exists on Feb 1 (not Feb 16)
    feb_16_rows = fx_rates[fx_rates["date"] == pd.Timestamp("2024-02-16")]
    assert len(feb_16_rows) == 0, "fx_rates must not have a Feb 16 entry — as-of lookup is required"

    feb_1_rows = fx_rates[fx_rates["date"] == pd.Timestamp("2024-02-01")]
    g = _build_fx_graph(feb_1_rows)
    cad_to_usd = _bfs_to_usd(g, "CAD")
    assert cad_to_usd is not None and 0.70 < cad_to_usd < 0.85, \
        f"CAD→USD on Feb 1 via triangulation should be ~0.771, got {cad_to_usd}"

    # R013's Month 2 ARR must reflect the correctly converted CAD deal
    exp = expected_report.loc[
        (expected_report["rep_id"] == "R013") & (expected_report["month"] == 2),
        "net_arr_usd"].iloc[0]
    act = report_df.loc[
        (report_df["rep_id"] == "R013") & (report_df["month"] == 2),
        "net_arr_usd"].iloc[0]
    assert abs(act - exp) <= 1.0, \
        f"R013 month 2: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
        "Check that deal (AMER, 1509) uses the 2024-02-01 CAD→EUR→USD rate, " \
        "not a 1.0 fallback from a missing 2024-02-16 rate."
