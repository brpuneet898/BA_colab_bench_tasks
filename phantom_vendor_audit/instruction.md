You are working as a senior internal audit data analyst at a large, diversified organization. Your team has received an anonymous tip suggesting that a procurement fraud scheme involving “phantom vendors” may have been active within the company’s accounts payable system during Q3 2024 (July–September).

A phantom vendor refers to a fictitious or duplicate vendor record created to divert company funds. This type of scheme can include registering the same legitimate entity multiple times with slight variations in name, directing payments to a common bank account, and splitting invoices to remain below approval thresholds.

You have been provided with three raw data extracts for this period. Your task is to conduct a forensic audit: identify and reconcile vendor entities, uncover suspicious behavioral patterns, establish links between invoices and payments, and generate a ranked list of the most suspicious vendor entities along with supporting evidence.

## Data

- **Load** `/workspace/data/vendor_master.csv` into a pandas DataFrame called `vendor_df`, ensuring all rows (including the header) are included. Record the total number of rows as  `total_vendors` (int).

- **Load** `/workspace/data/ap_invoices.csv` into a pandas DataFrame called `invoice_df`, including all rows (with header). Store the row count as `total_invoices` (int).

- **Load** `/workspace/data/bank_payments.csv` into a pandas DataFrame called `payment_df`, making sure all rows (including the header) are loaded. Save the total number of rows as `total_payments` (int).

### Data Schemas

**vendor_master.csv**: `vendor_id`, `vendor_name`, `address`, `city`, `state`, `zip_code`, `tax_id_last4`, `business_unit`, `registration_date`, `status`, `contact_email`

**ap_invoices.csv**: `invoice_id`, `vendor_id`, `invoice_date`, `amount`, `currency`, `description`, `po_number`, `approver_id`, `business_unit`, `payment_status`, `gl_account`

**bank_payments.csv**: `payment_id`, `bank_account_token`, `payment_date`, `amount`, `vendor_id`, `business_unit`, `batch_id`, `payment_method`

## Audit Objectives

### 1. Entity Resolution

Vendor names in the master dataset may be intentionally modified to obscure true identity. These variations can include changes that bypass typical text similarity checks, such as abbreviations or expanded forms, differences in casing, and minor punctuation or spacing adjustments. As a result, multiple vendor IDs may correspond to the same underlying entity.

Group vendor IDs into clusters that are likely to represent the same real-world entity. Assign each vendor ID a canonical_id, which is a common identifier shared across all members of the cluster. For vendors that do not belong to any cluster, use their own vendor_id as the canonical_id.

Record the total number of clusters containing two or more vendor IDs as num_entity_clusters (int).

### 2. Behavioral Pattern Analysis

Examine invoice submission behavior across vendors, with a focus on how invoices are distributed by day of the week for each vendor or vendor cluster. Fraudulent vendors may display unusual timing patterns that deviate from typical submission behavior.

For each vendor, calculate the proportion of invoices submitted on weekends (Saturday or Sunday). Store these values in a dictionary mapping vendor_id to the corresponding fraction as weekend_fractions (dict[str, float]), ensuring all values are rounded to 4 decimal places.

Identify vendors for whom all invoices (100%) are submitted on weekends. Store their vendor IDs in a list called weekend_only_vendors (list[str]), sorted in alphabetical order.

### 3. Invoice Amount Splitting Detection

Fraudulent vendors may break down a large payable amount into several smaller invoices to bypass detection or approval limits. Identify sets of invoices from the same vendor, submitted either on the same date or on consecutive dates, where the combined amount equals a round figure (i.e., exact multiples of $1,000).

Record the total number of such identified split-invoice groups as num_split_groups (int).

### 4. Bank Account Token Analysis

The bank_account_token field in the payments dataset is a tokenized (hashed) version of the actual bank account number. This tokenization is applied using a business-unit-specific salt, which means that vendors sharing the same real bank account within the same business unit will have identical tokens, while the same account used across different business units will result in different tokens.

Identify all pairs of vendor IDs that share the same bank account token within a given business unit. Store the total number of such pairs as num_shared_token_pairs (int).

### 5. Suspicious Vendor Ranking

Integrate signals from entity resolution, weekend submission anomalies, invoice-splitting patterns, and shared bank-token relationships to compute a risk score for each canonical vendor entity. Use these scores to rank and identify the top 10 most suspicious entities.

Store the canonical_id of the highest-ranked entity as top_suspicious_vendor (str).

## Required Outputs

### CSV Files (save to `/workspace/`)

- **`/workspace/vendor_clusters.csv`** — Entity resolution mapping.
  - Columns: `vendor_id`, `vendor_name`, `canonical_id`, `cluster_size`

- **`/workspace/suspicious_vendors.csv`** — Top 10 suspicious vendor entities ranked by risk.
  - Columns: `rank`, `canonical_id`, `risk_score`, `vendor_ids`, `evidence_summary`
  - `vendor_ids`: pipe-delimited list of vendor IDs in the cluster (e.g., `V-1001|V-1002|V-1003`)
  - `evidence_summary`: brief text describing the fraud indicators found

- **`/workspace/invoice_payment_linkage.csv`** — Linkage between invoices and payments.
  - Columns: `invoice_id`, `payment_id`, `vendor_id`, `invoice_amount`, `payment_amount`, `match_type`
  - `match_type`: one of `exact`, `partial`, `unmatched`

### JSON File

- **`/workspace/investigation_report.json`** — Structured evidence report with the following top-level keys:
  - `phantom_vendor_clusters` *(list[dict])*: each dict has `canonical_id` (str), `vendor_ids` (list[str]), `evidence` (list[str])
  - `weekend_pattern` *(dict)*: keys `total_weekend_only_vendors` (int), `vendor_ids` (list[str])
  - `split_invoice_groups` *(int)*: count of detected split-invoice groups
  - `shared_bank_tokens` *(list[dict])*: each dict has `vendor_ids` (list[str]), `business_unit` (str), `token_prefix` (str — first 8 chars of the token)

### Variables (captured in `notebook_variables.json`)

The following variables must be created directly in the notebook scope (not within any function):

- `total_vendors` *(int)* — total row count from vendor_master.csv
- `total_invoices` *(int)* — total row count from ap_invoices.csv
- `total_payments` *(int)* — total row count from bank_payments.csv
- `num_entity_clusters` *(int)* — total number of identified multi-vendor entity clusters
- `weekend_fractions` *(dict[str, float])* — dictionary mapping each vendor_id to its weekend invoice fraction, rounded to 4 decimal places
- `weekend_only_vendors` *(list[str])* — sorted list of vendor IDs whose invoices all fall on weekends
- `num_split_groups` *(int)* — total number of detected invoice-splitting groups
- `num_shared_token_pairs` *(int)* — total number of vendor pairs sharing a bank token within the same business unit
- `top_suspicious_vendor` *(str)* — canonical_id of the highest-ranked suspicious vendor entity
