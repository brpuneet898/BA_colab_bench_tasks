# Warehouse Liquidation Strategy

Review the inventory data and merchandising memo, then create two liquidation lists and a final report.

## Files

Use the following input files from `/environment/data/`:

* `Q3_Inventory_Sales_Data.csv`
* `merchandising_strategy.txt`

## Business objective

Recommend two liquidation lists that free up warehouse space while respecting the merchandising strategy. The two lists must contain exactly:

* 50 SKUs
* 100 SKUs

The 100-SKU list should represent a broader liquidation plan. The 50-SKU list should be a tighter, more selective version of the same logic.

## Required analysis

Use the Q3 weekly sales data to evaluate each SKU’s normalized sales performance.

When doing this analysis:

* Treat sales as weekly sales across the Q3 period.
* Only use the portion of the sales history that was available after the SKU launched.
* Normalize any one-off sales spikes before using the data for ranking. A reasonable approach is to detect unusually large weekly values and replace them with a typical value for that SKU.
* Use the merchandising memo as a hard business constraint.

## Strategic rules

Your liquidation recommendation must obey all of the following:

1. **Protect Home Automation**

   * No SKU in the `Home Automation` category may be liquidated.

2. **Protect recent launches**

   * No SKU launched within the last 8 weeks may be liquidated.

3. **Respect dependencies**

   * If a SKU depends on a base SKU, then the base SKU and the dependent SKU must be treated consistently.
   * If a base SKU is selected for liquidation, all dependent SKUs that require that base SKU must also be selected.
   * Do not leave orphaned accessories behind.

4. **Priority order**

   * Damaged SKUs must be prioritized first.
   * Dead stock must be prioritized next.
   * Remaining slots should be filled by the least attractive SKUs based on margin density.
   * Within the same priority tier, break ties by total volume in descending order.

5. **Dead stock rule**

   * Any SKU that would take more than two years to deplete at its normalized sales rate should be considered dead stock.

## Output files

Create these files in `/workspace/`:

### 1. `liquidation_list_50.csv`

### 2. `liquidation_list_100.csv`

Each of these files must contain exactly three columns:

* `Priority_Rank`
* `SKU`
* `Product_Name`

Requirements for both files:

* The number of rows must match the list size exactly.
* `Priority_Rank` must start at 1 and increase sequentially.
* Rank items in priority order.
* Within the same priority tier, break ties by total volume in descending order.
* Do not include duplicate SKUs.
* Do not include protected SKUs.

### 3. `final_report.csv`

Create a one-row CSV containing the following columns:

* `reclaimed_space_100`
* `total_margin_density_100`
* `total_margin_100`
* `reclaimed_space_50`
* `total_margin_density_50`
* `total_margin_50`

Definitions:

* `reclaimed_space_*` = total cubic footage freed by the selected SKUs
* `total_margin_density_*` = total effective margin divided by total cubic footage
* `total_margin_*` = total effective margin of the selected SKUs

## Expected behavior

A correct solution should:

* load the inventory and memo from the data folder,
* inspect the data carefully,
* normalize anomalous sales values,
* apply the business protections,
* account for dependency relationships,
* and build liquidation lists that follow the stated priority order.

The notebook should present a clear, professional analysis and write the required CSV outputs exactly as specified.
