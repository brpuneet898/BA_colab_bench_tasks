import json
import os
import pytest
import pandas as pd
from collections import defaultdict
from pathlib import Path

if os.path.exists('/workspace/data'):
    DATA_DIR      = Path('/workspace/data')
    WORKSPACE_DIR = Path('/workspace')
else:
    DATA_DIR      = Path(__file__).parent.parent / 'environment' / 'data'
    WORKSPACE_DIR = Path(__file__).parent.parent

VARIABLES_PATH = Path(os.environ.get('VARIABLES_PATH', '/logs/verifier/notebook_variables.json'))


@pytest.fixture(scope='module')
def notebook_vars():
    if not VARIABLES_PATH.exists():
        return None
    return json.load(open(VARIABLES_PATH))


@pytest.fixture(scope='module')
def raw_data():
    tp   = pd.read_csv(DATA_DIR / 'touchpoints.csv',  parse_dates=['timestamp'])
    conv = pd.read_csv(DATA_DIR / 'conversions.csv',  parse_dates=['conversion_timestamp'])
    cfg  = pd.read_csv(DATA_DIR / 'channel_config.csv')
    return tp, conv, cfg


# ── Anti-cheat sentinels ─────────────────────────────────────────────────────

def test_input_touchpoints_row_count(raw_data):
    tp, _, _ = raw_data
    assert len(tp) == 27_560, "Input touchpoints.csv must not be modified."

def test_input_conversions_row_count(raw_data):
    _, conv, _ = raw_data
    assert len(conv) == 3_359, "Input conversions.csv must not be modified."

def test_direct_touchpoints_present(raw_data):
    tp, _, _ = raw_data
    assert (tp['channel'] == 'direct').sum() >= 3_000

def test_multi_conversion_users_present(raw_data):
    _, conv, _ = raw_data
    assert (conv['user_id'].value_counts() >= 2).sum() >= 300

def test_refund_rows_present(raw_data):
    _, conv, _ = raw_data
    assert (conv['revenue'] < 0).sum() >= 25, "Refund rows must be present in conversions.csv."


# ── Output file ──────────────────────────────────────────────────────────────

def test_attribution_report_exists():
    assert (WORKSPACE_DIR / 'attribution_report.csv').exists()

def test_attribution_report_columns():
    df = pd.read_csv(WORKSPACE_DIR / 'attribution_report.csv')
    for col in ['channel', 'attributed_revenue', 'spend', 'roas']:
        assert col in df.columns, f"Missing column: {col}"

def test_attribution_report_channels():
    df = pd.read_csv(WORKSPACE_DIR / 'attribution_report.csv')
    assert {'paid_search', 'display', 'email', 'organic_social', 'direct'} <= set(df['channel'])


# ── Notebook variable presence ───────────────────────────────────────────────

def test_notebook_variables_exist(notebook_vars):
    if notebook_vars is None:
        pytest.fail('notebook_variables.json not found — notebook must export all required variables')
    for var in [
        'total_attributed_revenue', 'paid_search_attributed_revenue',
        'display_attributed_revenue', 'email_attributed_revenue',
        'organic_social_attributed_revenue', 'direct_attributed_revenue',
        'roas_paid_search', 'roas_display', 'roas_email',
    ]:
        assert var in notebook_vars, f"Missing notebook variable: {var}"


# ── Ground-truth fixture (runs once per session) ─────────────────────────────

@pytest.fixture(scope='module')
def ground_truth(raw_data):
    tp_raw, conv_raw, cfg = raw_data

    lookback = cfg.set_index('channel')['lookback_days'].to_dict()
    # cost_per_click and monthly_flat_fee are stored in US cents — convert to dollars
    cpc      = (cfg.set_index('channel')['cost_per_click']    / 100).to_dict()
    flat_fee = (cfg.set_index('channel')['monthly_flat_fee']  / 100).to_dict()

    # Normalise channel names: lowercase + underscores; drop unrecognised channels
    valid_channels = set(lookback.keys())
    tp_raw = tp_raw.copy()
    tp_raw['channel'] = (tp_raw['channel']
                         .str.lower()
                         .str.replace('-', '_', regex=False))
    tp_raw = tp_raw[tp_raw['channel'].isin(valid_channels)].reset_index(drop=True)

    # Convert touchpoint timestamps from US Eastern (naive) to UTC.
    # Q1 2024: EST (UTC-5) before 2024-03-10 03:00 ET, EDT (UTC-4) from that point on.
    DST_BOUNDARY = pd.Timestamp('2024-03-10 03:00:00')
    tp_utc = tp_raw.copy()
    offsets = tp_utc['timestamp'].apply(
        lambda ts: pd.Timedelta(hours=5) if ts < DST_BOUNDARY else pd.Timedelta(hours=4)
    )
    tp_utc['timestamp'] = tp_utc['timestamp'] + offsets

    # Clicks only (after timezone conversion)
    tp = (tp_utc[tp_utc['touchpoint_type'] == 'click']
          .copy().sort_values(['user_id', 'timestamp']).reset_index(drop=True))

    # Direct suppression — sequential scan
    rows = tp.to_dict('records')
    last_nd = {}
    for row in rows:
        uid, ts = row['user_id'], row['timestamp']
        if row['channel'] == 'direct':
            prev = last_nd.get(uid)
            if prev and (ts - prev['timestamp']).total_seconds() / 3600 <= 6:
                row['channel'] = prev['channel']
        if row['channel'] != 'direct':
            last_nd[uid] = {'channel': row['channel'], 'timestamp': ts}
    tp = pd.DataFrame(rows)

    # Pre-group by user for O(1) lookup per conversion
    user_tp   = {uid: g.reset_index(drop=True) for uid, g in tp.groupby('user_id')}
    lb_series = pd.Series(lookback)

    # Net revenue per conversion_id.
    # ALL conversion events (including net-zero refunds) define path-isolation boundaries;
    # only conversions with net revenue > 0 receive attribution credit.
    net = (conv_raw.groupby('conversion_id')
           .agg(user_id=('user_id', 'first'),
                conversion_timestamp=('conversion_timestamp', 'min'),
                revenue=('revenue', 'sum'))
           .reset_index())
    net_sorted = net.sort_values(['user_id', 'conversion_timestamp']).reset_index(drop=True)
    net_sorted['prev_ts'] = net_sorted.groupby('user_id')['conversion_timestamp'].shift(1)
    conv = net_sorted[net_sorted['revenue'] > 0].copy().reset_index(drop=True)

    channel_revenue = defaultdict(float)

    for _, c in conv.iterrows():
        uid, cts, rev, pts = c['user_id'], c['conversion_timestamp'], c['revenue'], c['prev_ts']
        utps = user_tp.get(uid)
        if utps is None:
            continue

        mask = utps['timestamp'] < cts
        if pd.notna(pts):
            mask &= utps['timestamp'] > pts
        path = utps[mask]
        if path.empty:
            continue

        # Exact Timedelta lookback — no hour truncation
        lb_cutoffs = path['channel'].map(lb_series).fillna(30).apply(
            lambda d: cts - pd.Timedelta(days=d)
        )
        elig = path[path['timestamp'] >= lb_cutoffs].copy()
        if elig.empty:
            continue

        elig = elig.sort_values('timestamp')

        # Channel dedup — earliest appearance per channel defines position
        deduped = (elig.groupby('channel', sort=False)
                   .first().reset_index()
                   .sort_values('timestamp').reset_index(drop=True))

        n = len(deduped)
        if   n == 1: weights = [1.0]
        elif n == 2: weights = [0.5, 0.5]
        else:        weights = [0.40] + [0.20 / (n - 2)] * (n - 2) + [0.40]

        for i, (_, td) in enumerate(deduped.iterrows()):
            channel_revenue[td['channel']] += rev * weights[i]

    # Spend: CPC channels use total raw clicks; flat-fee channels billed per active month
    raw_clicks_df = tp_raw[tp_raw['touchpoint_type'] == 'click'].copy()
    raw_clicks_df['month'] = raw_clicks_df['timestamp'].dt.month
    raw_clicks = raw_clicks_df['channel'].value_counts().to_dict()
    all_ch = list(lookback.keys())
    spend = {}
    for ch in all_ch:
        if cpc.get(ch, 0) > 0:
            spend[ch] = round(cpc[ch] * raw_clicks.get(ch, 0), 2)
        elif flat_fee.get(ch, 0) > 0:
            # Billed only for months in which the channel had at least one click
            ch_clicks = raw_clicks_df[raw_clicks_df['channel'] == ch]
            active_months = ch_clicks['month'].nunique() if len(ch_clicks) > 0 else 0
            spend[ch] = round(flat_fee[ch] * active_months, 2)
        else:
            spend[ch] = 0.0

    roas = {ch: round(channel_revenue.get(ch, 0.0) / spend[ch], 2)
            for ch in all_ch if spend[ch] > 0}

    return channel_revenue, spend, roas


# ── Correctness tests ────────────────────────────────────────────────────────

def test_total_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(sum(channel_revenue.values()), 2)
    actual   = round(float(notebook_vars['total_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"total_attributed_revenue {actual} != {expected}"

def test_paid_search_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(channel_revenue.get('paid_search', 0), 2)
    actual   = round(float(notebook_vars['paid_search_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"paid_search {actual} != {expected}"

def test_display_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(channel_revenue.get('display', 0), 2)
    actual   = round(float(notebook_vars['display_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"display {actual} != {expected}"

def test_email_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(channel_revenue.get('email', 0), 2)
    actual   = round(float(notebook_vars['email_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"email {actual} != {expected}"

def test_organic_social_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(channel_revenue.get('organic_social', 0), 2)
    actual   = round(float(notebook_vars['organic_social_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"organic_social {actual} != {expected}"

def test_direct_attributed_revenue(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    channel_revenue, _, _ = ground_truth
    expected = round(channel_revenue.get('direct', 0), 2)
    actual   = round(float(notebook_vars['direct_attributed_revenue']), 2)
    assert abs(actual - expected) < 1.0, f"direct {actual} != {expected}"

def test_roas_email(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    _, _, roas = ground_truth
    expected = roas.get('email', 0)
    actual   = round(float(notebook_vars['roas_email']), 2)
    assert abs(actual - expected) < 0.10, f"roas_email {actual} != {expected}"

def test_roas_paid_search(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    _, _, roas = ground_truth
    expected = roas.get('paid_search', 0)
    actual   = round(float(notebook_vars['roas_paid_search']), 2)
    assert abs(actual - expected) < 0.10, f"roas_paid_search {actual} != {expected}"

def test_roas_display(notebook_vars, ground_truth):
    if notebook_vars is None:
        pytest.skip('notebook_variables.json not found')
    _, _, roas = ground_truth
    expected = roas.get('display', 0)
    actual   = round(float(notebook_vars['roas_display']), 2)
    assert abs(actual - expected) < 0.10, f"roas_display {actual} != {expected}"
