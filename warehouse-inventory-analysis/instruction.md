A distribution company needs its June 2024 monthly warehouse inventory report. For each product held at each warehouse, the report must show the on-hand quantity and value, how much of that quantity is available to promise against new demand, and the cost of goods sold on everything shipped from that warehouse during June. Input files are in `/workspace/data/`; use only the files named below.

`warehouses.csv` lists each warehouse's name and region. `products.csv` identifies each product and its category. `receipts.csv` records inbound inventory — purchase-order receipts and customer returns — each with a quantity, a unit cost, and a hold_status. `shipments.csv` records outbound units shipped against customer orders, by warehouse. `transfers.csv` records inventory moved between two of the company's own warehouses, with the date it shipped from the source and the date it was received at the destination. `open_orders.csv` records customer orders placed but not yet shipped as of the report cutoff, June 30, 2024 — these reserve inventory against future fulfillment.

Inventory is costed FIFO: each receipt — whether a purchase-order receipt or a customer return — opens a cost layer at its warehouse, dated by its own receipt_date. Outbound units (shipments and transfers) draw from the oldest eligible cost layer first. A customer return is valued at the unit cost of the units originally shipped to the customer.

A receipt with hold_status `on_hold` is physically in the warehouse but is not yet eligible to be shipped or transferred; a receipt with hold_status `released` is.

A transfer's units leave the source warehouse's inventory on its ship_date. They do not become part of the destination warehouse's inventory until its receive_date — a transfer that has shipped but not yet been received as of June 30 belongs to neither warehouse.

Compute the following for each (product, warehouse) combination that appears in `receipts.csv`, as of June 30, 2024.

On-hand quantity and value (`on_hand_qty`, `on_hand_value`) reflect all inventory physically present, at its FIFO cost, regardless of hold status.

Allocated quantity (`allocated_qty`) is the total quantity reserved by open, unshipped orders for that product and warehouse.

Available-to-promise quantity (`available_to_promise_qty`) is on-hand quantity, less allocated quantity, less any quantity currently on hold.

Save `/workspace/inventory_position.csv` sorted by `product_id` then `warehouse_id`, with one row per (product, warehouse) combination present in `receipts.csv`, columns: `product_id`, `warehouse_id`, `on_hand_qty`, `on_hand_value`, `allocated_qty`, `available_to_promise_qty`. Round quantities and values to 2 decimal places.

For each shipment recorded in `shipments.csv` with a ship_date in June 2024, report `cogs_value`: the FIFO cost of the units shipped.

Save `/workspace/cogs_report.csv` sorted by `product_id`, then `warehouse_id`, then `shipment_id`, with one row per June shipment, columns: `shipment_id`, `product_id`, `warehouse_id`, `ship_date`, `quantity`, `cogs_value`. Round the same way.

Save `/workspace/summary.json` with keys: `combo_count` (integer), `total_on_hand_value`, `total_available_to_promise_qty`, `total_june_cogs` — all rounded to 2 decimal places.
