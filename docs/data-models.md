# Data Models (Pydantic Schemas)

> **Implements:** `src/models.py`
> **Spec origin:** Section 4

All data flows through typed Pydantic models. These are the contracts between pipeline stages.

---

## Invoice Input

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

---

## LLM-Extracted Attributes

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
- **Prepaid amortization months** (see `docs/classification.md`): line-item dates → default 12 months. No invoice-level fallback needed — if classification already decided "prepaid," billing is annual and 12 is the safe default.
- **Accrual detection** (see `docs/treatment.md`): line-item `service_period_end` → invoice-level `service_period_end` → `null` means "not accrual." This fallback is critical for INV-004, where the service period is at invoice level ("Service period: Dec 2025"), not in the individual line item descriptions.

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

---

## Classification Result

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

---

## Journal Entry

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

---

## Approval Record

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

---

## Invoice Processing Status

```python
class InvoiceStatus(BaseModel):
    invoice_id: str
    status: str  # See state machine in docs/approval.md
    flags: list[str] = []     # e.g. ["no_po_match"], ["tolerance_exceeded"], ["low_confidence_line:2"]
    error: str | None = None  # If processing failed, why
```

---

## Eval and Correction Models

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
