import os
import json
import pandas as pd
from pathlib import Path
from collections import defaultdict, deque

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

Q1_MONTHS   = [2, 3, 4]
CUTOFF_DATE = pd.Timestamp("2024-04-05")
GLOBAL_VP   = "R999"


def load_data():
    deals         = pd.read_csv(DATA_DIR / "deals.csv",         parse_dates=["close_date"])
    reps          = pd.read_csv(DATA_DIR / "reps.csv",          parse_dates=["hire_date"])
    account_teams = pd.read_csv(DATA_DIR / "account_teams.csv", parse_dates=["effective_from"])
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv",        parse_dates=["period_start", "period_end"])
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv", parse_dates=["filed_date", "cancelled_date"])
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv",      parse_dates=["date"])
    return deals, reps, account_teams, quotas, cancellations, fx_rates


# ─── FX ───────────────────────────────────────────────────────────────────────

def _build_graph(fx_rows):
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
            new_rate = rate * edge
            if nxt == "USD":
                return new_rate
            if nxt not in visited:
                visited.add(nxt)
                q.append((nxt, new_rate))
    return None


def build_fx_lookup(fx_rates, close_dates, currencies):
    """Return {(close_date, currency): usd_rate} using as-of-date lookup.

    FX rates are published on the 1st of each month.  For a deal closing on
    any other day, use the most recent published rate on or before close_date.
    """
    # All available rate dates, sorted ascending
    available_dates = sorted(fx_rates["date"].unique())

    def as_of(close_date):
        """Return the latest available rate date that is <= close_date."""
        result = None
        for d in available_dates:
            if d <= close_date:
                result = d
            else:
                break
        return result

    # Cache graphs per rate date (not per close date)
    graph_cache = {}

    lookup = {}
    for cd in close_dates:
        rate_date = as_of(cd)
        if rate_date is None:
            for c in currencies:
                lookup[(cd, c)] = 1.0
            continue
        if rate_date not in graph_cache:
            graph_cache[rate_date] = _build_graph(fx_rates[fx_rates["date"] == rate_date])
        g = graph_cache[rate_date]
        for c in currencies:
            if c == "USD":
                lookup[(cd, c)] = 1.0
            else:
                val = _bfs_to_usd(g, c)
                lookup[(cd, c)] = val if val is not None else 1.0
    return lookup


# ─── Deals ────────────────────────────────────────────────────────────────────

def process_deals(deals, fx_rates):
    deals = deals[deals["stage"] == "closed_won"].copy()
    # BOTTLENECK: deal_id is region-scoped but some deals were renegotiated — two
    # closed_won records exist for the same (region, deal_id). Keep only the latest.
    deals = deals.sort_values("close_date").drop_duplicates(["region", "deal_id"], keep="last")

    fx_lookup = build_fx_lookup(fx_rates, deals["close_date"].unique(), deals["currency"].unique())

    deals["fx_to_usd"]    = deals.apply(lambda r: fx_lookup[(r["close_date"], r["currency"])], axis=1)
    deals["base_arr_usd"] = (deals["total_contract_value"] / deals["contract_months"] * 12.0) * deals["fx_to_usd"]

    def best_multiplier(row):
        m = 1.0
        if row["product_line"] == "Enterprise Suite":
            m = max(m, 1.5)
        if row["contract_months"] >= 24:
            m = max(m, 1.3)
        if row["deal_source"] == "Outbound":
            m = max(m, 1.2)
        return m

    deals["multiplier"] = deals.apply(best_multiplier, axis=1)
    deals["arr_usd"]    = deals["base_arr_usd"] * deals["multiplier"]
    return deals


# ─── Attribution ──────────────────────────────────────────────────────────────

def resolve_attribution(deals, account_teams, reps):
    rep_region = reps.set_index("rep_id")["region"].to_dict()

    at_grouped = account_teams.groupby("account_id")

    records = []
    for _, deal in deals.iterrows():
        close_date = deal["close_date"]
        acct       = deal["account_id"]

        # Only team members active on or before close_date
        if acct in at_grouped.groups:
            at_acct  = at_grouped.get_group(acct)
            at_active = at_acct[at_acct["effective_from"] <= close_date]
        else:
            at_active = pd.DataFrame(columns=account_teams.columns)

        team = {"Primary_AE": None, "SDR": None, "Overlay_Specialist": None}
        for _, row in at_active.iterrows():
            if row["role"] in ("Primary_AE", "SDR", "Overlay_Specialist"):
                team[row["role"]] = row["rep_id"]

        ae  = team["Primary_AE"]
        sdr = team["SDR"]
        ov  = team["Overlay_Specialist"]
        if not ae:
            continue

        shares = defaultdict(float, {ae: 1.0})
        if sdr:
            shares[sdr] += 0.20
            shares[ae]   = 0.80
        if deal["product_line"] == "Enterprise Suite" and ov:
            shares[ov]  += 0.15
            if sdr:
                shares[sdr] += 0.15
                shares[ae]   = 0.70
            else:
                shares[ae]   = 0.85

        if rep_region.get(ae) == "EMEA":
            new_shares = {GLOBAL_VP: 0.05}
            for rep, share in shares.items():
                new_shares[rep] = share * 0.95
            shares = new_shares

        for rep, share in shares.items():
            if share > 0:
                records.append({
                    "deal_id":      deal["deal_id"],
                    "region":       deal["region"],
                    "close_date":   close_date,
                    "rep_id":       rep,
                    "credited_arr": deal["arr_usd"] * share,
                })

    return pd.DataFrame(records)


# ─── Cancellations ────────────────────────────────────────────────────────────

def resolve_cancellations(cancellations):
    """Return valid cancellation dict: {(region, deal_id): cancelled_date}.

    Rules:
    - Only approved cancellations.
    - Pre-cutoff: filed_date <= 2024-04-05.
    - Per (region, deal_id), the record with the latest filed_date wins.
    - Post-cutoff: if no pre-cutoff approved record exists, the deal is not cancelled.
    """
    approved = cancellations[cancellations["status"] == "approved"].copy()
    pre      = approved[approved["filed_date"] <= CUTOFF_DATE]
    pre      = pre.sort_values("filed_date", ascending=True)
    valid    = pre.drop_duplicates(["region", "deal_id"], keep="last")
    return valid.set_index(["region", "deal_id"])["cancelled_date"].to_dict()


# ─── Monthly ledger ───────────────────────────────────────────────────────────

_Q1_YMS = {(2024, 2), (2024, 3), (2024, 4)}


def _ym_add(d, n):
    """Return (year, month) of date d + n calendar months."""
    m0 = d.month - 1 + n
    return (d.year + m0 // 12, m0 % 12 + 1)


def build_monthly_ledger(credited, cancel_map):
    """Spread credited ARR over the 3-month recognition window.

    Month 1: exactly 1/3 of credited_arr.
    Month 2: exactly 1/3.
    Month 3: remainder (credited_arr - 2 * first_third), avoiding float drift.

    Uses year-month arithmetic so deals closing in December of the prior year
    correctly contribute their February tranche to Q1.

    Cancellation clawback is recorded in the *cancelled_date* month.
    All previously recognised Q1 tranches are clawed back in that month.
    """
    ledger = []

    for _, row in credited.iterrows():
        close_date = row["close_date"]
        arr        = row["credited_arr"]
        third      = arr / 3.0
        key        = (row["region"], row["deal_id"])

        cancel_date = cancel_map.get(key)
        cancel_ym   = (cancel_date.year, cancel_date.month) if cancel_date else (9999, 99)

        tranche_yms = [_ym_add(close_date, i) for i in range(3)]

        for i, ym in enumerate(tranche_yms):
            if ym not in _Q1_YMS:
                continue

            m           = ym[1]
            tranche_val = third if i < 2 else (arr - 2 * third)

            if cancel_ym < ym:
                continue
            elif cancel_ym == ym:
                clawback = sum(
                    (third if j < 2 else arr - 2 * third)
                    for j in range(i) if tranche_yms[j] in _Q1_YMS
                )
                if clawback > 0:
                    ledger.append({"rep_id": row["rep_id"], "month": m,
                                   "net_arr_usd": -clawback})
            else:
                ledger.append({"rep_id": row["rep_id"], "month": m,
                               "net_arr_usd": tranche_val})

        # Handle the case where cancel_ym falls in Q1 but no tranche lands in that month
        # (e.g. deal closes in Dec — tranches are Dec/Jan/Feb — cancel is in March or April).
        # The Q1-recognised tranches must still be clawed back in the cancel month.
        if cancel_ym in _Q1_YMS and cancel_ym not in set(tranche_yms):
            recognized = sum(
                (third if i < 2 else arr - 2 * third)
                for i, ym in enumerate(tranche_yms)
                if ym in _Q1_YMS and ym < cancel_ym
            )
            if recognized > 0:
                ledger.append({"rep_id": row["rep_id"], "month": cancel_ym[1],
                               "net_arr_usd": -recognized})

    if not ledger:
        return pd.DataFrame(columns=["rep_id", "month", "net_arr_usd"])

    df = pd.DataFrame(ledger)
    return df.groupby(["rep_id", "month"])["net_arr_usd"].sum().reset_index()


# ─── Quota proration ─────────────────────────────────────────────────────────

MONTH_BOUNDS = {
    2: (pd.Timestamp("2024-02-01"), pd.Timestamp("2024-02-29")),
    3: (pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-31")),
    4: (pd.Timestamp("2024-04-01"), pd.Timestamp("2024-04-30")),
}


def prorate_quotas(reps, quotas):
    """Return one row per (rep, month) with base_quota.

    For each month, the applicable quota record is the one where
    period_start <= month_start <= period_end. Base monthly =
    quota_usd / number of calendar months in that record's period.
    Prorated by active days for reps hired during Q1.
    """
    records = []
    for _, row in reps.iterrows():
        hire = row["hire_date"]
        rep_quotas = quotas[quotas["rep_id"] == row["rep_id"]]
        for m, (start, end) in MONTH_BOUNDS.items():
            applicable = rep_quotas[
                (rep_quotas["period_start"] <= start) &
                (rep_quotas["period_end"]   >= start)
            ]
            if applicable.empty:
                quota_monthly = 0.0
            else:
                q = applicable.iloc[0]
                n_months = (
                    (q["period_end"].year  - q["period_start"].year) * 12
                    + q["period_end"].month - q["period_start"].month
                    + 1
                )
                quota_monthly = q["quota_usd"] / n_months

            days_in = (end - start).days + 1
            active   = 0 if hire > end else (days_in if hire <= start else (end - hire).days + 1)
            records.append({
                "rep_id":     row["rep_id"],
                "rep_name":   row["rep_name"],
                "region":     row["region"],
                "month":      m,
                "base_quota": quota_monthly * (active / days_in),
            })

    return pd.DataFrame(records)


# ─── Report assembly ─────────────────────────────────────────────────────────

def build_report(ledger, monthly_quotas, rep_ids_in_scope):
    df = monthly_quotas.merge(
        ledger[["rep_id", "month", "net_arr_usd"]], on=["rep_id", "month"], how="left"
    )
    df["net_arr_usd"] = df["net_arr_usd"].fillna(0.0)
    df = df.sort_values(["rep_id", "month"])

    # Identify reps who hit ≥150% in Feb (effective_quota = base_quota since no prior shortfall).
    # These reps receive a 20% March base_quota uplift.
    boost_reps = set()
    feb = df[df["month"] == 2]
    for _, row in feb.iterrows():
        bq = row["base_quota"]
        if bq > 0 and row["net_arr_usd"] >= bq * 1.5:
            boost_reps.add(row["rep_id"])

    results = []
    for rep_id, group in df.groupby("rep_id"):
        shortfall = 0.0
        for _, row in group.iterrows():
            bq        = row["base_quota"] * (1.2 if row["month"] == 3 and rep_id in boost_reps else 1.0)
            eff_q     = bq + shortfall
            net       = row["net_arr_usd"]
            shortfall = max(0.0, eff_q - net)
            att_pct   = round(net / eff_q * 100, 2) if eff_q > 0 else 0.0
            results.append({
                "rep_id":          row["rep_id"],
                "rep_name":        row["rep_name"],
                "region":          row["region"],
                "month":           row["month"],
                "base_quota":      round(bq, 2),
                "effective_quota": round(eff_q, 2),
                "net_arr_usd":     round(net, 2),
                "attainment_pct":  att_pct,
            })

    report = pd.DataFrame(results)
    # Only report on reps in reps.csv scope (Global_VP R999 is excluded)
    report = report[report["rep_id"].isin(rep_ids_in_scope)]
    return report.sort_values(["rep_id", "month"]).reset_index(drop=True)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    deals, reps, account_teams, quotas, cancellations, fx_rates = load_data()

    deals      = process_deals(deals, fx_rates)
    credited   = resolve_attribution(deals, account_teams, reps)
    cancel_map = resolve_cancellations(cancellations)
    ledger     = build_monthly_ledger(credited, cancel_map)

    monthly_quotas = prorate_quotas(reps, quotas)
    rep_ids        = set(reps["rep_id"])

    report = build_report(ledger, monthly_quotas, rep_ids)

    report.to_csv(WORKSPACE_DIR / "monthly_rep_performance.csv", index=False)

    summary = {
        "total_net_arr_usd":    float(round(report["net_arr_usd"].sum(),    2)),
        "total_base_quota_usd": float(round(report["base_quota"].sum(),     2)),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
