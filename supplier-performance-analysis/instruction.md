# Q1 2024 Supplier Performance Scorecard

The procurement team requires a quarterly performance scorecard for all active suppliers covering Q1 2024 (January–March). The scorecard is used to support contract renewal decisions at the end of the quarter.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|-------------|
| `suppliers.csv` | Master list of all suppliers |
| `supplier_contracts.csv` | Contract terms per supplier, including SLA thresholds, penalty rates, and effective dates |
| `purchase_orders.csv` | All purchase orders raised in Q1 2024 across three regional warehouses: AMER, EMEA, and APAC |
| `delivery_records.csv` | All delivery events against purchase orders, including post-acceptance quantity adjustments |
| `quality_inspections.csv` | Quality inspection results per delivery (reference only) |
| `supplier_contacts.csv` | Supplier contact details (reference only) |
| `warehouse_metadata.csv` | Warehouse reference data (reference only) |

## Scope

Include all purchase orders where `order_date` falls within Q1 2024 (2024-01-01 to 2024-03-31, inclusive). All suppliers in `suppliers.csv` must appear in the output, even if they have no Q1 purchase orders.

## Metrics

Compute the following metrics per supplier across all Q1 purchase orders assigned to that supplier.

### On-Time Delivery Rate

A purchase order is **on time** if the goods were received on or before `promised_delivery_date`. To determine the fulfilled delivery date, use the received date of the latest delivery event with a positive received quantity for that purchase order.

`on_time_delivery_rate` = count of on-time POs / total Q1 POs for the supplier, rounded to 4 decimal places.

Suppliers with no Q1 POs: set to `null`.

### Net Fill Rate

The net quantity received for a purchase order is the algebraic sum of all `quantity_received` values across every delivery record for that order — this accounts for all quantity adjustments made after initial receipt.

`net_fill_rate` = sum of net quantities received across all Q1 POs / sum of `ordered_quantity` across all Q1 POs, rounded to 4 decimal places.

Suppliers with no Q1 POs: set to `null`.

### SLA Breaches and Penalty

Use the contract terms in effect on the **purchase order date** (`order_date`) to determine the applicable `fill_rate_sla_threshold` and `penalty_rate_pct` for that PO. A purchase order has an **SLA breach** if either condition holds:

- The PO was not delivered on time (received after `promised_delivery_date`), or
- The net fill rate for that individual PO falls below `fill_rate_sla_threshold`

Penalty for a breaching PO = `order_value_usd` × `penalty_rate_pct`.

`total_penalty_usd` per supplier = sum of all PO-level penalties, **capped** at `max_penalty_cap_usd`. Use the cap from the supplier's most recently effective contract.

`sla_breach_count` = number of Q1 POs with an SLA breach for that supplier.

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

Sort by `composite_score` ascending (nulls last), then by `supplier_id` ascending as a tiebreaker.

### `/workspace/summary.json`

```json
{
  "total_penalty_assessed_usd": <float, sum of total_penalty_usd across all suppliers, rounded to 2 dp>,
  "suppliers_meeting_all_sla": <integer, count of suppliers with sla_breach_count == 0 AND total_pos > 0>,
  "worst_on_time_supplier_id": <string, supplier_id with the lowest on_time_delivery_rate among suppliers with total_pos > 0>,
  "worst_fill_rate_supplier_id": <string, supplier_id with the lowest net_fill_rate among suppliers with total_pos > 0>,
  "total_sla_breach_count": <integer, sum of sla_breach_count across all suppliers>
}
```
