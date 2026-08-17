#!/usr/bin/env python3
"""
sheets-audit worker script.

Turns a raw `gws sheets +read --format json` dump into:
  1. A saved local CSV (the actual data, for the record / re-use).
  2. Exact, code-computed audit numbers (totals, top products, duplicates,
     empty descriptions, price outliers) printed as one JSON object.

The code does the counting. The calling agent does the interpreting.

Expected columns (case-insensitive, order doesn't matter): date,
description, quantity, price, total, status.

Usage:
    python audit.py <gws_values_json> <output_csv_path>
"""
import csv
import json
import statistics
import sys
from collections import defaultdict

# Windows' default stdout encoding is often a legacy codepage (e.g. cp1251),
# not UTF-8 — without this, redirecting stdout to a file silently corrupts
# any non-ASCII text (Cyrillic descriptions/status values) in the JSON output.
sys.stdout.reconfigure(encoding="utf-8")

PAID_STATUS = "оплачен"


def load_rows(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    values = data.get("values", [])
    if not values:
        raise SystemExit("No 'values' array found in gws JSON output")
    header = [h.strip().lower() for h in values[0]]
    rows = []
    # spreadsheet row numbers are 1-indexed and row 1 is the header
    for i, raw in enumerate(values[1:], start=2):
        row = {header[j]: (raw[j] if j < len(raw) else "") for j in range(len(header))}
        row["_row"] = i
        rows.append(row)
    return header, rows


def to_float(value):
    if value is None or value == "":
        return None
    s = str(value).strip().replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        s = s.replace(",", "")       # comma = thousands separator
    elif "," in s:
        s = s.replace(",", ".")      # comma = decimal separator
    try:
        return float(s)
    except ValueError:
        return None


def write_csv(header, rows, out_path):
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(h, "") for h in header])


def audit(rows):
    result = {}

    # ---- Step 1 (general): totals by status ----
    by_status = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for row in rows:
        status = (row.get("status") or "").strip() or "(без статуса)"
        total = to_float(row.get("total")) or 0.0
        by_status[status]["count"] += 1
        by_status[status]["sum"] += total
    result["by_status"] = {
        k: {"count": v["count"], "sum": round(v["sum"], 2)}
        for k, v in sorted(by_status.items(), key=lambda kv: -kv[1]["sum"])
    }

    # ---- Step 2 (by product): top sellers + which products net-lose money ----
    qty_paid = defaultdict(float)
    paid_orders = defaultdict(int)
    net_by_product = defaultdict(float)
    orders_all = defaultdict(int)
    for row in rows:
        desc = (row.get("description") or "").strip()
        if not desc:
            continue
        status = (row.get("status") or "").strip()
        qty = to_float(row.get("quantity")) or 0.0
        total = to_float(row.get("total")) or 0.0
        net_by_product[desc] += total
        orders_all[desc] += 1
        if status == PAID_STATUS:
            qty_paid[desc] += qty
            paid_orders[desc] += 1

    result["top_selling_products"] = [
        {"description": d, "total_quantity": q, "paid_orders": paid_orders[d]}
        for d, q in sorted(qty_paid.items(), key=lambda kv: -kv[1])[:10]
    ]
    result["worst_net_products"] = [
        {"description": d, "net_total": round(v, 2), "order_count": orders_all[d]}
        for d, v in sorted(net_by_product.items(), key=lambda kv: kv[1])[:10]
        if v < 0
    ]
    result["best_net_products"] = [
        {"description": d, "net_total": round(v, 2), "order_count": orders_all[d]}
        for d, v in sorted(net_by_product.items(), key=lambda kv: -kv[1])[:10]
    ]

    # Top-5 most expensive orders (by total, all statuses)
    with_total = [
        (row["_row"], row.get("date"), row.get("description"), to_float(row.get("total")), row.get("status"))
        for row in rows if to_float(row.get("total")) is not None
    ]
    result["top_expensive_orders"] = [
        {"row": r, "date": d, "description": desc, "total": total, "status": status}
        for r, d, desc, total, status in sorted(with_total, key=lambda t: -t[3])[:5]
    ]

    # ---- Step 4 (individual rows): duplicates + empty descriptions ----
    dup_groups = defaultdict(list)
    for row in rows:
        key = (row.get("date"), (row.get("description") or "").strip(), row.get("total"))
        dup_groups[key].append(row["_row"])
    result["duplicates"] = [
        {"date": k[0], "description": k[1], "total": k[2], "rows": v}
        for k, v in dup_groups.items() if len(v) > 1
    ]
    result["empty_description_rows"] = [
        {"row": row["_row"], "date": row.get("date"), "total": row.get("total"), "status": row.get("status")}
        for row in rows if not (row.get("description") or "").strip()
    ]

    # ---- Step 3 (within product): price outliers vs that product's own average ----
    prices_by_product = defaultdict(list)
    for row in rows:
        desc = (row.get("description") or "").strip()
        price = to_float(row.get("price"))
        if desc and price is not None:
            prices_by_product[desc].append((row["_row"], price))

    anomalies = []
    for desc, entries in prices_by_product.items():
        if len(entries) < 3:
            continue  # not enough samples to trust a mean/stdev for this product
        prices = [p for _, p in entries]
        avg = statistics.mean(prices)
        try:
            stdev = statistics.stdev(prices)
        except statistics.StatisticsError:
            stdev = 0.0
        if stdev == 0:
            continue
        for row_num, price in entries:
            z = (price - avg) / stdev
            if abs(z) > 2:
                anomalies.append({
                    "row": row_num,
                    "description": desc,
                    "price": price,
                    "product_avg_price": round(avg, 2),
                    "deviation_pct": round((price - avg) / avg * 100, 1),
                    "z_score": round(z, 2),
                })
    anomalies.sort(key=lambda a: -abs(a["z_score"]))
    result["price_anomalies"] = anomalies

    result["row_count"] = len(rows)
    return result


def main():
    if len(sys.argv) != 3:
        print("Usage: audit.py <gws_values_json> <output_csv_path>", file=sys.stderr)
        sys.exit(2)
    json_path, csv_path = sys.argv[1], sys.argv[2]
    header, rows = load_rows(json_path)
    write_csv(header, rows, csv_path)
    result = audit(rows)
    result["csv_saved_to"] = csv_path
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
