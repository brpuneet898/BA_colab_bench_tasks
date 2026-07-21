A company that distributes products needs a report on what's in their warehouses for the month of June 2024. This report has to show how many of each product are in each warehouse what these products are worth how many can be promised to customers and the cost of the products that were shipped from each warehouse in June.

To make this report we will use files from the /workspace/data/ folder. We will only use the following files: warehouses.csv, products.csv receipts.csv shipments.csv transfers.csv and open_orders.csv.

The warehouses.csv file has the names and regions of the warehouses. The products.csv file has the names and categories of the products. The receipts.csv file has the products that were received including the quantity, cost. If they are on hold or not. The shipments.csv file has the products that were shipped to customers from each warehouse. The transfers.csv file has the products that were moved from one warehouse to another. The open_orders.csv file has the customer orders that were placed but not shipped yet.

The products in the warehouse are costed using the first first out method. When a product is received it is given a cost and a date. When products are shipped or transferred the oldest products are used first. If a customer returns a product it is valued at the cost as when it was originally shipped.

If a receipt is on hold it means the product is in the warehouse but cannot be shipped or transferred yet. If a receipt is released it means the product can be shipped or transferred.

When a product is transferred from one warehouse to another it leaves the warehouse on the ship date and arrives at the second warehouse on the receive date. If a product is transferred but not received yet it does not belong to either warehouse.

We need to calculate the following for each product in each warehouse:

* The. Value of the products that are in the warehouse no matter if they are on hold or not.

* The quantity of products that are reserved for orders.

* The quantity of products that can be promised to customers, which is the quantity in the warehouse minus the quantity reserved and the quantity on hold.

We will save this information in a file called /workspace/inventory_position.csv sorted by product and warehouse. The file will have the following columns: product id warehouse id, quantity in the warehouse value of products in the warehouse quantity reserved and quantity that can be promised.

We also need to report the cost of the products that were shipped in June. We will save this information in a file called /workspace/cogs_report.csv sorted by product, warehouse and shipment. The file will have the following columns: shipment id, product id warehouse id, ship date, quantity shipped and cost of the products shipped.

Finally we will save a summary of the information in a file called /workspace/summary.json. This file will have the number of product and warehouse combinations the total value of products in the warehouses the total quantity of products that can be promised and the total cost of the products shipped in June.

All of the quantities and values will be rounded to 2 places.

* The following files will be used:

* warehouses.csv

* products.csv

* receipts.csv

* shipments.csv

* transfers.csv

* open_orders.csv

* The report will have the following information:

*. Value of products, in each warehouse

* Quantity of products reserved for orders

* Quantity of products that can be promised to customers

* The report will be saved in the following files:

* /workspace/inventory_position.csv

* /workspace/cogs_report.csv

* /workspace/summary.json
