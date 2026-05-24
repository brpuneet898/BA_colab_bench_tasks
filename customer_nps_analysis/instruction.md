We have customer survey data and need a calibrated NPS for our **corp** and **smb** segments. The two segments are separate engagements — handle them independently end-to-end (don't pool customers, weights, or targets across segments).

Three CSVs in `/workspace/data/`:

- `responses.csv`
- `customers.csv`
- `targets.csv`

The recency cutoff used everywhere below is the same global date for both segments: the maximum `response_date` in the entire `responses.csv` (call it `L`). Anything beyond 90 days before `L` is too old.

For each segment:

1. Use only customers in that segment whose `age_band` and `region` are both filled in.

2. From their responses, keep the rows that are `flag = "verified"`, `survey_type = "relationship"`, `score != 7`, and `response_date >= L - 90 days`. Each kept response gets a combined weight `weight * exp(-days_old / 30)`, where `days_old` is the integer day difference from that response to `L`.

3. We need at least two kept responses per customer to compute a stable representative score — they have to be on **two different days**, the customer's earliest and latest kept response must be **at least two weeks apart**, and the **earliest kept response itself has to land on or before `L − 30 days`**. For each remaining customer, the representative score is the weighted median of *all* their kept scores using the combined weights — sort their `(score, weight)` pairs by score ascending, and pick the smallest score whose cumulative weight first reaches at least half of the customer's total. 

4. Calibrate the panel via raking on both seg jointly:
   - Start every customer at weight `1.0`.
   - In each sweep, scale the weights by every level of `age_band` first.
   - Stop once the largest single-customer weight change between two consecutive sweeps drops below `1e-9`.

5. Once raking has converged, no single customer's calibrated weight may exceed `1.5`. The remaining customers keep the same weight ratios they had after raking, but the panel total still has to come out to `n`.

6. Compute the segment's calibrated NPS from these final weights.

In the notebook, set these variables for both segs:

- `customer_nps_<seg>` — calibrated NPS from the final weights, rounded to 2 decimals.
- `panel_size_<seg>` — number of customers in the calibrated panel.
- `promoter_count_<seg>` — unweighted count of customers whose representative score is `>= 9`.
- `detractor_count_<seg>` — unweighted count of customers whose representative score is `<= 6`.
