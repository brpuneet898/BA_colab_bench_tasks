# Q1 2024 Order Fulfillment Rate Report

The operations team for a multi-warehouse B2B distributor needs a quarterly fulfillment rate report by fulfillment region, used to identify which regions are under-serving demand.

## Input Data

All input files are located in `/workspace/data/`:

| File | Description |
|------|-------------|
| `customers.csv` | Customer master data |
| `warehouses.csv` | Warehouse master data (facility attributes; does not include region — see `warehouse_region_assignments.csv`) |
| `warehouse_region_assignments.csv` | Effective-dated `region` assignment for each warehouse. Columns: `warehouse_id`, `region`, `effective_from`, `effective_to`, `change_reason`, `recorded_at` |
| `products.csv` | Product catalog (reference only) |
| `carriers.csv` | Carrier master data (reference only) |
| `orders.csv` | Orders placed between 2023-10-01 and 2024-06-30, each assigned to a fulfilling warehouse via `assigned_warehouse_id` |
| `order_lines.csv` | Line items for each order in `orders.csv`. Columns: `order_id`, `line_id`, `product_id`, `quantity_ordered`, `unit_price`, `discount_pct`, `line_status`, `created_at` |
| `shipment_headers.csv` | One row per fulfillment document. Columns: `warehouse_id`, `shipment_id`, `carrier_id`, `transaction_type`, `event_date`, `tracking_number`, `freight_cost`, `carrier_service_level`, `created_at` |
| `shipment_line_items.csv` | Order-line-level quantity detail for documents in `shipment_headers.csv`. Columns: `warehouse_id`, `shipment_id`, `order_id`, `line_id`, `quantity` |

## Scope

Include all orders where `order_date` falls within Q1 2024 (2024-01-01 to 2024-03-31, inclusive). A fulfillment region is the region assigned to the order's `assigned_warehouse_id`, as recorded in `warehouse_region_assignments.csv`, in effect on the order's `order_date`.

This report is generated as of **2024-04-15**. For each in-scope order line, only fulfillment activity with `event_date` on or before 2024-04-15 is reflected — activity recorded after that date is not yet part of this report.

## Metrics

Compute the following for each region in `warehouse_region_assignments.csv`.

**Quantity ordered**: for each in-scope order line, its `quantity_ordered`. Sum this across all in-scope order lines belonging to that region.

**Quantity fulfilled (net)**: for each in-scope order line, the quantity from that line the customer has actually received and kept as of the 2024-04-15 cutoff, net of anything later returned or cancelled. Sum this across all in-scope order lines belonging to that region.

**Fill rate** = quantity fulfilled (net) / quantity ordered, rounded to 4 decimal places.

## Output Files

### `/workspace/region_fulfillment_report.csv`

One row per region in `warehouse_region_assignments.csv`. Columns in this exact order:

| Column | Type | Notes |
|--------|------|-------|
| `region` | string | |
| `quantity_ordered` | integer | |
| `quantity_fulfilled_net` | integer | |
| `fill_rate` | float | rounded to 4 decimal places |

Sort by `region` ascending.

### `/workspace/summary.json`

```json
{
  "total_quantity_ordered": <integer, sum of quantity_ordered across all regions>,
  "total_quantity_fulfilled_net": <integer, sum of quantity_fulfilled_net across all regions>,
  "overall_fill_rate": <float, total_quantity_fulfilled_net / total_quantity_ordered, rounded to 4 decimal places>,
  "best_performing_region": <string, region with the highest fill_rate>,
  "worst_performing_region": <string, region with the lowest fill_rate>
}
```
