import os
import json
import numpy as np
import pandas as pd
from pathlib import Path
import networkx as nx

DATA_DIR      = Path(os.environ.get("DATA_DIR",      "/workspace/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

REPORT_PERIOD_START = pd.Timestamp("2024-02-01")
REPORT_PERIOD_END   = pd.Timestamp("2024-04-30")

def load_data():
    deals         = pd.read_csv(DATA_DIR / "deals.csv", parse_dates=["close_date"])
    reps          = pd.read_csv(DATA_DIR / "reps.csv", parse_dates=["hire_date"])
    account_teams = pd.read_csv(DATA_DIR / "account_teams.csv")
    quotas        = pd.read_csv(DATA_DIR / "quotas.csv", parse_dates=["period_start"])
    cancellations = pd.read_csv(DATA_DIR / "cancellations.csv", parse_dates=["filed_date", "cancelled_date"])
    fx_rates      = pd.read_csv(DATA_DIR / "fx_rates.csv", parse_dates=["date"])
    return deals, reps, account_teams, quotas, cancellations, fx_rates

def compute_fx_rates_graph(fx_rates, dates, currencies):
    # Trap 1: FX Triangulation
    # Build graph per day to find rate to USD
    # fx_rates has: date, base_currency, quote_currency, rate (quote/base)
    # We want: USD value of 1 unit of foreign currency. So USD/Foreign.
    
    fx_dict = {}
    for d in dates:
        day_rates = fx_rates[fx_rates["date"] == d]
        G = nx.DiGraph()
        for _, row in day_rates.iterrows():
            G.add_edge(row["base_currency"], row["quote_currency"], weight=row["rate"])
            G.add_edge(row["quote_currency"], row["base_currency"], weight=1.0 / row["rate"])
            
        for c in set(currencies) - {"USD"}:
            if c not in G or "USD" not in G:
                fx_dict[(d, c)] = 1.0
                continue
                
            try:
                # Find path from the foreign currency to USD
                path = nx.shortest_path(G, source=c, target="USD")
                multiplier = 1.0
                for i in range(len(path) - 1):
                    multiplier *= G[path[i]][path[i+1]]["weight"]
                fx_dict[(d, c)] = multiplier
            except nx.NetworkXNoPath:
                fx_dict[(d, c)] = 1.0 # Fallback
                
    return fx_dict

def process_deals(deals, fx_rates):
    # Trap 5: Filter closed_won
    deals = deals[deals["stage"] == "closed_won"].copy()
    
    dates = deals["close_date"].unique()
    currencies = deals["currency"].unique()
    fx_dict = compute_fx_rates_graph(fx_rates, dates, currencies)
    
    def get_usd_rate(row):
        if row["currency"] == "USD": return 1.0
        return fx_dict.get((row["close_date"], row["currency"]), 1.0)
        
    deals["fx_to_usd"] = deals.apply(get_usd_rate, axis=1)
    
    # Base ARR USD
    deals["base_arr_usd"] = (deals["total_contract_value"] / deals["contract_months"] * 12.0) * deals["fx_to_usd"]
    
    # Trap 2: Mutually Exclusive Accelerators
    def get_multiplier(row):
        m = 1.0
        if row["product_line"] == "Enterprise Suite": m = max(m, 1.5)
        if row["contract_months"] >= 24: m = max(m, 1.3)
        if row["deal_source"] == "Outbound": m = max(m, 1.2)
        return m
        
    deals["multiplier"] = deals.apply(get_multiplier, axis=1)
    deals["arr_usd"] = deals["base_arr_usd"] * deals["multiplier"]
    
    return deals

def resolve_attribution(deals, account_teams):
    # Trap 2: Hierarchical Rules Engine
    credited_records = []
    
    # Map Primary_AE regions
    primary_reps = account_teams[account_teams["role"] == "Primary_AE"][["account_id", "rep_id"]]
    
    # Pre-index teams by account_id
    teams_dict = {}
    for _, row in account_teams.iterrows():
        teams_dict.setdefault(row["account_id"], []).append(row)
        
    for _, deal in deals.iterrows():
        acc_id = deal["account_id"]
        team_rows = teams_dict.get(acc_id, [])
        
        primary_ae = None
        sdr = None
        overlay = None
        
        for r in team_rows:
            if r["role"] == "Primary_AE": primary_ae = r["rep_id"]
            elif r["role"] == "SDR": sdr = r["rep_id"]
            elif r["role"] == "Overlay_Specialist": overlay = r["rep_id"]
        
        if not primary_ae: continue

        
        shares = {primary_ae: 1.0}
        
        if sdr:
            shares[sdr] = 0.20
            shares[primary_ae] = 0.80
            
        if deal["product_line"] == "Enterprise Suite" and overlay:
            shares[overlay] = 0.15
            if sdr:
                shares[sdr] = 0.15
                shares[primary_ae] = 0.70
            else:
                shares[primary_ae] = 0.85
                
        # Global VP cut if Primary_AE is EMEA. Wait, the deal region is the Primary_AE region.
        if deal["region"] == "EMEA":
            new_shares = {"R999": 0.05}
            for rep, share in shares.items():
                new_shares[rep] = share * 0.95
            shares = new_shares
            
        for rep, share in shares.items():
            if share > 0:
                credited_records.append({
                    "deal_id": deal["deal_id"],
                    "region": deal["region"],
                    "close_date": deal["close_date"],
                    "rep_id": rep,
                    "credited_arr": deal["arr_usd"] * share
                })
                
    return pd.DataFrame(credited_records)

def process_cancellations(cancellations):
    # Trap 3: Deduplication and Cutoffs
    # Only approved, filed <= 2024-04-05
    cancellations = cancellations[cancellations["status"] == "approved"].copy()
    
    # Find latest approved pre-cutoff
    cutoff = pd.Timestamp("2024-04-05")
    pre_cutoff = cancellations[cancellations["filed_date"] <= cutoff]
    
    # Sort by filed_date to get the latest
    pre_cutoff = pre_cutoff.sort_values("filed_date", ascending=True)
    valid_cancels = pre_cutoff.drop_duplicates(["region", "deal_id"], keep="last")
    return valid_cancels

def build_monthly_ledger(credited, valid_cancels):
    # Recognition: 1/3 per month for 3 months
    ledger = []
    
    # Make lookup for cancellations
    cancels_dict = valid_cancels.set_index(["region", "deal_id"])["cancelled_date"].to_dict()
    
    for _, row in credited.iterrows():
        close_month = row["close_date"].month
        arr = row["credited_arr"]
        
        cancel_date = cancels_dict.get((row["region"], row["deal_id"]))
        cancel_month = cancel_date.month if cancel_date else 99
        
        # Month 1
        m1 = close_month
        if m1 >= 2 and m1 <= 4:
            if cancel_month <= m1:
                # Cancelled before or during month 1
                pass
            else:
                ledger.append({"rep_id": row["rep_id"], "month": m1, "recognized_arr": arr / 3.0, "clawback_arr": 0.0})
                
        # Month 2
        m2 = close_month + 1
        if m2 >= 2 and m2 <= 4:
            if cancel_month <= m1:
                pass # Already dead
            elif cancel_month == m2:
                # Cancelled in m2. Clawback m1.
                ledger.append({"rep_id": row["rep_id"], "month": m2, "recognized_arr": 0.0, "clawback_arr": arr / 3.0})
            else:
                # Still alive
                ledger.append({"rep_id": row["rep_id"], "month": m2, "recognized_arr": arr / 3.0, "clawback_arr": 0.0})
                
        # Month 3
        m3 = close_month + 2
        if m3 >= 2 and m3 <= 4:
            if cancel_month <= m2:
                pass # Dead
            elif cancel_month == m3:
                # Cancelled in m3. Clawback m1 and m2.
                ledger.append({"rep_id": row["rep_id"], "month": m3, "recognized_arr": 0.0, "clawback_arr": (arr / 3.0) * 2.0})
            else:
                # Alive
                ledger.append({"rep_id": row["rep_id"], "month": m3, "recognized_arr": arr - (arr / 3.0) * 2.0, "clawback_arr": 0.0})
                
    if not ledger:
        return pd.DataFrame(columns=["rep_id", "month", "net_arr_usd"])
        
    ledger_df = pd.DataFrame(ledger)
    grouped = ledger_df.groupby(["rep_id", "month"])[["recognized_arr", "clawback_arr"]].sum().reset_index()
    grouped["net_arr_usd"] = grouped["recognized_arr"] - grouped["clawback_arr"]
    return grouped

def prorate_quotas(reps, quotas):
    # Q1 target is 2024-02-01
    q1_quotas = quotas[quotas["period_start"] == "2024-02-01"]
    df = reps.merge(q1_quotas, on="rep_id", how="left")
    
    # Base monthly is quota / 3
    df["base_monthly_quota"] = df["quota_usd"] / 3.0
    
    records = []
    for _, row in df.iterrows():
        hire = row["hire_date"]
        
        for m, start, end in [(2, pd.Timestamp("2024-02-01"), pd.Timestamp("2024-02-29")),
                              (3, pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-31")),
                              (4, pd.Timestamp("2024-04-01"), pd.Timestamp("2024-04-30"))]:
            days_in_month = (end - start).days + 1
            if hire > end:
                active = 0
            elif hire > start:
                active = (end - hire).days + 1
            else:
                active = days_in_month
                
            q = row["base_monthly_quota"] * (active / days_in_month)
            records.append({
                "rep_id": row["rep_id"],
                "rep_name": row["rep_name"],
                "region": row["region"],
                "month": m,
                "base_quota": q
            })
            
    return pd.DataFrame(records)

def build_report(ledger, monthly_quotas):
    # Merge and calculate effective quota with rollover
    df = monthly_quotas.merge(ledger[["rep_id", "month", "net_arr_usd"]], on=["rep_id", "month"], how="left")
    df["net_arr_usd"] = df["net_arr_usd"].fillna(0.0)
    
    # Sort carefully for rollover
    df = df.sort_values(["rep_id", "month"])
    
    results = []
    for rep_id, group in df.groupby("rep_id"):
        shortfall = 0.0
        for _, row in group.iterrows():
            eff_q = row["base_quota"] + shortfall
            net = row["net_arr_usd"]
            shortfall = max(0.0, eff_q - net)
            
            att_pct = (net / eff_q * 100) if eff_q > 0 else 0.0
            
            results.append({
                "rep_id": row["rep_id"],
                "rep_name": row["rep_name"],
                "region": row["region"],
                "month": row["month"],
                "base_quota": round(row["base_quota"], 2),
                "effective_quota": round(eff_q, 2),
                "net_arr_usd": round(net, 2),
                "attainment_pct": round(att_pct, 2)
            })
            
    return pd.DataFrame(results)

def main():
    deals, reps, account_teams, quotas, cancellations, fx_rates = load_data()

    deals = process_deals(deals, fx_rates)
    credited = resolve_attribution(deals, account_teams)
    
    valid_cancels = process_cancellations(cancellations)
    ledger = build_monthly_ledger(credited, valid_cancels)
    
    monthly_quotas = prorate_quotas(reps, quotas)

    report = build_report(ledger, monthly_quotas)
    # Ensure R999 is in the report if they got revenue, but they don't have a quota.
    # Instruction says: Include one row per rep per month. R999 isn't in reps.csv, so they won't be here.
    # Actually, the Global_VP is not in reps.csv. We only report on reps in reps.csv.
    
    # Only keep reps that are in reps.csv
    rep_ids = set(reps["rep_id"])
    report = report[report["rep_id"].isin(rep_ids)]
    
    report.to_csv(WORKSPACE_DIR / "monthly_rep_performance.csv", index=False)

    summary = {
        "total_net_arr_usd":   float(round(report["net_arr_usd"].sum(),   2)),
        "total_base_quota_usd":     float(round(report["base_quota"].sum(),      2)),
    }
    with open(WORKSPACE_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

if __name__ == "__main__":
    main()
