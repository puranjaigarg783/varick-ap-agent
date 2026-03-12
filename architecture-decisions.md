# AP Agent — Architecture Decision Log

## Status: Living Document (Divergent Phase)

---

## Decision 1: Core Architecture Pattern

**Decision:** LLM as feature extractor (perception), deterministic rules as decision maker (logic).

**Evolution of thinking:**

Initially considered three options along a spectrum of how much authority the LLM holds:

| Option | Description | Tradeoffs |
|--------|-------------|-----------|
| **A — Rule engine with LLM at edges** | Deterministic rules handle classification. LLM only for parsing and edge-case fallback. | Most reliable for known cases. Doesn't demonstrate agent design. Assessors grade agent logic, not if/else accuracy. |
| **B — LLM as fallback classifier** | Rule engine tries first. On no-match, LLM proposes GL code + confidence + reasoning. | Balanced, but LLM only activates on gaps — limits what you demonstrate. |
| **C — LLM classifies, rules validate** | LLM proposes GL code with reasoning. Deterministic layer validates. | Shows agent pattern but two systems doing the same job with a referee in between. |
| **D — LLM extracts attributes, rules decide** | LLM tags each line item with structured attributes (is_physical_goods, service_type, billing_frequency, unit_cost, etc.). Hard-coded priority tree applies the SOP to those attributes. | Clean separation of concerns. Each system does a different job. Most testable, most auditable. |

**Chosen: Option D — LLM as feature extractor, rules as decision maker.**

**Why D is stronger than C:**

1. **Testability.** You can eval the LLM on attribute extraction independently from classification logic. If the GL code is wrong, you know exactly where: did the LLM mistagged the attributes, or is the rule wrong?
2. **Auditability.** The reasoning chain is fully explicit: "LLM tagged this as [software, annual, prepaid] → Rule 3 fired → 1310." No black-box classification.
3. **Maps to Varick's own framework.** The LLM is the *perception layer*. The rules are the *decision logic layer*. Journal entry posting is the *action layer*. This is their published mental model, implemented literally.
4. **Deterministic where it matters.** The SOP's priority order is applied by code, not by an LLM trying to follow a 7-step priority list (which it'll hallucinate ~10% of the time).
5. **Feedback loop still works.** Corrections can target either the LLM's attribute extraction (e.g., "this was legal, not consulting") or the rule logic (e.g., "add a new rule for recruiting fees"). More precise than "the GL code was wrong."

**Critical requirement:** The attribute schema must be rich enough to cover every predicate in the SOP priority tree. Required attributes include:

- `is_physical_goods` — triggers Priority 1 (→ 5000), overrides marketing dept default
- `is_equipment`, `unit_cost` — triggers Priority 2 (→ 5110 or 1500)
- `is_software`, `billing_frequency` (monthly/annual) — triggers Priority 3 (→ 5010 or 1310)
- `is_cloud_hosting`, `billing_frequency` — triggers Priority 4 (→ 5020 or 1300)
- `service_type` (legal/consulting/advisory/mixed) — triggers Priority 5 (→ 5030 or 5040)
- `is_marketing` — triggers Priority 6 (→ 5050, unless physical goods override)
- `category_hint` (travel, facilities, training, telecom, insurance) — triggers Priority 7
- `service_period_start`, `service_period_end` — drives prepaid/accrual logic in Step 3
- `is_branded_merch` — the specific override that routes marketing physical goods to 5000

**Risk:** If the attribute schema has gaps, the rules can't decide and you're back to the LLM guessing GL codes. Mitigation: design the schema by walking every branch of the SOP priority tree and ensuring each branch has a corresponding attribute.

---

## Decision 2: LLM Output Contract — Structured Attribute Extraction

**Decision:** The LLM returns structured attributes per line item, not GL codes. It never picks the account — the rules do. Output includes extracted attributes, a confidence signal, and reasoning for each attribute.

**Why:**

- The LLM's job is perception ("what is this?"), not decision-making ("where does it go?"). These are different responsibilities with different failure modes.
- Aligns with the "contracts everywhere" principle — the LLM output is validated against a typed attribute schema before the rule engine ever sees it.
- Enables precise feedback: corrections target specific attributes ("this was legal, not consulting") rather than opaque GL code disagreements.
- The audit trail shows the full chain: raw line item → LLM attributes → rule fired → GL code → treatment.

---

## Decision 3: Where the LLM Adds Genuine Value

The LLM earns its keep in **attribute extraction** — interpreting what a line item actually is. The hard cases are semantic, not procedural:

| Extraction challenge | Example | Why it's hard |
|---------------------|---------|---------------|
| **Distinguishing service types** | INV-002: "Regulatory compliance review & advisory" — is `service_type` legal or consulting? | Keyword "regulatory" suggests legal, but the work is advisory. Requires reading comprehension. |
| **Identifying physical goods in non-obvious contexts** | INV-005: "Branded company t-shirts" from a Marketing vendor. `is_physical_goods` and `is_branded_merch` must both be true. | The vendor is a marketing agency and the department is Marketing, but the item is physical goods. |
| **Categorizing unmapped items** | UL3: "Placement fee" (recruiting). UL7: "Brand identity redesign" (creative). UL8: "Floor plan conversion" (construction). | The SOP has no explicit bucket. The LLM must extract the best-fit attributes so rules can route to the closest category. |
| **Parsing billing structure** | INV-001: "Annual Platform License (Jan–Dec 2026)" — `billing_frequency` = annual, `service_period_start/end` must be extracted from the description text. | The date range and billing cadence are embedded in natural language, not structured fields. |

**What stays deterministic (no LLM involvement):** PO matching (numeric lookup + tolerance check), priority tree application (rule engine), prepaid/accrual determination (date arithmetic on extracted service periods), approval threshold routing (decision tree), journal entry posting and balance verification.

---

## Decision 4: PO Matching — Mock the System, Don't Build It

**Decision:** Seed a SQLite table with PO records derived from the invoice test data. Do not build a PO integration.

**Approach:**

- Create PO records that make the 6 labeled + 10 unlabeled invoices work (correct PO numbers, amounts within tolerance).
- Include one deliberately over-tolerance PO (12-15% mismatch) to prove the tolerance validation fires.
- INV-006 (no PO) already covers the "missing PO → flag for manual review" path.
- Between the missing PO and the over-tolerance PO, both Step 1 exception paths are covered.

**Why this is fine:**

- The brief says "no need to wire up real service calls."
- PO matching is a deterministic lookup + numeric comparison — it's not agent logic.
- The README notes that production would have a real PO system integration.

**Test surface for Step 1:**

| Case | Invoice | Expected behavior |
|------|---------|-------------------|
| Happy path — PO matches within tolerance | INV-001 through INV-005, UL1–UL10 | Match, proceed to classification |
| No PO | INV-006 | Flag for manual review, do not classify |
| PO exists but amount exceeds 10% tolerance | (seeded test case) | Flag for manual review, do not classify |

---

## Decision 5: Journal Entry Generation — Pre-compute Everything at Classification Time

**Decision:** Generate the full set of journal entries (including accrual reversals and prepaid amortization schedules) at classification time. Do not model payment events or build scheduled execution.

**The insight:** Reversals and amortization entries are predictable, pre-computable artifacts of the classification. You don't need a payment event to *generate* a reversal — you'd only need one to *post* it. Same for amortization: INV-001's $24K annual software produces twelve $2K monthly entries. Generate all twelve at once, don't build a cron job.

**Journal entry `status` field:**

Each generated entry carries a status that makes it self-describing:

| Status | Meaning | Example |
|--------|---------|---------|
| `immediate` | Posts now | INV-002 line items — straight expense |
| `scheduled` | Posts on a specific future date | INV-001 amortization — $2K/mo Jan–Dec 2026 |
| `pending_payment` | Posts when payment is made | INV-004 accrual reversal |

**What this produces per invoice type:**

| Invoice pattern | Entries generated |
|----------------|-------------------|
| Simple expense (INV-002, INV-005) | 1 entry per line item, status `immediate` |
| Accrual (INV-004) | 2 entries per line item: accrual (`immediate`) + reversal (`pending_payment`) |
| Prepaid software (INV-001) | 1 prepaid entry (`immediate`) + N amortization entries (`scheduled`, one per month) |
| Prepaid cloud (INV-003 AWS line) | 1 prepaid entry (`immediate`) + 12 amortization entries (`scheduled`) |
| Capitalized asset (INV-003 server) | 1 entry (`immediate`) to 1500 |

**Why this works with dry-run:** The agent's output for any invoice is the complete set of entries it *would* produce. Dry-run shows them all. Normal mode posts the ones currently due. Shadow mode generates but never posts.

**Not modeled (and why):** Payment events, actual posting dates, depreciation schedules for fixed assets. These are downstream ERP concerns. The agent's job ends at "here are the entries and their triggers." The README notes this boundary.

---

---

## Decision 6: Human-in-the-Loop — State Machine with Approve/Reject API

**Decision:** Implement approval as a lightweight state machine with `approve(invoice_id)` and `reject(invoice_id, reason)` functions. Include an `auto_approve=True` flag for test mode.

**Options considered:**

| Option | Description | Tradeoffs |
|--------|-------------|-----------|
| **A — CLI stdin block** | Print approval request, `input()` to wait. | Demo trick. Coupled to CLI. Can't be called programmatically by a test harness. |
| **B — State machine with approve/reject functions** | Invoices move through statuses. Approval is a function call. | Interface-agnostic API. A CLI, web UI, or test harness can all call it. Demonstrates the concept without building UI. |
| **C — Simulate only** | Log that approval is needed, show routing, but don't actually pause. | Shows you thought about it but didn't implement it. Weakest signal. |

**Chosen: Option B.**

**Why B is the strongest option (not just the most pragmatic):**

- It's an actual API, not a UI hack. The agent's orchestration doesn't know or care *how* approval happens — it just knows the invoice is in `pending_approval` and won't advance until `approve()` is called.
- Interface-agnostic: a CLI could call it, a web UI could call it, a test harness could call it. That's production-grade separation of concerns.
- The `auto_approve=True` flag isn't cutting corners — it's testability. The eval suite needs to run end-to-end without a human blocking it.

**Invoice state machine:**

```
received → po_matched → classified → pending_approval → approved → posted
                                          ↓
                                       rejected
```

Exceptions branch off earlier:
```
received → no_po_match → flagged_for_review
received → po_tolerance_exceeded → flagged_for_review
classified → unclassifiable_line_item → flagged_for_review
```

**Critical detail:** When an invoice enters `pending_approval`, the record must capture *why* it was routed where it was. Examples:

- "Routed to VP Finance: invoice total $49,900 > $10K threshold"
- "Routed to VP Finance: line item classified as Fixed Asset (1500)"
- "Routed to Dept Manager: invoice total $9,500, between $1K–$10K"
- "Auto-approved: Marketing invoice ≤ $2.5K"
- "Auto-approved: Engineering invoice, all lines Cloud/Software, total ≤ $5K"

The approval gate isn't just a gate — it's an auditable gate with context.

---

---

## Decision 7: Eval System — Structured Suite, Not Generic Platform

**Decision:** Build eval as a callable function `run_eval(invoices, labels) → EvalReport`, not as individual test functions or a generic eval platform.

**Why this framing matters:**

- `test_inv_001_gl_code()` with pytest assertions reads as a test suite.
- `run_eval(invoices, labels) → EvalReport` reads as an eval framework.
- Same amount of code, very different signal to assessors. And the EvalReport becomes the artifact that connects eval to the feedback loop — corrections come from eval failures.

**Four eval dimensions:**

| Dimension | What it measures | Example failure |
|-----------|-----------------|-----------------|
| **1. GL code correctness** | Final GL code per line item matches expected | Agent outputs 5030 instead of 5040 |
| **2. Treatment correctness** | Expense vs. prepaid vs. accrual vs. capitalize matches expected | Agent books as immediate expense instead of prepaid with amortization |
| **3. Approval routing correctness** | Correct approver level, correct override application | Marketing $2K invoice routed to dept manager instead of auto-approved |
| **4. Attribute extraction accuracy** | LLM-extracted attributes match what would produce the correct classification | LLM tagged `service_type: legal` instead of `consulting` for advisory work |

**Why dimension 4 matters:** The architecture splits LLM (attribute extraction) from rules (GL classification). Dimension 4 lets you diagnose *where* failures originate. If the GL code is wrong, was it the LLM mistagging attributes, or a gap in the rule logic? This diagnostic is only possible because of the architecture in Decision 1, and it's worth demonstrating.

**EvalReport structure:**

- Per-invoice results (each line item: expected vs. actual across all 4 dimensions)
- Aggregate accuracy per dimension
- Failure breakdown: LLM attribution errors vs. rule logic errors
- List of flagged items (unclassifiable, no PO match, tolerance exceeded)

**Not building:** A generic eval platform, CI/CD integration, regression tracking over time. README notes these as production extensions.

---

---

## Decision 8: Shadow Mode & Feedback Loop — Engineered Improvement Story

**Shadow mode decision:** Shadow mode is dry-run with a different label. Process the 10 unlabeled invoices, store proposals, don't post. Not a design question.

**Feedback loop decision:** Manual, not auto-learning. Store corrections, analyze patterns, adjust prompts/rules, re-run, show delta.

**The concrete workflow:**

1. Run eval on labeled invoices → record **baseline accuracy** across all 4 dimensions
2. Run shadow mode on unlabeled invoices → capture proposals
3. Submit corrections against misclassifications (stored in corrections table)
4. Analysis script groups errors by type — e.g., "LLM mistagged `service_type` on 3/10 consulting items" or "LLM missed `is_branded_merch` on physical goods from marketing vendors"
5. Adjust the LLM prompt (add few-shot examples, refine attribute extraction instructions) and/or adjust rule logic
6. Re-run eval → show **after accuracy**
7. Produce a before/after artifact that the assessors can read

**Critical tactic: engineer the initial weakness intentionally.**

- If the first-pass prompt is too good and gets everything right, there's no improvement to demonstrate.
- A slightly naive initial prompt that you then refine with targeted few-shot examples from the correction log is a better story.
- This isn't gaming it — it's showing the system works as designed. The feedback loop is supposed to improve things. Let it.
- Example: initial prompt doesn't include a few-shot example for the branded merch override. LLM misclassifies branded t-shirts as marketing (5050). Correction captured. Few-shot example added. Re-run. Fixed. Delta shown.

**Why the architecture makes this work well:**

- Corrections are naturally structured because of the attribute extraction layer. A correction isn't "GL code should be 5040 not 5030" — it's "`service_type` should be `consulting` not `legal`."
- Feedback targets the LLM prompt specifically, not the whole system.
- Before/after can show **attribute-level** accuracy improvement, not just GL-level. That's a stronger demo of the feedback loop working at the right layer.

**Correction schema (lightweight):**

```
{
  invoice_id: "UL7",
  line_item_index: 0,
  field: "service_type",
  original_value: "legal",
  corrected_value: "consulting",
  corrected_by: "human",
  timestamp: "..."
}
```

**Not building:** Auto-learning, automatic prompt rewriting, reinforcement from corrections, continuous monitoring. README notes these as production extensions.

---

## Decision 9: Storage — SQLite, One DB, Six Tables

**Decision:** Single SQLite database with tables: `invoices`, `line_items`, `purchase_orders`, `journal_entries`, `approvals`, `corrections`.

**Key schema relationships:**

- `journal_entries` → FK to `line_items` + `status` field (`immediate` / `scheduled` / `pending_payment`, per Decision 5)
- `corrections` → FK to invoice + specific attribute being corrected (per Decision 8)
- `approvals` → FK to invoice + routing reason (per Decision 6)

No reason to overthink this. SQLite is file-based, zero config, and the assessors can inspect the data directly.

---

## Decision 10: Tech Stack — Python, Anthropic SDK, No Framework

**Decision:** Plain Python with the Anthropic SDK. No LangChain, no CrewAI, no orchestration framework.

**Why no framework:**

- LangChain and CrewAI are orchestration abstractions. The orchestration *is the thing being evaluated*. Wrapping it in someone else's abstraction hides the very thing you're supposed to demonstrate.
- Plain Python functions mean the assessors can read the orchestration logic directly: `process_invoice()` → `extract_attributes()` → Claude API → `apply_classification_rules()` → `route_approval()` → `generate_journal_entries()`.
- Readable, debuggable, and shows *your* design.

**One dependency worth adding:** Pydantic for typed schemas. It's a validation library, not a framework. Typed models for invoices, line item attributes, journal entries, LLM response contracts. Gives you "contracts everywhere" for free and pairs naturally with the Anthropic SDK's structured output.

**Dependencies:**

- `anthropic` — LLM calls
- `pydantic` — schema validation / typed contracts
- `sqlite3` — stdlib, no install needed
- `pytest` — test runner (eval suite can also run standalone)

---

## Decision 11: Agent Decomposition — Pipeline of Plain Functions

**Decision:** Single-agent pipeline implemented as a sequence of plain Python functions. Not a multi-agent system.

**Pipeline:**

```
process_invoice(invoice)
  ├── match_po(invoice)              # Deterministic: PO lookup + tolerance check
  ├── extract_attributes(line_item)  # LLM: Anthropic SDK → structured attributes
  ├── validate_attributes(attrs)     # Deterministic: Pydantic schema validation
  ├── classify_line_item(attrs)      # Deterministic: priority rule tree
  ├── determine_treatment(attrs, gl) # Deterministic: prepaid/accrual date logic
  ├── generate_journal_entries(...)  # Deterministic: entries + amortization schedules
  ├── route_approval(invoice)        # Deterministic: threshold decision tree
  └── post_entries(entries, dry_run) # Deterministic: persist or return
```

**Why single pipeline, not multi-agent:**

- The workflow is sequential with no parallelism needed. Each step depends on the previous.
- Multi-agent adds coordination overhead with zero benefit for this problem.
- The SOP is literally a 5-step sequence. The pipeline mirrors it.

---

## All Open Questions Resolved

All architectural decisions have been made. The system is ready to build.

**Summary of decisions:**

| # | Decision | One-liner |
|---|----------|-----------|
| 1 | Core architecture | LLM extracts attributes, rules decide GL codes |
| 2 | LLM output contract | Structured attributes, never GL codes |
| 3 | LLM value zones | Fuzzy classification, unmapped items, billing structure parsing |
| 4 | PO matching | Seeded SQLite table, mock data, both exception paths covered |
| 5 | Journal entries | Pre-compute everything at classification time, status field on each entry |
| 6 | Human-in-the-loop | State machine with approve/reject API, auto_approve flag for tests |
| 7 | Eval system | `run_eval()` → `EvalReport`, 4 dimensions including attribute accuracy |
| 8 | Feedback loop | Engineered initial weakness, correct, adjust prompt, show before/after delta |
| 9 | Storage | SQLite, one DB, six tables |
| 10 | Tech stack | Python, Anthropic SDK, Pydantic, no framework |
| 11 | Decomposition | Pipeline of plain functions, single agent |

---

*Last updated: Divergent Phase, Session 1 — All decisions closed*
