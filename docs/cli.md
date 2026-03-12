# CLI Interface

> **Implements:** `cli.py`
> **Spec origin:** Section 15

---

## Input Interface Contract

**LOCKED DECISION — How invoices enter the system:**

Three layers, one flow:

```
JSON files (data/)  →  init-db loads into SQLite  →  CLI reads from DB  →  constructs Invoice model  →  calls process_invoice()
```

1. **Storage format:** Two JSON files. `data/invoices_labeled.json` is an array of 6 invoice objects. `data/invoices_unlabeled.json` is an array of 10 invoice objects. Each object conforms to the `Invoice` Pydantic schema. One file per set, not one file per invoice.

2. **Database loading:** `python cli.py init-db` reads all three JSON files (`data/invoices_labeled.json`, `data/invoices_unlabeled.json`, `data/purchase_orders.json`), inserts into `invoices`, `line_items`, and `purchase_orders` tables. Idempotent — drops and recreates on each run. After `init-db`, the JSON files are never read again. All operations work from the database.

3. **Function interface:** `process_invoice(invoice: Invoice, db, client, mode)` takes a Pydantic `Invoice` model. The caller is responsible for constructing it. The eval suite, shadow mode, and unit tests call this directly.

4. **CLI bridge:** `python cli.py process INV-001` queries the `invoices` and `line_items` tables, constructs the `Invoice` Pydantic model from the DB rows, and calls `process_invoice()`. The CLI is the user-facing interface; the function is the programmatic interface. Both go through the same pipeline.

**Not built:** stdin JSON parsing, REST API, file-watch ingestion, streaming input. The invoices are known test data preloaded into SQLite. Production would have an API endpoint or message queue — the README notes this.

// WHY: The assessment says "takes a vendor invoice as input." The concrete input is a Pydantic `Invoice` model passed to `process_invoice()`. The CLI and JSON files are convenience wrappers for the assessor to run the system. The function interface is what the eval suite and tests use. Keeping the DB as the single source of truth after init means every command (process, eval, shadow, status) reads from the same place — no file path juggling, no parsing at runtime.

---

## Commands

```
python cli.py process <invoice_id> [--mode normal|dry_run|shadow|auto]
python cli.py process-all [--mode normal|dry_run|shadow|auto]
python cli.py approve <invoice_id> [--by <n>]
python cli.py reject <invoice_id> --reason <reason> [--by <n>]
python cli.py eval
python cli.py shadow
python cli.py feedback apply-corrections <corrections_file.json>
python cli.py feedback analyze
python cli.py feedback report
python cli.py status [<invoice_id>]
python cli.py init-db
python cli.py demo
```

---

## Command Details

- `process`: Process a single invoice. If `--mode` is `normal` and approval is required, prints the approval routing and exits with status `pending_approval`. User calls `approve` or `reject` to continue.
- `process-all`: Process all invoices in the database. Respects mode flag.
- `approve` / `reject`: Human-in-the-loop actions. Only work on invoices in `pending_approval` status.
- `eval`: Run the eval suite on all 6 labeled invoices with `mode="auto"`. Prints the `EvalReport`.
- `shadow`: Process all 10 unlabeled invoices in shadow mode. Store proposals.
- `feedback apply-corrections`: Load corrections from a JSON file and store in the corrections table.
- `feedback analyze`: Analyze stored corrections, print error patterns.
- `feedback report`: Run eval twice (baseline prompt, then refined prompt) and print the before/after comparison.
- `status`: Print current status, flags, approval info, and journal entries for an invoice (or all invoices).
- `init-db`: Create the database, load all data from `data/invoices_labeled.json`, `data/invoices_unlabeled.json`, and `data/purchase_orders.json` into the `invoices`, `line_items`, and `purchase_orders` tables. Idempotent — drops and recreates tables on each run.
- `demo`: **The assessor command.** Runs the full showcase sequence end-to-end with no interaction required. Executes the following steps in order, printing a section header before each:

  1. **Init** — Fresh database (`init-db`)
  2. **Baseline Eval** — Process 6 labeled invoices with naive prompt (`mode="auto"`), print EvalReport showing ~77% GL accuracy
  3. **Shadow Mode** — Process 10 unlabeled invoices, print proposals
  4. **Apply Corrections** — Load `data/corrections.json` (ships with the repo, contains the 5 corrections from Section 14 of the spec)
  5. **Error Analysis** — Print grouped error patterns
  6. **Refined Eval** — Re-init DB, process 6 labeled invoices with refined prompt, print improved EvalReport
  7. **Before/After Report** — Print the full feedback loop comparison (see `docs/eval-and-feedback.md`)

  Step 6 requires a fresh DB because the labeled invoices were already processed in step 2. `init-db` resets state. The refined prompt is loaded by applying the corrections and regenerating the prompt with few-shot additions (same logic as `feedback report`).

  `data/corrections.json` is a pre-built JSON file containing the 5 corrections from the feedback loop spec. It ships with the repo. The assessor doesn't create it — the demo loads it automatically.

---

## Output Format

**LOCKED DECISION — Output is both SQLite and stdout. SQLite is the system of record. Stdout is the human-readable display.**

| Mode | Written to SQLite | Displayed to stdout |
|------|-------------------|-------------------|
| `normal` / `auto` | Yes — transaction committed | Full processing summary |
| `dry_run` | No — same code path, transaction **rolled back** | Shows what WOULD be posted |
| `shadow` | Yes — transaction committed, entries with `posted=0` | Proposal summary |

**Stdout is structured text tables, not JSON.** The assessor runs the CLI and reads the output — it needs to be scannable, not parseable. JSON is for machines; the CLI is for humans.

**Per-command output content:**

`process` / `process-all` — For each invoice, display:
- Invoice header: ID, vendor, PO match result (with tolerance %), department, total
- Line item table: index, description, amount, GL code, GL name, treatment, rule triggered
- Approval routing: required level, reason, override if applied
- Journal entries table: date, debit account, credit account, amount, description, status
- Balance check result: pass/fail with amounts
- Final status

`status` — Same as above but reads from DB (already processed). If invoice not yet processed, shows current status and any flags.

`eval` — Summary block followed by per-invoice detail:
- Aggregate accuracy: GL, treatment, approval, attribute (fraction + percentage)
- Failure list: invoice ID, line index, dimension, expected vs. actual
- Flagged items: INV-006 no-PO result, any tolerance failures

`shadow` — Same as `process-all` output but with a header indicating shadow mode and a note that nothing was posted.

`feedback report` — Before/after comparison:
- Baseline accuracy (all 4 dimensions)
- Corrections applied (count + list)
- Error patterns identified
- Prompt changes made
- After accuracy (all 4 dimensions)
- Delta per dimension

`feedback analyze` — Error pattern summary: field name, pattern description, count of occurrences.

`approve` / `reject` — Confirmation line: invoice ID, action taken, decided by.

`demo` — Runs all steps sequentially. Each step prints a section header:
```
══════════════════════════════════════════════════
[1/7] Initializing database...
══════════════════════════════════════════════════
```
Followed by the output of each step (eval output, shadow output, feedback report). The full output is long but readable — it's the complete story from baseline to improvement in one terminal session.

**Not built:** JSON output flag, machine-readable export, CSV export. The assessors read stdout. Production would add structured output formats — the README notes this.

// WHY: The CLI output is the primary artifact the assessors interact with. It must show the full audit trail: what was extracted, what rule fired, what entries were generated, what approval was routed, whether the balance checks out. Structured text tables are the right format — dense enough to show everything, readable enough to scan.
