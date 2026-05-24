You are a senior analyst on the monetization analytics team of a large digital publisher. The publisher serves multiple sponsored creatives (ads) inside an infinite-scroll content feed. When a user clicks, the billing system assigns credit to exactly one creative per pageview using a proprietary deterministic rule. Your task is to reverse-engineer this credit-assignment rule from labeled training data and apply it to compute billable click-through rates on a held-out evaluation set.

## Data

Files in `data/`:

- **`impressions_train.csv`** — One row per creative-impression per pageview (training). Includes `credited_click` (1 if credited, 0 otherwise).
- **`impressions_eval.csv`** — Same schema without `credited_click`.

- **`scroll_segments_train.csv`** / **`scroll_segments_eval.csv`** — Contiguous scroll segments per pageview with speed measurements.

- **`visibility_intervals_train.csv`** / **`visibility_intervals_eval.csv`** — Time intervals during which each creative was visible in the viewport, with average viewport share.

- **`creative_catalog.csv`** — Creative metadata: `creative_id`, `advertiser_id`, `creative_format`, `bid_cpm`.

## Goal

Determine the exact deterministic click-credit rule. The billing system considers attention-quality viewability signals; not all data sources may be needed, but all are potentially relevant. Apply the discovered rule to produce:

### Variables (notebook)

- **`n_train_impressions`** (`int`): Row count of `impressions_train.csv`.
- **`total_billable_impressions_eval`** (`int`): Billable impressions in the eval set. A "billable impression" meets the billing system's viewability criteria — determine what qualifies.

### Files

- **`/workspace/credited_clicks_eval.csv`** — One row per click-bearing pageview:
  - `pageview_id` (str)
  - `credited_creative_id` (str)

- **`/workspace/creative_ctr_summary.csv`** — One row per creative in the eval set:
  - `creative_id` (str)
  - `billable_impressions` (int)
  - `credited_clicks` (int)
  - `ctr` (float): credited_clicks / billable_impressions (0.0 if no billable impressions)
