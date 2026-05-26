# Multi-Touch Attribution Analysis

Build a Q1 2024 (January 1 – March 31) marketing attribution report that assigns revenue credit to channels using position-based attribution, then computes channel-level spend and ROAS.

## Attribution Model

Apply **position-based (U-shaped) attribution** to each conversion:

- **First touch**: 40% of conversion revenue
- **Last touch**: 40% of conversion revenue
- **Middle touches**: the remaining 20%, split equally among all intermediate positions

For paths with 1 or 2 eligible channels after deduplication, distribute revenue equally across them.

## Data Preparation Rules

Only `click` touchpoints are eligible for attribution. Exclude all impression touchpoints before applying any further data preparation steps.

**Direct traffic suppression**: a `direct` click touchpoint that occurs within **6 hours** of the immediately preceding click touchpoint by the same user must be reassigned to that preceding channel. If no immediately preceding click touchpoint exists within 6 hours, the `direct` touchpoint is kept as-is.

**Lookback window**: each channel has its own lookback window (in days) defined in `channel_config.csv`. Only include touchpoints that fall within that channel's `lookback_days` before the conversion timestamp.

**Channel deduplication**: within a single conversion's eligible path, if the same channel appears more than once, treat it as **one touchpoint**. Its position in the path is determined by its earliest appearance.

**Conversion path isolation**: each user may convert multiple times. A conversion's eligible touchpoints are those that occurred **after the user's immediately preceding conversion** (or from the start of the data if it is the user's first conversion) and before the current conversion timestamp.

## Cost and ROAS

Channel costs are defined in `channel_config.csv`. Q1 covers January, February, and March (3 calendar months).

Channels with zero spend have no ROAS.

**ROAS** = attributed revenue / spend (round to 2 decimal places).

## Input Data

- `/workspace/data/touchpoints.csv` — `touchpoint_id`, `user_id`, `channel`, `timestamp`, `touchpoint_type`
- `/workspace/data/conversions.csv` — `conversion_id`, `user_id`, `conversion_timestamp`, `revenue`
- `/workspace/data/channel_config.csv` — `channel`, `lookback_days`, `cost_per_click`, `monthly_flat_fee`

## Required Outputs

Save `/workspace/attribution_report.csv` with columns: `channel`, `attributed_revenue`, `spend`, `roas`
(use empty string or 0 for `roas` where spend is zero)

Assign the following **top-level notebook variables** (JSON-serializable scalars, rounded to 2 decimal places):

- `total_attributed_revenue` — sum of attributed revenue across all channels
- `paid_search_attributed_revenue`
- `display_attributed_revenue`
- `email_attributed_revenue`
- `organic_social_attributed_revenue`
- `direct_attributed_revenue`
- `roas_paid_search`
- `roas_display`
- `roas_email`
