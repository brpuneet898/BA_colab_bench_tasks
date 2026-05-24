Implement a deterministic attribution audit engine for a customer-journey dataset.

## Input

The input CSV is available at the exact path:

`/workspace/data/customer_journey_test_case.csv`

The file contains these columns:

- `user_id`
- `timestamp`
- `channel`
- `interaction_type`
- `campaign_name`
- `revenue`
- `is_conversion`

## Goal

You must build a reusable attribution engine that can evaluate the base dataset and several counterfactual scenario datasets derived from it.

This task is intentionally not a one-off calculation. Your code must implement the attribution policy as a reusable function that works on modified datasets as well.

## Required Deliverables

Your code must programmatically create all of the following files:

1. `/workspace/notebook.ipynb`
2. `/workspace/attribution_engine.py`
3. `/logs/verifier/notebook_variables.json`

## Required Python API

The file `/workspace/attribution_engine.py` must define this function:

```python
def attribute_conversions(df, lookback_days=14):
    ...
```

The function must accept a pandas DataFrame and return a JSON-serializable dictionary with this structure:

```json
{
  "conversion_count": 2,
  "total_revenue": 275.0,
  "channel_totals": {
    "Direct Traffic": 150.0,
    "Email": 125.0
  },
  "per_conversion": [
    {
      "conversion_timestamp": "2026-05-01T15:30:00",
      "user_id": "USR_9928A",
      "conversion_channel": "Direct Traffic",
      "revenue": 150.0,
      "winning_channel": "Direct Traffic",
      "winning_reason": "fallback_to_conversion_channel",
      "eligible_click_count": 0,
      "excluded_counts": {
        "conversion_row": 1,
        "same_or_after_conversion": 0,
        "non_click": 16,
        "outside_lookback": 1
      }
    }
  ]
}
```

## Attribution Policy

Apply the following rules exactly.

### A. Preprocessing

1. Parse `timestamp` as a datetime.
2. Drop exact duplicate rows before doing any attribution logic.
3. Sort rows by `user_id`, then `timestamp`, then `channel`, then `campaign_name`.

### B. Conversion Identification

4. A conversion row is any row where:
   - `is_conversion == True`
   - and `revenue > 0`
5. A dataset may contain more than one conversion, including multiple conversions for the same user.
6. Each conversion must be attributed independently.

### C. Candidate Touchpoints

For a given conversion row, only rows for the same `user_id` are relevant.

Each relevant row must be assigned to exactly one of the following mutually exclusive categories, in this order:

1. `conversion_row`
   - the conversion row itself
2. `same_or_after_conversion`
   - any row with timestamp greater than or equal to the conversion timestamp, excluding the conversion row itself
3. `non_click`
   - any pre-conversion row whose `interaction_type` is not exactly `"click"`
4. `outside_lookback`
   - any pre-conversion click row older than the lookback window
5. eligible
   - any pre-conversion click row within the lookback window

### D. Attribution Rule

7. If at least one eligible click exists, assign 100% of that conversion’s revenue to exactly one winning channel:
   - choose the eligible click with the latest timestamp
   - if multiple eligible clicks share the same latest timestamp, choose the alphabetically smallest `channel`
   - if there is still a tie, choose the alphabetically smallest `campaign_name`
8. If no eligible clicks exist, assign 100% of the conversion revenue to the conversion row’s own `channel`.
9. Do not use fractional attribution.
10. Do not use impressions, opens, or visits as eligible marketing touchpoints.
11. Do not merge multiple conversions together before attribution.

## Required Scenario Evaluation

Your script must evaluate the attribution engine on the following five scenario datasets and save all five outputs into `/logs/verifier/notebook_variables.json`.

The JSON file must contain exactly one top-level key:

```json
{
  "scenario_results": {
    "...": {}
  }
}
```

The required scenarios are:

### 1. `base_case`
Run the engine on the raw CSV exactly as loaded.

### 2. `recent_email_click`
Take the base dataset and modify the most recent row satisfying:
- `channel == "Email"`
- `interaction_type == "click"`

Move that row’s timestamp to exactly 3 days before the earliest conversion timestamp in the base dataset.

### 3. `paid_search_override`
Take the base dataset and append this new row:

- `user_id`: same as the earliest conversion user
- `timestamp`: exactly 2 hours before the earliest conversion timestamp
- `channel`: `"Paid Search"`
- `interaction_type`: `"click"`
- `campaign_name`: `"Brand_Rescue"`
- `revenue`: `0.0`
- `is_conversion`: `False`

### 4. `tie_break_same_timestamp`
Take the base dataset and append these two rows, both exactly 90 minutes before the earliest conversion timestamp:

Row A:
- `channel`: `"Affiliate"`
- `interaction_type`: `"click"`
- `campaign_name`: `"Partner_A"`

Row B:
- `channel`: `"Paid Social"`
- `interaction_type`: `"click"`
- `campaign_name`: `"Social_A"`

All other fields should match the earliest conversion user, with `revenue = 0.0` and `is_conversion = False`.

### 5. `second_conversion_extension`
Take the base dataset and append the following four rows for the same converting user:

Row A:
- timestamp = 20 days after the earliest conversion timestamp
- channel = `"Direct Traffic"`
- interaction_type = `"visit"`
- campaign_name = `"Return_Visit"`
- revenue = `0.0`
- is_conversion = `False`

Row B:
- timestamp = 25 days after the earliest conversion timestamp
- channel = `"Email"`
- interaction_type = `"click"`
- campaign_name = `"Winback_25D"`
- revenue = `0.0`
- is_conversion = `False`

Row C:
- timestamp = 31 days after the earliest conversion timestamp
- channel = `"Display Ad"`
- interaction_type = `"impression"`
- campaign_name = `"Retargeting_Late"`
- revenue = `0.0`
- is_conversion = `False`

Row D:
- timestamp = 32 days after the earliest conversion timestamp
- channel = `"Direct Traffic"`
- interaction_type = `"visit"`
- campaign_name = `"Organic_Return"`
- revenue = `125.0`
- is_conversion = `True`

## Final JSON Requirements

The file `/logs/verifier/notebook_variables.json` must contain exactly:

```json
{
  "scenario_results": {
    "base_case": { ... },
    "recent_email_click": { ... },
    "paid_search_override": { ... },
    "tie_break_same_timestamp": { ... },
    "second_conversion_extension": { ... }
  }
}
```

No extra top-level keys are allowed.

## Additional Requirements

- All returned values must be JSON-serializable.
- `channel_totals` must map channel names to floats.
- `per_conversion` must be sorted by `conversion_timestamp`, then `user_id`.
- `channel_totals` must be sorted alphabetically by channel name before serialization.
- `conversion_timestamp` must be formatted using ISO 8601 without timezone, for example: `2026-05-01T15:30:00`
- The notebook file may be minimal, but it must be a valid `.ipynb` file.
