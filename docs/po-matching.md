# Step 1 — PO Matching

> **Implements:** `src/po_matching.py`
> **Spec origin:** Section 6

---

## Function Signature

```python
def match_po(invoice: Invoice, db: sqlite3.Connection) -> POMatchResult
```

---

## Logic

1. If `invoice.po_number` is `None` → return `POMatchResult(matched=False, reason="no_po_provided")`.
2. Query `purchase_orders` table for `po_number`.
3. If PO not found → return `POMatchResult(matched=False, reason="po_not_found")`.
4. Compute tolerance: `abs(invoice.total - po.amount) / po.amount`.
5. If tolerance > 0.10 → return `POMatchResult(matched=False, reason="tolerance_exceeded", tolerance_pct=computed_value)`.
6. Otherwise → return `POMatchResult(matched=True, po_amount=po.amount, tolerance_pct=computed_value)`.

---

## POMatchResult Schema

```python
class POMatchResult(BaseModel):
    matched: bool
    reason: str | None = None       # "no_po_provided", "po_not_found", "tolerance_exceeded"
    po_amount: float | None = None
    tolerance_pct: float | None = None
```

---

## Failure Behavior

When `matched=False`: the pipeline sets invoice status to `flagged_for_review`, records the flag reason, and **stops processing**. No classification, no journal entries, no approval routing. The SOP says: "No PO → flag for manual review, do not classify."

// WHY: The SOP is explicit — no PO means no classification. We don't partially process. The invoice sits in `flagged_for_review` until a human provides a PO or overrides.
