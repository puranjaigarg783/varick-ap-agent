# Step 5 — Journal Entry Generation

> **Implements:** `src/journal.py`
> **Spec origin:** Section 11

---

## Function Signature

```python
def generate_journal_entries(
    invoice: Invoice,
    line_item: LineItem,
    line_index: int,
    classification: ClassificationResult,
    attrs: ExtractedAttributes
) -> list[JournalEntry]
```

---

## Entry Generation Rules

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

---

## Balance Verification

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

---

## Posting Behavior by Mode

| Mode       | DB writes during processing | Transaction outcome | `posted` flag |
|------------|---------------------------|--------------------|----|
| `normal`   | All stores + status updates | **Committed** | `1` for immediate entries |
| `auto`     | All stores + status updates + auto-approve | **Committed** | `1` for immediate entries |
| `dry_run`  | All stores + status updates (same code path) | **Rolled back** — zero DB side effects | N/A |
| `shadow`   | All stores + status updates | **Committed** | `0` for all entries (proposals only) |
