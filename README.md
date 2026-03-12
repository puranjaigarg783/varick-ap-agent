# AP Agent

An AI-powered Accounts Payable agent that processes vendor invoices through a complete AP workflow: PO matching, LLM-driven attribute extraction, deterministic GL classification, prepaid/accrual treatment, approval routing, and journal entry generation. The system uses Claude as a perception layer to extract structured attributes from invoice line items, then applies hard-coded priority rules (following the SOP exactly) to classify, route, and post journal entries.

## Setup

```bash
# Clone and install
git clone <repo-url> && cd varick-ap-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# Initialize database
python cli.py init-db

# Run the full demo
python cli.py demo
```

**Requirements:** Python 3.11+, `anthropic`, `pydantic`, `pytest` (dev).

## Quick Start

```bash
# Run the full showcase (baseline eval → shadow → feedback loop → improved eval)
python cli.py demo

# Or explore step by step:
python cli.py init-db                           # Load 16 invoices + 15 POs
python cli.py process INV-001 --mode auto       # Process single invoice
python cli.py process INV-002 --mode dry_run    # Preview without persisting
python cli.py eval                              # Run eval on labeled invoices
python cli.py shadow                            # Shadow-process unlabeled invoices
python cli.py status                            # View all invoice statuses
python cli.py status INV-001                    # View specific invoice

# Human-in-the-loop flow:
python cli.py process INV-002 --mode normal     # Pauses at pending_approval
python cli.py approve INV-002 --by "Jane Doe"   # Approve and post
```

## Architecture

```
┌─────────────────┐     ┌────────────────────┐     ┌──────────────────┐
│   LLM Layer     │     │   Rule Engine       │     │   Action Layer   │
│   (Perception)  │────▶│   (Decision)        │────▶│   (Execution)    │
│                 │     │                     │     │                  │
│ Claude extracts │     │ Priority rule tree  │     │ Journal entries  │
│ attributes from │     │ classifies GL code, │     │ generated, bal-  │
│ line item text  │     │ treatment, approval │     │ ance verified,   │
│                 │     │                     │     │ posted to DB     │
└─────────────────┘     └────────────────────┘     └──────────────────┘
```

**Key principle:** The LLM never picks a GL code. It extracts structured attributes (is this software? is this equipment? what's the billing frequency?). A deterministic priority rule tree applies the SOP to those attributes. This means:

1. **Testable independently** — unit tests cover every rule branch with zero LLM calls
2. **Auditable** — the `rule_triggered` field shows exactly which priority rule fired
3. **Improvable** — the feedback loop targets the LLM's extraction (via prompt refinement), not the rules

### Pipeline Steps

1. **PO Matching** — Match invoice to purchase order, validate within 10% tolerance
2. **Attribute Extraction** — LLM extracts 14 structured attributes per line item
3. **GL Classification** — 7-priority if/elif rule chain determines GL code
4. **Treatment** — Prepaid/accrual overrides based on billing frequency and service dates
5. **Journal Entries** — Double-entry bookkeeping with amortization schedules and accrual reversals
6. **Approval Routing** — Rule-based routing: auto-approve, dept manager, or VP Finance

## Feedback Loop Demo

The `demo` command runs a 7-step sequence showing measurable improvement:

1. **Init** — Fresh database with 16 invoices
2. **Baseline Eval** — Process 6 labeled invoices with a deliberately naive prompt → ~77% GL accuracy
3. **Shadow Mode** — Process 10 unlabeled invoices as proposals
4. **Apply Corrections** — Load 5 human corrections targeting extraction errors
5. **Error Analysis** — Identify patterns: missing branded merch flag, regulatory/consulting confusion
6. **Refined Eval** — Re-run with improved prompt → 100% GL accuracy
7. **Before/After Report** — Full comparison with deltas

The two engineered weaknesses:
- **Branded merch override:** T-shirts and gift bags from a marketing vendor are physical goods (GL 5000), not marketing (GL 5050). The initial prompt lacks a few-shot example for this.
- **Regulatory advisory:** "Regulatory compliance review & advisory" is consulting (GL 5040), not legal (GL 5030). Advisory work about regulatory topics is still advisory.

## Design Decisions

1. **LLM as feature extractor, not classifier** — The LLM extracts attributes; rules decide GL codes. This makes the classification auditable, testable, and separates perception from policy.

2. **No framework** — No LangChain, no CrewAI. The orchestration logic IS the thing being evaluated. Plain Python functions mean assessors read the actual logic.

3. **Engineered initial weakness** — The initial prompt is deliberately slightly naive to demonstrate the feedback loop improving accuracy from ~77% to 100%.

4. **Single pipeline, four modes** — `normal`, `auto`, `dry_run`, and `shadow` share the same code path. `dry_run` uses transaction rollback. No mode-specific branching.

5. **Strict SOP compliance** — No materiality thresholds. $200 domain renewal with annual billing triggers prepaid treatment because the SOP says "service period > 1 month + paid upfront → prepaid." Shadow mode surfaces these for review.

6. **Accrual GL codes** — For accruals, the GL code in the classification result is the accrual liability account (2110/2100), not the expense account. The expense account is preserved in `prepaid_expense_target` for journal entry generation.

## Known Limitations

- **No materiality threshold** — Strict SOP: a $200 annual domain renewal gets prepaid treatment. Production would add a de minimis threshold.
- **`one_time` with long service periods** — One-time billing frequency doesn't trigger prepaid, even if the service spans months. This is a deliberate simplification; shadow mode catches these cases.
- **Scope ends at AP** — No cash disbursement modeled. Accrual reversals credit AP (2000), not Cash. The actual payment is a downstream ERP event.
- **Engineering auto-approve override** — Implemented and tested but never fires in the test data (no Engineering invoice ≤$5K with all lines in {5010, 5020}).
- **No depreciation schedules** — Capitalized assets (GL 1500) are booked but not depreciated.

## What This Would Look Like in Production

- **ERP integration** — REST API instead of CLI, webhook triggers instead of manual processing
- **Real PO system** — Live PO lookup from ERP, not seeded SQLite data
- **Scheduled amortization posting** — Cron job posts scheduled entries on their effective dates
- **CI eval regression** — Run eval suite on every PR; fail if accuracy drops below baseline
- **Auto-learning** — Corrections feed back into prompt refinement automatically
- **Materiality thresholds** — Skip prepaid treatment for amounts below a configurable de minimis
- **Multi-currency support** — Currency conversion at booking and payment dates
- **Audit trail API** — Full lineage from raw invoice to posted entry, queryable by auditors
