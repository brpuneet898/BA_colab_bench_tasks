# Q1 2024 Supplier Performance Scorecard

The procurement team requires a Q1 2024 performance scorecard for all active suppliers. The scorecard supports contract renewal decisions at the end of the quarter.

## Input Data

All input files are in `/workspace/data/`:

| File | Description |
|------|-------------|
| `suppliers.csv` | Master list of all suppliers |
| `supplier_contracts.csv` | Contract terms per supplier, including SLA thresholds, penalty rates, and effective dates |
| `purchase_orders.csv` | All purchase orders raised in Q1 2024 across three regional warehouses: AMER, EMEA, and APAC |
| `delivery_records.csv` | Delivery events against purchase orders |
| `regional_penalty_rates.csv` | Regional penalty adjustment multipliers, indexed by warehouse and contract tier |
| `product_uom_reference.csv` | Unit-of-measure definitions and EA-equivalent conversion factors |
| `quality_inspections.csv` | Quality inspection results per delivery (reference only) |
| `supplier_contacts.csv` | Supplier contact details (reference only) |
| `warehouse_metadata.csv` | Warehouse reference data (reference only) |

## Scope

Include all purchase orders where `order_date` falls within Q1 2024 (2024-01-01 to 2024-03-31, inclusive). All suppliers in `suppliers.csv` must appear in the output, even if they have no Q1 purchase orders.

## Metrics

Compute the following metrics per supplier across all Q1 purchase orders assigned to that supplier.

### On-Time Delivery Rate

A purchase order is **on time** if the net quantity received on or before `promised_delivery_date + grace_period_days` meets or exceeds `ordered_quantity`, where `grace_period_days` is drawn from the applicable contract.

`on_time_delivery_rate` = count of on-time POs / total Q1 POs for the supplier, rounded to 4 decimal places. Null for suppliers with no Q1 POs.

### Net Fill Rate

The net quantity received for a purchase order is the cumulative on-hand quantity from qualifying delivery events — stock received counts toward it; returns arising from non-conforming goods reduce it — regardless of when those events occurred.

`net_fill_rate` = sum of net quantities received across all Q1 POs / sum of `ordered_quantity` across all Q1 POs, rounded to 4 decimal places. Null for suppliers with no Q1 POs.

### SLA Breaches and Penalty

Use the contract in effect at the time of each purchase order's `order_date`. Where a supplier holds more than one contract, apply the one with the most recent `contract_effective_from` that covers the order date and has not been superseded as of that date. A purchase order has an **SLA breach** if either condition holds:

- The PO was not delivered on time (as defined above), or
- The net fill rate for that individual PO falls below `fill_rate_sla_threshold`

**Escalating penalty for repeat breaches:** Where a supplier has 6 or more SLA-breaching purchase orders in Q1, each breaching PO from the 6th onwards — evaluated in ascending `order_date` order, with `po_id` as a tiebreaker — is assessed at **twice** the standard `penalty_rate_pct`. The first five breaching purchase orders are always assessed at the standard rate.

Penalty for a breaching PO = `order_value_usd` × applicable `penalty_rate_pct` × regional multiplier from `regional_penalty_rates.csv`.

`total_penalty_usd` per supplier = sum of all PO-level penalties, **capped** at `max_penalty_cap_usd`. Use the cap from the supplier's most recently effective contract.

`sla_breach_count` = number of Q1 POs with an SLA breach (unaffected by whether escalation applies).

### Composite Score

`composite_score` = (`on_time_delivery_rate` × 0.6) + (`net_fill_rate` × 0.4), rounded to 4 decimal places. Null for suppliers with no Q1 POs.

## Output Files

### `/workspace/supplier_scorecard.csv`

One row per supplier from `suppliers.csv`. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `supplier_id` | string | |
| `supplier_name` | string | |
| `total_pos` | integer | 0 for suppliers with no Q1 POs |
| `on_time_delivery_rate` | float | null for suppliers with no Q1 POs |
| `net_fill_rate` | float | null for suppliers with no Q1 POs |
| `sla_breach_count` | integer | 0 for suppliers with no Q1 POs |
| `total_penalty_usd` | float | 0.0 for suppliers with no Q1 POs |
| `composite_score` | float | null for suppliers with no Q1 POs |
| `max_consecutive_breach_streak` | integer | 0 for suppliers with no Q1 POs; the length of the longest uninterrupted run of SLA-breaching purchase orders for that supplier, evaluated in ascending `order_date` order with `po_id` as a tiebreaker |

Sort by `composite_score` ascending (nulls last), then by `supplier_id` ascending as a tiebreaker.

### `/workspace/summary.json`

```json
{
  "total_penalty_assessed_usd": <float, sum of total_penalty_usd across all suppliers, rounded to 2 dp>,
  "suppliers_meeting_all_sla": <integer, count of suppliers with sla_breach_count == 0 AND total_pos > 0>,
  "worst_on_time_supplier_id": <string, supplier_id with the lowest on_time_delivery_rate among suppliers with total_pos > 0; ties broken by lowest supplier_id alphabetically>,
  "worst_fill_rate_supplier_id": <string, supplier_id with the lowest net_fill_rate among suppliers with total_pos > 0; ties broken by lowest supplier_id alphabetically>,
  "total_sla_breach_count": <integer, sum of sla_breach_count across all suppliers>
}
```
