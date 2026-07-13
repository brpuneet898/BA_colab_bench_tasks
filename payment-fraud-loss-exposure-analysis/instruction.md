# Q1 2024 Fraud Loss Exposure Report

The risk team at a payment processor requires a quarterly fraud loss exposure report covering Q1 2024 (January–March). The processor routes card-not-present transactions on behalf of its merchants through three acquiring gateways, and the report is reviewed by the risk committee to monitor loss exposure by merchant risk tier.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|--------------|
| `merchants.csv` | Master list of merchants (reference only) |
| `merchant_risk_tiers.csv` | Risk tier assigned to each merchant, with `tier_effective_from` and `tier_effective_to` recording the period each assignment applied |
| `gateway_metadata.csv` | Gateway reference data (reference only) |
| `transactions.csv` | All card-not-present transactions processed in Q1 2024 across gateways `ALPHA`, `BRAVO`, and `CHARLIE`. Each row records the `gateway_id` that processed the transaction, a `transaction_id`, and the `payment_instrument_id` used. |
| `fraud_disputes.csv` | One row per fraud case (`case_id`), recording the `reason_code` describing the cardholder's claim, the `filed_date`, and the transaction reported when the case was opened (`reported_gateway_id`, `reported_transaction_id`) |
| `dispute_resolutions.csv` | Resolution records for each `case_id`, recording `resolution_status` and `resolution_date`. A case may carry more than one resolution record as it moves through review. |
| `case_transactions.csv` | The transactions associated with each `case_id`, identified by `gateway_id` and `transaction_id` |

## Scope

Include all transactions where `transaction_date` falls within Q1 2024 (2024-01-01 to 2024-03-31, inclusive).

## Grouping

For each combination of merchant risk tier and calendar month that had at least one Q1 transaction, compute the metrics below. Assign each transaction to the risk tier that was in effect for its merchant on the transaction's `transaction_date`.

## Metrics

### Transaction Volume

`total_transaction_volume_usd` = sum of `amount_usd` across all Q1 transactions assigned to the tier/month.

`transaction_count` = count of those transactions.

### Confirmed Fraud Loss

A fraud case represents **confirmed fraud loss** when the cardholder's claim is that a transaction was unauthorized, and the case's most recently recorded resolution has the merchant held liable for the transaction amount. Cases whose most recent resolution remains unresolved, was decided in the merchant's favor, or whose claim concerns something other than an unauthorized transaction (for example, a billing or fulfillment complaint) do not count as confirmed fraud loss.

Confirmed fraud loss for a case is the sum of `amount_usd` across every transaction associated with that `case_id` in `case_transactions.csv`. Each transaction's loss is assigned to the tier/month determined by its own `transaction_date` and merchant.

### Velocity-Based Fraud Detection

Independent of dispute cases, a payment instrument (`payment_instrument_id`) used at three or more distinct merchants within any 48-hour window is considered a velocity-fraud cluster — a pattern consistent with a compromised instrument being tested across multiple merchants before detection. Every transaction using that payment instrument within such a window also counts as confirmed fraud loss, whether or not a dispute case was filed for it.

`confirmed_fraud_loss_usd` = sum of `amount_usd` for transactions belonging to a confirmed-fraud case or a velocity-fraud cluster, assigned to the tier/month of each underlying transaction. A transaction counted through one basis is not counted a second time if it also qualifies through the other.

`confirmed_fraud_count` = count of those transactions.

### Fraud Loss Rate

`fraud_loss_rate_bps` = `confirmed_fraud_loss_usd` / `total_transaction_volume_usd` × 10,000, rounded to 2 decimal places.

## Output Files

### `/workspace/fraud_loss_report.csv`

One row per (risk tier, month) combination with at least one Q1 transaction. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `risk_tier` | string | |
| `month` | string | `YYYY-MM` format |
| `total_transaction_volume_usd` | float | rounded to 2 decimal places |
| `transaction_count` | integer | |
| `confirmed_fraud_loss_usd` | float | rounded to 2 decimal places |
| `confirmed_fraud_count` | integer | |
| `fraud_loss_rate_bps` | float | rounded to 2 decimal places |

Sort by `risk_tier` ascending, then `month` ascending.

### `/workspace/summary.json`

```json
{
  "total_transaction_volume_usd": <float, sum of total_transaction_volume_usd across all rows, rounded to 2 dp>,
  "total_confirmed_fraud_loss_usd": <float, sum of confirmed_fraud_loss_usd across all rows, rounded to 2 dp>,
  "total_confirmed_fraud_count": <integer, sum of confirmed_fraud_count across all rows>,
  "overall_fraud_loss_rate_bps": <float, total_confirmed_fraud_loss_usd / total_transaction_volume_usd x 10,000, rounded to 2 dp>,
  "highest_loss_rate_tier": <string, the risk_tier with the highest fraud_loss_rate_bps when confirmed fraud loss and transaction volume are each aggregated across the full quarter for that tier>
}
```
