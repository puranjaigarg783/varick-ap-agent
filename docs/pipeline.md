# Agent Orchestrator & Tools

> **Implements:** `src/agent.py`, `src/tools.py`

---

## Architecture

The LLM agent orchestrates the invoice workflow via tool use. Every deterministic function (PO matching, classification, treatment, journal entries, approval routing) is wrapped as a tool. The LLM provides perception (attribute extraction via tool call parameters) and workflow reasoning. The tools enforce every business rule deterministically.

Eight tools are defined in `src/tools.py` — see the `TOOL_SCHEMAS` list for full JSON schemas. The `TOOL_HANDLERS` dict maps tool names to handler functions.

---

## Processing Context

All tools operate on a shared `ProcessingContext` created at the start of each invoice. The agent triggers tools, reads their results, and reasons about what to do next.

Key fields: `po_result`, `line_results` (keyed by line item index), `journal_entries`, `approval`, `flags`, `status`, `completed`.

---

## Unit Cost Resolution

The `classify_line_item` handler resolves unit cost via a deterministic precedence chain (first non-null wins):

1. `line_item.unit_cost` — explicit in invoice data (authoritative)
2. `line_item.amount / line_item.quantity` — computed when quantity > 1
3. Agent-provided `unit_cost` parameter — LLM's parsing (fallback only)
4. `line_item.amount` — last resort

The agent's `unit_cost` parameter maps to what was previously `unit_cost_extracted` — a fallback, not authoritative.

---

## Error Handling

**Transient API errors:** Retry up to 3 times with exponential backoff. If all retries fail, the transaction rolls back and the invoice is set to error.

**Malformed tool calls:** The handler returns a structured error response (e.g., `{"error": "line_item_index 5 is out of range"}`). The agent sees it and can self-correct. After 3 consecutive errors on the same tool, the invoice is flagged for review.

**Agent stuck:** If `max_iterations` (30) is reached without `complete_processing` or `flag_for_review`, the invoice is set to error.

**Rule engine failures:** `UNCLASSIFIED` results are flagged in the DB regardless of whether the agent calls `flag_for_review`.

---

## Locked Decisions

**Transaction wrapper:** `BEGIN` → agent loop → `COMMIT` or `ROLLBACK`. Dry-run rolls back. All tool handlers write inside this transaction.

**Dry-run via rollback:** Executes the exact same code path. All `store_*` calls fire. Transaction is rolled back at the end.

**Max iterations = 30:** A typical 3-line-item invoice takes ~10 iterations. Cap at 30 handles up to ~8 line items with headroom.

**Conversation trace:** Full `messages` array stored as JSON in `conversation_traces` table — every reasoning step, tool call, and tool result for audit.

---

## Mode Semantics

- `"normal"` — Full processing. Pauses at `pending_approval`. Transaction committed.
- `"dry_run"` — Same code path, transaction rolled back. Idempotent.
- `"shadow"` — Full processing, entries stored with `posted=0`. Transaction committed.
- `"auto"` — Auto-approves all invoices regardless of routing. Used by eval suite.
