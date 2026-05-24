# Cyber Week Promotional Post-Mortem

You are a Senior Retail Data Analyst at a global e-commerce brand. The VP of Global Sales has requested an urgent financial post-mortem for the recent "Cyber Week Promo". 

The specific promotional window, constraints, and business logic requirements are detailed in the VP's memo located at `/workspace/data/promo_strategy.txt`. 

## Data Sources

You have been provided with the following data files in `/workspace/data/`:
1. **`orders.csv`**: Contains all global transaction records (`order_id`, `order_date`, `region`, `category`, `revenue`, `cogs`).
2. **`returns.csv`**: Contains refund records for returned items (`return_id`, `order_id`, `return_date`, `refund_amount`).
3. **`promo_strategy.txt`**: Internal memo defining the promo window and calculation rules.

## Task Directives

Your goal is to calculate the total order volume, net profit, and return rate exclusively for the orders that originated during the Cyber Week promotional window. 

- **Global Market Representation:** Ensure all regional data is accurately captured and preserved in your calculations. No global region should be excluded.
- **Return Attribution:** Calculate the exact return rate and refund deductions for the promotional cohort. Note that refunds must be accurately attributed to the original purchase, regardless of when the return was physically processed.
- **Net Profit Formula:** Calculate net profit as `(Total Revenue - Total COGS) - Total Refunds` for the cohort. 

You must determine how to best ingest, clean, and merge this data to arrive at the true financial metrics.

## Required Outputs

### 1. Notebook Variables
You must define and store the following variables at the top-level scope of your notebook. Ensure they are explicitly named and cast to the correct JSON-serializable types:

| Variable Name | Type | Description |
|---|---|---|
| `total_promo_orders` | `int` | The total number of orders placed during the promo window. |
| `global_promo_net_profit` | `float` | The final net profit for the promo cohort (rounded to 2 decimal places). |
| `promo_return_rate` | `float` | The return rate for the promo cohort (`number of returned orders / total promo orders`), rounded to 4 decimal places. |

### 2. Exported CSV
Save your final metrics as a CSV file at exactly **`/workspace/promo_summary.csv`**. The file must contain exactly one row of data with the following exact column headers:
- `total_promo_orders`
- `global_promo_net_profit`
- `promo_return_rate`