---
name: sheets-audit
description: Use when asked to audit, check, or find problems in a Google Sheets orders/sales spreadsheet — duplicate rows, missing descriptions, outlier prices, top-selling products, or sales/returns totals. Also use when someone gives a spreadsheet ID and asks for a data-quality or sales summary.
---

# Sheets Audit

## Overview

Code counts, the agent thinks. This skill pulls order data out of a Google
Sheet via `gws`, saves it as a real local CSV, hands the CSV to a Python
script for exact arithmetic (`scripts/audit.py`), and only then interprets
the script's JSON output in prose. Never eyeball totals or spot-check
duplicates by reading rows manually — 2000-row spreadsheets are exactly
where manual counting silently gets it wrong.

Expects columns (case-insensitive, any order): `date`, `description`,
`quantity`, `price`, `total`, `status`. Paid rows are matched on the exact
status text `оплачен`.

## Workflow

1. **Get the spreadsheet ID.** Take it from the user's argument/message. If
   missing, ask — don't guess.
2. **Find the real sheet/tab name.** Don't assume `Sheet1` or `Лист1`:
   ```
   gws sheets spreadsheets get --params '{"spreadsheetId":"<ID>"}'
   ```
   Read `sheets[].properties.title` from the result. If several sheets
   exist, ask which one (or pick the one that matches the task).
3. **Fetch all rows and save the raw dump:**
   ```
   mkdir -p sheets-audit-output
   gws sheets +read --spreadsheet "<ID>" --range "<sheet title>" --format json > sheets-audit-output/raw.json
   ```
4. **Run the script** — this both writes the CSV and computes every number:
   ```
   python "<this skill's dir>/scripts/audit.py" sheets-audit-output/raw.json sheets-audit-output/data.csv > sheets-audit-output/audit.json
   ```
5. **Read `audit.json` and write the summary as a funnel** — general to
   specific, each step narrowing focus. Don't jump straight to row-level
   anomalies:
   1. **Общее** — `by_status`: total paid sum, total returns, total pending.
      What's the overall health?
   2. **По товарам** — `top_selling_products` (leaders by quantity, paid
      only) and `worst_net_products` / `best_net_products` (net total per
      product across all statuses — negative net means returns outweigh
      sales for that product).
   3. **Внутри товаров** — `price_anomalies`: rows priced far from that
      same product's own average (z-score > 2, needs 3+ samples of that
      product to be meaningful — say so if a flagged product has a small
      sample).
   4. **Отдельные строки** — `duplicates` (same date+description+total) and
      `empty_description_rows`. List them concretely (row numbers), don't
      just report a count.

## Quick reference — `audit.json` fields

| Field | Contents |
|---|---|
| `by_status` | count + sum per status value |
| `top_selling_products` | top 10 by quantity, paid orders only |
| `worst_net_products` / `best_net_products` | net total per product, all statuses |
| `top_expensive_orders` | top 5 rows by `total` |
| `duplicates` | groups of rows sharing date+description+total |
| `empty_description_rows` | rows with blank description |
| `price_anomalies` | rows where price deviates >2 stdev from that product's own mean |
| `row_count`, `csv_saved_to` | bookkeeping |

## Common mistakes

- **Assuming the tab is named `Sheet1`.** Always look it up (step 2) —
  real spreadsheets get renamed.
- **Reporting a duplicate/anomaly count without listing rows.** The point
  of running code on 2000 rows is precision; keep that precision in the
  summary — cite row numbers, not just "there are some duplicates."
- **Treating every flagged price anomaly as a data-entry error.** A
  z-score outlier on a product with only 3-4 samples is a weak signal —
  say so, don't present it with false confidence.
- **Skipping straight to anomalies.** Follow the funnel — general totals
  first, so the reader has context before the row-level detail.
