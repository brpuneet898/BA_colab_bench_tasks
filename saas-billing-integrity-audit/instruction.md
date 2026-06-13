Perform an H1 2025 (January 1–June 30, 2025) billing integrity audit and write the results to `/workspace/billing_audit.csv` and `/workspace/summary.json`.

Input files are operational exports from billing, CRM, and platform systems. Data may contain inconsistent formats, synchronization issues, redundant fields, undocumented columns, and invalid records. Use only information relevant to the audit.

Exclude:

* Accounts designated as test accounts.
* Usage records designated as test usage.
* Invoices with `status = 'void'`.
* Invoices where `void_reason` is populated.
* Child invoices of consolidated invoices.

Business policies:

* Reduction amendments may generate contractual credits.
* Usage overages are subject to contractual billing.
* Recognised revenue for reseller accounts equals the gross invoice amount multiplied by `reseller_margin_pct`.
* When multiple SLA metrics breach in the same billing period for the same account, only the highest-severity breach generates a credit.
* Billing periods are contract-dependent.
* Historical FX rates govern currency conversion where required.
* Pricing, discounts, entitlements, amendments, credits, invoices, payments, and revenue recognition must follow contractual and system records.

Input files:

`/workspace/data/accounts.csv`

`/workspace/data/contracts.csv`

`/workspace/data/amendments.csv`

`/workspace/data/usage_records.csv`

`/workspace/data/invoices.csv`

`/workspace/data/invoice_line_items.csv`

`/workspace/data/payments.csv`

`/workspace/data/credit_memos.csv`

`/workspace/data/sla_records.csv`

`/workspace/data/fx_rates.csv`

`/workspace/data/products.csv`

`/workspace/data/price_book.csv`

Save `/workspace/billing_audit.csv` with columns:

* `discrepancy_type` (one of: `amendment_undercredit`, `unbilled_overage`, `reseller_overstatement`, `sla_credit_overcount`)
* `account_id`
* `contract_id` (leave empty for account-level discrepancies)
* `reference_id`
* `period`
* `billed_amount_usd`
* `correct_amount_usd`
* `discrepancy_usd` (abs(correct_amount_usd − billed_amount_usd), always ≥ 0)

Include one row per identified discrepancy.

Save `/workspace/summary.json` with keys:

* `discrepancy_bucket_1_usd`
* `discrepancy_bucket_2_usd`
* `discrepancy_bucket_3_usd`
* `discrepancy_bucket_4_usd`
* `total_discrepancy_usd`
* `accounts_with_discrepancies`
* `valid_invoice_count` (count of invoices in the input dataset after applying all exclusions, not restricted to H1)

The four discrepancy buckets must correspond to the four distinct `discrepancy_type` values in `billing_audit.csv`, one bucket per type.

All monetary values must be plain Python `float` values and all counts must be plain Python `int` values.
