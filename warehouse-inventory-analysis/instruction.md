A company that distributes products needs their June 2024 monthly warehouse inventory report. For each product held at each warehouse, the report needs to show how much is on hand and what its worth, how much of that can still be promised to new customers, and the cost of everything that shipped out of that warehouse during June.

The input files are in `/workspace/data/`, only use the files named below: `warehouses.csv`, `products.csv`, `receipts.csv`, `shipments.csv`, `transfers.csv`, and `open_orders.csv`.

`warehouses.csv` has each warehouse's name and region. `products.csv` has each product's name and category. `receipts.csv` is inbound inventory, purchase order receipts and customer returns. `shipments.csv` is units that shipped out to customers, by warehouse. `transfers.csv` is inventory moved between two of the company's own warehouses, with a ship date from the source and a receive date at the destination. `open_orders.csv` is customer demand for a product at a warehouse that reserves inventory going forward.

Inventory is costed FIFO (first in, first out). Every receipt, whether its a purchase order receipt or a customer return, opens a cost layer at its warehouse, dated by its own `receipt_date`. Outbound units, shipments and transfers both, draw from the oldest eligible cost layer first. A customer return is valued at the unit cost of the units that were originally shipped to the customer.

A receipt with `hold_status` `on_hold` is physically sitting in the warehouse, but its not eligible to ship or transfer yet, a receipt with `hold_status` `released` is eligible.

A transfer's units leave the source warehouse on its ship date, they don't become part of the destination warehouse until its receive date. So a transfer that shipped but hasn't been received yet as of June 30 belongs to neither warehouse.

For each product and warehouse combination that shows up in `receipts.csv`, figure out the following as of June 30, 2024.

On-hand quantity and value is all inventory physically sitting in the warehouse, at its FIFO cost, no matter the hold status.

Available-to-promise quantity is what's on hand, minus whatever is still owed against open orders that hasn't shipped yet, minus whatever is currently on hold. That still-owed amount is `allocated_qty` on its own.

Save `/workspace/inventory_position.csv`, sorted by `product_id` then `warehouse_id`, one row per product and warehouse combination that appears in `receipts.csv`, with columns `product_id`, `warehouse_id`, `on_hand_qty`, `on_hand_value`, `allocated_qty`, `available_to_promise_qty`. Round quantities and values to 2 decimal places.

For every shipment in `shipments.csv` with a ship date in June 2024, report `cogs_value`, the FIFO cost of the units that shipped. Save `/workspace/cogs_report.csv`, sorted by `product_id`, then `warehouse_id`, then `shipment_id`, one row per June shipment, with columns `shipment_id`, `product_id`, `warehouse_id`, `ship_date`, `quantity`, `cogs_value`. Round it the same way.

Save `/workspace/summary.json` with keys `combo_count` (integer), `total_on_hand_value`, `total_available_to_promise_qty`, and `total_june_cogs`, all rounded to 2 decimal places.
