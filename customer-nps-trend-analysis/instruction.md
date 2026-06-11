Build an H1 2025 NPS program report for a B2B SaaS subscription business. The company surveys customers monthly to measure relationship NPS. Your report must follow the company's reporting standard and produce two output files.

## Input Data

All input files are at `/workspace/data/`.

**`/workspace/data/customers.csv`**
One row per customer account.
- `customer_id` — unique account identifier
- `segment` — account tier: `enterprise`, `mid_market`, or `smb`
- `monthly_recurring_revenue` — MRR in USD at the time of the report
- `signup_date` — date the account was created (YYYY-MM-DD)
- `status` — account status; all active accounts are `active`
- `is_test_account` — boolean; test accounts must be excluded from all calculations

**`/workspace/data/survey_invitations.csv`**
One row per survey invitation sent.
- `invitation_id` — unique invitation identifier
- `customer_id` — references `customers.csv`
- `wave` — survey wave in YYYY-MM format (2025-01 through 2025-06)
- `sent_date` — date the invitation was sent (YYYY-MM-DD)
- `channel` — delivery channel: `email`, `in_app`, or `phone_ivr`

**`/workspace/data/survey_responses.csv`**
One row per survey response received.
- `response_id` — unique response identifier
- `invitation_id` — references `survey_invitations.csv`
- `response_date` — date the response was submitted (YYYY-MM-DD)
- `score` — numeric score given by the respondent
- `primary_theme` — the primary complaint theme cited by detractors; null for non-detractors

**`/workspace/data/survey_channels.csv`**
Reference table for survey delivery channels.
- `channel` — channel name
- `description` — channel description
- `score_scale` — the scale used for scores on this channel

**`/workspace/data/support_tickets.csv`**
Support tickets opened by customers during H1 2025.
- `ticket_id` — unique ticket identifier
- `customer_id` — references `customers.csv`
- `opened_date` — date the ticket was opened (YYYY-MM-DD)
- `status` — ticket status: `open`, `closed`, or `pending`
- `category` — ticket category

## Business Rules

**NPS definition.** NPS is computed from responses to the standard 0–10 relationship survey question. Promoters score 9–10; detractors score 0–6; passives score 7–8. NPS = (% promoters − % detractors) × 100, expressed as a decimal percentage points value (e.g. 15.0, not 0.15). Only responses to the standard 0–10 NPS question are valid NPS inputs; responses from channels that use a different scale must be excluded.

**Company reporting standard.** Overall NPS for a period is the segment-weighted average of the per-segment NPS values for that period. Each segment's weight equals its share of the active customer base (non-test-account customers). Use the full H1 customer base to determine segment weights; do not recompute weights per wave.

**Quarter definitions.** Q1 covers waves 2025-01, 2025-02, and 2025-03. Q2 covers waves 2025-04, 2025-05, and 2025-06. Per-quarter NPS aggregates all qualifying responses across the three waves in that quarter.

**Revenue at risk.** For each complaint theme, revenue at risk equals the sum of `monthly_recurring_revenue` across all distinct detractor customers who cited that theme in at least one qualifying response during H1 2025. Each customer is counted at most once per theme regardless of how many responses they submitted citing that theme.

**Exclusions.** Exclude test accounts from all calculations. Only qualifying 0–10 NPS responses count toward NPS and revenue-at-risk calculations; non-qualifying channel responses do not contribute.

## Output Requirements

### `/workspace/nps_report.csv`

One row per (wave, segment) combination — 18 rows total (6 waves × 3 segments). Columns, in order:

| Column | Type | Description |
|---|---|---|
| `wave` | string | Wave identifier (e.g. `2025-01`) |
| `segment` | string | Segment name (`enterprise`, `mid_market`, `smb`) |
| `invited` | integer | Number of non-test-account customers invited in this wave/segment |
| `responses` | integer | Number of qualifying 0–10 NPS responses received |
| `response_rate` | float | `responses / invited`, rounded to 4 decimal places |
| `segment_nps` | float | NPS for this segment and wave, rounded to 2 decimal places |

### `/workspace/summary.json`

A JSON object with exactly these keys and types:

| Key | Type | Description |
|---|---|---|
| `overall_nps_q1` | float | Base-weighted overall NPS for Q1, rounded to 2 decimal places |
| `overall_nps_q2` | float | Base-weighted overall NPS for Q2, rounded to 2 decimal places |
| `nps_change_h1` | float | `overall_nps_q2 − overall_nps_q1`, rounded to 2 decimal places |
| `nps_trend` | string | `"improving"` if `nps_change_h1 > 0`, else `"declining"` |
| `top_revenue_risk_theme` | string | Theme with the highest revenue at risk in H1 2025 |
| `top_revenue_at_risk` | float | Revenue at risk for that theme, rounded to 2 decimal places |
| `valid_response_count` | integer | Total qualifying 0–10 NPS responses across all waves, excluding test accounts |

All float values in `summary.json` must be plain Python `float`, not numpy types. All integer values must be plain Python `int`.
