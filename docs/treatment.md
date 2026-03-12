# Step 3 — Prepaid & Accrual Treatment

> **Implements:** `src/treatment.py`
> **Spec origin:** Sections 9, 18

---

## Function Signature

```python
def determine_treatment(
    attrs: ExtractedAttributes,
    classification: ClassificationResult,
    invoice: Invoice
) -> ClassificationResult  # Returns updated classification with treatment adjustments
```

---

## Prepaid Logic

**LOCKED DECISION — "Paid upfront" proxy:**

The SOP's prepaid trigger is: "service period > 1 month + paid upfront." We are not modeling payments. `billing_frequency == "annual"` is the sole proxy for "paid upfront." It is the only billing frequency value that implies both conditions simultaneously: a multi-month service period AND a single upfront payment.

`one_time` with a long service period does NOT trigger prepaid. This is a deliberate simplification — `one_time` was designed for single-deliverable items (patent filing, placement fee) where the concept of "upfront" doesn't apply. If a future invoice has a lump-sum multi-month engagement billed as `one_time`, it expenses and gets caught in shadow mode review. The README notes this as a known limitation.

**Prepaid flow:**

Prepaid is already handled by the classification rules in `docs/classification.md` for **software** (`billing_frequency == "annual"` → 1310) and **cloud** (`billing_frequency == "annual"` → 1300). For all other categories, `determine_treatment()` applies the SOP's general prepaid rule.

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
3. Accrual check (see below)

// WHY: The SOP's prepaid rule is general — "service period > 1 month + paid upfront → book to Prepaid account." It is not limited to software and cloud. The classification rules in `docs/classification.md` handle software and cloud prepaid inline because they have dedicated prepaid accounts (1310, 1300). Everything else flows through here and lands in 1300 (Prepaid General). `billing_frequency == "annual"` is the single, deterministic proxy for the SOP's two-part trigger. The service period check is belt-and-suspenders — it catches contradictory LLM extractions but never fires in normal operation because annual billing implies a multi-month period.

---

## Accrual Logic

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

---

## Treatment Priority

Accrual check runs AFTER classification. If a line item is both prepaid-eligible (annual billing) AND accrual-eligible (service period before invoice date), **accrual wins**. Rationale: if the service already happened, you don't prepay for it — you accrue it.

// WHY: This scenario is unlikely in the test data but the rule must be explicit. Fail-closed: accrual treatment is the conservative choice.

---

## INV-004 Accrual Detail — Worked Example

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
