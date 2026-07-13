# Q1 2024 Supplier Performance Scorecard

The procurement team requires a quarterly performance scorecard for all active suppliers covering Q1 2024 (January–March). The scorecard is used to support contract renewal decisions at the end of the quarter.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|-------------|
| `suppliers.csv` | Master list of all suppliers |
| `supplier_contracts.csv` | Contract terms per supplier, including SLA thresholds, penalty rates, effective dates, and supersession dates |
| `purchase_orders.csv` | All purchase orders raised in Q1 2024 across three regional warehouses: AMER, EMEA, and APAC. Where a purchase order has been amended, the file contains one row per version; each row carries an `amendment_date` recording when those terms were established. The `quantity_uom` column records the unit of measure for `ordered_quantity`. |
| `delivery_records.csv` | All delivery events against purchase orders; each row carries a `delivery_type` column (`Primary`, `Return`, or `Rework`) indicating the nature of the event. `Rework` events record units identified for internal quality remediation. `Return` events carry a `return_basis` field recording the basis for the return. |
| `quality_inspections.csv` | Quality inspection results per delivery (reference only) |
| `supplier_contacts.csv` | Supplier contact details (reference only) |
| `warehouse_metadata.csv` | Warehouse reference data (reference only) |
| `regional_penalty_rates.csv` | Regional penalty adjustment multipliers, indexed by warehouse and contract tier |
| `product_uom_reference.csv` | Unit-of-measure definitions and EA-equivalent conversion factors |

## Scope

Include all purchase orders where `order_date` falls within Q1 2024 (2024-01-01 to 2024-03-31, inclusive). All suppliers in `suppliers.csv` must appear in the output, even if they have no Q1 purchase orders.

## Metrics

Compute the following metrics per supplier across all Q1 purchase orders assigned to that supplier.

### On-Time Delivery Rate

A purchase order is **on time** if the net quantity received by `promised_delivery_date + grace_period_days` meets or exceeds the `ordered_quantity`, where `grace_period_days` is the value from the applicable contract for that purchase order.

`on_time_delivery_rate` = count of on-time POs / total Q1 POs for the supplier, rounded to 4 decimal places.

Suppliers with no Q1 POs: set to `null`.

### Net Fill Rate

The net quantity received for a purchase order is the running on-hand quantity across qualifying delivery events for that order — Primary deliveries increase it and Return deliveries arising from non-conforming goods decrease it — regardless of when those qualifying events occurred.

`net_fill_rate` = sum of net quantities received across all Q1 POs / sum of `ordered_quantity` across all Q1 POs, rounded to 4 decimal places.

Suppliers with no Q1 POs: set to `null`.

### SLA Breaches and Penalty

Use the contract terms in effect at the time of each purchase order to determine the applicable `fill_rate_sla_threshold`, `penalty_rate_pct`, and `grace_period_days`. The effective penalty rate for each purchase order is the contract `penalty_rate_pct` multiplied by the regional adjustment from `regional_penalty_rates.csv`, indexed by the purchase order's `warehouse_id` and the applicable contract's `contract_tier`. Where a supplier has more than one contract record, apply the terms from the contract that was in effect on the purchase order's `order_date`, has not been superseded as of that date (recorded in `contract_superseded_by`), and has the most recent `contract_effective_from` as of that date. A purchase order has an **SLA breach** if either condition holds:

- The PO was not delivered on time (as defined above), or
- The net fill rate for that individual PO falls below `fill_rate_sla_threshold`

**Escalating penalty for repeat breaches:** Where a supplier has 6 or more SLA-breaching purchase orders in Q1, each breaching PO from the 6th onwards — evaluated in ascending `order_date` order, with `po_id` as a tiebreaker within the same date — is assessed at **twice** the standard `penalty_rate_pct`. The first five breaching purchase orders for that supplier are always assessed at the standard rate. Suppliers with fewer than 6 SLA breaches in Q1 are not subject to escalation.

Penalty for a breaching PO = `order_value_usd` × applicable `penalty_rate_pct` (standard or 2× escalated, as determined above).

`total_penalty_usd` per supplier = sum of all PO-level penalties, **capped** at `max_penalty_cap_usd`. Use the cap from the supplier's most recently effective contract.

`sla_breach_count` = number of Q1 POs with an SLA breach for that supplier (the count is unaffected by whether escalation applies).

### Composite Score

`composite_score` = (`on_time_delivery_rate` × 0.6) + (`net_fill_rate` × 0.4), rounded to 4 decimal places.

Suppliers with no Q1 POs: set to `null`.

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
| `max_consecutive_breach_streak` | integer | 0 for suppliers with no Q1 POs; the length of the longest uninterrupted run of SLA-breaching purchase orders for that supplier, evaluated in ascending `order_date` order with `po_id` as a tiebreaker within the same date |

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
