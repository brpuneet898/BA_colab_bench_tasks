Q1 2024 Supplier Performance Scorecard
We need a Q1 2024 performance scorecard for all of our active suppliers. The procurement team uses this scorecard to decide which suppliers to renew contracts with on a quarterly basis.

INPUT DATA
All files are in /workspace/data/
suppliers.csv - contains our list of suppliers
supplier_contracts.csv - contains relevant contract terms for each supplier: SLA thresholds, penalty rates, effective dates. The max_penalty_cap column is expressed in the currency specific to a contract, not necessarily USD. Subsidiary suppliers do not have a max_penalty_cap value because their penalties are governed with other suppliers in the same group, according to supplier_hierarchy.csv
purchase_orders.csv - all purchase orders issued in the warehouses in Q1 2024. There are three warehouses: AMER, EMEA, and APAC. When a purchase order was revised, both the original and the revised purchase orders appear in this list for the same warehouse_id and po_id, with different amendment_dates. All fields are present for each row, including amendment_date
delivery_records.csv - delivery records related to purchase orders, including ship dates and received dates
regional_penalty_rates.csv - penalty rates for different warehouse regions and contract tiers
product_uom_reference.csv - conversion rates between different units of measures (UOM)
escalation_rules.csv - rules for penalty escalation, by contract_tier. breach_threshold - the number of breach events before penalties escalate. escalation_multiplier - the multiplier applicable to penalties from the point of escalation onwards.
fx_rates.csv - monthly fx rates in USD for different currencies, one row per currency per reporting_month. Relevant rates are those for reporting_month Jan-Mar 2024.
supplier_hierarchy.csv - parent-subsidiary relationships for 12 of our suppliers. Each row shows which subsidiary supplier_id belongs to which parent_company_id , and the group_penalty_cap_usd applicable to the group of suppliers
quality_inspections.csv - quality inspection data, reference only supplier_contacts.csv - supplier contacts, reference only warehouse_metadata.csv - warehouse metadata, reference only
SCOPE
All purchase orders with order_date between 2024-01-01 and 2024-03-31 (inclusive). All suppliers should appear in the result, even if they did not have purchase orders during this period.

METRICS
For each supplier, calculate the following values across all their purchase orders from Q1:
On-Time Delivery Rate
At the time of writing, a supplier purchase order is considered to be delivered on time if the net quantity delivered (by date of acceptance of Primary delivery minus Rejection returns) met/exceeded the ordered quantity. Net quantity is calculated as a cumulative sum of accepted Primary deliveries and Rejection returns (excluding internal events), using the dates on which risk transferred to us per incoterms on the purchase order:
For delivery event records, which date should we use to assess on-time delivery?
FOB, EXW, CIF -> use ship_date as the date on which risk transferred to us
DDP, DAP -> use received_date as the date on which risk transferred to us
The effective delivery date is the promised_delivery_date , adjusted for weekends: first weekday after promised_delivery_date if it falls on a Saturday/Sunday. Next, add grace_period_days business days (Mon-Fri only) as specified by the applicable contract to this date to arrive at the effective delivery deadline for the purchase order.
The on-time delivery rate is the percentage of purchase orders delivered on time. If a supplier did not have any purchase orders during this period, this metric is null.
Net Fill Rate
The net quantity delivered for a purchase order is the cumulative value of accepted Primary deliveries, less Rejection returns, and excluding any internal events. It is calculated as a running total, netting deliveries and returns across all events related to a purchase order, regardless of the date on which the event occurred.
The net fill rate for a purchase order is its net quantity delivered divided by the quantity ordered. The overall net fill rate for a supplier is the aggregate quantity delivered for all purchase orders in Q1, divided by the aggregate quantity ordered for these purchase orders. If a supplier did not have any purchase orders during this period, this metric is null.
SLA Breaches and Penalty
For each purchase order, use the contract that was in force on the order_date of that purchase order. In case of multiple contracts for a supplier, choose the one which is not superseded with the latest contract_effective_from date applicable to that order. A purchase order is a breach of SLA if it was not delivered on time, or has a fill rate below fill_rate_sla_threshold for that purchase order.
For calculating penalties, count the number of consecutive breach events for a supplier in Q1, across purchase orders, ordered by date of breach ascending, and with po_id as a tie-breaker. Once the number of breaches exceeds breach_threshold for a particular contract_tier (as defined in escalation_rules.csv), all additional breaches for that supplier are subject to a higher penalty rate with escalation_multiplier applied. For each breach, calculate the appropriate penalty based on the order value of the purchase order that was breached, according to the following logic.
Penalty for an individual breach = order_value_usd penalty_rate_pct regional_penalty_rate, with regional_penalty_rate defined in regional_penalty_rates.csv for the warehouse region of the purchase order, and the contract tier of the applicable contract for that purchase order. However, where the purchase order only failed the fill-rate SLA (i.e. it was delivered on time, but with insufficient quantity), the order_value_usd should be reduced by the percentage corresponding to the difference between the ordered and the actual fill-rates for that purchase order. For example, if a purchase order was filled at 90%, the value used to calculate penalty would be 0.9 order_value_usd.
The total penalty for a supplier is the sum of penalties for all breached purchase orders. Apply the following capping rules to the total penalty for a supplier:
For suppliers that are not listed in supplier_hierarchy.csv , cap the penalty at the max_penalty_cap defined in their latest contract (the one with highest contract_effective_from date). Express this value in USD using the fx_rates.csv for Mar 2024 (usd_per_unit column) and apply this cap to the total penalty for the supplier. If a supplier is listed in supplier_hierarchy.csv , determine the total penalty for all suppliers in the group with the same parent_company_id , cap this aggregated penalty at the group_penalty_cap_usd , and then distribute this penalty to individual suppliers in the group in proportion to their individual penalties. Do not apply max_penalty_cap from a supplier's own contract, since it is listed as null for subsidiary suppliers.
Calculate sla_breach_count as the total number of purchase orders that were breached.
Composite Score
The composite score is computed as: on_time_delivery_rate 0.6 + net_fill_rate 0.4, rounded to 4 decimal places. If a supplier did not have any purchase orders during this period, this metric is null.
OUTPUT FILES
/workspace/supplier_scorecard.csv
one row per supplier, with the following columns in this exact order:
supplier_id          string supplier_name         string total_pos           integer, 0 if the supplier did not have any purchase orders on_time_delivery_rate     float, null if the supplier did not have any purchase orders net_fill_rate         float, null if the supplier did not have any purchase orders sla_breach_count        integer, 0 if the supplier did not have any purchase orders total_penalty_usd       float, 0.0 if the supplier did not have any purchase orders composite_score        float, null if the supplier did not have any purchase orders max_consecutive_breach_streak integer, 0 if the supplier did not have any purchase orders this is the maximum number of consecutive breach events for the supplier (for example, if a supplier had 3 breach events for consecutive purchase orders, and another 4 breach events for another set of consecutive purchase orders, this value would be 4)
Order by composite_score ascending (nulls last), then by supplier_id ascending to break ties.
/workspace/summary.json
{
“total_penalty_assessed_usd”: sum of total_penalty_usd across all suppliers, rounded to 2 decimal places,
“suppliers_meeting_all_sla”: number of suppliers with sla_breach_count == 0 and total_pos > 0,
“worst_on_time_supplier_id”: the supplier id with the lowest on_time_delivery_rate among suppliers with total_pos > 0. In case of a tie between multiple suppliers, use the lowest supplier_id among them to break the tie.
“worst_fill_rate_supplier_id”: the supplier id with the lowest net_fill_rate among suppliers with total_pos > 0. In case of a tie between multiple suppliers, use the lowest supplier_id among them to break the tie.
“total_sla_breach_count”: sum of sla_breach_count across all suppliers
}
