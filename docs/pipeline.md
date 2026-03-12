# Pipeline Orchestrator & Error Handling

> **Implements:** `src/pipeline.py`
> **Spec origin:** Sections 12, 17

---

## Main Function

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

---

## Orchestration Flow

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

---

## InvoiceProcessingResult

```python
class InvoiceProcessingResult(BaseModel):
    status: str               # Final status of the invoice after this processing run
    entries: list[JournalEntry] = []
    approval: ApprovalRecord | None = None
    flags: list[str] = []
    error: str | None = None
```

---

## Error Handling

### LLM Call Failures

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

### Rule Engine Failures

- If `classify_line_item` returns `UNCLASSIFIED`, the line item is flagged and the invoice proceeds. Other line items are still classified.
- The `UNCLASSIFIED` GL code propagates to journal entries as a zero entry (no debit/credit). The invoice is flagged for review.

### Database Failures

- The entire `process_invoice` call is wrapped in a single transaction (see Orchestration Flow above). No partial state: either all writes for an invoice succeed, or none do.
- If processing fails mid-transaction, the transaction is rolled back. The invoice status is then set to `"error"` in a separate transaction with the error message stored.
- Dry-run mode uses this same transaction pattern — all writes happen, then the transaction is rolled back intentionally. This means dry-run gets the same error handling behavior as normal mode.
