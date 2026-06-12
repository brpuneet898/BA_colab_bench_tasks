Build an H1 2025 relationship NPS program report for a B2B SaaS company. The company surveys accounts monthly across six waves (January through June 2025) and tracks satisfaction by customer segment. Produce a wave-level detail report and a JSON summary covering the full first half of the year.

The company's relationship NPS survey uses the standard 0–10 scale. Promoters score 9 or 10, detractors score 6 or below, and passives score 7 or 8. NPS equals percent promoters minus percent detractors, expressed in percentage points. Only responses collected through channels that administer the 0–10 NPS question are included in NPS calculations.

The company measures satisfaction at the account level. Each account designates a primary contact to represent the relationship; only the primary contact's response (is_primary_contact = True in contacts.csv) counts toward NPS for that account and wave. Accounts whose primary contact did not respond in a given wave are excluded from that wave's respondent counts and NPS.

Segment classification for all NPS calculations uses each account's contracted tier at the time of their wave invitation, as documented in segment_history.csv. Segment weights for overall NPS are derived from the contracted account base as of January 1, 2025. Q1 covers waves 2025-01, 2025-02, and 2025-03. Q2 covers waves 2025-04, 2025-05, and 2025-06.

For each complaint theme, revenue at risk is the total contracted MRR carried by distinct detractor accounts that cited that theme in at least one qualifying H1 response, expressed in USD. Each account's monthly_recurring_revenue is denominated in its billing_currency; convert to USD using the most recently available exchange rate on or before the date of the account's earliest qualifying H1 response citing that theme. An account's MRR counts once per theme regardless of how many qualifying responses it submitted. Exclude test accounts (is_test_account = True) from all calculations.

Input data (raw exports from the company's survey platform and CRM; apply standard data cleaning practices before analysis):

`/workspace/data/accounts.csv` — account_id, current_segment (enterprise, mid_market, or smb), billing_currency (three-letter ISO code: USD, EUR, or GBP), monthly_recurring_revenue (in the account's billing_currency), signup_date (YYYY-MM-DD), status, is_test_account (boolean; exclude these accounts from all calculations)

`/workspace/data/contacts.csv` — contact_id, account_id, is_primary_contact (boolean; True for the account's designated NPS relationship contact)

`/workspace/data/segment_history.csv` — account_id, segment, valid_from (YYYY-MM-DD), valid_to (YYYY-MM-DD; empty means currently active)

`/workspace/data/survey_invitations.csv` — invitation_id, contact_id, wave (YYYY-MM, 2025-01 through 2025-06), sent_date (YYYY-MM-DD), channel (email, in_app, or phone_ivr)

`/workspace/data/survey_responses.csv` — response_id, invitation_id, response_date (YYYY-MM-DD), score (numeric), primary_theme (primary complaint theme for detractors; null for non-detractors)

`/workspace/data/fx_rates.csv` — date (YYYY-MM-DD), currency (three-letter ISO code), usd_rate (USD per one unit of the currency; USD rows carry usd_rate = 1.0)

`/workspace/data/support_tickets.csv` — ticket_id, account_id, opened_date (YYYY-MM-DD), status (open, closed, or pending), category

Required outputs:

Save `/workspace/nps_report.csv` with columns wave, segment, invited (integer count of non-test accounts in that segment and wave), responses (integer count of qualifying NPS responses received from primary contacts), response_rate (float, responses / invited, rounded to 4 decimal places), segment_nps (float, NPS for that segment and wave, rounded to 2 decimal places). The file must have exactly 18 rows, one per wave/segment combination, with columns in the order listed.

Save `/workspace/summary.json` with keys overall_nps_q1 (float, base-weighted overall NPS for Q1, rounded to 2 decimal places), overall_nps_q2 (float, base-weighted overall NPS for Q2, rounded to 2 decimal places), nps_change_h1 (float, overall_nps_q2 minus overall_nps_q1, rounded to 2 decimal places), nps_trend (string, "improving" if nps_change_h1 is positive, otherwise "declining"), top_revenue_risk_theme (string, the theme with the highest revenue at risk in H1 2025), top_revenue_at_risk (float, USD revenue at risk for that theme, rounded to 2 decimal places), valid_response_count (integer, total qualifying NPS responses from primary contacts across all H1 waves excluding test accounts). All float values must be plain Python float and all integer values must be plain Python int.
