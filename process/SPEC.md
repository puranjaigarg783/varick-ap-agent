# AP Agent — Technical Specification

## Status: Phase 2 — Convergent Spec (Living Document)

> **Purpose of this document:** This is the single source of truth for the AP Agent build. It is written to be pasted directly into `CLAUDE.md` so that Claude Code has persistent, unambiguous context for every implementation decision. Every sentence is a constraint or a decision. If something is not in this document, it is not in scope.

> **Reasoning convention:** Throughout this spec, `// WHY:` blocks explain the reasoning behind decisions. These exist so that the implementing agent (Claude Code) understands *intent*, not just *instruction* — enabling it to make correct micro-decisions when the spec doesn't cover an edge case. They also serve as a reference for the human reviewing the spec.

---

## 1. System Overview

The AP Agent is a Python CLI application that processes vendor invoices through an Accounts Payable workflow: PO matching → attribute extraction (LLM) → GL classification (deterministic rules) → prepaid/accrual treatment → approval routing → journal entry generation.

**Core architecture:** LLM as perception layer (attribute extraction), deterministic rules as decision layer (GL classification + treatment + approval), plain functions as action layer (journal entry generation + posting).

// WHY: The LLM never picks a GL code. It extracts structured attributes from line item descriptions. A hard-coded priority rule tree applies the SOP to those attributes. This separation means: (1) you can test the LLM and the rules independently, (2) the audit trail is fully explicit, (3) the feedback loop can target the exact layer that failed.

---

## 2. Project Structure

```
ap-agent/
├── CLAUDE.md                  # This spec, verbatim
├── README.md                  # Setup, architecture, tradeoffs
├── pyproject.toml             # Dependencies: anthropic, pydantic, pytest
├── src/
│   ├── __init__.py
│   ├── models.py              # All Pydantic schemas (Section 4)
│   ├── db.py                  # SQLite setup, seed data, queries (Section 5)
│   ├── po_matching.py         # Step 1: PO lookup + tolerance (Section 6)
│   ├── attribute_extraction.py # LLM call: line item → attributes (Section 7)
│   ├── classification.py      # Step 2: priority rule tree (Section 8)
│   ├── treatment.py           # Step 3: prepaid/accrual logic (Section 9)
│   ├── approval.py            # Step 4: routing + state machine (Section 10)
│   ├── journal.py             # Step 5: entry generation (Section 11)
│   ├── pipeline.py            # Orchestrator: process_invoice() (Section 12)
│   └── prompts.py             # LLM prompt templates (Section 7.3)
├── eval/
│   ├── __init__.py
│   ├── runner.py              # run_eval() → EvalReport (Section 13)
│   ├── labels.py              # Ground truth for 6 labeled invoices
│   └── feedback.py            # Correction ingestion + analysis (Section 14)
├── data/
│   ├── invoices_labeled.json  # 6 labeled invoices
│   ├── invoices_unlabeled.json # 10 unlabeled invoices
│   ├── purchase_orders.json   # Seeded PO data
│   └── corrections.json       # Pre-built corrections for feedback loop demo (Section 14)
├── cli.py                     # CLI entry point (Section 15)
└── tests/
    └── test_rules.py          # Unit tests for deterministic components
```

// WHY: One file per pipeline step. Each file maps to one section of this spec. Claude Code can work on one file at a time without needing full-system context. The `eval/` directory is separate from `src/` because it's a consumer of the agent, not part of it. Data lives in `data/` as JSON files (invoices, POs), not as hardcoded Python dicts. The one exception is `eval/labels.py` — ground truth labels are part of the eval logic, not input data the agent processes.

---

## 3. Dependencies

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

## 4. Data Models (Pydantic Schemas)

All data flows through typed Pydantic models. These are the contracts between pipeline stages.

### 4.1 Invoice Input

```python
class LineItem(BaseModel):
    description: str          # Raw line item text, e.g. "Annual Platform License (Jan–Dec 2026)"
    amount: float             # Dollar amount for this line item
    quantity: int = 1         # Number of units (default 1 if not specified)
    unit_cost: float | None = None  # Per-unit cost if applicable. If null, unit_cost = amount / quantity.

class Invoice(BaseModel):
    invoice_id: str           # e.g. "INV-001", "UL1"
    vendor: str               # Vendor name
    po_number: str | None     # PO reference or None (INV-006 case)
    date: str                 # Invoice date, ISO format "YYYY-MM-DD"
    department: str           # Issuing department: "Engineering", "Legal", "Marketing", "Operations"
    line_items: list[LineItem]
    total: float              # Invoice total (must equal sum of line_items[].amount)
    service_period_start: str | None = None  # ISO date, invoice-level service period if stated
    service_period_end: str | None = None
```

// WHY: `unit_cost` is nullable because many invoices state only a total per line. The pipeline computes `unit_cost = amount / quantity` when null. This matters for the $5K equipment threshold (Priority 2). `service_period_start/end` at invoice level covers cases like INV-004 where the service period applies to the whole invoice, not individual lines.

### 4.2 LLM-Extracted Attributes

This is the LLM's output contract. The LLM fills this schema for each line item. The rule engine consumes it.

```python
class ExtractedAttributes(BaseModel):
    # Priority 1: Physical goods
    is_physical_goods: bool       # Tangible items: supplies, stationery, toner, merch, monitors, etc.
    is_branded_merch: bool        # Subset of physical goods: t-shirts, swag, gift bags with company branding

    # Priority 2: Equipment
    is_equipment: bool            # Hardware, machines, devices (laptops, servers, monitors)
    unit_cost_extracted: float | None = None  # Per-unit cost if LLM parses it from description text (e.g., "3x $1,800" → 1800). Fallback only — structured invoice data takes precedence.

    # Priority 3: Software/SaaS
    is_software: bool             # Software licenses, SaaS subscriptions, platform fees

    # Priority 4: Cloud hosting
    is_cloud_hosting: bool        # AWS, Azure, GCP, Cloudflare, hosting infrastructure

    # Priority 5: Professional services
    service_type: str | None = None  # One of: "legal", "consulting", "mixed_legal", null
    # "legal" = direct legal actions: litigation, patent filing/prosecution, contract drafting/review, regulatory filing
    # "consulting" = advisory, review, strategy, assessment, implementation, creative/design services
    #               — includes work ABOUT legal/regulatory topics if the nature of the work is advisory/review
    # "mixed_legal" = single engagement contains both direct legal actions and non-legal work
    # null = not a professional service

    # Priority 6: Marketing
    is_marketing: bool            # Is the LINE ITEM marketing activity? Ad spend, campaigns, sponsorships, booth rentals, agency management fees.
                                  # Derived from line item content, NOT from invoice department. A t-shirt from the Marketing dept is NOT marketing.

    # Priority 7: Other categories
    category_hint: str | None = None  # Closed enum: "travel", "facilities", "training", "telecom", "insurance", "recruiting", "catering", or null
    # Used when none of the above flags match. null if no category applies or item is unrecognizable.

    # Temporal attributes (for Step 3 — prepaid/accrual)
    billing_frequency: str | None = None  # Enum: "monthly", "annual", "one_time", "usage_based", or null
    service_period_start: str | None = None  # ISO date "YYYY-MM-DD", or null if no date range in text
    service_period_end: str | None = None    # ISO date "YYYY-MM-DD", or null if no date range in text

    # Meta
    confidence: float             # 0.0–1.0. Below 0.7 → flag for human review.
    reasoning: str                # One sentence explaining the key attribute decisions.
```

**Critical invariants the LLM must respect:**
1. `is_branded_merch = True` implies `is_physical_goods = True`. Never branded merch without physical goods.
2. `is_equipment = True` implies `is_physical_goods = True`. Equipment is a subset of physical goods.
3. `service_type` is only non-null when the line item is a professional service.
4. `is_marketing` is derived from the **line item content**, not the invoice department field. The department is passed to the LLM as context for ambiguous items, but `is_marketing` answers "is this line item marketing activity?" — not "does this come from the marketing department?" Physical goods from marketing vendors/departments get `is_marketing = False`. The SOP's branded merch override depends on this: if `is_marketing` were derived from department, every INV-005 line would be `True` and Priority 1 could never fire for the t-shirts and gift bags.
   - INV-005 line 1 "Q1 digital ad campaign management" → `is_marketing=True` (the work IS marketing)
   - INV-005 line 2 "Branded company t-shirts (500 units)" → `is_marketing=False, is_physical_goods=True, is_branded_merch=True`
   - INV-005 line 3 "Conference booth rental – Mar 2026" → `is_marketing=True` (booth rental is marketing spend)
   - INV-005 line 4 "Conference attendee gift bags (branded)" → `is_marketing=False, is_physical_goods=True, is_branded_merch=True`
5. Multiple flags CAN be true simultaneously (e.g., `is_equipment = True` AND `is_physical_goods = True`). The priority tree resolves conflicts.

// WHY: The attribute schema was designed by walking every branch of the SOP priority tree. Each branch has at least one corresponding attribute. If the schema has gaps, the rules can't decide and we're back to the LLM guessing GL codes. The `confidence` field and `reasoning` string exist for auditability and to trigger human review on low-confidence extractions.

**LOCKED DECISION — `billing_frequency` enum and UL6 treatment:**
The enum `["monthly", "annual", "one_time", "usage_based"]` is final. The rule engine only asks "is this annual?" — all other values route to expense treatment. No additional values needed.

UL6 (Cloudflare domain renewal $200, SSL cert $300) will have `billing_frequency = "annual"` because that is factually correct — they are annual. The LLM extracts facts, not policy. Prepaid treatment applies per strict SOP: "service period > 1 month + paid upfront → book to Prepaid account." No materiality threshold is implemented because the SOP does not specify one. UL6 is unlabeled; there is no expected answer to match. Shadow mode surfaces the prepaid treatment for human review, which is exactly what shadow mode is for.

**LOCKED DECISION — `service_period_start` / `service_period_end` format and extraction rules:**

Format: ISO date strings `"YYYY-MM-DD"`. Optional — both default to `None`.

LLM extraction rules (must be explicit in the prompt):
1. **Only extract when the text contains a specific date range or named time period.** "Annual Platform License (Jan–Dec 2026)" → `"2026-01-01"` / `"2026-12-31"`. "Patent filing & prosecution" → `null` / `null`.
2. **Never infer or fabricate dates.** If no period is stated or implied, return `null`.
3. **Extract single-month periods when stated.** "Conference booth rental – Mar 2026" → `"2026-03-01"` / `"2026-03-31"`. The rule engine decides whether >1 month triggers prepaid — the LLM just extracts what's there.
4. **Named months/quarters expand to full date range.** "Q1 2026" → `"2026-01-01"` / `"2026-03-31"`. "Mar 2026" → `"2026-03-01"` / `"2026-03-31"`. "Dec 2025" → `"2025-12-01"` / `"2025-12-31"`.

Fallback chain for downstream logic:
- **Prepaid amortization months** (Section 8.4): line-item dates → default 12 months. No invoice-level fallback needed — if classification already decided "prepaid," billing is annual and 12 is the safe default.
- **Accrual detection** (Section 9.3): line-item `service_period_end` → invoice-level `service_period_end` → `null` means "not accrual." This fallback is critical for INV-004, where the service period is at invoice level ("Service period: Dec 2025"), not in the individual line item descriptions.

// WHY: Extracting dates when present and returning null when absent is the cleanest contract. The rule engine handles all policy decisions (is this >1 month? is the invoice date after the service period?). The LLM's job is perception only — "what dates are stated in this text?" Single-month extraction matters because suppressing it would mean the rule engine can't distinguish "single month, don't prepay" from "no period stated, default to 12 months." Better to have the data and not need it.

**LOCKED DECISION — `category_hint` enum:**

Closed enum: `["travel", "facilities", "training", "telecom", "insurance", "recruiting", "catering"]` plus `null`. No `"creative"`, no `"other"`.

The rule engine's `category_map` maps five of these to GL codes: `travel` → 5060, `facilities` → 5070, `training` → 5080, `telecom` → 5090, `insurance` → 5100. `recruiting` and `catering` are valid LLM outputs but have NO rule engine mapping — they fall through to UNCLASSIFIED intentionally. This is "fail closed" per the SOP.

Why `recruiting` and `catering` exist without mappings: they give human reviewers structured context when an item is flagged ("this is a recruiting fee" is more useful than null), and they feed the feedback loop with categorized data.

Why no `"creative"`: creative/design work is professional services (`service_type="consulting"`). Priority 5 handles it. It never reaches `category_hint`.

Why no `"other"`: semantically empty. Tells the human reviewer nothing and the rule engine nothing. `null` is more honest — it means the LLM genuinely couldn't categorize it.

Most unlabeled invoice edge cases resolve before `category_hint` is consulted:
- UL3 (placement fee) → `service_type="consulting"` → Priority 5 → 5040
- UL7 (brand redesign) → `service_type="consulting"` → Priority 5 → 5040, or `is_marketing=True` → Priority 6 → 5050
- UL10 (catering for product launch) → `is_marketing=True` (marketing dept, product launch event) → Priority 6 → 5050

### 4.3 Classification Result

```python
class ClassificationResult(BaseModel):
    gl_code: str              # The GL account code, e.g. "5010", "1310"
    gl_name: str              # Human-readable name, e.g. "Prepaid Software"
    rule_triggered: str       # Which priority rule fired, e.g. "Priority 3: Software/SaaS — annual prepayment"
    treatment: str            # One of: "expense", "prepaid", "accrual", "capitalize"
    prepaid_expense_target: str | None = None  # For prepaid items: the expense GL code to amortize into
    amortization_months: int | None = None     # For prepaid items: number of months to amortize over
    accrual_type: str | None = None            # For accruals: "professional_services" → 2110, "other" → 2100
```

// WHY: `rule_triggered` is the audit trail. It shows exactly which priority rule fired and why. `prepaid_expense_target` and `amortization_months` are only populated for prepaid treatments — they drive journal entry generation. `accrual_type` determines whether the accrual goes to 2110 or 2100.

### 4.4 Journal Entry

```python
class JournalEntry(BaseModel):
    entry_id: str             # Auto-generated UUID
    invoice_id: str
    line_item_index: int      # Which line item this entry belongs to
    date: str                 # Effective date of this entry (ISO format)
    debit_account: str        # GL code
    credit_account: str       # GL code (always "2000 — Accounts Payable" for initial booking)
    amount: float
    description: str          # Human-readable, e.g. "Amortization: Jan 2026 — Annual Platform License"
    status: str               # One of: "immediate", "scheduled", "pending_payment"
    is_reversal: bool = False # True for accrual reversal entries
```

// WHY: `credit_account` is always Accounts Payable ("2000") for the initial booking entries. For amortization entries, the debit is the expense account (e.g., 5010) and the credit is the prepaid account (e.g., 1310). For accrual reversals, debit is the accrual account (e.g., 2110) and credit is Accounts Payable. The `status` field controls posting behavior: `immediate` posts now, `scheduled` posts on its `date`, `pending_payment` posts when payment occurs.

### 4.5 Approval Record

```python
class ApprovalRecord(BaseModel):
    invoice_id: str
    required_level: str       # "auto_approve", "dept_manager", "vp_finance"
    routing_reason: str       # Human-readable, e.g. "Invoice total $49,900 > $10K threshold"
    override_applied: str | None = None  # e.g. "Marketing auto-approve ≤ $2.5K" or null
    status: str               # "pending", "approved", "rejected"
    decided_by: str | None = None  # Who approved/rejected (null while pending)
    decided_at: str | None = None  # ISO datetime
    rejection_reason: str | None = None
```

### 4.6 Invoice Processing Status

```python
class InvoiceStatus(BaseModel):
    invoice_id: str
    status: str  # See state machine in Section 10
    flags: list[str] = []     # e.g. ["no_po_match"], ["tolerance_exceeded"], ["low_confidence_line:2"]
    error: str | None = None  # If processing failed, why
```

### 4.7 Eval and Correction Models

```python
class EvalResult(BaseModel):
    invoice_id: str
    line_item_index: int
    gl_code_expected: str
    gl_code_actual: str
    gl_correct: bool
    treatment_expected: str
    treatment_actual: str
    treatment_correct: bool
    approval_expected: str       # Expected approval level
    approval_actual: str
    approval_correct: bool
    attribute_errors: list[str]  # e.g. ["service_type: expected consulting, got legal"]

class EvalReport(BaseModel):
    timestamp: str
    total_line_items: int
    gl_accuracy: float           # 0.0–1.0
    treatment_accuracy: float
    approval_accuracy: float
    attribute_accuracy: float    # Fraction of attributes that match expected
    results: list[EvalResult]
    failure_summary: dict[str, int]  # e.g. {"service_type_mismatch": 3, "missing_branded_merch": 1}

class Correction(BaseModel):
    invoice_id: str
    line_item_index: int
    field: str                # The attribute or output field being corrected
    original_value: str
    corrected_value: str
    corrected_by: str = "human"
    timestamp: str
```

---

## 5. Storage — SQLite Schema

Single file: `ap_agent.db`. Created on first run. Seeded with PO data and invoice data.

### 5.1 Tables

```sql
CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    department TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'  -- 'open', 'matched', 'closed'
);

CREATE TABLE invoices (
    invoice_id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    po_number TEXT,
    date TEXT NOT NULL,
    department TEXT NOT NULL,
    total REAL NOT NULL,
    service_period_start TEXT,
    service_period_end TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    raw_json TEXT NOT NULL  -- Full invoice JSON for audit trail
);

CREATE TABLE line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
    line_index INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_cost REAL,
    extracted_attributes TEXT,  -- JSON blob of ExtractedAttributes
    gl_code TEXT,
    gl_name TEXT,
    rule_triggered TEXT,
    treatment TEXT,
    classification_json TEXT,  -- Full ClassificationResult JSON for audit
    UNIQUE(invoice_id, line_index)
);

CREATE TABLE journal_entries (
    entry_id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
    line_item_index INTEGER NOT NULL,
    date TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    credit_account TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,  -- 'immediate', 'scheduled', 'pending_payment'
    is_reversal INTEGER NOT NULL DEFAULT 0,
    posted INTEGER NOT NULL DEFAULT 0  -- 0 = not posted, 1 = posted
);

CREATE TABLE approvals (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(invoice_id),
    required_level TEXT NOT NULL,
    routing_reason TEXT NOT NULL,
    override_applied TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    decided_at TEXT,
    rejection_reason TEXT
);

CREATE TABLE corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL,
    line_item_index INTEGER NOT NULL,
    field TEXT NOT NULL,
    original_value TEXT NOT NULL,
    corrected_value TEXT NOT NULL,
    corrected_by TEXT NOT NULL DEFAULT 'human',
    timestamp TEXT NOT NULL
);
```

### 5.2 Seed Data — Purchase Orders (`data/purchase_orders.json`)

PO data is stored in `data/purchase_orders.json` as an array of PO objects. Loaded into the `purchase_orders` table by `init-db`. Derived from the invoice test data to make PO matching work.

```
PO-2026-044  | Cloudware Solutions   | $24,000 | Engineering   # INV-001
PO-2026-051  | Morrison & Burke LLP  | $9,500  | Legal         # INV-002
PO-2026-038  | TechDirect Inc.       | $49,900 | Engineering   # INV-003
PO-2025-189  | Apex Strategy Group   | $8,700  | Operations    # INV-004
PO-2026-062  | BrightSpark Agency    | $23,500 | Marketing     # INV-005
# INV-006 has no PO — this is intentional
PO-2026-077  | DataSync Pro          | $24,000 | Engineering   # UL1
PO-2026-081  | WeWork                | $4,500  | Operations    # UL2
PO-2026-090  | TalentBridge Partners | $25,000 | Engineering   # UL3
PO-2026-085  | Global Tech Summit    | $6,800  | Engineering   # UL4
PO-2026-092  | RenewTech             | $4,500  | Operations    # UL5
PO-2026-055  | Cloudflare            | $500    | Engineering   # UL6
PO-2026-088  | Sarah Chen Design LLC | $8,000  | Marketing     # UL7
PO-2026-095  | ModernSpace Builders  | $12,000 | Operations    # UL8
PO-2026-060  | Twilio Inc.           | $2,300  | Engineering   # UL9
PO-2026-098  | Fresh Bites Catering  | $1,500  | Marketing     # UL10
```

**Over-tolerance test PO:** Add one PO with a deliberate mismatch to test the 10% tolerance validation. Choose one of the unlabeled invoices and seed its PO amount at 12–15% off from the invoice total. Specifically:

- Modify `PO-2026-085` (UL4 — Global Tech Summit) to have amount `$5,800` instead of `$6,800`. This is a ~14.7% mismatch. UL4's invoice total is $6,800. This triggers the tolerance check failure.

// WHY: We need both exception paths tested: no PO (INV-006) and PO with tolerance exceeded (UL4 with seeded mismatch). This proves Step 1 validation is real, not just a passthrough.

---

## 6. Step 1 — PO Matching (`po_matching.py`)

### 6.1 Function Signature

```python
def match_po(invoice: Invoice, db: sqlite3.Connection) -> POMatchResult
```

### 6.2 Logic

1. If `invoice.po_number` is `None` → return `POMatchResult(matched=False, reason="no_po_provided")`.
2. Query `purchase_orders` table for `po_number`.
3. If PO not found → return `POMatchResult(matched=False, reason="po_not_found")`.
4. Compute tolerance: `abs(invoice.total - po.amount) / po.amount`.
5. If tolerance > 0.10 → return `POMatchResult(matched=False, reason="tolerance_exceeded", tolerance_pct=computed_value)`.
6. Otherwise → return `POMatchResult(matched=True, po_amount=po.amount, tolerance_pct=computed_value)`.

### 6.3 POMatchResult Schema

```python
class POMatchResult(BaseModel):
    matched: bool
    reason: str | None = None       # "no_po_provided", "po_not_found", "tolerance_exceeded"
    po_amount: float | None = None
    tolerance_pct: float | None = None
```

### 6.4 Failure Behavior

When `matched=False`: the pipeline sets invoice status to `flagged_for_review`, records the flag reason, and **stops processing**. No classification, no journal entries, no approval routing. The SOP says: "No PO → flag for manual review, do not classify."

// WHY: The SOP is explicit — no PO means no classification. We don't partially process. The invoice sits in `flagged_for_review` until a human provides a PO or overrides.

---

## 7. LLM Attribute Extraction (`attribute_extraction.py`)

### 7.1 Function Signature

```python
async def extract_attributes(
    line_item: LineItem,
    invoice_context: dict,  # vendor, department, invoice-level service period, etc.
    client: anthropic.Anthropic
) -> ExtractedAttributes
```

### 7.2 LLM Call Configuration

- **Model:** `claude-sonnet-4-20250514` (default, configurable via `AP_AGENT_MODEL` environment variable)
- **Temperature:** 0.0 (deterministic extraction — we want consistency, not creativity)
- **Max tokens:** 1024 (attribute extraction is compact)
- **Tool use / structured output:** Use Anthropic SDK's structured output with the `ExtractedAttributes` Pydantic model as the response schema. This guarantees the response conforms to the schema — no parsing needed.

// WHY: Sonnet, not Haiku, not Opus. The extraction task is well-scoped (bounded by a Pydantic schema) but the hard cases require genuine reading comprehension: distinguishing "advisory work about regulatory topics" from "direct legal work," recognizing physical goods from a marketing agency as NOT marketing activity, parsing ambiguous service descriptions like "Premium Support & Implementation Services." Haiku would nail the obvious cases but fumble these. Opus is overkill — the schema constraint eliminates formatting errors and the prompt is tight. Cost is irrelevant at this volume (~64 LLM calls total including feedback loop re-runs). The model string is configurable via environment variable so the assessor can swap models without code changes.

### 7.3 Prompt Design (`prompts.py`)

**LOCKED DECISION — Prompt structure and information boundary:**

The LLM sees extraction instructions only. It does NOT see the SOP, GL codes, priority rules, approval thresholds, or journal entry structure. If the LLM sees "Priority 1: Physical goods → 5000," it will start reasoning about GL codes instead of extracting attributes. The attribute extraction becomes contaminated by classification reasoning that belongs in the rule engine. The architecture's testability depends on this boundary.

| LLM sees | LLM does NOT see |
|----------|-----------------|
| Attribute schema (field names, types, enums) | GL codes or account names |
| Extraction guidance per attribute | Priority rule tree |
| Few-shot examples (attributes only, no GL codes) | SOP document |
| Invoice context (vendor, dept, date) | Approval thresholds |
| Line item description and amounts | Journal entry structure |

**System prompt** (stable across all calls, changes only during feedback loop):
- Section A: Role and constraints
- Section B: Attribute extraction instructions
- Section C: Few-shot examples

**User message** (varies per line item):
- Invoice context + line item details

Few-shot examples go in the system prompt, not the user message. They are part of the extraction instructions, not part of the input. Placing them in the user message risks the model pattern-matching too aggressively against the examples instead of reasoning about the actual line item.

The system prompt has three sections:

**Section A — Role and constraints:**
```
You are an accounting attribute extractor. Your job is to analyze a line item from a vendor invoice and extract structured attributes. You NEVER determine the GL account code — that is done by a downstream rule engine. You ONLY extract factual attributes about what the line item is. You do not know and should not guess what account codes exist or how they map to attributes.
```

**Section B — Attribute extraction instructions:**
Each attribute gets a one-line definition and a brief "when to set true" guide. This section must be tuned through the feedback loop. The initial version is intentionally slightly naive (see Section 14).

Must include this explicit instruction for service periods:
```
For service_period_start and service_period_end:
- Only extract when the line item text or invoice context contains a specific date range, named month, or named quarter.
- Never infer or fabricate dates. If no period is stated or implied, return null for both.
- Extract single-month periods when stated: "Mar 2026" → 2026-03-01 / 2026-03-31.
- Expand named quarters: "Q1 2026" → 2026-01-01 / 2026-03-31.
- If only a year range is given: "Jan–Dec 2026" → 2026-01-01 / 2026-12-31.
- If no dates at all: null / null. Do not guess.
```

Must include this explicit instruction for is_marketing:
```
For is_marketing:
- Assess the LINE ITEM, not the invoice department. The department field is context, not a classification signal.
- is_marketing = true means the line item IS marketing activity: ad spend, campaigns, sponsorships, booth rentals, agency management fees.
- is_marketing = false for tangible/physical goods even if purchased by or for the Marketing department.
```

// WHY: The initial prompt states the general principle (assess the line item, not the department) but does NOT enumerate specific physical goods like t-shirts or gift bags. That specificity is intentionally withheld for the engineered weakness (Section 14). The LLM may still misclassify branded merch from marketing vendors without a few-shot example showing the pattern. The feedback loop adds the example and the specificity.

**Section C — Few-shot examples:**
Start with 2–3 examples in the initial prompt. More are added during the feedback loop iteration. Must include at least one example with null service periods. Format:

```
Line item: "Annual Platform License (Jan–Dec 2026)"
Vendor: Cloudware Solutions | Dept: Engineering
→ is_software: true, billing_frequency: annual, service_period_start: 2026-01-01, service_period_end: 2026-12-31, confidence: 0.95

Line item: "Patent filing & prosecution"
Vendor: Morrison & Burke LLP | Dept: Legal
→ service_type: legal, billing_frequency: one_time, service_period_start: null, service_period_end: null, confidence: 0.95
```

**The user message** for each call:
```
Invoice: {invoice_id} | Vendor: {vendor} | Department: {department}
Invoice date: {date}
Invoice-level service period: {service_period_start} to {service_period_end} (if stated)

Line item: "{description}"
Amount: ${amount} | Quantity: {quantity} | Unit cost: ${unit_cost}

Extract the structured attributes for this line item.
```

### 7.4 Post-Extraction Validation

After the LLM returns `ExtractedAttributes`, run these validation checks:

1. **Invariant check:** If `is_branded_merch = True` but `is_physical_goods = False` → force `is_physical_goods = True`, log a warning.
2. **Invariant check:** If `is_equipment = True` but `is_physical_goods = False` → force `is_physical_goods = True`, log a warning.
3. **Confidence check:** If `confidence < 0.7` → add flag `low_confidence_line:{line_index}` to the invoice. Processing continues, but the flag is surfaced in output.
4. **Unit cost resolution (precedence chain — first non-null wins):**
   1. `line_item.unit_cost` — explicit in the invoice data. Authoritative.
   2. `line_item.amount / line_item.quantity` — if `quantity > 1`, compute it deterministically.
   3. `attrs.unit_cost_extracted` — LLM parsed a per-unit cost from the description. Fallback only.
   4. `line_item.amount` — if `quantity == 1` and nothing else is available, amount IS unit cost.
   Store the resolved unit cost on the line item record.

// WHY: Deterministic data takes precedence over LLM extraction. INV-003 MacBooks have `quantity=3, amount=5400` → step 2 fires → `unit_cost=1800`. The LLM's `unit_cost_extracted` is irrelevant. But `unit_cost_extracted` stays on the schema because in production, invoices arrive as unstructured text where "3x $1,800" is in the description, not in structured fields. The LLM parsing it is a real perception capability worth demonstrating — it just isn't authoritative when structured data exists.

### 7.5 Batching Strategy

Process line items sequentially within an invoice, one LLM call per line item. Do NOT batch multiple line items into a single call.

// WHY: Each line item gets the invoice-level context but its own extraction. This avoids cross-contamination (where the LLM's attributes for line 2 are influenced by its extraction of line 1) and makes it trivial to pinpoint which call produced which attributes.

---

## 8. Step 2 — GL Classification (`classification.py`)

### 8.1 Function Signature

```python
def classify_line_item(attrs: ExtractedAttributes, unit_cost: float) -> ClassificationResult
```

### 8.2 Priority Rule Tree

**Conflict resolution meta-principle:** Evaluate rules in order 1→7. First match wins. Stop. No scoring, no weighting, no tiebreaking. Multiple attributes CAN be true simultaneously — the priority order determines which one matters. This is a hard-coded if/elif chain, not a lookup table, and not a scoring system.

The rules are evaluated **in priority order**:

```python
def classify_line_item(attrs, unit_cost):
    # Priority 1: Physical goods
    if attrs.is_physical_goods and not attrs.is_equipment:
        return ClassificationResult(
            gl_code="5000", gl_name="Office Supplies",
            rule_triggered="Priority 1: Physical goods → 5000",
            treatment="expense"
        )

    # Priority 2: Equipment
    if attrs.is_equipment:
        if unit_cost >= 5000:
            return ClassificationResult(
                gl_code="1500", gl_name="Fixed Assets",
                rule_triggered="Priority 2: Equipment, unit cost ≥ $5K → 1500 (capitalize)",
                treatment="capitalize"
            )
        else:
            return ClassificationResult(
                gl_code="5110", gl_name="Equipment (under $5,000)",
                rule_triggered="Priority 2: Equipment, unit cost < $5K → 5110",
                treatment="expense"
            )

    # Priority 3: Software/SaaS
    if attrs.is_software:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1310", gl_name="Prepaid Software",
                rule_triggered="Priority 3: Software — annual prepayment → 1310",
                treatment="prepaid",
                prepaid_expense_target="5010",
                amortization_months=_compute_amortization_months(attrs)
            )
        else:
            return ClassificationResult(
                gl_code="5010", gl_name="Software & Subscriptions",
                rule_triggered="Priority 3: Software — monthly/usage → 5010",
                treatment="expense"
            )

    # Priority 4: Cloud hosting
    if attrs.is_cloud_hosting:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1300", gl_name="Prepaid Expenses (General)",
                rule_triggered="Priority 4: Cloud hosting — annual prepayment → 1300",
                treatment="prepaid",
                prepaid_expense_target="5020",
                amortization_months=_compute_amortization_months(attrs)
            )
        else:
            return ClassificationResult(
                gl_code="5020", gl_name="Cloud Hosting & Infrastructure",
                rule_triggered="Priority 4: Cloud hosting — monthly/usage → 5020",
                treatment="expense"
            )

    # Priority 5: Professional services
    if attrs.service_type in ("legal", "mixed_legal"):
        return ClassificationResult(
            gl_code="5030", gl_name="Professional Services — Legal",
            rule_triggered=f"Priority 5: Professional services — {attrs.service_type} → 5030",
            treatment="expense"
        )
    if attrs.service_type == "consulting":
        return ClassificationResult(
            gl_code="5040", gl_name="Professional Services — Consulting",
            rule_triggered="Priority 5: Professional services — consulting → 5040",
            treatment="expense"
        )

    # Priority 6: Marketing
    if attrs.is_marketing:
        return ClassificationResult(
            gl_code="5050", gl_name="Marketing & Advertising",
            rule_triggered="Priority 6: Marketing → 5050",
            treatment="expense"
        )

    # Priority 7: Other categories
    # Note: "recruiting" and "catering" are valid category_hint enum values but are
    # intentionally NOT in this map. They fall through to UNCLASSIFIED — fail closed.
    category_map = {
        "travel": ("5060", "Travel & Entertainment"),
        "facilities": ("5070", "Facilities & Maintenance"),
        "training": ("5080", "Training & Development"),
        "telecom": ("5090", "Telecom & Internet"),
        "insurance": ("5100", "Insurance Expense"),
    }
    if attrs.category_hint in category_map:
        code, name = category_map[attrs.category_hint]
        # Special case: insurance with prepaid treatment handled in Step 3
        return ClassificationResult(
            gl_code=code, gl_name=name,
            rule_triggered=f"Priority 7: {attrs.category_hint} → {code}",
            treatment="expense"
        )

    # Fallback: unclassifiable
    return ClassificationResult(
        gl_code="UNCLASSIFIED", gl_name="Unclassified",
        rule_triggered="No matching rule — flagged for human review",
        treatment="expense"
    )
```

### 8.3 Critical Rule Interactions

**Multi-flag resolution — every scenario in the test data:**

| Flags true simultaneously | Rule hit | Result | Why |
|--------------------------|----------|--------|-----|
| `is_physical_goods`, `is_branded_merch` | Priority 1 | 5000 | Physical goods, not equipment → P1 fires. P6 (marketing) never reached. |
| `is_physical_goods`, `is_equipment`, unit < $5K | Priority 2 | 5110 | P1 checks `is_physical_goods AND NOT is_equipment` → skips. P2 fires. |
| `is_physical_goods`, `is_equipment`, unit ≥ $5K | Priority 2 | 1500 | Same skip from P1. P2 fires with capitalize. |
| `is_software`, `billing_frequency=annual` | Priority 3 | 1310 | Single-flag match, no conflict. |
| `is_cloud_hosting`, `billing_frequency=annual` | Priority 4 | 1300 | Single-flag match, no conflict. |
| `is_marketing`, `is_physical_goods` | Priority 1 | 5000 | P1 fires first. P6 never reached. This IS the branded merch override. |
| `is_marketing`, `category_hint=training` | Priority 6 | 5050 | P6 fires first. P7 never reached. |

The if/elif chain IS the conflict resolution. There is no secondary mechanism.

**Physical goods override for branded merch from marketing:**
- INV-005 has "Branded company t-shirts" from a Marketing vendor. The LLM should extract `is_physical_goods=True, is_branded_merch=True`. Priority 1 fires (physical goods → 5000) BEFORE Priority 6 (marketing → 5050) is ever reached. The priority order IS the override mechanism.

**Equipment is a subset of physical goods:**
- Priority 1 checks `is_physical_goods and NOT is_equipment`. Equipment items (which are also physical goods) skip Priority 1 and hit Priority 2 where the $5K threshold applies. This is not two rules competing — it's one rule with an explicit carve-out.

**LOCKED DECISION — INV-002 line 2: "Regulatory compliance review & advisory" → 5040 (consulting)**

This is resolved by the LLM, not the rule engine. No rule-level override exists or is needed.

The ambiguity: the SOP lists "regulatory" as a legal sub-type (→ 5030), and "advisory" as consulting (→ 5040). This line item has both signals. The assessment expects 5040.

Resolution: the distinction is between *direct legal actions* and *advisory work about legal topics*. "Litigation, patent filing, contract drafting, regulatory filing" are direct legal actions — you are doing the legal thing. "Regulatory compliance review & advisory" is someone reviewing your posture and advising you — the nature of the work is consulting, the subject matter happens to be regulatory.

The LLM extracts `service_type="consulting"`. The rule engine follows it to 5040. No override, no special case, no description parsing in the rules.

This is architecturally correct: semantic disambiguation is a perception task (LLM's job). The rule engine handles structured predicates, not text interpretation. Adding a rule like "if description contains 'advisory' AND 'regulatory', force 5040" would build a second NLP system inside the rule engine, violating the separation of concerns.

The initial prompt is intentionally naive about this distinction (engineered weakness #2, Section 14). The feedback loop adds explicit guidance: "For work described as review, advisory, assessment, or consultation — even if the subject matter is regulatory, compliance, or legal topics — set service_type to consulting. Only set legal for direct legal actions: litigation, patent filing/prosecution, contract drafting/review, or regulatory filing."

### 8.4 Amortization Month Computation

```python
def _compute_amortization_months(attrs: ExtractedAttributes) -> int:
    """Compute months between service_period_start and service_period_end."""
    if attrs.service_period_start and attrs.service_period_end:
        start = date.fromisoformat(attrs.service_period_start)
        end = date.fromisoformat(attrs.service_period_end)
        months = (end.year - start.year) * 12 + (end.month - start.month)
        return max(months, 1)  # At least 1 month
    # Default: 12 months if service period not parseable
    return 12
```

// WHY: Default to 12 months because "annual" implies 12 months. But if the LLM extracted specific dates (e.g., "Feb 26–Jan 27" = 12 months), use those.

---

## 9. Step 3 — Prepaid & Accrual Treatment (`treatment.py`)

### 9.1 Function Signature

```python
def determine_treatment(
    attrs: ExtractedAttributes,
    classification: ClassificationResult,
    invoice: Invoice
) -> ClassificationResult  # Returns updated classification with treatment adjustments
```

### 9.2 Prepaid Logic

**LOCKED DECISION — "Paid upfront" proxy:**

The SOP's prepaid trigger is: "service period > 1 month + paid upfront." We are not modeling payments. `billing_frequency == "annual"` is the sole proxy for "paid upfront." It is the only billing frequency value that implies both conditions simultaneously: a multi-month service period AND a single upfront payment.

`one_time` with a long service period does NOT trigger prepaid. This is a deliberate simplification — `one_time` was designed for single-deliverable items (patent filing, placement fee) where the concept of "upfront" doesn't apply. If a future invoice has a lump-sum multi-month engagement billed as `one_time`, it expenses and gets caught in shadow mode review. The README notes this as a known limitation.

**Prepaid flow:**

Prepaid is already handled by the classification rules in Section 8 for **software** (`billing_frequency == "annual"` → 1310) and **cloud** (`billing_frequency == "annual"` → 1300). For all other categories, `determine_treatment()` applies the SOP's general prepaid rule.

**General prepaid rule:** If `billing_frequency == "annual"` AND `classification.treatment == "expense"` (i.e., not already handled as prepaid by the classification rules):
- Validate service period > 1 month as a safety check. If the LLM extracted specific dates showing ≤ 1 month, do NOT override to prepaid (contradictory extraction). If dates are null, default to > 1 month because "annual" implies 12 months.
- Override: `treatment="prepaid"`, `gl_code="1300"` (Prepaid Expenses General), `prepaid_expense_target=classification.gl_code` (the original expense account), `amortization_months` from service period or default 12.

This catches cases like UL6 (Cloudflare domain/SSL, classified as telecom 5090, `billing_frequency="annual"`). The result: book to 1300, amortize monthly to 5090.

**Insurance special case:** If `category_hint == "insurance"` AND `billing_frequency == "annual"`:
- Override: `gl_code="1320"` (not 1300), `treatment="prepaid"`, `prepaid_expense_target="5100"`.
- Compute `amortization_months` from service period or default 12.
- Insurance uses its own prepaid account (1320) per the chart of accounts, so it is checked BEFORE the general prepaid rule.

**Evaluation order within `determine_treatment()`:**
1. Insurance prepaid check (specific account 1320)
2. General prepaid check (general account 1300, only if not already prepaid from classification)
3. Accrual check (Section 9.3)

// WHY: The SOP's prepaid rule is general — "service period > 1 month + paid upfront → book to Prepaid account." It is not limited to software and cloud. The classification rules in Section 8 handle software and cloud prepaid inline because they have dedicated prepaid accounts (1310, 1300). Everything else flows through here and lands in 1300 (Prepaid General). `billing_frequency == "annual"` is the single, deterministic proxy for the SOP's two-part trigger. The service period check is belt-and-suspenders — it catches contradictory LLM extractions but never fires in normal operation because annual billing implies a multi-month period.

### 9.3 Accrual Logic

**Trigger condition:** `invoice_date > service_period_end`. Strict greater-than, not greater-than-or-equal. If the invoice arrives on the last day of the service period, the service is still "within period" — no accrual.

**Service period end resolution (fallback chain):**
1. Line-item `attrs.service_period_end` — if the LLM extracted a date from the line item description, use it.
2. Invoice-level `invoice.service_period_end` — fallback if the line-item value is null.
3. Both null → `_is_accrual` returns `False`. Default to expense. Accrual is the exceptional treatment; without a known service period end, we cannot determine the service was delivered before the invoice.

If both line-item and invoice-level values exist, **line-item wins**. Invoice-level is fallback only, never override.

```python
def _is_accrual(attrs: ExtractedAttributes, invoice: Invoice) -> bool:
    """Check if service period ended before invoice date. Strict greater-than."""
    # Line-item service period takes precedence over invoice-level
    spe = attrs.service_period_end or invoice.service_period_end
    if spe is None:
        return False  # No service period known → not accrual
    return date.fromisoformat(invoice.date) > date.fromisoformat(spe)
```

**INV-004 — both paths produce the same result:**
- Invoice date: `2026-01-15`. Invoice-level `service_period_end`: `"2025-12-31"`.
- Line 1: "Operational efficiency assessment – Dec 2025". The LLM may extract `service_period_end = "2025-12-31"` from "Dec 2025" in the description. If so, the line-item value is used. If not, the invoice-level `"2025-12-31"` catches it. Either way: `2026-01-15 > 2025-12-31` → true → accrual.
- Line 2: "Travel expenses – Dec 2025 on-site visits". Same logic. Same result.

**When accrual is triggered:**
- Override `treatment` to `"accrual"`
- Set `accrual_type`:
  - If `service_type` in ("legal", "consulting", "mixed_legal") → `"professional_services"` (books to 2110)
  - Otherwise → `"other"` (books to 2100)
- The GL code for the expense itself does NOT change. The accrual account (2110 or 2100) is used in the journal entry debit/credit, not in the classification GL code.

// WHY: INV-004 is the accrual test case. The operational assessment ($7,500) is professional services → 2110. The travel ($1,200) is not professional services → 2100. Both need reversal entries. The dual-path resolution for service_period_end (line-item first, invoice-level fallback) means the system works whether or not the LLM parses "Dec 2025" from the descriptions — the invoice-level backstop guarantees correctness.

### 9.4 Treatment Priority

Accrual check runs AFTER classification. If a line item is both prepaid-eligible (annual billing) AND accrual-eligible (service period before invoice date), **accrual wins**. Rationale: if the service already happened, you don't prepay for it — you accrue it.

// WHY: This scenario is unlikely in the test data but the rule must be explicit. Fail-closed: accrual treatment is the conservative choice.

---

## 10. Step 4 — Approval Routing (`approval.py`)

### 10.1 Invoice State Machine

```
received → po_matched → classified → pending_approval → approved → posted
                                           ↓
                                        rejected
```

Exception branches:
```
received → flagged_for_review   (no PO, tolerance exceeded)
classified → flagged_for_review  (unclassifiable line item)
```

Valid status values: `"received"`, `"po_matched"`, `"classified"`, `"pending_approval"`, `"approved"`, `"rejected"`, `"posted"`, `"flagged_for_review"`.

### 10.2 Approval Routing Function

```python
def route_approval(invoice: Invoice, line_classifications: list[ClassificationResult]) -> ApprovalRecord
```

**Routing decision tree (evaluated in order — first match wins):**

1. **Fixed Asset override:** If ANY line item has `gl_code == "1500"` → `required_level = "vp_finance"`, reason: "Line item classified as Fixed Asset (1500)".

2. **Marketing override:** If `department == "Marketing"` AND `invoice.total <= 2500` → `required_level = "auto_approve"`, override: "Marketing auto-approve ≤ $2.5K".

3. **Engineering cloud/software override:** If `department == "Engineering"` AND `invoice.total <= 5000` AND all line items have `gl_code` in `{"5010", "5020"}` → `required_level = "auto_approve"`, override: "Engineering auto-approve ≤ $5K, all lines Cloud/Software".

   **LOCKED DECISION — GL codes for Engineering override:** Exactly `{"5010", "5020"}`. Prepaid variants (1310, 1300) do NOT qualify. The SOP names specific GL codes, not conceptual categories. Prepaid items involve balance sheet entries and amortization schedules — non-routine treatment that should get human review, not auto-approval. This override is for routine, recurring cloud/software expenses.

   **Note:** This override never fires in the test data. No Engineering invoice is ≤ $5K with all lines in {5010, 5020}. UL6 ($500, Engineering) classifies as 1300 (prepaid telecom). UL9 ($2,300, Engineering) classifies as 5090 (telecom). The override is still implemented and covered by synthetic unit tests in `tests/test_rules.py`.

4. **Base thresholds:**
   - `invoice.total <= 1000` → `required_level = "auto_approve"`, reason: "Invoice total ≤ $1K".
   - `1000 < invoice.total <= 10000` → `required_level = "dept_manager"`, reason: "Invoice total between $1K–$10K".
   - `invoice.total > 10000` → `required_level = "vp_finance"`, reason: "Invoice total > $10K".

5. **Fail closed:** If no rule matched: `required_level = "denied"`, reason: "No matching approval rule — denied (fail closed)".

   **This rule is unreachable under valid input.** The base thresholds (rule 4) partition all positive amounts into three exhaustive, non-overlapping ranges: ≤$1K, $1K–$10K, >$10K. Every invoice with `total > 0` matches exactly one. Rules 1–3 may short-circuit earlier, but rule 4 is the universal backstop. There is no combination of department, amount, or GL codes that skips all five rules.

   **Keep it anyway.** Two reasons: (1) defensive code costs nothing and protects against future bugs — if someone adds a new override and introduces a gap, this catches it instead of silently proceeding with no approval; (2) the SOP explicitly says "No matching rule → deny (fail closed)" and implementing it demonstrates we read the SOP literally, even for unreachable branches.

// WHY: The order matters. Fixed Asset override (rule 1) fires first — even a $500 invoice with a fixed asset line goes to VP Finance. Marketing and Engineering overrides (rules 2-3) are checked before base thresholds (rule 4) because they are exceptions to the base rules. The SOP says "Any Fixed Asset → VP Finance regardless" — that's rule 1. The base thresholds are exhaustive over all positive amounts, making rule 5 unreachable but present for defense-in-depth.

### 10.3 Expected Approval Routes (Labeled Invoices)

| Invoice | Total     | Dept        | Key Factor              | Expected Route     |
|---------|-----------|-------------|-------------------------|--------------------|
| INV-001 | $24,000   | Engineering | > $10K                  | vp_finance         |
| INV-002 | $9,500    | Legal       | $1K–$10K               | dept_manager       |
| INV-003 | $49,900   | Engineering | Fixed Asset (1500) line | vp_finance         |
| INV-004 | $8,700    | Operations  | $1K–$10K               | dept_manager       |
| INV-005 | $23,500   | Marketing   | > $10K                  | vp_finance         |
| INV-006 | $3,800    | Marketing   | N/A — flagged (no PO)   | N/A                |

### 10.4 Human-in-the-Loop API

```python
def approve(invoice_id: str, decided_by: str, db: sqlite3.Connection) -> bool:
    """Approve an invoice in pending_approval status. Returns True if successful."""

def reject(invoice_id: str, decided_by: str, reason: str, db: sqlite3.Connection) -> bool:
    """Reject an invoice in pending_approval status. Returns True if successful."""
```

**Behavior:**
- Both functions check that the invoice is in `pending_approval` status. If not, return `False`.
- On approve:
  1. Set `approvals.status = "approved"`, record `decided_by` and timestamp.
  2. Set `invoices.status = "posted"`.
  3. Update `journal_entries` for this invoice: set `posted=1` for all entries where `status="immediate"`. Scheduled and pending_payment entries remain `posted=0`.
  4. Return `True`.
- On reject:
  1. Set `approvals.status = "rejected"`, record `decided_by`, `rejection_reason`, and timestamp.
  2. Set `invoices.status = "rejected"`.
  3. Journal entries remain in the DB with `posted=0`. They are not deleted — they're an audit trail of what WOULD have posted.
  4. Return `True`.

// WHY: In normal mode, `process_invoice` commits entries to the DB with `posted=0` and pauses at `pending_approval`. The `approve()` function completes the pipeline by flipping immediate entries to `posted=1`. This means the entries are always in the DB after processing — approval just changes their posting status. Rejection leaves them as unposted proposals, preserving the audit trail.

**Auto-approve flag:** `process_invoice()` accepts a `mode` parameter. In `mode="auto"`, invoices that route to `auto_approve` are approved automatically. Invoices that route to `dept_manager` or `vp_finance` are ALSO auto-approved (to enable end-to-end testing without human blocking). In `mode="interactive"`, only `auto_approve` routes are auto-approved; others pause at `pending_approval`.

// WHY: The eval suite needs to run end-to-end without a human. `mode="auto"` enables that. `mode="interactive"` demonstrates the actual human-in-the-loop flow. The CLI supports both modes.

---

## 11. Step 5 — Journal Entry Generation (`journal.py`)

### 11.1 Function Signature

```python
def generate_journal_entries(
    invoice: Invoice,
    line_item: LineItem,
    line_index: int,
    classification: ClassificationResult,
    attrs: ExtractedAttributes
) -> list[JournalEntry]
```

### 11.2 Entry Generation Rules

**Case 1: Simple expense** (`treatment == "expense"`)
- 1 entry: debit `gl_code`, credit `"2000"` (Accounts Payable), amount = line item amount, status = `"immediate"`.

**Case 2: Capitalize** (`treatment == "capitalize"`)
- 1 entry: debit `"1500"`, credit `"2000"`, amount = line item amount, status = `"immediate"`.

**Case 3: Prepaid** (`treatment == "prepaid"`)
- 1 booking entry: debit `gl_code` (prepaid account, e.g., 1310), credit `"2000"`, amount = full line item amount, status = `"immediate"`, date = invoice date.
- N amortization entries: debit `prepaid_expense_target` (e.g., 5010), credit `gl_code` (e.g., 1310), amount = `line_item.amount / amortization_months`, status = `"scheduled"`, date = last day of each month in the service period.

Example for INV-001 ($24,000 annual software, Jan–Dec 2026):
```
Entry 1: DR 1310 $24,000 / CR 2000 $24,000 — "Prepaid: Annual Platform License" — immediate, 2026-01-05
Entry 2: DR 5010 $2,000 / CR 1310 $2,000 — "Amortization: Jan 2026" — scheduled, 2026-01-31
Entry 3: DR 5010 $2,000 / CR 1310 $2,000 — "Amortization: Feb 2026" — scheduled, 2026-02-28
... (through Dec 2026)
Entry 13: DR 5010 $2,000 / CR 1310 $2,000 — "Amortization: Dec 2026" — scheduled, 2026-12-31
```
Total: 13 entries per prepaid line item.

**Case 4: Accrual** (`treatment == "accrual"`)
- 1 accrual entry: debit expense GL code (e.g., 5040), credit accrual account (`"2110"` if `accrual_type == "professional_services"`, else `"2100"`), amount = line item amount, status = `"immediate"`, date = service period end date (or invoice date if no period).
- 1 reversal entry: debit accrual account, credit `"2000"` (Accounts Payable), same amount, status = `"pending_payment"`, `is_reversal = True`.

The reversal credits AP (2000), NOT Cash. The agent's scope ends at Accounts Payable. The reversal moves the liability from the accrual account to AP — "we now owe this through the normal payables process." The actual cash disbursement (DR 2000 / CR Cash) is a downstream ERP event we do not model. The README notes this boundary.

Example for INV-004 line 1 ($7,500 consulting, service period Dec 2025):
```
Entry 1: DR 5040 $7,500 / CR 2110 $7,500 — "Accrual: Operational efficiency assessment – Dec 2025" — immediate, 2025-12-31
Entry 2: DR 2110 $7,500 / CR 2000 $7,500 — "Reversal: Operational efficiency assessment" — pending_payment
```

The accrual booking date is `2025-12-31` (service period end), not `2026-01-15` (invoice date). The expense is recognized in December when the service was delivered, not January when the invoice arrived. This is the accounting purpose of accrual entries.

### 11.3 Balance Verification

**Invariant:** The sum of `amount` across all entries where `status == "immediate"` AND `is_reversal == False` must equal `invoice.total`.

```python
def verify_balance(invoice: Invoice, entries: list[JournalEntry]) -> bool:
    """Verify initial booking entries sum to invoice total."""
    total_initial = sum(
        e.amount for e in entries
        if e.status == "immediate" and not e.is_reversal
    )
    return abs(total_initial - invoice.total) < 0.01  # Cent-level tolerance for float math
```

If the check fails, flag the invoice for review. Do not block processing — the entries are still stored but the flag is surfaced.

**Why these two filters select the right entries:**

Initial bookings — the entries representing "money coming in the door from this invoice" — are always `status="immediate"` and `is_reversal=False`. Everything else is either an internal reclassification or a future liability movement:

| Entry type | status | is_reversal | Included? | Why |
|-----------|--------|-------------|-----------|-----|
| Simple expense booking | immediate | False | **Yes** | Initial booking |
| Capitalize booking | immediate | False | **Yes** | Initial booking |
| Prepaid booking | immediate | False | **Yes** | Initial booking ($24K to 1310) |
| Prepaid amortization | scheduled | False | **No** | Internal reclassification (1310 → 5010) |
| Accrual booking | immediate | False | **Yes** | Initial booking ($7,500 to 5040/2110) |
| Accrual reversal | pending_payment | True | **No** | Future liability movement (2110 → 2000) |

**Proof across all labeled invoices:**

- INV-001: $24,000 prepaid booking = $24,000 ✓ (12 amortizations excluded)
- INV-002: $4,500 + $3,200 + $1,800 = $9,500 ✓
- INV-003: $5,400 + $8,500 + $36,000 = $49,900 ✓ (12 amortizations excluded)
- INV-004: $7,500 + $1,200 = $8,700 ✓ (2 reversals excluded)
- INV-005: $15,000 + $2,000 + $5,000 + $1,500 = $23,500 ✓

// WHY: The SOP says "Verify line items sum to invoice total." This invariant is the precise implementation. The cent-level tolerance handles IEEE 754 float division in amortization arithmetic (e.g., $24,000 / 12 = $2,000.00 exactly, but other amounts may not divide evenly). The two filters (`immediate` + not reversal) are stable across all treatment types — no special-casing needed for prepaid vs. accrual vs. expense.

### 11.4 Posting Behavior by Mode

| Mode       | DB writes during processing | Transaction outcome | `posted` flag |
|------------|---------------------------|--------------------|----|
| `normal`   | All stores + status updates | **Committed** | `1` for immediate entries |
| `auto`     | All stores + status updates + auto-approve | **Committed** | `1` for immediate entries |
| `dry_run`  | All stores + status updates (same code path) | **Rolled back** — zero DB side effects | N/A |
| `shadow`   | All stores + status updates | **Committed** | `0` for all entries (proposals only) |

---

## 12. Pipeline Orchestrator (`pipeline.py`)

### 12.1 Main Function

```python
def process_invoice(
    invoice: Invoice,
    db: sqlite3.Connection,
    client: anthropic.Anthropic,
    mode: str = "normal"  # "normal", "dry_run", "shadow", "auto"
) -> InvoiceProcessingResult
```

`mode` semantics:
- `"normal"`: Full processing. Pauses at `pending_approval` for non-auto-approve invoices. Posts immediate entries after approval. Transaction committed.
- `"dry_run"`: Full processing through the SAME code path as normal/auto. All DB writes happen inside a transaction that is **rolled back** at the end. The function returns the full `InvoiceProcessingResult` with entries, approval routing, and classifications — but nothing is persisted. Idempotent — can be run repeatedly with zero DB side effects.
- `"shadow"`: Full processing, all DB writes committed, but entries are stored with `posted=0`. Used for unlabeled invoices. NOT idempotent — running twice on the same invoice will fail (unique constraints).
- `"auto"`: Like normal, but auto-approves all invoices regardless of routing level. Used by the eval suite. Transaction committed.

### 12.2 Orchestration Flow

```python
def process_invoice(invoice, db, client, mode):
    # Transaction wrapper — all DB writes happen inside this.
    # dry_run → rollback. All other modes → commit.
    #
    # The code path is IDENTICAL for all modes until the final commit/rollback.
    # This eliminates mode-specific branching throughout the function.

    db.execute("BEGIN")
    try:
        result = _process_invoice_inner(invoice, db, client, mode)

        if mode == "dry_run":
            db.execute("ROLLBACK")
            result.status = "dry_run_complete"
        else:
            db.execute("COMMIT")

        return result

    except Exception as e:
        db.execute("ROLLBACK")
        set_invoice_status(invoice.invoice_id, "error", db)  # This gets its own transaction
        raise

def _process_invoice_inner(invoice, db, client, mode):
    # 1. PO Matching
    po_result = match_po(invoice, db)
    if not po_result.matched:
        set_invoice_status(invoice.invoice_id, "flagged_for_review", db)
        add_flag(invoice.invoice_id, po_result.reason, db)
        return InvoiceProcessingResult(status="flagged", reason=po_result.reason)

    set_invoice_status(invoice.invoice_id, "po_matched", db)

    # 2. Attribute Extraction + Classification (per line item)
    all_classifications = []
    all_entries = []
    has_unclassifiable = False

    for i, line_item in enumerate(invoice.line_items):
        # 2a. LLM extraction
        attrs = extract_attributes(line_item, invoice_context(invoice), client)
        validate_and_fix_invariants(attrs)
        store_attributes(invoice.invoice_id, i, attrs, db)

        # 2b. Resolve unit cost (deterministic first, LLM fallback)
        unit_cost = resolve_unit_cost(line_item, attrs)
        # Precedence: line_item.unit_cost → amount/quantity → attrs.unit_cost_extracted → amount

        # 2c. Classification
        classification = classify_line_item(attrs, unit_cost)

        # 2d. Treatment override (prepaid/accrual)
        classification = determine_treatment(attrs, classification, invoice)

        store_classification(invoice.invoice_id, i, classification, db)
        all_classifications.append(classification)

        if classification.gl_code == "UNCLASSIFIED":
            has_unclassifiable = True
            add_flag(invoice.invoice_id, f"unclassifiable_line:{i}", db)

        # 2e. Journal entries
        entries = generate_journal_entries(invoice, line_item, i, classification, attrs)
        all_entries.extend(entries)

    if has_unclassifiable:
        set_invoice_status(invoice.invoice_id, "flagged_for_review", db)

    set_invoice_status(invoice.invoice_id, "classified", db)

    # 3. Balance verification
    verify_balance(invoice, all_entries)

    # 4. Approval routing
    approval = route_approval(invoice, all_classifications)
    store_approval(approval, db)

    # 5. Store entries — always with posted=0
    # approve() will flip immediate entries to posted=1 when called
    store_entries(all_entries, db, posted=False)

    # 6. Approval gate
    set_invoice_status(invoice.invoice_id, "pending_approval", db)

    if mode == "auto" or approval.required_level == "auto_approve":
        approve(invoice.invoice_id, "system", db)
        set_invoice_status(invoice.invoice_id, "posted", db)
        return InvoiceProcessingResult(status="posted", entries=all_entries, approval=approval)
    elif mode == "shadow":
        return InvoiceProcessingResult(status="shadow_complete", entries=all_entries, approval=approval)
    else:
        # mode == "normal" or "dry_run" (dry_run follows same path, rolled back by caller)
        # Pause here — return with status pending_approval
        # The caller (CLI or test harness) must call approve() or reject() to continue
        return InvoiceProcessingResult(status="pending_approval", entries=all_entries, approval=approval)
```

**LOCKED DECISION — Dry-run via transaction rollback:**

Dry-run executes the EXACT same code path as normal mode. Every `store_*` and `set_*` call fires. At the end, the transaction is rolled back instead of committed. This means:
- The function returns the full `InvoiceProcessingResult` with entries, approval, and classifications.
- No DB state is changed. The invoice remains in whatever status it was before the call.
- Dry-run is fully idempotent — can be run any number of times with zero side effects.
- No mode-specific `if` statements inside the inner function. One code path, tested once.

// WHY: Branching at the top ("skip all store/set calls in dry-run") would require a mode check at every call site. That's bug-prone — miss one and dry-run silently persists partial state. Transaction rollback is one decision point at the boundary. The inner function doesn't know or care what mode it's in (except for shadow's `posted=0` flag and the approval gate behavior). Same code, same LLM calls, same rules, different persistence behavior.

### 12.3 InvoiceProcessingResult

```python
class InvoiceProcessingResult(BaseModel):
    status: str               # Final status of the invoice after this processing run
    entries: list[JournalEntry] = []
    approval: ApprovalRecord | None = None
    flags: list[str] = []
    error: str | None = None
```

---

## 13. Eval System (`eval/runner.py`)

### 13.1 Function Signature

```python
def run_eval(
    invoices: list[Invoice],
    labels: dict,              # Ground truth: {invoice_id: {line_index: {gl_code, treatment, ...}}}
    db: sqlite3.Connection,
    client: anthropic.Anthropic
) -> EvalReport
```

### 13.2 Ground Truth Labels (`eval/labels.py`)

Hardcoded from the assessment's "Expected GL" column. This is a Python dict, not a JSON file — labels are part of the eval logic, not input data.

**Label schema per line item:**
```python
{
    "gl_code": str,          # Expected final GL code (for accruals, this is the accrual account, not the expense)
    "treatment": str,        # Expected treatment: "expense", "prepaid", "accrual", "capitalize"
    "approval": str,         # Expected approval level: "auto_approve", "dept_manager", "vp_finance"
    "key_attributes": dict   # Only attributes that DRIVE the classification for this line item
}
```

`key_attributes` principle: only include attributes the classification depends on. If Priority 6 (`is_marketing`) fires, don't include `category_hint` — it's never consulted. The eval measures attribute accuracy (dimension 4) against these; including attributes the classification doesn't depend on measures noise.

```python
LABELS = {
    "INV-001": {
        0: {"gl_code": "1310", "treatment": "prepaid", "approval": "vp_finance",
            "key_attributes": {"is_software": True, "billing_frequency": "annual"}}
    },
    "INV-002": {
        0: {"gl_code": "5030", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"}},
        1: {"gl_code": "5040", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"}},
        2: {"gl_code": "5030", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"}}
    },
    "INV-003": {
        0: {"gl_code": "5110", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True}},
        1: {"gl_code": "1500", "treatment": "capitalize", "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True}},
        2: {"gl_code": "1300", "treatment": "prepaid", "approval": "vp_finance",
            "key_attributes": {"is_cloud_hosting": True, "billing_frequency": "annual"}}
    },
    "INV-004": {
        0: {"gl_code": "2110", "treatment": "accrual", "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"}},
        1: {"gl_code": "2100", "treatment": "accrual", "approval": "dept_manager",
            "key_attributes": {"category_hint": "travel"}}
    },
    "INV-005": {
        0: {"gl_code": "5050", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_marketing": True}},
        1: {"gl_code": "5000", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True}},
        2: {"gl_code": "5050", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_marketing": True}},
        3: {"gl_code": "5000", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True}}
    },
    "INV-006": {
        "expected_flag": "no_po_provided"
    }
}
```

### 13.3 Eval Execution Flow

1. Process each invoice using `process_invoice(invoice, db, client, mode="auto")`.
2. For INV-006: verify it was flagged with `no_po_provided`. No GL/treatment eval.
3. For all others: compare each line item's actual classification against the label.
4. For attribute accuracy: compare LLM-extracted attributes against `key_attributes` in the label.
5. Aggregate results into `EvalReport`.

### 13.4 Accuracy Computation

```
gl_accuracy = correct_gl_codes / total_line_items_with_labels
treatment_accuracy = correct_treatments / total_line_items_with_labels
approval_accuracy = correct_approval_routes / total_invoices_with_labels
attribute_accuracy = correct_key_attributes / total_key_attributes_checked
```

**Attribute accuracy detail:** For each line item, iterate over the `key_attributes` dict in the label. For each key-value pair, check the corresponding field in the LLM's `ExtractedAttributes` output. A key attribute is "correct" if the extracted value matches the expected value exactly (bool match for booleans, string equality for enums). Only `key_attributes` are checked — the other ~12 fields in `ExtractedAttributes` are not evaluated. This means `total_key_attributes_checked` is the sum of `len(key_attributes)` across all labeled line items, not `15 × total_line_items`.

// WHY: `key_attributes` are reverse-engineered from expected GL codes via the rule tree. The rule tree is deterministic — given an expected GL code, there is exactly one set of attributes that could have produced it through the if/elif chain. We only check the attributes that drove the classification because checking "did the LLM correctly set `is_cloud_hosting=False` on a patent filing" measures noise, not capability. The feedback loop (Section 14) uses attribute-level failures to target prompt improvements — this only works if the failures are meaningful.

INV-006 is excluded from all accuracy calculations (it's an exception test, not a classification test).

---

## 14. Feedback Loop (`eval/feedback.py`)

### 14.1 The Engineered Improvement Story

**Phase A — Baseline (intentionally slightly naive):**

The initial LLM prompt in `prompts.py` is deliberately imperfect in two specific ways:

1. **No few-shot example for branded merch override.** The LLM's initial prompt does not include an example of "branded company t-shirts" being classified as `is_physical_goods=True, is_branded_merch=True`. Expected failure: INV-005 lines 2 and 4 get `is_physical_goods=False, is_marketing=True` → classified 5050 instead of 5000.

2. **Ambiguous guidance on "regulatory compliance review."** The initial prompt does not explicitly call out that advisory/review work is `service_type="consulting"` even when it mentions "regulatory." Expected failure: INV-002 line 2 gets `service_type="legal"` → classified 5030 instead of 5040.

Run eval → record **baseline accuracy** across all 4 dimensions. Store the EvalReport.

**Phase B — Corrections:**

After baseline eval, submit corrections:
```python
corrections = [
    Correction(invoice_id="INV-005", line_item_index=1, field="is_physical_goods",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=1, field="is_branded_merch",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=3, field="is_physical_goods",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=3, field="is_branded_merch",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-002", line_item_index=1, field="service_type",
               original_value="legal", corrected_value="consulting"),
]
```

**Phase C — Error analysis:**

```python
def analyze_corrections(corrections: list[Correction]) -> dict:
    """Group corrections by field and pattern. Returns summary."""
```

Output: `{"is_branded_merch_missing": 2, "service_type_legal_vs_consulting": 1}`.

**Phase D — Prompt refinement:**

Based on error patterns, add to the prompt:
1. A few-shot example: `"Branded company t-shirts (500 units)" → is_physical_goods: true, is_branded_merch: true, is_marketing: false`.
2. An explicit instruction: `"For regulatory compliance review, advisory, or assessment work — even if it mentions 'regulatory' — set service_type to 'consulting' unless the work is litigation, patent filing, or contract drafting."`

**Phase E — Re-run eval:**

Run eval again with the refined prompt. Record **after accuracy**. Compare against baseline.

**Phase F — Produce before/after artifact:**

```python
def generate_improvement_report(baseline: EvalReport, after: EvalReport, corrections: list[Correction]) -> str:
    """Generate a human-readable before/after comparison. Printed to stdout by `feedback report`."""
```

Output format (structured text to stdout, consistent with all other CLI output):
```
══════════════════════════════════════════════════
Feedback Loop Report
══════════════════════════════════════════════════

Accuracy Comparison:
┌────────────────────┬──────────────┬──────────────┬────────────┐
│ Dimension          │ Baseline     │ After        │ Delta      │
├────────────────────┼──────────────┼──────────────┼────────────┤
│ GL Code            │ 10/13 (76.9%)│ 13/13 (100%) │ +23.1 pp   │
│ Treatment          │ 13/13 (100%) │ 13/13 (100%) │  —         │
│ Approval Routing   │  5/5  (100%) │  5/5  (100%) │  —         │
│ Attribute (key)    │ 18/23 (78.3%)│ 23/23 (100%) │ +21.7 pp   │
└────────────────────┴──────────────┴──────────────┴────────────┘

Corrections Applied: 5
┌────────────┬──────┬─────────────────────┬──────────┬───────────┐
│ Invoice    │ Line │ Field               │ Was      │ Should be │
├────────────┼──────┼─────────────────────┼──────────┼───────────┤
│ INV-005    │  1   │ is_physical_goods   │ false    │ true      │
│ INV-005    │  1   │ is_branded_merch    │ false    │ true      │
│ INV-005    │  3   │ is_physical_goods   │ false    │ true      │
│ INV-005    │  3   │ is_branded_merch    │ false    │ true      │
│ INV-002    │  1   │ service_type        │ legal    │ consulting│
└────────────┴──────┴─────────────────────┴──────────┴───────────┘

Error Patterns Identified: 2
  1. Missing branded merch flag (2 line items, 4 attribute errors)
     → LLM did not recognize physical goods from marketing vendor
  2. Regulatory/consulting confusion (1 line item, 1 attribute error)
     → LLM tagged advisory work as legal based on subject matter

Prompt Changes Applied:
  1. Added few-shot example: "Branded company t-shirts (500 units)"
     → is_physical_goods: true, is_branded_merch: true, is_marketing: false
  2. Added instruction: advisory/review work about regulatory topics
     → service_type: consulting, not legal

Baseline Failures (now fixed):
  INV-005 line 1: GL 5050 → 5000 (physical goods, not marketing)
  INV-005 line 3: GL 5050 → 5000 (physical goods, not marketing)
  INV-002 line 1: GL 5030 → 5040 (consulting, not legal)
══════════════════════════════════════════════════
```

This is the assessor-facing artifact. It tells the complete story in one screen: what was wrong, why it was wrong, what was changed, and the measurable result. The format is illustrative — exact layout may vary, but all sections must be present.

// WHY: The feedback loop is not "the system learns by itself." It's a structured human-in-the-loop improvement cycle: eval → correct → analyze → adjust → re-eval → measure. The engineered initial weakness ensures there's something to improve. The before/after report is the assessor-facing artifact that proves the loop works.

---

## 15. CLI Interface (`cli.py`)

### 15.0 Input Interface Contract

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

### 15.1 Commands

```
python cli.py process <invoice_id> [--mode normal|dry_run|shadow|auto]
python cli.py process-all [--mode normal|dry_run|shadow|auto]
python cli.py approve <invoice_id> [--by <name>]
python cli.py reject <invoice_id> --reason <reason> [--by <name>]
python cli.py eval
python cli.py shadow
python cli.py feedback apply-corrections <corrections_file.json>
python cli.py feedback analyze
python cli.py feedback report
python cli.py status [<invoice_id>]
python cli.py init-db
python cli.py demo
```

### 15.2 Command Details

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
  4. **Apply Corrections** — Load `data/corrections.json` (ships with the repo, contains the 5 corrections from Section 14)
  5. **Error Analysis** — Print grouped error patterns
  6. **Refined Eval** — Re-init DB, process 6 labeled invoices with refined prompt, print improved EvalReport
  7. **Before/After Report** — Print the full feedback loop comparison (Section 14, Phase F)

  Step 6 requires a fresh DB because the labeled invoices were already processed in step 2. `init-db` resets state. The refined prompt is loaded by applying the corrections and regenerating the prompt with few-shot additions (same logic as `feedback report`).

  `data/corrections.json` is a pre-built JSON file containing the 5 corrections from Section 14. It ships with the repo. The assessor doesn't create it — the demo loads it automatically.

### 15.3 Output Format

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

---

## 16. Invoice Data Files

### 16.1 Labeled Invoices (`data/invoices_labeled.json`)

Each invoice follows the `Invoice` Pydantic schema. The 6 labeled invoices from the assessment are encoded exactly:

- **INV-001:** Cloudware Solutions, PO-2026-044, 2026-01-05, Engineering, 1 line item ($24,000), total $24,000
- **INV-002:** Morrison & Burke LLP, PO-2026-051, 2026-01-20, Legal, 3 line items ($4,500 + $3,200 + $1,800), total $9,500
- **INV-003:** TechDirect Inc., PO-2026-038, 2026-02-01, Engineering, 3 line items ($5,400 + $8,500 + $36,000), total $49,900. MacBook line: `quantity=3, unit_cost=1800`. Server line: `quantity=1, unit_cost=8500`. AWS line: `quantity=1`.
- **INV-004:** Apex Strategy Group, PO-2025-189, 2026-01-15, Operations, 2 line items ($7,500 + $1,200), total $8,700. Service period: 2025-12-01 to 2025-12-31.
- **INV-005:** BrightSpark Agency, PO-2026-062, 2026-02-10, Marketing, 4 line items ($15,000 + $2,000 + $5,000 + $1,500), total $23,500. All lines `quantity=1` — t-shirts and gift bags are lump sum.
- **INV-006:** QuickPrint Co., po_number=null, 2026-02-20, Marketing, 1 line item ($3,800), total $3,800

### 16.2 Unlabeled Invoices (`data/invoices_unlabeled.json`)

10 invoices (UL1–UL10) encoded in the same format. Quantity encoding for multi-unit lines:

- **UL4** line 1: Conference registration — `quantity=2, unit_cost=1800, amount=3600`
- **UL5** line 1: Refurbished Dell 27" monitors — `quantity=10, unit_cost=450, amount=4500`
- All other unlabeled invoice lines: `quantity=1`

**LOCKED DECISION — `quantity` encoding rules:**

`quantity` exists on `LineItem` only. There is no `quantity_extracted` on `ExtractedAttributes`. The rule engine never asks "how many units?" — it only asks "what's the unit cost?" for the Priority 2 equipment threshold ($5K).

Encoding principle: only set `quantity > 1` when the per-unit cost is relevant to a rule engine decision. Specifically, this means equipment/hardware where the $5K threshold applies (INV-003 MacBooks, UL4 conference registrations, UL5 monitors). For physical goods where Priority 1 fires regardless of unit cost (t-shirts, gift bags, brochures), encode as `quantity=1` lump sum — breaking out per-unit cost is noise the rule engine never consumes.

// WHY: The LLM's `unit_cost_extracted` is the fallback for production scenarios with unstructured data. For our test data, we control the JSON and encode quantities where they matter. Lump-summing items where unit cost is irrelevant keeps the data honest about what the system actually uses.

---

## 17. Error Handling

### 17.1 LLM Call Failures

Two failure modes with different strategies:

**Transient API errors (rate limit, timeout, 5xx):**
- Retry up to 3 times with exponential backoff (1s, 2s, 4s).
- Same prompt, same parameters. The prompt isn't the problem.
- If all 3 retries fail, flag the line item as `extraction_failed` and mark the invoice for review. Processing continues for other line items.

**Schema validation failure (structured output doesn't conform to `ExtractedAttributes`):**
- Do NOT retry. At temperature 0, the model will produce the same output deterministically. Retrying the same prompt is wasted API calls.
- Do NOT retry with a modified prompt. That's scope creep — prompt modification is the feedback loop's job, not the error handler's.
- Log the raw response for debugging. Flag the line item as `extraction_failed`. Mark the invoice for review.
- This should be near-impossible: the Anthropic SDK's structured output enforces schema conformance at the API level. If it fires, it's an SDK bug or API regression, not a prompt problem.

**No fallback to unstructured parsing.** If structured output fails, it fails. No regex extraction from freetext. No "try again without the schema." The system fails visibly and the human reviews it.

// WHY: The distinction matters because the retry strategy depends on whether the failure is transient (API hiccup → retry) or deterministic (model can't produce valid output → don't retry). Conflating them wastes API calls on failures that will never self-resolve. The hard "no fallback" rule prevents silent degradation — if the LLM can't fill the schema, we'd rather flag it than guess.

### 17.2 Rule Engine Failures

- If `classify_line_item` returns `UNCLASSIFIED`, the line item is flagged and the invoice proceeds. Other line items are still classified.
- The `UNCLASSIFIED` GL code propagates to journal entries as a zero entry (no debit/credit). The invoice is flagged for review.

### 17.3 Database Failures

- The entire `process_invoice` call is wrapped in a single transaction (see Section 12.2). No partial state: either all writes for an invoice succeed, or none do.
- If processing fails mid-transaction, the transaction is rolled back. The invoice status is then set to `"error"` in a separate transaction with the error message stored.
- Dry-run mode uses this same transaction pattern — all writes happen, then the transaction is rolled back intentionally. This means dry-run gets the same error handling behavior as normal mode.

---

## 18. INV-004 Accrual Detail — Worked Example

This is the trickiest invoice in the test set. Spelling out the exact expected behavior:

**Input:** Invoice date 2026-01-15, service period Dec 2025 (2025-12-01 to 2025-12-31).

**Line 1: "Operational efficiency assessment – Dec 2025" — $7,500**
- LLM extracts: `service_type="consulting"`, `service_period_end="2025-12-31"`
- Classification: 5040 (consulting)
- Treatment: accrual (invoice date 2026-01-15 > service_period_end 2025-12-31)
- `accrual_type="professional_services"` → uses account 2110
- **GL code in classification result:** "5040" — this is the expense account, which goes into the journal entry. But the expected GL per the assessment is "2110" because the assessment labels show the primary booking account.

**Reconciliation with assessment labels:**
The assessment says expected GL is "2110" for this line. This means the label represents the **primary booking account** (the accrual liability), not the expense classification. Our system classifies the expense type (5040) and then the treatment logic produces the journal entry with 2110.

For eval purposes: check that `treatment == "accrual"` AND the journal entry debits the correct expense account (5040) AND credits the correct accrual account (2110). The label check for `gl_code` should match the **accrual account** (2110 for line 1, 2100 for line 2), since that's what the assessment expects.

**Line 2: "Travel expenses – Dec 2025 on-site visits" — $1,200**
- LLM extracts: `category_hint="travel"`, `service_period_end="2025-12-31"`
- Classification: 5060 (travel)
- Treatment: accrual → `accrual_type="other"` → account 2100
- Expected GL per assessment: 2100

**Journal entries for INV-004:**
```
DR 5040 $7,500 / CR 2110 $7,500 — accrual booking, immediate, date 2025-12-31
DR 2110 $7,500 / CR 2000 $7,500 — accrual reversal, pending_payment
DR 5060 $1,200 / CR 2100 $1,200 — accrual booking, immediate, date 2025-12-31
DR 2100 $1,200 / CR 2000 $1,200 — accrual reversal, pending_payment
```

// WHY: This worked example eliminates any ambiguity about how the accrual flow works. The assessment's "expected GL" for accruals refers to the accrual liability account (2110/2100), not the expense account. The eval labels must match this convention.

---

## 19. INV-002 Line 2 — The Regulatory Advisory Edge Case

**Line: "Regulatory compliance review & advisory" — $3,200**
**Expected: 5040 (Consulting)**

This is the hardest classification call in the test set. The full analysis and locked decision are in Section 8.3. Summary:

The SOP lists "regulatory" as a legal sub-type, but that refers to *direct regulatory actions* (filing, compliance submissions). "Regulatory compliance review & advisory" is advisory work about a regulatory subject — the nature of the work (advisory) dominates the subject matter (regulatory). The LLM resolves this by extracting `service_type="consulting"`. No rule-level override.

The initial prompt is intentionally naive about this distinction (engineered weakness #2). The feedback loop (Section 14) adds the explicit guidance that makes the LLM get it right on re-run.

// WHY: This edge case exists in the spec as a standalone section because it tests two things simultaneously: (1) the architecture's claim that semantic disambiguation belongs in the LLM layer, and (2) the feedback loop's ability to improve LLM behavior through targeted prompt refinement. Getting it wrong initially and then fixing it is a stronger demo than getting it right by accident.

---

## 20. Testing Strategy

### 20.1 Unit Tests (`tests/test_rules.py`)

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

### 20.2 Integration Tests (Eval Suite)

The eval suite (Section 13) IS the integration test. It runs the full pipeline end-to-end against labeled data.

### 20.3 No Mocking the LLM

The eval suite calls the real Anthropic API. No mocked LLM responses.

// WHY: The LLM's actual behavior on these inputs is what we're evaluating. Mocking it would test nothing. The unit tests for deterministic components don't need the LLM.

---

## 21. README Outline

The README must cover:

1. **One-paragraph summary** — what this is, how it works.
2. **Setup instructions** — clone, install deps, set `ANTHROPIC_API_KEY`, `python cli.py init-db`.
3. **Quick start** — `python cli.py demo` runs the full showcase. Or run individual commands for step-by-step exploration.
4. **Architecture** — the three-layer diagram (LLM perception → rule decision → action), why this split.
5. **Feedback loop demo** — how to reproduce the before/after improvement (or just run `demo`).
6. **Design decisions** — link to or inline the key decisions from Phase 1 (LLM as feature extractor, not classifier; engineered initial weakness; etc.).
7. **Known limitations** — no materiality threshold (strict SOP), `one_time` with long service period expenses instead of prepaid, agent scope ends at AP (no cash disbursement), Engineering override never fires in test data.
8. **What this would look like in production** — ERP integration, real PO system, scheduled amortization posting, CI eval regression, auto-learning, REST API, materiality thresholds.

---

## Appendix A: Full Chart of Accounts Reference

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

*End of spec. Every decision is locked. Every rule is explicit. Build it exactly as written.*
