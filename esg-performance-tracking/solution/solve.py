"""
FY2024 ESG Performance Tracking Report.

facility_id in facilities.csv is only unique within a business_unit_id —
several facility codes are reused by two different business units each.
Joining emissions_ledger.csv to facilities.csv on facility_id alone would
create phantom cross-business-unit matches (row duplication, and ownership
dates borrowed from the wrong business unit's facility). The join key must be
["business_unit_id", "facility_id"].

A subset of (business_unit_id, facility_id) pairs were divested early in
FY2024 and reacquired later in the same year — they appear as TWO rows in
facilities.csv sharing the same composite key, each with its own
ownership_start/ownership_end. The join must NOT deduplicate facilities.csv
on (business_unit_id, facility_id) before merging: keeping both rows and
letting the ownership-window filter apply per row naturally unions the two
windows and excludes the gap quarter between them.

emissions_ledger.csv is a continuous facility-meter feed for all of FY2024,
independent of who owned the facility when — it is not pre-scoped to any
business unit's ownership window. Emissions attributable to a business unit
must be restricted to ledger rows whose reporting_date falls within
[ownership_start, ownership_end or open]. A plain `reporting_date <=
ownership_end` comparison evaluates to False for every NaT row, so the NaT
case must be handled explicitly
(`ownership_end.isna() | (reporting_date <= ownership_end)`).

Some verified_emission entries are later corrected by a companion
emission_restatement row that references the original entry via
original_entry_id. The corrected figure supersedes the original for gross
emissions purposes — the stale original must be dropped once a correction
exists, and the two must never be summed together. A further subset of
those restatements are themselves later corrected by a SECOND restatement,
whose original_entry_id points at the first restatement's entry_id rather
than at the original verified_emission entry. Excluding only entries
referenced by a restatement's original_entry_id where the excluded row is
itself a verified_emission misses this — an intermediate restatement that
has since been superseded must also be dropped. The correct rule excludes
ANY ledger row (verified_emission or emission_restatement) whose entry_id
appears anywhere in the original_entry_id column, regardless of chain depth.

Most Scope 2 verified_emission entries have a companion row for the same
facility, month, and emission source, distinguished only by a `method`
column: one `location_based`, one `market_based`. These are two GHG
Protocol accountings of the same physical activity, not two additive
quantities — summing both roughly doubles Scope 2. Only the location-based
method (or entries with no method value at all) counts toward the gross
figure. A subset of facilities do not do dual-method reporting at all —
their Scope 2 entries carry no method value, same convention as Scope 1.
Filtering with a literal `method == "location_based"` (rather than
`method.isna() | (method == "location_based")`) silently drops these
facilities' entire Scope 2 figure.

"Gross" Scope 1 / Scope 2 emissions means before any offsets or renewable
energy certificates are netted in. transaction_type == "purchased_offset" /
"renewable_energy_certificate" rows carry negative quantity_tco2e values;
they must be excluded from the gross figure, not summed together with
verified_emission/emission_restatement rows.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

if Path("/workspace/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("/workspace/data"), Path("/workspace")
elif Path("../environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("../environment/data"), Path("..")
elif Path("environment/data").exists():
    DATA_DIR, WORKSPACE_DIR = Path("environment/data"), Path(".")
else:
    DATA_DIR, WORKSPACE_DIR = Path("data"), Path(".")

FY_START = pd.Timestamp("2024-01-01")
FY_END   = pd.Timestamp("2024-12-31")


def load_data():
    business_units = pd.read_csv(DATA_DIR / "business_units.csv")
    facilities = pd.read_csv(
        DATA_DIR / "facilities.csv",
        parse_dates=["ownership_start", "ownership_end"],
    )
    ledger = pd.read_csv(DATA_DIR / "emissions_ledger.csv", parse_dates=["reporting_date"])
    return business_units, facilities, ledger


def scope_ledger_to_ownership(ledger, facilities):
    """Attach ownership window + region to each ledger row via the composite
    key (business_unit_id, facility_id), then keep only rows whose
    reporting_date falls within that business unit's ownership of the
    facility. NaT ownership_end means still owned (no upper bound)."""
    merged = ledger.merge(
        facilities[["business_unit_id", "facility_id", "region",
                    "ownership_start", "ownership_end"]],
        on=["business_unit_id", "facility_id"], how="inner",
    )
    in_window = (
        (merged["reporting_date"] >= merged["ownership_start"]) &
        (merged["ownership_end"].isna() | (merged["reporting_date"] <= merged["ownership_end"]))
    )
    return merged[in_window].copy()


def build_report(scoped_ledger):
    df = scoped_ledger.copy()
    df["quarter"] = "2024-Q" + ((df["reporting_date"].dt.month - 1) // 3 + 1).astype(str)

    # Entries later corrected by a restatement must use the corrected figure,
    # not the stale original — and never both. A restatement can itself be
    # superseded by a further restatement, so exclusion is not limited to
    # verified_emission rows: drop ANY row (of either type) whose entry_id
    # appears as someone else's original_entry_id, at any chain depth.
    superseded_ids = set(
        df.loc[df["transaction_type"] == "emission_restatement", "original_entry_id"].dropna()
    )
    is_verified = df["transaction_type"] == "verified_emission"
    is_restatement = df["transaction_type"] == "emission_restatement"
    gross_candidates = df[(is_verified | is_restatement) & ~df["entry_id"].isin(superseded_ids)].copy()

    # Scope 2 is reported under two GHG Protocol methods (location-based and
    # market-based) for the same physical activity — only one counts toward
    # the gross figure. Scope 1 rows have no method distinction (NaN).
    is_gross_method = gross_candidates["method"].isna() | (gross_candidates["method"] == "location_based")
    gross = gross_candidates[is_gross_method]

    pivot = (
        gross.groupby(["business_unit_id", "facility_id", "quarter", "scope"])["quantity_tco2e"]
        .sum().unstack("scope").fillna(0.0)
    )
    pivot = pivot.rename(columns={"Scope 1": "gross_scope1_tco2e", "Scope 2": "gross_scope2_tco2e"})
    for col in ("gross_scope1_tco2e", "gross_scope2_tco2e"):
        if col not in pivot.columns:
            pivot[col] = 0.0

    report = pivot.reset_index()
    report["total_gross_tco2e"] = report["gross_scope1_tco2e"] + report["gross_scope2_tco2e"]

    for col in ("gross_scope1_tco2e", "gross_scope2_tco2e", "total_gross_tco2e"):
        report[col] = report[col].round(2)

    return (
        report[["business_unit_id", "facility_id", "quarter",
                "gross_scope1_tco2e", "gross_scope2_tco2e", "total_gross_tco2e"]]
        .sort_values(["business_unit_id", "facility_id", "quarter"])
        .reset_index(drop=True)
    )


def build_summary(scoped_ledger, report):
    gross_total = float(round(report["total_gross_tco2e"].sum(), 2))

    offsets = scoped_ledger[
        scoped_ledger["transaction_type"].isin(["purchased_offset", "renewable_energy_certificate"])
    ]
    offset_total = float(round(offsets["quantity_tco2e"].abs().sum(), 2))

    facility_count = int(
        report[["business_unit_id", "facility_id"]].drop_duplicates().shape[0]
    )

    bu_totals = report.groupby("business_unit_id")["total_gross_tco2e"].sum()
    worst_bu = str(bu_totals.idxmax())

    return {
        "total_gross_emissions_tco2e": gross_total,
        "total_offset_credits_tco2e": offset_total,
        "facility_count_included": facility_count,
        "business_unit_with_highest_emissions": worst_bu,
    }


def main():
    business_units, facilities, ledger = load_data()

    scoped = scope_ledger_to_ownership(ledger, facilities)
    report = build_report(scoped)
    report.to_csv(WORKSPACE_DIR / "esg_report.csv", index=False)

    summary = build_summary(scoped, report)
    with open(WORKSPACE_DIR / "esg_summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
