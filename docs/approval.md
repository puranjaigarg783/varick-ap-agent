# Step 4 — Approval Routing

> **Implements:** `src/approval.py`
> **Spec origin:** Section 10

---

## Invoice State Machine

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

---

## Approval Routing Function

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

---

## Expected Approval Routes (Labeled Invoices)

| Invoice | Total     | Dept        | Key Factor              | Expected Route     |
|---------|-----------|-------------|-------------------------|--------------------|
| INV-001 | $24,000   | Engineering | > $10K                  | vp_finance         |
| INV-002 | $9,500    | Legal       | $1K–$10K               | dept_manager       |
| INV-003 | $49,900   | Engineering | Fixed Asset (1500) line | vp_finance         |
| INV-004 | $8,700    | Operations  | $1K–$10K               | dept_manager       |
| INV-005 | $23,500   | Marketing   | > $10K                  | vp_finance         |
| INV-006 | $3,800    | Marketing   | N/A — flagged (no PO)   | N/A                |

---

## Human-in-the-Loop API

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
