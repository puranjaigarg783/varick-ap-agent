# Agent Orchestrator, Tools & Error Handling

> **Implements:** `src/agent.py`, `src/tools.py`
> **Spec origin:** Sections 5–6, 11, 17 of `spec-change-agent-refactor.md`
> **For system prompt design, LLM config, and few-shot examples:** see `docs/llm-extraction.md`

---

## Architecture

The LLM agent orchestrates the invoice workflow via tool use. Every deterministic function — PO matching, classification, treatment, journal entries, balance verification, approval routing — is wrapped as a tool the agent calls. The LLM provides perception (attribute extraction via tool call parameters) and workflow reasoning (deciding what step comes next). The tools enforce every business rule deterministically.

The rule logic inside each tool is **identical** to the original deterministic functions. Only the orchestration layer changed.

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

Signature is identical to the original pipeline. All callers (CLI, eval, shadow) are unaffected.

`mode` semantics (unchanged):
- `"normal"`: Full processing. Pauses at `pending_approval` for non-auto-approve invoices. Transaction committed.
- `"dry_run"`: Full processing through the SAME code path. All DB writes happen inside a transaction that is **rolled back** at the end. Returns the full `InvoiceProcessingResult` — but nothing is persisted. Idempotent.
- `"shadow"`: Full processing, all DB writes committed, but entries stored with `posted=0`. NOT idempotent.
- `"auto"`: Like normal, but auto-approves all invoices regardless of routing level. Used by the eval suite. Transaction committed.

---

## Processing Context

All tools operate on a shared `ProcessingContext` created at the start of each invoice. The agent does not manage state — it triggers tools, reads their results, and reasons about what to do next.

```python
class ProcessingContext:
    invoice: Invoice
    db: sqlite3.Connection
    mode: str                  # "normal", "dry_run", "shadow", "auto"
    po_result: POMatchResult | None = None
    line_results: dict[int, LineProcessingResult] = {}  # keyed by line_item_index
    journal_entries: list[JournalEntry] = []
    approval: ApprovalRecord | None = None
    flags: list[str] = []
    status: str = "received"
    completed: bool = False

class LineProcessingResult(BaseModel):
    attributes: ExtractedAttributes
    unit_cost: float
    classification: ClassificationResult
    treatment_applied: bool = False
```

// WHY: Shared context eliminates the need to pass full state through every tool call. The agent calls `classify_line_item` with attributes, the tool stores the result in context. The agent calls `apply_treatment` for that line item, the tool reads the classification from context. This mirrors how the original pipeline accumulated state across function calls — the mechanism changes (shared context vs. local variables), the data flow doesn't.

---

## Tool Definitions (`src/tools.py`)

Eight tools, each with a JSON schema (for the Anthropic API `tools` parameter) and a handler that wraps an existing deterministic function.

### Tool 1: `lookup_purchase_order`

```json
{
    "name": "lookup_purchase_order",
    "description": "Look up a purchase order by number and validate the amount against the invoice total within 10% tolerance. Call this first for every invoice. If the invoice has no PO number, call flag_for_review instead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "po_number": {
                "type": "string",
                "description": "The purchase order number from the invoice"
            }
        },
        "required": ["po_number"]
    }
}
```

**Handler:** Calls `match_po()` from `po_matching.py`. Stores result in `ctx.po_result`. Returns `POMatchResult` as JSON.

### Tool 2: `classify_line_item`

The critical tool. The agent provides extracted attributes as parameters. The tool applies the priority rule tree and returns the classification. The agent's parameters ARE the attribute extraction — folding what was previously a separate LLM call into the agent's tool invocation.

```json
{
    "name": "classify_line_item",
    "description": "Classify a line item using the extracted attributes you have determined. You provide what the line item IS (physical goods, equipment, software, etc.) and the rule engine determines the correct GL account. You NEVER determine the GL code yourself — this tool does that. Call this once per line item after analyzing it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "line_item_index": {
                "type": "integer",
                "description": "Zero-based index of the line item in the invoice"
            },
            "is_physical_goods": {
                "type": "boolean",
                "description": "True if this is a tangible physical item: supplies, stationery, toner, merch, monitors, etc."
            },
            "is_branded_merch": {
                "type": "boolean",
                "description": "True if this is branded merchandise: t-shirts, swag, gift bags with company branding. Must be a subset of physical goods."
            },
            "is_equipment": {
                "type": "boolean",
                "description": "True if this is hardware, machines, or devices: laptops, servers, monitors. Equipment is a subset of physical goods."
            },
            "is_software": {
                "type": "boolean",
                "description": "True if this is a software license, SaaS subscription, or platform fee"
            },
            "is_cloud_hosting": {
                "type": "boolean",
                "description": "True if this is cloud infrastructure: AWS, Azure, GCP, Cloudflare, hosting"
            },
            "service_type": {
                "type": ["string", "null"],
                "enum": ["legal", "consulting", "mixed_legal", null],
                "description": "For professional services only. 'legal' = direct legal actions (litigation, patent filing, contract drafting, regulatory filing). 'consulting' = advisory, review, strategy, assessment, implementation, creative/design services — includes advisory work ABOUT legal/regulatory topics. 'mixed_legal' = engagement contains both. null = not a professional service."
            },
            "is_marketing": {
                "type": "boolean",
                "description": "True if the LINE ITEM itself is marketing activity: ad spend, campaigns, sponsorships, booth rentals, agency management fees. Assess the line item content, NOT the invoice department. Physical goods from marketing vendors/departments are NOT marketing."
            },
            "category_hint": {
                "type": ["string", "null"],
                "enum": ["travel", "facilities", "training", "telecom", "insurance", "recruiting", "catering", null],
                "description": "For items that don't match the categories above. null if no category applies."
            },
            "billing_frequency": {
                "type": ["string", "null"],
                "enum": ["monthly", "annual", "one_time", "usage_based", null],
                "description": "How this item is billed. 'annual' = yearly upfront payment. 'monthly' = recurring monthly. 'one_time' = single deliverable. 'usage_based' = pay per use. null if unclear."
            },
            "service_period_start": {
                "type": ["string", "null"],
                "description": "ISO date YYYY-MM-DD. Only provide when the text contains a specific date range, named month, or named quarter. Never fabricate dates. null if no period stated."
            },
            "service_period_end": {
                "type": ["string", "null"],
                "description": "ISO date YYYY-MM-DD. Same rules as service_period_start."
            },
            "unit_cost": {
                "type": "number",
                "description": "Per-unit cost for this line item. If the invoice specifies quantity and amount, compute amount/quantity. If the description mentions per-unit pricing (e.g., '3x $1,800'), use that. Otherwise, use the line item amount."
            },
            "confidence": {
                "type": "number",
                "description": "Your confidence in these attributes, 0.0 to 1.0. Below 0.7 means you are unsure."
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining your key attribute decisions for this line item."
            }
        },
        "required": [
            "line_item_index", "is_physical_goods", "is_branded_merch", "is_equipment",
            "is_software", "is_cloud_hosting", "service_type", "is_marketing",
            "category_hint", "billing_frequency", "service_period_start",
            "service_period_end", "unit_cost", "confidence", "reasoning"
        ]
    }
}
```

**Handler logic:**
1. Construct `ExtractedAttributes` from the agent's parameters.
2. Run invariant checks (unchanged from original): force `is_physical_goods=True` if `is_branded_merch` or `is_equipment` is true. Log a warning.
3. Resolve unit cost with the existing precedence chain (first non-null wins):
   1. `line_item.unit_cost` — explicit in the invoice data. Authoritative.
   2. `line_item.amount / line_item.quantity` — if `quantity > 1`, compute deterministically.
   3. Agent-provided `unit_cost` parameter — the LLM parsed a per-unit cost. Fallback only.
   4. `line_item.amount` — if `quantity == 1` and nothing else is available.
4. Call `classify_line_item(attrs, resolved_unit_cost)` from `classification.py`.
5. Store attributes and classification in `ctx.line_results[line_item_index]`.
6. Store in DB: `store_attributes()`, `store_classification()`.
7. If `confidence < 0.7`, add flag `low_confidence_line:{line_item_index}`.
8. If `gl_code == "UNCLASSIFIED"`, add flag.
9. Return `ClassificationResult` as JSON to the agent.

**LOCKED DECISION — Unit cost resolution inside the tool handler, not the agent:**

The agent provides `unit_cost` as its best parsing of per-unit cost. The tool handler applies the deterministic precedence chain: structured invoice data (`line_item.unit_cost`, `amount/quantity`) overrides the agent's value. The agent's `unit_cost` parameter maps to what was previously `unit_cost_extracted` — a fallback, not authoritative.

// WHY: The tool's parameters are the `ExtractedAttributes` schema flattened into individual fields. This makes every attribute a named, typed, required parameter that the agent must explicitly fill. The agent can't skip attributes or provide partial data — the schema enforces completeness. The tool handler constructs the `ExtractedAttributes` object internally, so all downstream code (classification, treatment, eval) receives the exact same typed object as before.

### Tool 3: `apply_treatment`

```json
{
    "name": "apply_treatment",
    "description": "Check if a classified line item needs prepaid or accrual treatment. Call this after classify_line_item for each line item. The tool checks if the billing is annual (prepaid) or if the service period ended before the invoice date (accrual).",
    "input_schema": {
        "type": "object",
        "properties": {
            "line_item_index": {
                "type": "integer",
                "description": "Zero-based index of the line item (must have been classified already)"
            }
        },
        "required": ["line_item_index"]
    }
}
```

**Handler:** Reads classification and attributes from `ctx.line_results[line_item_index]`. Calls `determine_treatment(attrs, classification, ctx.invoice)` from `treatment.py`. Updates classification in context and DB. Returns the updated `ClassificationResult` (with treatment override if applicable).

// WHY: The treatment tool takes only `line_item_index` because all the data it needs (attributes, classification, invoice dates) is already in the processing context from the classify step. The agent doesn't re-specify service periods — the tool reads them from the stored attributes. This prevents the agent from providing inconsistent dates between classify and treatment calls.

### Tool 4: `generate_journal_entries`

```json
{
    "name": "generate_journal_entries",
    "description": "Generate journal entries for all classified line items. Call this once after all line items have been classified and treatment-checked.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

**Handler:** Iterates over all `ctx.line_results`, calls `generate_journal_entries()` from `journal.py` for each. Aggregates into `ctx.journal_entries`. Returns a summary: number of entries generated, total amount, breakdown by type (immediate, scheduled, pending_payment).

### Tool 5: `verify_balance`

```json
{
    "name": "verify_balance",
    "description": "Verify that the generated journal entries balance against the invoice total. Call this after generate_journal_entries.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

**Handler:** Calls `verify_balance(ctx.invoice, ctx.journal_entries)` from `journal.py`. Returns pass/fail with computed vs. expected totals.

### Tool 6: `route_approval`

```json
{
    "name": "route_approval",
    "description": "Determine the approval routing for this invoice based on the total amount, department, and line item classifications. Call this after balance verification passes.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

**Handler:** Calls `route_approval(ctx.invoice, list(ctx.line_results.values()))` from `approval.py`. Stores in `ctx.approval`. Returns `ApprovalRecord` as JSON.

### Tool 7: `flag_for_review`

```json
{
    "name": "flag_for_review",
    "description": "Flag this invoice for manual human review. Use this when: the invoice has no PO number, the PO amount exceeds tolerance, or a line item cannot be classified. Processing stops after this call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why the invoice needs review, e.g. 'No purchase order number provided' or 'PO amount exceeds 10% tolerance'"
            }
        },
        "required": ["reason"]
    }
}
```

**Handler:** Sets `ctx.status = "flagged_for_review"`, adds flag, sets `ctx.completed = True`. Returns confirmation.

### Tool 8: `complete_processing`

```json
{
    "name": "complete_processing",
    "description": "Signal that you have finished processing this invoice. Call this as your final action after all line items are classified, entries generated, balance verified, and approval routed.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

**Handler:** Performs the approval gate logic. If `mode == "auto"` or `auto_approve`, calls `approve()`. Stores entries in DB. Sets final status. Sets `ctx.completed = True`. Returns the final status.

// WHY: `complete_processing` is the terminal tool. Without it, the agent loop would need to detect completion from message content, which is brittle. A dedicated tool makes the termination condition deterministic: `ctx.completed == True` ends the loop.

### Tool Registration

```python
TOOLS = [
    lookup_purchase_order_schema,
    classify_line_item_schema,
    apply_treatment_schema,
    generate_journal_entries_schema,
    verify_balance_schema,
    route_approval_schema,
    flag_for_review_schema,
    complete_processing_schema,
]

TOOL_HANDLERS = {
    "lookup_purchase_order": handle_lookup_purchase_order,
    "classify_line_item": handle_classify_line_item,
    "apply_treatment": handle_apply_treatment,
    "generate_journal_entries": handle_generate_journal_entries,
    "verify_balance": handle_verify_balance,
    "route_approval": handle_route_approval,
    "flag_for_review": handle_flag_for_review,
    "complete_processing": handle_complete_processing,
}
```

---

## Agent Loop (`src/agent.py`)

### Orchestration Flow

```python
def process_invoice(invoice, db, client, mode):
    # Transaction wrapper — identical to original pipeline.
    # dry_run → rollback. All other modes → commit.

    db.execute("BEGIN")
    try:
        result = _run_agent(invoice, db, client, mode)

        if mode == "dry_run":
            db.execute("ROLLBACK")
            result.status = "dry_run_complete"
        else:
            db.execute("COMMIT")

        return result

    except Exception as e:
        db.execute("ROLLBACK")
        set_invoice_status(invoice.invoice_id, "error", db)  # Gets its own transaction
        raise


def _run_agent(invoice, db, client, mode):
    ctx = ProcessingContext(invoice=invoice, db=db, mode=mode)

    # Build the user message with full invoice details
    user_message = format_invoice_for_agent(invoice)

    # Initialize conversation
    messages = [{"role": "user", "content": user_message}]

    # Agent loop — iterate until completion or max iterations
    max_iterations = 30
    system_prompt = get_system_prompt()  # From prompts.py

    for iteration in range(max_iterations):
        response = client.messages.create(
            model=get_model(),  # AP_AGENT_MODEL env var, default claude-sonnet-4-20250514
            max_tokens=4096,
            temperature=0.0,
            system=system_prompt,
            messages=messages,
            tools=TOOLS,
        )

        # Append assistant response to conversation
        messages.append({"role": "assistant", "content": response.content})

        # Check if the agent is done (no tool calls, just text)
        if response.stop_reason == "end_turn":
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input, ctx)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        # Append tool results to conversation
        messages.append({"role": "user", "content": tool_results})

        # Check if processing is complete
        if ctx.completed:
            break

    # Store conversation trace for audit
    store_conversation_trace(invoice.invoice_id, messages, db)

    # Build result from context
    return InvoiceProcessingResult(
        status=ctx.status,
        entries=ctx.journal_entries,
        approval=ctx.approval,
        flags=ctx.flags,
    )
```

### Tool Execution

```python
def execute_tool(tool_name: str, tool_input: dict, ctx: ProcessingContext) -> dict:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return handler(tool_input, ctx)
    except Exception as e:
        return {"error": str(e)}
```

Tool handlers return dicts (serialized to JSON for the agent). Errors are returned as structured error responses so the agent can reason about them and self-correct.

### User Message Format

```python
def format_invoice_for_agent(invoice: Invoice) -> str:
    """Format the full invoice as the user message for the agent."""
```

Template:

```
Process this invoice:

Invoice ID: {invoice_id}
Vendor: {vendor}
PO Number: {po_number or "NONE"}
Date: {date}
Department: {department}
Total: ${total}
Service Period: {service_period_start} to {service_period_end} (if stated, otherwise "Not specified")

Line Items:
  0: "{description}" — ${amount}, Quantity: {quantity}, Unit cost: ${unit_cost or "not specified"}
  1: "{description}" — ${amount}, Quantity: {quantity}, Unit cost: ${unit_cost or "not specified"}
  ...

Process this invoice according to the SOP. Work through each step, calling the appropriate tools.
```

// WHY: The entire invoice is in a single user message. The agent sees all line items at once and can reason about them collectively (e.g., "all line items are cloud/software, so the Engineering override might apply"). The agent processes the whole invoice in one conversation, calling `classify_line_item` for each line item as it works through them.

---

## Locked Decisions

**LOCKED DECISION — Transaction wrapper is unchanged:**

The outer `process_invoice` function uses the identical transaction pattern. `BEGIN` → agent loop → `COMMIT` or `ROLLBACK`. Dry-run rolls back. All other modes commit. All tool handlers write to the DB inside this transaction. One code path, one decision point at the boundary.

**LOCKED DECISION — Dry-run via transaction rollback:**

Unchanged. Dry-run executes the EXACT same code path — the agent runs the full loop, all tool handlers write to DB, all `store_*` and `set_*` calls fire. At the end, the transaction is rolled back. No mode-specific branching inside the agent loop or tool handlers (except for shadow's `posted=0` flag and the approval gate in `complete_processing`).

**LOCKED DECISION — Max iterations = 30:**

A typical invoice with 3 line items takes ~10 iterations (1 PO lookup + 3 classify + 3 treatment + 1 generate + 1 verify + 1 route + 1 complete). The cap at 30 handles invoices with up to ~8 line items with headroom. Hitting the cap flags the invoice as error. This is the circuit breaker.

---

## Conversation Trace Storage

One new table added to the DB schema (also add to `init-db` in `src/db.py`):

```sql
CREATE TABLE conversation_traces (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(invoice_id),
    messages TEXT NOT NULL,  -- Full JSON conversation (messages array)
    tool_calls_count INTEGER NOT NULL,
    iterations INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);
```

The full `messages` array is stored as JSON. This is the audit trail — every agent reasoning step, every tool call with parameters, every tool result.

// WHY: The conversation trace is richer than the original `reasoning` field on `ExtractedAttributes`: it shows not just what attributes the agent chose, but WHY (its reasoning text before each tool call), and how it handled the full workflow.

---

## InvoiceProcessingResult

Unchanged.

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

The agent loop makes one API call per iteration. Failure handling:

**Transient API errors (rate limit, timeout, 5xx):**
- Retry the `client.messages.create` call up to 3 times with exponential backoff (1s, 2s, 4s).
- Same messages, same tools. The conversation state is preserved.
- If all 3 retries fail, raise the exception. The outer transaction rolls back. Invoice is set to error.

**Malformed tool calls (agent provides invalid parameters):**
- The tool handler returns a structured error response: `{"error": "line_item_index 5 is out of range (invoice has 3 line items)"}`.
- The error is appended to the conversation as a tool result. The agent sees it and can self-correct.
- If the agent makes 3 consecutive errors on the same tool, the loop flags the invoice for review and stops.

// WHY: The agent can self-correct from structured error feedback. This is a genuine advantage of the agent pattern over the pipeline — the pipeline would crash; the agent can reason about the error and try again.

**No schema validation failure mode.** The Anthropic API enforces tool parameter schemas at the API level. If the agent's tool call doesn't conform to the schema, the API rejects it before it reaches the handler. There is no separate "schema validation" retry logic.

### Agent Stuck / Infinite Loop

If the agent reaches `max_iterations` (30) without calling `complete_processing` or `flag_for_review`, the loop terminates. The invoice is set to error with reason `"agent_exceeded_max_iterations"`.

### Rule Engine Failures

Unchanged. If `classify_line_item` returns `UNCLASSIFIED`, the tool handler flags the line item and returns the result to the agent. The agent should call `flag_for_review`. If it doesn't, the flag is still recorded in the DB from the tool handler.

### Database Failures

Unchanged. The entire `process_invoice` call is wrapped in a single transaction. No partial state: either all writes succeed, or none do. If processing fails mid-transaction, the transaction is rolled back. The invoice status is set to `"error"` in a separate transaction.
