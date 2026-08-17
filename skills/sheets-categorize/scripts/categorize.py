#!/usr/bin/env python3
"""
sheets-categorize — end-to-end pipeline, parameterized by spreadsheet ID.

    python categorize.py <SPREADSHEET_ID> [options]

Does everything from one call:
  1. Finds the real sheet/tab name (gws sheets spreadsheets get).
  2. Reads all rows (gws sheets +read).
  3. Dedupes descriptions (mechanical — exact, no AI needed here).
  4. Classifies each unique description into a category via a real call to
     the Anthropic API (this is the part that needs judgment — an LLM does
     it, not a hand-written mapping). Requires ANTHROPIC_API_KEY.
  5. Applies the mapping to every row, in original order (mechanical).
  6. Writes the result back as a new column via gws, batched into explicit
     bounded ranges (`values update`, never a looped `values append` — see
     SKILL.md Common Mistakes for why that corrupts the sheet on repeat
     calls).
  7. Reads the written range back and diffs it against what was intended.

Options:
    --sheet NAME        Tab name (default: auto-detect; first GRID sheet)
    --column LETTER      Column to write into (default: auto-detect first
                          empty column after existing headers)
    --header TEXT         Header for the new column (default: Категория)
    --model NAME          Anthropic model (default: claude-sonnet-5)
    --batch-size N        Rows per write batch (default: 250)
    --classify-batch N    Descriptions per classification call (default: 150)
    --dry-run             Do everything except the actual gws write calls
    --api-key KEY         Overrides ANTHROPIC_API_KEY env var

Example:
    python categorize.py 19BWaMWgNjTtm2wS0pdXrt5ierT4Rp4fBDkdKyD3zJ94
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")


def _resolve_gws_command():
    """Build the argv prefix used to invoke gws.

    On Windows, npm installs `gws` as a .cmd shim. subprocess.run(["gws", ...])
    can't exec a .cmd file directly (FileNotFoundError), and routing through
    shell=True works only for small payloads — cmd.exe caps the whole command
    line at ~8191 chars, which a few hundred spreadsheet rows of JSON blows
    past even though direct process creation allows ~32767. So on Windows we
    skip the shim and invoke `node.exe run.js` directly, which is exactly
    what the shim itself does (see its source) — no shell, no extra limit.
    On POSIX, `gws` is a real executable/shebang script; call it as-is.
    """
    gws_path = shutil.which("gws")
    if gws_path is None:
        raise SystemExit("gws not found on PATH — install with: npm install -g @googleworkspace/cli")
    if platform.system() != "Windows":
        return [gws_path]
    npm_dir = os.path.dirname(gws_path)
    node_exe = os.path.join(npm_dir, "node.exe")
    run_js = os.path.join(npm_dir, "node_modules", "@googleworkspace", "cli", "run.js")
    if os.path.exists(node_exe) and os.path.exists(run_js):
        return [node_exe, run_js]
    return [gws_path]  # fall back to shell-based invocation if layout differs


GWS_CMD = _resolve_gws_command()
_NEEDS_SHELL = len(GWS_CMD) == 1 and GWS_CMD[0].lower().endswith((".cmd", ".bat"))


def run_gws(args, expect_json=True):
    """Run a gws command, return parsed stdout JSON (or raw text)."""
    proc = subprocess.run(
        GWS_CMD + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=_NEEDS_SHELL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gws {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
    if not expect_json:
        return proc.stdout
    text = proc.stdout.strip()
    if not text:
        return {}
    return json.loads(text)


def find_sheet_title(spreadsheet_id, requested=None):
    meta = run_gws(["sheets", "spreadsheets", "get",
                     "--params", json.dumps({"spreadsheetId": spreadsheet_id})])
    sheets = [s["properties"] for s in meta.get("sheets", []) if s["properties"].get("sheetType") == "GRID"]
    if requested:
        for s in sheets:
            if s["title"] == requested:
                return s
        raise SystemExit(f"Sheet '{requested}' not found. Available: {[s['title'] for s in sheets]}")
    if len(sheets) == 1:
        return sheets[0]
    raise SystemExit(f"Multiple sheets found, pass --sheet: {[s['title'] for s in sheets]}")


def fetch_all_rows(spreadsheet_id, sheet_title):
    result = run_gws(["sheets", "+read", "--spreadsheet", spreadsheet_id,
                       "--range", sheet_title, "--format", "json"])
    values = result.get("values", [])
    if not values:
        raise SystemExit("Sheet has no data")
    header = [h.strip().lower() for h in values[0]]
    return header, values[1:]


def find_empty_column(spreadsheet_id, sheet_title, header):
    """First column letter past the existing header columns; verified empty."""
    def col_letter(n):  # 1-indexed -> 'A', 'B', ... 'Z', 'AA'...
        s = ""
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    candidate = col_letter(len(header) + 1)
    probe = run_gws(["sheets", "+read", "--spreadsheet", spreadsheet_id,
                      "--range", f"{sheet_title}!{candidate}1:{candidate}10", "--format", "json"])
    if probe.get("values"):
        raise SystemExit(
            f"Column {candidate} isn't empty (found data in the first 10 rows) — "
            f"pass --column explicitly to a column you've confirmed is safe to overwrite."
        )
    return candidate


def extract_unique(header, rows):
    desc_idx = header.index("description")
    return sorted({row[desc_idx].strip() for row in rows if len(row) > desc_idx and row[desc_idx].strip()})


def classify_with_ai(descriptions, model, api_key, batch_size):
    """The judgment step: ask Claude to build description -> category."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    mapping = {}
    for i in range(0, len(descriptions), batch_size):
        chunk = descriptions[i:i + batch_size]
        prompt = (
            "You are classifying product/item descriptions from an order spreadsheet into a "
            "small set of sensible categories you choose yourself, based on what the products "
            "actually are. Use categories appropriate to this specific set of products (e.g. a "
            "hardware store and a grocery store need different categories) — aim for roughly "
            "5-12 categories total, each with more than one item where possible.\n\n"
            "Respond with ONLY a JSON object mapping each description (exact string, unchanged) "
            "to a single category name, in the input language. No markdown, no explanation, no "
            "code fences — just the raw JSON object.\n\n"
            "Descriptions:\n" + "\n".join(f"- {d}" for d in chunk)
        )
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            batch_mapping = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Model returned non-JSON output for batch {i}: {e}\n{text[:500]}")
        mapping.update(batch_mapping)
    missing = [d for d in descriptions if d not in mapping]
    if missing:
        print(f"WARNING: model didn't return a category for {len(missing)} descriptions: {missing[:10]}", file=sys.stderr)
    return mapping


def apply_mapping(header, rows, mapping, column_header):
    desc_idx = header.index("description")
    column = [[column_header]]
    for row in rows:
        desc = row[desc_idx].strip() if len(row) > desc_idx else ""
        column.append([mapping.get(desc, "")])
    return column


def write_column(spreadsheet_id, sheet_title, column_letter, values, batch_size, dry_run):
    """Batched `values update` with an explicit bounded range per call —
    NOT `values append` in a loop (see SKILL.md Common Mistakes: repeated
    append calls mis-detect the target table and drift to the wrong
    column/rows once earlier calls have already written data)."""
    for i in range(0, len(values), batch_size):
        chunk = values[i:i + batch_size]
        row_start, row_end = i + 1, i + len(chunk)
        rng = f"{sheet_title}!{column_letter}{row_start}:{column_letter}{row_end}"
        print(f"{'[dry-run] would write' if dry_run else 'writing'} {len(chunk)} rows -> {rng}")
        if dry_run:
            continue
        run_gws(["sheets", "spreadsheets", "values", "update",
                 "--params", json.dumps({"spreadsheetId": spreadsheet_id, "range": rng, "valueInputOption": "USER_ENTERED"}),
                 "--json", json.dumps({"values": chunk})])


def verify_write(spreadsheet_id, sheet_title, column_letter, expected):
    rng = f"{sheet_title}!{column_letter}1:{column_letter}{len(expected)}"
    result = run_gws(["sheets", "+read", "--spreadsheet", spreadsheet_id, "--range", rng, "--format", "json"])
    actual = result.get("values", [])
    # Sheets omits cells holding "" rather than returning ['']; normalize before comparing.
    norm = lambda v: [x if x else [""] for x in v]
    if norm(actual) == norm(expected):
        print(f"VERIFIED: {rng} matches exactly.")
        return True
    print(f"MISMATCH between written and expected values in {rng}.", file=sys.stderr)
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("spreadsheet_id")
    p.add_argument("--sheet")
    p.add_argument("--column")
    p.add_argument("--header", default="Категория")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--batch-size", type=int, default=250)
    p.add_argument("--classify-batch", type=int, default=150)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--api-key")
    args = p.parse_args()

    import os
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("No API key: pass --api-key or set ANTHROPIC_API_KEY")

    sheet = find_sheet_title(args.spreadsheet_id, args.sheet)
    title = sheet["title"]
    print(f"Sheet: {title}")

    header, rows = fetch_all_rows(args.spreadsheet_id, title)
    print(f"{len(rows)} data rows")

    column_letter = args.column or find_empty_column(args.spreadsheet_id, title, header)
    print(f"Target column: {column_letter}")

    unique = extract_unique(header, rows)
    print(f"{len(unique)} unique descriptions")

    mapping = classify_with_ai(unique, args.model, api_key, args.classify_batch)
    print(f"Classified into {len(set(mapping.values()))} categories")

    column_values = apply_mapping(header, rows, mapping, args.header)
    write_column(args.spreadsheet_id, title, column_letter, column_values, args.batch_size, args.dry_run)

    if not args.dry_run:
        verify_write(args.spreadsheet_id, title, column_letter, column_values)

    print(json.dumps({
        "sheet": title,
        "column": column_letter,
        "row_count": len(rows),
        "unique_descriptions": len(unique),
        "categories": sorted(set(mapping.values())),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
