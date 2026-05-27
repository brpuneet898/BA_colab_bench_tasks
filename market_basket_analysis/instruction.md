You are a data analyst at a regional grocery chain. The merchandising team wants to understand which products customers tend to buy together, so they can redesign shelf layouts and plan cross-promotional offers.

You have been given three datasets exported from the point-of-sale (POS) system:

- `/workspace/data/transactions.csv` — every POS line item recorded in the system
- `/workspace/data/items.csv` — the product catalogue with item identifiers and names
- `/workspace/data/stores.csv` — store reference data (provided for context)

## Your Task

Perform a market basket association analysis on the transaction data to discover frequently co-purchased products. Use the Apriori algorithm with a minimum support of 0.03 and a minimum confidence of 0.30. Report only rules with exactly one antecedent and one consequent.

## Data Notes

transactions.csv columns:
- `transaction_id` — POS transaction identifier
- `basket_id` — customer basket identifier
- `item_id` — product identifier at time of sale
- `quantity` — number of units; negative values indicate a return
- `transaction_type` — transaction type; values are `regular` or `promo_bundle`
- `store_id`, `transaction_date` — store and date of sale

A basket_id may contain both purchases and returns for the same product.

items.csv columns:
- `item_id` — POS item identifier
- `item_name` — display name of the item
- `canonical_item_id` — stable product identifier
- `category` — product category

The POS system underwent a migration on 2024-07-01. After migration, some products were assigned new `item_id` values. The product catalogue contains entries for both pre- and post-migration identifiers.

## Required Output

Save your association rules to `/workspace/association_rules.csv` with the following columns (in this order):

| Column | Type | Description |
|---|---|---|
| `antecedent` | string | Product name on the left-hand side of the rule |
| `consequent` | string | Product name on the right-hand side of the rule |
| `support` | float | Fraction of baskets containing both items |
| `confidence` | float | P(consequent \| antecedent) |
| `lift` | float | Ratio of observed to expected co-occurrence |

The file must be sorted by `lift` in descending order. Break ties by `antecedent` in ascending alphabetical order.

Each product should appear under a single consistent name in the output.

## Required Notebook Variables

The following variables must be assigned at the top level of your notebook (not inside functions or classes) and must be plain Python integers so they can be serialised:

| Variable | Description |
|---|---|
| `raw_row_count` | Total number of rows in `transactions.csv` before any filtering |
| `return_rows_removed` | Number of rows excluded because they represent returned items |
| `contaminated_baskets_removed` | Number of basket_ids excluded from the analysis because they contained at least one promotional bundle transaction |
| `split_basket_count` | Number of `basket_id` values associated with more than one `transaction_id`, computed after removing return transactions and excluding basket_ids containing promotional bundle events |
| `valid_basket_count` | Number of baskets with at least two distinct products that were passed to the Apriori algorithm |
