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
    shares = defaultdict(float, {ae: 1.0})
    if deal_row["product_line"] == "Enterprise Suite" and ov:
        shares[ov]  += 0.15
        if sdr:
            shares[sdr] += 0.15
            shares[ae]   = 0.70
        else:
            shares[ae]   = 0.85
    elif sdr:
        shares[sdr] += 0.20
        shares[ae]   = 0.80
    if rep_region.get(ae) == "EMEA":
        scaled = defaultdict(float, {"R999": 0.05})
        for rep, s in shares.items():
            scaled[rep] += s * 0.95
        shares = scaled
    return dict(shares)


_Q1_YMS = {(2024, 2), (2024, 3), (2024, 4)}


def _ym_add(d, n):
    """Return (year, month) of date d + n calendar months."""
    m0 = d.month - 1 + n
    return (d.year + m0 // 12, m0 % 12 + 1)


def _monthly_tranches(arr, close_date, cancel_date):
    """Spread arr over 3 tranches starting at close_date; return [(month_int, amount)] for Q1 only."""
    third      = arr / 3.0
    tranche_yms = [_ym_add(close_date, i) for i in range(3)]
    cancel_ym   = (cancel_date.year, cancel_date.month) if cancel_date is not None else (9999, 99)
    result = []
    for i, ym in enumerate(tranche_yms):
        if ym not in _Q1_YMS:
            continue
        m       = ym[1]
        tranche = third if i < 2 else (arr - 2 * third)
        if cancel_ym < ym:
            pass
        elif cancel_ym == ym:
            clawback = sum(
                (third if j < 2 else (arr - 2 * third))
                for j in range(i) if tranche_yms[j] in _Q1_YMS
            )
            if clawback > 0:
                result.append((m, -clawback))
        else:
            result.append((m, tranche))

    # Handle cancel_ym in Q1 with no matching tranche month (e.g. Dec close, March cancel).
    if cancel_ym in _Q1_YMS and cancel_ym not in set(tranche_yms):
        recognized = sum(
            (third if i < 2 else (arr - 2 * third))
            for i, ym in enumerate(tranche_yms)
            if ym in _Q1_YMS and ym < cancel_ym
        )
        if recognized > 0:
            result.append((cancel_ym[1], -recognized))

    return result


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def raw_data():
    return (
        pd.read_csv(DATA_DIR / "deals.csv",         parse_dates=["close_date"]),
        pd.read_csv(DATA_DIR / "reps.csv",          parse_dates=["hire_date"]),
        pd.read_csv(DATA_DIR / "account_teams.csv", parse_dates=["effective_from"]),
        pd.read_csv(DATA_DIR / "quotas.csv",        parse_dates=["period_start", "period_end"]),
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

    at_grouped = account_teams.groupby("account_id")

    won = deals[deals["stage"] == "closed_won"].copy()
    won = won.sort_values("close_date").drop_duplicates(["region", "deal_id"], keep="last")
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
        close_date = deal["close_date"]
        acct       = deal["account_id"]
        if acct in at_grouped.groups:
            at_acct  = at_grouped.get_group(acct)
            at_active = at_acct[at_acct["effective_from"] <= close_date]
        else:
            at_active = pd.DataFrame(columns=account_teams.columns)
        team = {"Primary_AE": None, "SDR": None, "Overlay_Specialist": None}
        for _, row in at_active.iterrows():
            if row["role"] in ("Primary_AE", "SDR", "Overlay_Specialist"):
                team[row["role"]] = row["rep_id"]
        shares     = _resolve_attribution(deal, team, rep_region)
        cdate      = cancel_map.get((deal["region"], deal["deal_id"]))
        for rep, share in shares.items():
            if share > 0:
                for m, net in _monthly_tranches(deal["arr_usd"] * share, close_date, cdate):
                    ledger[(rep, m)] += net

    def _quota_monthly(rep_id, month_start, quotas):
        applicable = quotas[
            (quotas["rep_id"]       == rep_id) &
            (quotas["period_start"] <= month_start) &
            (quotas["period_end"]   >= month_start)
        ]
        if applicable.empty:
            return 0.0
        q = applicable.iloc[0]
        n_months = (
            (q["period_end"].year  - q["period_start"].year) * 12
            + q["period_end"].month - q["period_start"].month
            + 1
        )
        return q["quota_usd"] / n_months

    records = []
    for _, row in reps.iterrows():
        hire = row["hire_date"]
        for m, (start, end) in MONTH_BOUNDS.items():
            days_in = (end - start).days + 1
            active  = 0 if hire > end else (days_in if hire <= start else (end - hire).days + 1)
            base_m  = _quota_monthly(row["rep_id"], start, quotas)
            records.append({"rep_id": row["rep_id"], "rep_name": row["rep_name"],
                             "region": row["region"], "month": m,
                             "base_quota": base_m * (active / days_in)})

    monthly_q = pd.DataFrame(records)
    rep_ids   = set(reps["rep_id"])

    # Identify reps at ≥150% Feb attainment (effective_quota = base_quota in Feb).
    boost_reps = set()
    for rep_id, group in monthly_q.groupby("rep_id"):
        feb_rows = group[group["month"] == 2]
        if not feb_rows.empty:
            bq_feb  = feb_rows.iloc[0]["base_quota"]
            net_feb = ledger.get((rep_id, 2), 0.0)
            if bq_feb > 0 and net_feb >= bq_feb * 1.5:
                boost_reps.add(rep_id)

    results   = []
    for rep_id, group in monthly_q.groupby("rep_id"):
        shortfall = 0.0
        for _, row in group.sort_values("month").iterrows():
            net = ledger.get((rep_id, row["month"]), 0.0)
            bq  = row["base_quota"] * (1.2 if row["month"] == 3 and rep_id in boost_reps else 1.0)
            eff_q = bq + shortfall
            shortfall = max(0.0, eff_q - net)
            att   = round(net / eff_q * 100, 2) if eff_q > 0 else 0.0
            results.append({"rep_id": rep_id, "rep_name": row["rep_name"],
                             "region": row["region"], "month": row["month"],
                             "base_quota": round(bq, 2),
                             "effective_quota": round(eff_q, 2),
                             "net_arr_usd": round(net, 2), "attainment_pct": att})

    df = pd.DataFrame(results)
    return df[df["rep_id"].isin(rep_ids)].sort_values(["rep_id", "month"]).reset_index(drop=True)


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_01_output_structure(report_df, summary_json):
    """Both output files exist, CSV has required columns, and contains 600 rows."""
    assert REPORT_PATH.exists(),  "monthly_rep_performance.csv not found"
    assert SUMMARY_PATH.exists(), "summary.json not found"
    assert report_df is not None
    for col in ["rep_id", "rep_name", "region", "month",
                "base_quota", "effective_quota", "net_arr_usd", "attainment_pct"]:
        assert col in report_df.columns, f"Missing column: {col}"
    assert "total_net_arr_usd"    in summary_json
    assert "total_base_quota_usd" in summary_json
    assert len(report_df) == 600, f"Expected 600 rows, got {len(report_df)}"
    assert set(report_df["month"].unique()) == {2, 3, 4}


def test_02_sentinel_input_data():
    """Input files must not be modified; key structural properties are anchored."""
    from collections import Counter as _Counter

    deals = pd.read_csv(DATA_DIR / "deals.csv")
    assert len(deals) == 5025, f"deals.csv must not be modified (expected 5025 rows, got {len(deals)})"

    at = pd.read_csv(DATA_DIR / "account_teams.csv")
    r081_ae = set(at[(at["rep_id"] == "R081") & (at["role"] == "Primary_AE")]["account_id"])
    assert len(r081_ae) >= 5, \
        f"R081 must be Primary_AE for at least 5 accounts (got {len(r081_ae)})"

    assert "effective_from" in at.columns, "account_teams.csv must have effective_from column"
    late_sdrs = at[(at["role"] == "SDR") & (at["effective_from"] == "2024-03-01")]
    assert len(late_sdrs) == 25, f"Expected 25 SDR rows with effective_from=2024-03-01, got {len(late_sdrs)}"

    fx = pd.read_csv(DATA_DIR / "fx_rates.csv", parse_dates=["date"])
    assert (fx["date"].dt.day == 1).all(), \
        "fx_rates.csv modified — rates must be monthly (1st of each month) only"

    cancels = pd.read_csv(DATA_DIR / "cancellations.csv")
    emea_cancels = cancels[(cancels["region"] == "EMEA") & (cancels["status"] == "approved")]
    assert len(emea_cancels) > 0, "cancellations.csv must contain at least one approved EMEA cancellation (Trap 6)"
    # Trap 6 is a Dec-closed EMEA deal cancelled in March
    assert any(
        (emea_cancels["cancelled_date"] >= "2024-03-01") & (emea_cancels["cancelled_date"] <= "2024-03-31")
    ), "cancellations.csv must contain at least one March cancellation for Trap 6"

    won_keys = list(zip(deals[deals["stage"] == "closed_won"]["region"],
                        deals[deals["stage"] == "closed_won"]["deal_id"]))
    dupes = [k for k, v in _Counter(won_keys).items() if v > 1]
    assert len(dupes) == 25, f"Expected 25 (region, deal_id) pairs with 2 closed_won rows, got {len(dupes)}"

    quotas = pd.read_csv(DATA_DIR / "quotas.csv", parse_dates=["period_start"])
    mar_revisions = quotas[quotas["period_start"] == pd.Timestamp("2024-03-01")]
    assert len(mar_revisions) >= 20, \
        f"Expected ≥20 mid-quarter quota revision rows (period_start=2024-03-01), got {len(mar_revisions)}"


def test_03_summary_totals(summary_json, expected_report):
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


def test_04_emea_ae_global_vp_attribution(report_df, expected_report):
    """Trap 2 — Global_VP 5% cut based on Primary_AE's home region, not deal region.

    R081 is an EMEA rep. The instruction requires the cut to trigger when the
    Primary_AE's home region in reps.csv is EMEA. A model that forgets the VP
    cut entirely will give R081 too much ARR across all Q1 months.
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


def test_05_cancellation_clawback_in_cancelled_month(report_df, expected_report):
    """Trap 4 — clawback is recorded in cancelled_date month, not filed_date month.

    Deal (AMER, 788): filed 2024-02-29, cancelled_date 2024-03-02.
    The deal closes 2024-02-10; its February tranche is recognised first, then
    clawed back in March. A model using filed_date.month would cancel in February
    (same month as tranche 1), producing zero recognition and incorrectly voiding
    March and April — making R047's February too low and March wrong.
    """
    assert report_df is not None
    for month in [2, 3]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R047") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R047") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R047 month {month}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Clawback must be recorded in cancelled_date month, not filed_date month."


def test_06_cancellation_composite_key(report_df, expected_report):
    """Trap 3 — cancellation key is (region, deal_id); deal_id alone is not unique.

    (APAC, deal_id=293) is cancelled (cancelled_date 2024-03-31). A separate deal
    with deal_id=293 exists in EMEA (owned by R112, closes 2023-12-18) with no
    cancellation. That EMEA deal has only a February Q1 tranche. A model matching
    cancellations by deal_id alone incorrectly voids R112's February tranche and
    records a phantom clawback in March.
    """
    assert report_df is not None
    for month in [2, 3]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R112") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R112") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R112 month {month}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Cancellation (APAC, 293) must not affect the separate EMEA deal with the same deal_id."


def test_07_new_hire_quota_proration(report_df, expected_report):
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


def test_08_quota_rollover(report_df):
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


def test_09_fx_as_of_date_and_triangulation(report_df, expected_report, raw_data):
    """Trap 1 — FX rates are monthly; mid-month closes must use the most recent prior rate,
    and missing direct USD pairs require chaining through intermediate currencies.

    Deal (AMER, 558) closes 2024-03-11 in CAD. No rate exists for Mar 11.
    The correct lookup uses the 2024-03-01 rate and chains: CAD→EUR→USD (2 hops).
    A model doing an exact-date lookup gets no match and falls back to 1.0, treating
    CAD as USD — overstating this deal's March tranche.
    R053 is the sole Primary_AE for this account with no SDR or Overlay, so the
    error flows directly into their Month 3 net_arr_usd.
    """
    assert report_df is not None
    _, _, _, _, _, fx_rates = raw_data

    # Confirm no rate exists on Mar 11 — as-of lookup is required
    mar_11_rows = fx_rates[fx_rates["date"] == pd.Timestamp("2024-03-11")]
    assert len(mar_11_rows) == 0, "fx_rates must not have a Mar 11 entry — as-of lookup is required"

    mar_1_rows = fx_rates[fx_rates["date"] == pd.Timestamp("2024-03-01")]
    g = _build_fx_graph(mar_1_rows)
    cad_to_usd = _bfs_to_usd(g, "CAD")
    assert cad_to_usd is not None and 0.60 < cad_to_usd < 0.85, \
        f"CAD→USD on Mar 1 via triangulation expected ~0.74, got {cad_to_usd}"

    # R053's Month 3 ARR must reflect the correctly converted CAD deal
    exp = expected_report.loc[
        (expected_report["rep_id"] == "R053") & (expected_report["month"] == 3),
        "net_arr_usd"].iloc[0]
    act = report_df.loc[
        (report_df["rep_id"] == "R053") & (report_df["month"] == 3),
        "net_arr_usd"].iloc[0]
    assert abs(act - exp) <= 1.0, \
        f"R053 month 3: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
        "Check that deal (AMER, 558) uses the 2024-03-01 CAD→EUR→USD rate, " \
        "not a 1.0 fallback from a missing 2024-03-11 rate."


def test_10_clawback_when_cancel_month_has_no_tranche(report_df, expected_report):
    """Trap 6 — clawback must be recorded in cancelled_date month even when no deal tranche
    falls in that month.

    Deal (EMEA, 197) closes 2023-12-26; its three tranches land in Dec 2023, Jan 2024,
    Feb 2024. The deal is cancelled (cancelled_date 2024-03-15) after all tranches are past.
    The Q1-recognised Feb tranche must be clawed back in March. A model that only generates
    clawbacks when the cancel month coincides with a tranche will silently drop it; a model
    that sweeps all pre-cancel tranches (including non-Q1 Dec/Jan) will over-claw by ~2x.
    """
    assert report_df is not None
    for month, label in [(2, "Feb"), (3, "March")]:
        exp = expected_report.loc[
            (expected_report["rep_id"] == "R107") & (expected_report["month"] == month),
            "net_arr_usd"].iloc[0]
        act = report_df.loc[
            (report_df["rep_id"] == "R107") & (report_df["month"] == month),
            "net_arr_usd"].iloc[0]
        assert abs(act - exp) <= 1.0, \
            f"R107 {label}: got {act:.2f}, expected {exp:.2f} (diff {act-exp:+.2f}). " \
            "Clawback must equal only Q1-recognised tranches, recorded in cancelled_date month."

def test_11_march_quota_uplift(report_df, expected_report, raw_data):
    """Trap 5 — Reps reaching ≥150% Feb attainment receive a 20% March base_quota boost.

    The uplift applies to March base_quota only; April base_quota is the original
    prorated value. Verified by finding reps with ≥150% Feb attainment and checking
    that March base_quota is exactly 1.2× the unmodified prorated base_quota.
    """
    if report_df is None:
        pytest.skip("monthly_rep_performance.csv not found")

    _, _, _, quotas, _, _ = raw_data
    # Reps with mid-quarter quota revisions have different March base_quotas;
    # exclude them from the uplift-invariant check (feb_gt * 1.2 != mar_gt for them).
    promoted_reps = set(quotas[quotas["period_start"] == pd.Timestamp("2024-03-01")]["rep_id"])

    feb_rows = expected_report[expected_report["month"] == 2]
    high_achievers = feb_rows[
        (feb_rows["effective_quota"] > 0) &
        (feb_rows["net_arr_usd"] >= feb_rows["effective_quota"] * 1.5) &
        (~feb_rows["rep_id"].isin(promoted_reps))
    ]["rep_id"].unique()

    assert len(high_achievers) > 0, "No non-promoted reps achieved ≥150% Feb attainment — March uplift rule cannot be tested"

    for rep_id in high_achievers[:5]:
        feb_gt = expected_report.loc[
            (expected_report["rep_id"] == rep_id) & (expected_report["month"] == 2),
            "base_quota"].iloc[0]
        mar_gt = expected_report.loc[
            (expected_report["rep_id"] == rep_id) & (expected_report["month"] == 3),
            "base_quota"].iloc[0]
        apr_gt = expected_report.loc[
            (expected_report["rep_id"] == rep_id) & (expected_report["month"] == 4),
            "base_quota"].iloc[0]

        mar_act = report_df.loc[
            (report_df["rep_id"] == rep_id) & (report_df["month"] == 3),
            "base_quota"].iloc[0]
        apr_act = report_df.loc[
            (report_df["rep_id"] == rep_id) & (report_df["month"] == 4),
            "base_quota"].iloc[0]

        assert abs(mar_act - mar_gt) < 1.0, \
            f"{rep_id} March base_quota {mar_act:.2f} != expected {mar_gt:.2f}."
        assert abs(mar_gt - feb_gt * 1.2) < 1.0, \
            f"{rep_id} March boost not applied: {mar_gt:.2f} != {feb_gt * 1.2:.2f}. " \
            "Reps at ≥150% Feb attainment must have March base_quota increased by 20%."
        assert abs(apr_act - apr_gt) < 1.0, \
            f"{rep_id} April base_quota {apr_act:.2f} != expected {apr_gt:.2f}. " \
            "The 20% uplift applies to March only; April base_quota must be unmodified."


def test_12_account_team_scd(report_df, expected_report, raw_data):
    """Trap 7 — SDR attribution requires effective_from ≤ deal close_date.

    25 accounts have an SDR with effective_from = 2024-03-01. Deals on those
    accounts closed in February must use no-SDR attribution (Primary_AE = 100%).
    A model that prebuilds a static team dict gives those Primary_AEs only 80%
    of their February deal ARR.
    """
    _, _, account_teams, _, _, _ = raw_data
    late_sdr_accts = set(
        account_teams[
            (account_teams["role"] == "SDR") &
            (account_teams["effective_from"] == pd.Timestamp("2024-03-01"))
        ]["account_id"]
    )
    assert len(late_sdr_accts) >= 20, \
        f"Expected ≥20 trap accounts, found {len(late_sdr_accts)} — account_teams.csv may not have been regenerated"

    ae_rows = account_teams[
        (account_teams["account_id"].isin(late_sdr_accts)) &
        (account_teams["role"] == "Primary_AE")
    ]
    ae_reps = ae_rows["rep_id"].unique()[:5]

    failures = []
    for rep_id in ae_reps:
        exp_row = expected_report[
            (expected_report["rep_id"] == rep_id) & (expected_report["month"] == 2)
        ]
        act_row = report_df[
            (report_df["rep_id"] == rep_id) & (report_df["month"] == 2)
        ]
        if exp_row.empty or act_row.empty:
            continue
        diff = abs(act_row["net_arr_usd"].iloc[0] - exp_row["net_arr_usd"].iloc[0])
        if diff > 1.0:
            failures.append(
                f"{rep_id} Feb net_arr_usd: got {act_row['net_arr_usd'].iloc[0]:.2f}, "
                f"expected {exp_row['net_arr_usd'].iloc[0]:.2f} (diff {diff:+.2f}). "
                "SDR effective_from=2024-03-01 must exclude SDR from Feb deals."
            )
    assert not failures, "\n".join(failures)
