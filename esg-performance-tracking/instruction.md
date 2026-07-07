Build an FY2024 (January 1 – December 31) ESG performance report for a multinational conglomerate with several business units, each operating facilities across regions. For each combination of business unit, facility, and fiscal quarter, compute gross Scope 1 and Scope 2 emissions (metric tons CO2e) for facilities the business unit owned at any point during that quarter.

Each entry in `emissions_ledger.csv` is linked to a facility in `facilities.csv` through its `business_unit_id` and `facility_id`. A facility is considered owned by a business unit for the portion of FY2024 between its `ownership_start` and `ownership_end`; emissions ledger entries outside that ownership period are not attributable to the business unit.

Scope 2 entries may specify a reporting method in the `method` column — `location_based` or `market_based`, GHG Protocol's two accounting methods for the same physical activity. Entries reported under the market-based method are a separate accounting of that same activity and must be excluded from gross Scope 2 emissions, not summed in alongside it. Scope 1 entries are not subject to this distinction.

Input data:

`/workspace/data/business_units.csv` — business_unit_id, business_unit_name

`/workspace/data/facilities.csv` — business_unit_id, facility_id, region, ownership_start, ownership_end

`/workspace/data/emissions_ledger.csv` — entry_id, business_unit_id, facility_id, reporting_date, scope (`Scope 1` or `Scope 2`), transaction_type, quantity_tco2e, method (`location_based` or `market_based`, where applicable); attribute each entry to the fiscal quarter of its `reporting_date`

Required outputs:

Save `/workspace/esg_report.csv` with columns: business_unit_id, facility_id, quarter (`2024-Q1` through `2024-Q4`), gross_scope1_tco2e (float, rounded to 2 decimal places), gross_scope2_tco2e (float, rounded to 2 decimal places), total_gross_tco2e (float, rounded to 2 decimal places). Include one row per (business_unit_id, facility_id, quarter) with at least one `emissions_ledger.csv` entry attributable to the business unit in that quarter, sorted by business_unit_id then facility_id then quarter.

Save `/workspace/esg_summary.json` with keys: total_gross_emissions_tco2e (float, rounded to 2 decimal places, sum of total_gross_tco2e across all report rows), total_offset_credits_tco2e (float, rounded to 2 decimal places, total offset and renewable energy certificate credits retired by the facilities included in the report, over the same ownership-scoped periods as the report), facility_count_included (int, number of distinct business_unit_id/facility_id pairs included in the report), business_unit_with_highest_emissions (string, the business_unit_id with the highest total gross emissions across FY2024).
