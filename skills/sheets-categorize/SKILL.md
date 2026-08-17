---
name: sheets-categorize
description: Use when asked to categorize, classify, tag, or label rows in a Google Sheet by product/item description, and write the result back as a new column. Also use when someone wants a description-to-category mapping applied across a whole spreadsheet.
---

# Sheets Categorize

## Overview

One script, parameterized by spreadsheet ID, does the whole pipeline —
find the sheet, read it, classify, write back, verify. Classification is
still where the judgment lives, but it's now a real Anthropic API call made
by the script itself, not something the calling agent free-hands inline —
that keeps the skill runnable on its own, not just from inside a
conversation. This is a **mutating** skill — it writes to the user's real
spreadsheet — unlike the read-only `sheets-audit` skill, which this one
shares the fetch/sheet-discovery approach with.

## Usage

```
export ANTHROPIC_API_KEY=sk-...
python scripts/categorize.py <SPREADSHEET_ID> [--sheet NAME] [--column LETTER] [--dry-run]
```

Everything else (tab name, target column, batch sizes) auto-detects with a
safety check — see `--help` for the full option list. Always run
`--dry-run` first on a sheet you haven't touched before; it does every step
except the actual `gws` write calls.

## What the script does internally

1. Finds the real sheet/tab name (`gws sheets spreadsheets get`) — never
   assumes `Sheet1`.
2. Reads all rows (`gws sheets +read`).
3. Dedupes descriptions — exact, mechanical.
4. **Classifies each unique description via the Anthropic API** — the
   model picks its own taxonomy for the data it's given rather than using a
   hardcoded category list, because different spreadsheets need different
   categories.
5. Applies the mapping to every row, in original order — mechanical.
6. Auto-detects the first empty column after the existing headers and
   confirms it's actually empty before writing (asks for `--column`
   explicitly if not).
7. Writes back with `values update`, batched into **explicit bounded
   ranges** per call.
8. Reads the written range back and diffs it against what was meant to be
   written.

## Common mistakes

- **Calling `gws` via `subprocess.run(["gws", ...])` on Windows.** npm
  installs `gws` as a `.cmd` shim there, not a directly-executable binary —
  plain `subprocess.run` raises `FileNotFoundError`. Routing through
  `shell=True` "fixes" that but only for small payloads: `cmd.exe` caps the
  whole command line at ~8191 characters, and a few hundred spreadsheet
  rows of JSON blows past that even though direct process creation allows
  ~32767. **Resolve the shim's actual target and call it directly** — a
  `.cmd` shim on Windows just runs `node.exe <path>/run.js %*`; find that
  real path (see `_resolve_gws_command()` in `categorize.py`) and invoke
  `node.exe run.js <args>` with no shell involved. On POSIX `gws` is
  already a real executable/shebang script — this whole problem doesn't
  exist there, so don't add `shell=True` unconditionally, it breaks POSIX's
  argument handling instead.
- **Using `values append` for a batched write, called multiple times in a
  loop.** `append` searches for "a table" starting at the given range and
  writes after its last row — but once earlier append calls have added
  data, later calls in the same loop can mis-detect the table boundary and
  land in the wrong column, at the wrong row, or overlapping a previous
  batch. Observed in practice: repeated `append` calls anchored at the same
  single-cell range drifted into a completely different column, thousands
  of rows past the real data, with batches overlapping each other instead
  of continuing cleanly. The first call (into an empty column) worked fine
  — it's repetition that breaks it. **Use `values update` with an
  explicit, fully-bounded range per batch instead** (`G251:G500`, not
  `G251` or `G1`). `update` targets exactly the cells you name — no table
  detection, no drift, safe to retry or re-run a batch.
- **Not checking whether the target column is really empty first.** Writing
  a new column assumes there's nothing already there; the script checks
  before writing — don't skip that if modifying the flow.
- **Skipping the post-write verification.** A write that returns
  `updatedRange` without an error is not proof the range is what you
  intended — diff the actual sheet content against what you meant to
  write, every time, especially after any batched write.
- **Trusting the model's JSON output without stripping markdown fences.**
  Models sometimes wrap JSON in ` ```json ... ``` ` even when told not to
  — `classify_with_ai` strips that before parsing; don't remove the strip
  step to "simplify."
