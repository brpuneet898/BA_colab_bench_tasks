Build a Q1 2024 (January 1 – March 31) marketing attribution report that assigns revenue credit to channels using position-based attribution, then computes channel-level spend and ROAS.

Attribution model: apply position-based (U-shaped) attribution to each conversion — first touch gets 40% of conversion revenue, last touch gets 40%, and the remaining 20% is split equally among all intermediate positions. For paths with 1 or 2 eligible channels after deduplication, distribute revenue equally across them.

Data preparation: only click touchpoints are eligible for attribution; exclude all impression touchpoints before applying any further data preparation steps.

Direct traffic suppression — evaluate this rule across each user's full click history before any per-conversion filtering. A direct click touchpoint that occurs within 6 hours of the immediately preceding click touchpoint by the same user must be reassigned to that preceding channel. If no immediately preceding click touchpoint exists within 6 hours, the direct touchpoint is kept as-is.

Lookback window — each channel has its own lookback window (in days) defined in channel_config.csv. Only include touchpoints that fall within that channel's lookback_days before the conversion timestamp.

Channel deduplication — within a single conversion's eligible path, if the same channel appears more than once, treat it as one touchpoint. Its position in the path is determined by its earliest appearance.

Conversion path isolation — each user may convert multiple times. All conversion events (including those later fully refunded) define path boundaries. A conversion's eligible touchpoints are those that occurred after the user's immediately preceding conversion (or from the start of the data if it is the user's first conversion) and before the current conversion timestamp. Only conversions with net revenue greater than zero receive attribution credit.

Cost and ROAS: channel costs are defined in channel_config.csv. Q1 covers January, February, and March (3 calendar months). Channels with zero spend have no ROAS. ROAS = attributed revenue / spend, rounded to 2 decimal places.

Input data:

/workspace/data/touchpoints.csv — touchpoint_id, user_id, channel, timestamp (US Eastern time), touchpoint_type; channel values reflect the naming conventions of the source system at export time and may differ in capitalisation or delimiter from channel_config.csv; the export includes interactions from all integrated sources including third-party networks not present in channel_config.csv

/workspace/data/conversions.csv — conversion_id, user_id, conversion_timestamp (UTC), revenue; a conversion_id may appear more than once if the purchase was subsequently refunded — refund rows carry a negative revenue value

/workspace/data/channel_config.csv — channel, lookback_days, cost_per_click (US cents), monthly_flat_fee (US cents)

Required outputs:

Save /workspace/attribution_report.csv with columns: channel, attributed_revenue, spend, roas (use empty string or 0 for roas where spend is zero).

Assign the following top-level notebook variables as JSON-serializable scalars rounded to 2 decimal places: total_attributed_revenue (sum of attributed revenue across all channels), paid_search_attributed_revenue, display_attributed_revenue, email_attributed_revenue, organic_social_attributed_revenue, direct_attributed_revenue, roas_paid_search, roas_display, roas_email.
