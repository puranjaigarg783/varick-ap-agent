# AP Agent — CLAUDE.md

> **This file is the navigation layer.** It contains system-wide context that applies to every task, plus an index into domain-specific docs under `/docs`. Pull in the relevant doc when working on a component — don't carry the full spec in context.

> **Reasoning convention:** Throughout the spec docs, `// WHY:` blocks explain the reasoning behind decisions. These exist so you understand *intent*, not just *instruction* — enabling correct micro-decisions when the spec doesn't cover an edge case.

> **LOCKED DECISION** markers mean the decision is final. Do not revisit or propose alternatives. Implement exactly as written.

---

## Document Ownership

Three documents form the project's knowledge base. Each owns its domain. Reference, don't reproduce.

- **Assessment PDF** (`Varick_Take-Home_Assessment.pdf`) — The north star. Owns the SOP text, chart of accounts, 6 labeled invoices with expected GL codes, 10 unlabeled invoices, and deliverable requirements. When in doubt about business rules, the assessment is authoritative.
- **Architecture Decision Log** (`architecture-decisions.md`) — Owns the strategic reasoning: why LLM-as-orchestrator with deterministic tools, why no framework, why engineered weakness, why single agent. Read this for "why did we choose this approach?" context.
- **Spec docs** (`docs/*.md`) — Own implementation details: schemas, function signatures, rule logic, prompt design, error handling. Read these for "how exactly do I build this?" context.

---

## System Overview

The AP Agent is a Python CLI application that processes vendor invoices through an Accounts Payable workflow: PO matching → GL classification → prepaid/accrual treatment → approval routing → journal entry generation.

**Core architecture:** LLM as orchestrator (workflow reasoning + tool calls), deterministic functions as tools (GL classification + treatment + approval + journal entries), plain functions as action layer (posting + verification).

The agent receives an invoice and a system prompt containing the SOP. It reasons through the workflow step by step, calling tools to execute each stage. Every tool enforces its rules deterministically — the LLM cannot bypass the priority tree, fabricate a GL code, or override an approval threshold. The LLM decides what to do and when; the tools decide how to do it correctly.

// WHY: This pattern gives you agentic orchestration without sacrificing auditability. The LLM's reasoning trace is fully capturable — you can see why it called tools in a given order, how it handled edge cases, and where its judgment was wrong. The deterministic tools remain independently testable. The feedback loop can target both the orchestration layer (did the agent reason correctly about the workflow?) and the extraction layer (did it interpret the line item correctly?), not just extraction alone.

---

## Project Structure

```
ap-agent/
├── CLAUDE.md                  # This file — navigation layer
├── README.md                  # Setup, architecture, tradeoffs
├── pyproject.toml             # Dependencies: anthropic, pydantic, pytest
├── docs/                      # Domain-specific spec docs (see index below)
├── src/
│   ├── __init__.py
│   ├── models.py              # All Pydantic schemas         → docs/data-models.md
│   ├── db.py                  # SQLite setup, seed data      → docs/storage.md
│   ├── po_matching.py         # PO lookup (also a tool)      → docs/po-matching.md
│   ├── classification.py      # Priority rule tree (also a tool) → docs/classification.md
│   ├── treatment.py           # Prepaid/accrual logic (also a tool) → docs/treatment.md
│   ├── approval.py            # Routing + approve/reject (also a tool) → docs/approval.md
│   ├── journal.py             # Entry generation + balance (also tools) → docs/journal-entries.md
│   ├── tools.py               # Tool schemas + handlers      → docs/pipeline.md
│   ├── agent.py               # Agent loop + orchestration    → docs/pipeline.md
│   └── prompts.py             # Orchestrator system prompt    → docs/llm-extraction.md
├── eval/
│   ├── __init__.py
│   ├── runner.py              # run_eval() → EvalReport      → docs/eval-and-feedback.md
│   ├── labels.py              # Ground truth labels           → docs/eval-and-feedback.md
│   └── feedback.py            # Corrections + analysis        → docs/eval-and-feedback.md
├── data/
│   ├── invoices_labeled.json  # 6 labeled invoices           → docs/storage.md
│   ├── invoices_unlabeled.json # 10 unlabeled invoices       → docs/storage.md
│   ├── purchase_orders.json   # Seeded PO data               → docs/storage.md
│   └── corrections.json       # Pre-built corrections         → docs/eval-and-feedback.md
├── cli.py                     # CLI entry point              → docs/cli.md
└── tests/
    └── test_rules.py          # Unit tests                   → see Testing Strategy below
```

// WHY: One file per domain. The deterministic functions (`po_matching.py`, `classification.py`, `treatment.py`, `approval.py`, `journal.py`) are unchanged in their internal logic — they are now also callable as tools via `tools.py`. The agent loop in `agent.py` replaces the old hardcoded pipeline in `pipeline.py`. The system prompt in `prompts.py` now drives orchestration, not just extraction. The `eval/` directory is separate from `src/` because it's a consumer of the agent, not part of it.

---

## Dependencies

```toml
[project]
name = "ap-agent"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]
```

**No other dependencies.** No LangChain, no CrewAI, no web framework, no ORM. `sqlite3` is stdlib.

// WHY: The orchestration logic IS the thing being evaluated. Wrapping it in someone else's framework hides the design. Plain Python functions mean the assessors read YOUR logic directly.

---

## Development Commands

```bash
# Install dependencies (editable mode with dev extras)
pip install -e ".[dev]"

# Set API key (required for agent and eval suite)
export ANTHROPIC_API_KEY=sk-ant-...

# Initialize the SQLite database with seed data
python cli.py init-db

# Run the full demo (processes invoices, shows feedback loop)
python cli.py demo

# Run unit tests (deterministic rules, no LLM calls)
pytest tests/test_rules.py

# View agent reasoning trace for a processed invoice
python cli.py trace <invoice_id>
```

---

## Chart of Accounts

| Code | Account                          | Category |
|------|----------------------------------|----------|
| 1300 | Prepaid Expenses (General)       | Prepaid  |
| 1310 | Prepaid Software                 | Prepaid  |
| 1320 | Prepaid Insurance                | Prepaid  |
| 1500 | Fixed Assets                     | Asset    |
| 2000 | Accounts Payable                 | Liability|
| 2100 | Accrued Expenses (General)       | Accrual  |
| 2110 | Accrued Professional Services    | Accrual  |
| 5000 | Office Supplies                  | Expense  |
| 5010 | Software & Subscriptions         | Expense  |
| 5020 | Cloud Hosting & Infrastructure   | Expense  |
| 5030 | Professional Services — Legal    | Expense  |
| 5040 | Professional Services — Consulting| Expense |
| 5050 | Marketing & Advertising          | Expense  |
| 5060 | Travel & Entertainment           | Expense  |
| 5070 | Facilities & Maintenance         | Expense  |
| 5080 | Training & Development           | Expense  |
| 5090 | Telecom & Internet               | Expense  |
| 5100 | Insurance Expense                | Expense  |
| 5110 | Equipment (under $5,000)         | Expense  |

Note: 2000 (Accounts Payable) is not in the assessment's chart of accounts but is required for double-entry bookkeeping. All initial invoice bookings credit AP.

---

## Testing Strategy

### Unit Tests (`tests/test_rules.py`)

Test every branch of the classification rule tree with synthetic `ExtractedAttributes` objects. No LLM calls.

```python
def test_priority_1_physical_goods():
    attrs = ExtractedAttributes(is_physical_goods=True, is_equipment=False, ...)
    result = classify_line_item(attrs, unit_cost=50)
    assert result.gl_code == "5000"

def test_priority_2_equipment_under_5k():
    attrs = ExtractedAttributes(is_equipment=True, is_physical_goods=True, ...)
    result = classify_line_item(attrs, unit_cost=1800)
    assert result.gl_code == "5110"

def test_priority_2_equipment_over_5k():
    attrs = ExtractedAttributes(is_equipment=True, is_physical_goods=True, ...)
    result = classify_line_item(attrs, unit_cost=8500)
    assert result.gl_code == "1500"

# ... one test per branch
```

Test approval routing with synthetic invoice totals and department combinations.

Test journal entry generation for each treatment type.

Test accrual detection with various date combinations.

### Integration Tests (Eval Suite)

The eval suite (see `docs/eval-and-feedback.md`) IS the integration test. It runs the full agent end-to-end against labeled data.

### No Mocking the LLM

The eval suite calls the real Anthropic API. No mocked LLM responses.

// WHY: The LLM's actual behavior on these inputs is what we're evaluating. Mocking it would test nothing. The unit tests for deterministic components don't need the LLM.

---

## README Outline

The README must cover:

1. **One-paragraph summary** — what this is, how it works.
2. **Setup instructions** — clone, install deps, set `ANTHROPIC_API_KEY`, `python cli.py init-db`.
3. **Quick start** — `python cli.py demo` runs the full showcase. Or run individual commands for step-by-step exploration.
4. **Architecture** — the agent diagram (LLM orchestrator → deterministic tools → DB), why this split.
5. **Feedback loop demo** — how to reproduce the before/after improvement (or just run `demo`).
6. **Design decisions** — link to or inline the key decisions (LLM as orchestrator with deterministic tools; engineered initial weakness; etc.).
7. **Known limitations** — no materiality threshold (strict SOP), `one_time` with long service period expenses instead of prepaid, agent scope ends at AP (no cash disbursement), Engineering override never fires in test data.
8. **What this would look like in production** — ERP integration, real PO system, scheduled amortization posting, CI eval regression, auto-learning, REST API, materiality thresholds.

---

## Spec Doc Index

Read the relevant doc when working on a component. Each doc is self-contained for its domain.

| Doc | Covers | Read when working on |
|-----|--------|---------------------|
| `docs/data-models.md` | All Pydantic schemas, field invariants, locked decisions on enums | `src/models.py`, or any file that consumes/produces these types |
| `docs/storage.md` | SQLite schema (incl. conversation_traces table), seed data, invoice JSON encoding | `src/db.py`, `data/*.json` |
| `docs/po-matching.md` | PO lookup, tolerance validation, failure behavior | `src/po_matching.py` |
| `docs/llm-extraction.md` | System prompt design, information boundary, LLM config, prompt versioning, few-shot examples, regulatory edge case | `src/prompts.py` |
| `docs/classification.md` | Priority rule tree, multi-flag resolution, amortization months | `src/classification.py` |
| `docs/treatment.md` | Prepaid/accrual logic, treatment priority, INV-004 worked example | `src/treatment.py` |
| `docs/approval.md` | State machine, routing rules, human-in-the-loop API, expected routes | `src/approval.py` |
| `docs/journal-entries.md` | Entry generation (4 cases), balance verification, posting by mode | `src/journal.py` |
| `docs/pipeline.md` | Agent loop, tool schemas + handlers, ProcessingContext, transaction handling, mode semantics, error handling | `src/agent.py`, `src/tools.py` |
| `docs/eval-and-feedback.md` | Eval system, ground truth labels, feedback loop, engineered weakness, before/after report | `eval/*.py`, `data/corrections.json` |
| `docs/cli.md` | CLI commands, input interface, output format, demo sequence, trace command | `cli.py` |

---

*Every decision is locked. Every rule is explicit. Build it exactly as written.*
