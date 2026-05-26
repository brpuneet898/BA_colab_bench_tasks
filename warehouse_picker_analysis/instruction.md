Build a **picker productivity report** for a warehouse distribution center covering January–June 2024. The center operates three 8-hour shifts: DAY (06:00–14:00), SWING (14:00–22:00), and NIGHT (22:00–06:00 next day). Compute picks-per-labor-hour broken down by **zone** and **shift** to identify underperforming areas.

---

## Input Data

All files are located in `/workspace/data/`.

**`workers.csv`** — Worker master table
- `worker_id` (str), `name` (str), `hire_date` (date), `employment_type` (str: full_time / part_time / temp), `home_zone_id` (str), `is_trainer` (bool)

**`shifts.csv`** — Shift definitions
- `shift_id` (str), `shift_name` (str), `start_time` (str HH:MM), `end_time` (str HH:MM)

**`shift_assignments.csv`** — Daily worker-shift schedule
- `assignment_id` (str), `worker_id` (str), `shift_id` (str), `date` (date), `clock_in_time` (datetime), `clock_out_time` (datetime, may be null), `break_duration_min` (int), `is_training_shift` (bool), `trainee_worker_id` (str, null if not a training shift)

**`zones.csv`** — Warehouse zone master
- `zone_id` (str), `zone_name` (str), `zone_type` (str)

**`picks.csv`** — Individual pick events (~200,000 rows)
- `pick_id` (str), `worker_id` (str), `sku_id` (str), `bin_id` (str), `zone_id` (str, may be null), `quantity` (int), `pick_timestamp` (datetime), `order_id` (str), `batch_id` (str, null for single picks), `pick_type` (str: single / batch)

**`bins.csv`** — Storage bin master
- `bin_id` (str), `zone_id` (str), `aisle` (str), `bay` (int), `level` (str)

**`skus.csv`** — SKU master
- `sku_id` (str), `sku_name` (str), `category` (str), `weight_kg` (float), `uom` (str)

---

## Required Output

Save `productivity_report.csv` to `/workspace/productivity_report.csv` with exactly these columns:

| Column | Type | Description |
|---|---|---|
| `zone_id` | str | Zone identifier |
| `zone_name` | str | Zone display name |
| `shift_name` | str | DAY / SWING / NIGHT |
| `total_picks` | int | Clean picks attributed to this zone-shift combination |
| `total_labor_hours` | float | Total effective labor hours for pickers in this zone-shift (2 decimal places) |
| `picks_per_labor_hour` | float | total_picks / total_labor_hours (2 decimal places) |

Include only zone-shift combinations where both `total_picks > 0` and `total_labor_hours > 0`. Sort by `picks_per_labor_hour` descending.

---

## Required Notebook Variables

Assign the following at **top-level scope** (not inside functions or classes). All must be plain Python types (int, float) — not numpy or pandas types.

| Variable | Type | Description |
|---|---|---|
| `total_raw_picks` | int | Total rows loaded from picks.csv before any cleaning |
| `duplicate_picks_removed` | int | Exact duplicate rows removed |
| `orphaned_picks_removed` | int | Picks whose worker_id has no record in workers.csv or shift_assignments.csv |
| `out_of_window_picks_removed` | int | Picks whose pick_timestamp falls outside the worker's shift window |
| `training_picks_removed` | int | Picks excluded because the worker was on a training shift |
| `null_clockout_count` | int | Shift assignments where clock_out_time is missing |
| `valid_labor_hours_total` | float | Sum of all effective labor hours used in the productivity calculation (2 decimal places) |

---

## Data Cleaning

The picks data has quality issues common to warehouse scanner systems. Clean it before computing any metrics:

- **Duplicates**: Remove exact duplicate rows.
- **Workforce coverage**: Exclude picks from any `worker_id` not present in `workers.csv` or `shift_assignments.csv`.
- **Shift window**: Each pick must be matched to the worker's shift assignment and fall within the actual clock-in/clock-out window. Picks outside this window are scanner bleed-over and should be excluded.
- **Training shifts**: Picks on shifts where `is_training_shift = True` do not reflect individual worker performance and must be excluded.
- **Zone**: Exclude picks where `zone_id` is null.

---

## Labor Hours

Effective labor hours per shift assignment = clocked duration minus `break_duration_min`. `clock_out_time` is occasionally missing; `shifts.csv` contains the scheduled shift times for reference. Labor hours are attributed to the worker's `home_zone_id`. All shift assignments contribute to the labor hour totals for their zone-shift, regardless of pick count.

---

## Notes

All timestamps are in US Central Time. No timezone conversion is needed. Notebook variables must be plain Python `int` or `float` — not numpy scalars or pandas types.
