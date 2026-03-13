# AP Agent — Spec Change: Pipeline → Tool-Use Agent

## Status: ACTIVE — Supersedes pipeline sections of original spec

> **Purpose of this document:** This spec change converts the AP Agent from a hardcoded pipeline with an LLM extraction step into a tool-use agent where the LLM orchestrates the full SOP workflow by calling deterministic tools. Every section below either replaces, modifies, or explicitly preserves a section of the original spec. If this document doesn't mention a section, that section is unchanged.

> **Reasoning convention:** Same as the original spec. `// WHY:` blocks explain intent. `LOCKED DECISION` markers are final.

---

## 1. What This Changes and Why

**The problem:** The original architecture is a deterministic pipeline with an LLM at one extraction point. The LLM fills a schema and never participates again. This is technically sound but architecturally not an agent — the LLM has no agency over the workflow. It doesn't reason about what to do, doesn't decide what step comes next, doesn't adapt when something is ambiguous. The assessment asks for "an AI agent that automates an Accounts Payable workflow." A pipeline with a smart parser doesn't demonstrate agent design.

**The change:** The LLM becomes the orchestrator. It receives an invoice, reasons through the SOP step by step, and calls deterministic tools to execute each step. The existing deterministic functions — PO matching, classification rules, treatment logic, approval routing, journal entry generation, balance verification — become tools the agent invokes. The LLM never bypasses the rules. It provides perception (attribute extraction via tool call parameters) and judgment (workflow reasoning), while the tools enforce every business rule deterministically.

**What is preserved:**
- The LLM still never picks a GL code. The classification tool applies the priority rule tree.
- The LLM still never decides approval routing. The approval tool applies the threshold logic.
- Every deterministic function is unchanged in its internal logic.
- The information boundary is preserved: the agent doesn't see the priority rule tree, GL code mappings, or approval thresholds. The tools encapsulate those.
- The eval system, feedback loop, and engineered weakness all still work — the feedback loop now targets the orchestrator's system prompt instead of a separate extraction prompt.
- The transaction/rollback pattern for dry-run is unchanged.
- All Pydantic models are unchanged.
- All data files are unchanged.
- The SQLite schema is unchanged (one table added for conversation traces).

**What is new:**
- The LLM drives the workflow via Anthropic tool use, making multiple tool calls per invoice.
- Attribute extraction is folded into the agent's tool calls — when the agent calls `classify_line_item`, its parameters ARE the extracted attributes. No separate extraction step.
- The agent's reasoning trace (the full conversation with tool calls and results) becomes the audit trail.
- One LLM conversation per invoice replaces N independent extraction calls.

// WHY: This change exists because the assessment is for an agentic AI company evaluating whether you can build agent systems. The original architecture demonstrates reliability engineering but not agent design. The refactored architecture demonstrates both: the agent pattern (LLM orchestrates via tools) AND the reliability pattern (tools enforce deterministic rules). The agent's judgment is visible in its reasoning trace; the rules' enforcement is visible in the tool results. Both are auditable.

---

## 2. What Does NOT Change

These sections of the original spec are **completely unchanged**. Do not modify the corresponding source files' internal logic.

| Original spec section | File | Status |
|----------------------|------|--------|
| §4 Data Models | `src/models.py` | **UNCHANGED** — all Pydantic schemas stay identical |
| §5 Storage | `src/db.py` | **UNCHANGED** — schema stays, one table added (see §7 below) |
| §6 PO Matching | `src/po_matching.py` | **UNCHANGED** — `match_po()` logic identical, now also callable as tool |
| §8 Classification | `src/classification.py` | **UNCHANGED** — `classify_line_item()` rule tree identical |
| §8.4 Amortization | `src/classification.py` | **UNCHANGED** |
| §9 Treatment | `src/treatment.py` | **UNCHANGED** — `determine_treatment()` identical |
| §10 Approval | `src/approval.py` | **UNCHANGED** — `route_approval()`, `approve()`, `reject()` identical |
| §11 Journal Entries | `src/journal.py` | **UNCHANGED** — `generate_journal_entries()`, `verify_balance()` identical |
| §13 Eval Labels | `eval/labels.py` | **UNCHANGED** |
| §16 Invoice Data | `data/*.json` | **UNCHANGED** |
| §18 INV-004 Worked Example | — | **UNCHANGED** — same expected behavior |
| §19 INV-002 Edge Case | — | **UNCHANGED** — same expected behavior, same engineered weakness |
| §20 Testing Strategy | `tests/test_rules.py` | **UNCHANGED** — unit tests for deterministic components stay identical |
| Appendix A Chart of Accounts | — | **UNCHANGED** |

// WHY: The deterministic functions are the safety layer. Their logic is correct and tested. The refactor changes how they're called (by an agent via tools instead of by a hardcoded pipeline), not what they do.

---

## 3. Updated System Overview

**Replaces:** §1 of the original spec.

The AP Agent is a Python CLI application that processes vendor invoices through an Accounts Payable workflow. An LLM agent receives each invoice, reasons through the SOP steps, and calls deterministic tools: PO matching → line-item classification → prepaid/accrual treatment → journal entry generation → balance verification → approval routing.

**Core architecture:** LLM as orchestrator and perception layer (workflow reasoning + attribute extraction via tool call parameters), deterministic tools as decision and action layer (GL classification + treatment + approval + journal entries).

```
┌──────────────────────────────────────────────┐
│              LLM Agent (Orchestrator)         │
│  • Receives invoice                          │
│  • Reasons through SOP steps                 │
│  • Analyzes line items → provides attributes │
│  • Calls tools in sequence                   │
│  • Handles edge cases with judgment          │
└──────────┬───────────────────────────────────┘
           │ tool calls (structured parameters)
           ▼
┌──────────────────────────────────────────────┐
│           Deterministic Tools                 │
│  • lookup_purchase_order()                   │
│  • classify_line_item()    ← rule tree       │
│  • apply_treatment()       ← date logic      │
│  • generate_entries()      ← entry rules     │
│  • verify_balance()        ← arithmetic      │
│  • route_approval()        ← threshold tree  │
│  • flag_for_review()                         │
│  • complete_processing()                     │
└──────────────────────────────────────────────┘
```

**The information boundary is preserved.** The agent's system prompt contains workflow instructions and attribute extraction guidance. It does NOT contain GL codes, the priority rule tree, approval thresholds, or journal entry structure. The tools encapsulate those rules. The agent provides perception (what is this line item?) via tool call parameters. The tools provide decisions (which GL code? what treatment?) via tool results.

// WHY: The LLM never picks a GL code. It provides structured attributes when calling `classify_line_item`, and the tool's internal rule tree returns the GL code. This is the same separation as before — perception in the LLM, decisions in the rules — but now the LLM also orchestrates the workflow. The agent pattern adds orchestration without sacrificing determinism.

---

## 4. Updated Project Structure

**Replaces:** §2 of the original spec.

```
ap-agent/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── docs/                      # Spec docs (updated per this change)
├── src/
│   ├── __init__.py
│   ├── models.py              # UNCHANGED — all Pydantic schemas
│   ├── db.py                  # MINOR CHANGE — one table added for traces
│   ├── po_matching.py         # UNCHANGED — match_po() logic
│   ├── classification.py      # UNCHANGED — classify_line_item() rule tree
│   ├── treatment.py           # UNCHANGED — determine_treatment() logic
│   ├── approval.py            # UNCHANGED — route_approval(), approve(), reject()
│   ├── journal.py             # UNCHANGED — generate_journal_entries(), verify_balance()
│   ├── tools.py               # NEW — tool definitions (schemas + handlers)
│   ├── agent.py               # NEW — agent loop (replaces pipeline.py)
│   └── prompts.py             # REWRITTEN — orchestrator system prompt
├── eval/
│   ├── __init__.py
│   ├── runner.py              # MODIFIED — extracts attributes from tool call logs
│   ├── labels.py              # UNCHANGED
│   └── feedback.py            # MINOR CHANGE — targets orchestrator prompt
├── data/                      # UNCHANGED
├── cli.py                     # MINOR CHANGE — calls agent instead of pipeline
└── tests/
    └── test_rules.py          # UNCHANGED
```

**Removed:** `src/attribute_extraction.py`, `src/pipeline.py`

// WHY: `attribute_extraction.py` is eliminated because the agent provides attributes directly as `classify_line_item` tool call parameters. There is no separate extraction step — the agent's reasoning IS the extraction. `pipeline.py` is replaced by `agent.py` which implements the tool-use agent loop. The tool definitions in `tools.py` wrap the existing deterministic functions with Anthropic tool-use schemas.

---

## 5. Tool Definitions (`src/tools.py`)

**New file.** Defines 8 tools the agent can call. Each tool has a JSON schema (for the Anthropic API) and a handler function that wraps an existing deterministic function.

### 5.1 Processing Context

All tools operate on a shared `ProcessingContext` created at the start of each invoice. Tools read from and write to this context. The agent does not manage state — it triggers tools, reads their results, and reasons about what to do next.

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

// WHY: Shared context eliminates the need to pass full state through every tool call. The agent calls `classify_line_item` with attributes, the tool stores the result in context. The agent calls `apply_treatment` for that line item, the tool reads the classification from context and applies the override. This mirrors how the original pipeline accumulated state across function calls — the mechanism changes (shared context vs. local variables), the data flow doesn't.

### 5.2 Tool Schemas

Each tool is defined as an Anthropic tool-use schema. The handler function receives the agent's parameters plus the processing context.

**Tool 1: `lookup_purchase_order`**

```python
TOOL_SCHEMA = {
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

Handler: calls `match_po()` from `po_matching.py`. Stores result in `ctx.po_result`. Returns `POMatchResult` as JSON.

**Tool 2: `classify_line_item`**

This is the critical tool. The agent provides extracted attributes as parameters. The tool applies the priority rule tree and returns the classification. The agent's parameters ARE the attribute extraction — folding what was previously a separate LLM call into the agent's tool invocation.

```python
TOOL_SCHEMA = {
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
        "required": ["line_item_index", "is_physical_goods", "is_branded_merch", "is_equipment",
                      "is_software", "is_cloud_hosting", "service_type", "is_marketing",
                      "category_hint", "billing_frequency", "service_period_start",
                      "service_period_end", "unit_cost", "confidence", "reasoning"]
    }
}
```

Handler logic:
1. Construct `ExtractedAttributes` from the agent's parameters.
2. Run invariant checks (same as original §7.4): force `is_physical_goods=True` if `is_branded_merch` or `is_equipment` is true.
3. Resolve unit cost with the existing precedence chain: `line_item.unit_cost` → `amount/quantity` → agent-provided `unit_cost` → `amount`. The agent's `unit_cost` parameter replaces `unit_cost_extracted` — it's the LLM's parsing, which the deterministic data overrides when available.
4. Call `classify_line_item(attrs, resolved_unit_cost)` from `classification.py`.
5. Store attributes and classification in `ctx.line_results[line_item_index]`.
6. Store in DB: `store_attributes()`, `store_classification()`.
7. If `confidence < 0.7`, add flag `low_confidence_line:{line_item_index}`.
8. If `gl_code == "UNCLASSIFIED"`, add flag.
9. Return `ClassificationResult` as JSON to the agent.

// WHY: The tool's parameters are the `ExtractedAttributes` schema flattened into individual fields. This makes every attribute a named, typed, required parameter that the agent must explicitly fill. The agent can't skip attributes or provide partial data — the schema enforces completeness. The tool handler constructs the `ExtractedAttributes` object internally, so all downstream code (classification, treatment, eval) receives the exact same typed object as before. The only change is who fills it: the orchestrator agent instead of a separate extraction call.

**LOCKED DECISION — Unit cost resolution inside the tool handler, not the agent:**

The agent provides `unit_cost` as its best parsing of per-unit cost. The tool handler applies the deterministic precedence chain: structured invoice data (`line_item.unit_cost`, `amount/quantity`) overrides the agent's value. This preserves the original spec's §7.4 rule: deterministic data takes precedence over LLM extraction. The agent's `unit_cost` parameter maps to what was previously `unit_cost_extracted` — a fallback, not authoritative.

**Tool 3: `apply_treatment`**

```python
TOOL_SCHEMA = {
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

Handler: reads the classification and attributes from `ctx.line_results[line_item_index]`. Calls `determine_treatment(attrs, classification, ctx.invoice)` from `treatment.py`. Updates the classification in context and DB. Returns the updated `ClassificationResult` (with treatment override if applicable).

// WHY: The treatment tool takes only `line_item_index` because all the data it needs (attributes, classification, invoice dates) is already in the processing context from the classify step. The agent doesn't re-specify service periods — the tool reads them from the stored attributes. This prevents the agent from providing inconsistent dates between the classify and treatment calls.

**Tool 4: `generate_journal_entries`**

```python
TOOL_SCHEMA = {
    "name": "generate_journal_entries",
    "description": "Generate journal entries for all classified line items. Call this once after all line items have been classified and treatment-checked.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

Handler: iterates over all `ctx.line_results`, calls `generate_journal_entries()` from `journal.py` for each. Aggregates into `ctx.journal_entries`. Returns a summary: number of entries generated, total amount, breakdown by type (immediate, scheduled, pending_payment).

**Tool 5: `verify_balance`**

```python
TOOL_SCHEMA = {
    "name": "verify_balance",
    "description": "Verify that the generated journal entries balance against the invoice total. Call this after generate_journal_entries.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

Handler: calls `verify_balance(ctx.invoice, ctx.journal_entries)` from `journal.py`. Returns pass/fail with the computed vs. expected totals.

**Tool 6: `route_approval`**

```python
TOOL_SCHEMA = {
    "name": "route_approval",
    "description": "Determine the approval routing for this invoice based on the total amount, department, and line item classifications. Call this after balance verification passes.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

Handler: calls `route_approval(ctx.invoice, list(ctx.line_results.values()))` from `approval.py`. Stores in `ctx.approval`. Returns `ApprovalRecord` as JSON.

**Tool 7: `flag_for_review`**

```python
TOOL_SCHEMA = {
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

Handler: sets `ctx.status = "flagged_for_review"`, adds flag, sets `ctx.completed = True`. Returns confirmation.

**Tool 8: `complete_processing`**

```python
TOOL_SCHEMA = {
    "name": "complete_processing",
    "description": "Signal that you have finished processing this invoice. Call this as your final action after all line items are classified, entries generated, balance verified, and approval routed.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

Handler: performs the approval gate logic (same as original §12.2 step 6). If `mode == "auto"` or `auto_approve`, calls `approve()`. Stores entries in DB. Sets final status. Sets `ctx.completed = True`. Returns the final status.

// WHY: `complete_processing` is the terminal tool. Without it, the agent loop would need to detect completion from message content, which is brittle. A dedicated tool makes the termination condition deterministic: `ctx.completed == True` ends the loop.

### 5.3 Tool Registration

All 8 tools are registered in a `TOOLS` list and a `TOOL_HANDLERS` dict:

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

## 6. Agent Loop (`src/agent.py`)

**New file. Replaces:** `src/pipeline.py` (§12 of original spec).

### 6.1 Main Function

```python
def process_invoice(
    invoice: Invoice,
    db: sqlite3.Connection,
    client: anthropic.Anthropic,
    mode: str = "normal"  # "normal", "dry_run", "shadow", "auto"
) -> InvoiceProcessingResult
```

Signature is identical to the original. All callers (CLI, eval, shadow) are unaffected.

### 6.2 Agent Loop

```python
def process_invoice(invoice, db, client, mode):
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
        set_invoice_status(invoice.invoice_id, "error", db)
        raise


def _run_agent(invoice, db, client, mode):
    ctx = ProcessingContext(invoice=invoice, db=db, mode=mode)

    # Build the user message with full invoice details
    user_message = format_invoice_for_agent(invoice)

    # Initialize conversation
    messages = [{"role": "user", "content": user_message}]

    # Agent loop — iterate until completion or max iterations
    max_iterations = 30  # Safety cap. Typical invoice: 5–15 iterations.
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

**LOCKED DECISION — Transaction wrapper is unchanged:**

The outer `process_invoice` function uses the identical transaction pattern from the original spec (§12.2). `BEGIN` → agent loop → `COMMIT` or `ROLLBACK`. Dry-run rolls back. All other modes commit. The agent loop runs inside the transaction. All tool handlers write to the DB inside this transaction. One code path, one decision point at the boundary.

**LOCKED DECISION — Max iterations = 30:**

A typical invoice with 3 line items takes ~10 iterations (1 PO lookup + 3 classify + 3 treatment + 1 generate + 1 verify + 1 route + 1 complete). The cap at 30 handles invoices with up to ~8 line items with headroom. If the agent hits 30 iterations without completing, the invoice is flagged as error. This is the circuit breaker.

// WHY: The agent loop replaces a hardcoded for-loop with an LLM-driven conversation. The LLM decides what tool to call next based on the invoice and the results of previous tools. The max iteration cap prevents runaway loops (the agent getting confused and calling tools indefinitely). The conversation trace is the audit trail — every reasoning step and tool call is captured.

### 6.3 Tool Execution

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

Tool handlers return dicts (serialized to JSON for the agent). Errors are returned as structured error responses so the agent can reason about them and potentially retry or flag for review.

### 6.4 User Message Format

```python
def format_invoice_for_agent(invoice: Invoice) -> str:
    """Format the full invoice as the user message for the agent."""
```

The user message contains the complete invoice:

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

// WHY: The entire invoice is in a single user message. The agent sees all line items at once and can reason about them collectively (e.g., "all line items are cloud/software, so the Engineering override might apply"). This is different from the original spec's one-line-item-per-call approach — the agent processes the whole invoice in one conversation, calling `classify_line_item` for each line item as it works through them.

---

## 7. Orchestrator System Prompt (`src/prompts.py`)

**Replaces:** §7.3 of original spec (extraction-only prompt → orchestrator prompt).

### 7.1 Prompt Structure

The system prompt has four sections:

**Section A — Role and workflow:**
```
You are an Accounts Payable agent. Your job is to process vendor invoices by following the Standard Operating Procedure (SOP) step by step.

For each invoice, follow these steps in order:

Step 1 — PO Matching:
Check the purchase order. If the invoice has no PO number, flag it for review using the flag_for_review tool and stop. If it has a PO, call lookup_purchase_order to validate. If the PO check fails (not found or tolerance exceeded), flag for review and stop.

Step 2 — Line-Item Classification:
For EACH line item in the invoice, analyze it and call classify_line_item with your assessment. You must determine what the item is — physical goods, equipment, software, professional services, marketing activity, etc. — and provide these attributes to the classification tool. The tool will determine the correct GL account.

Step 3 — Treatment Check:
After classifying each line item, call apply_treatment to check for prepaid or accrual recognition.

Step 4 — Journal Entries & Verification:
After ALL line items are classified and treatment-checked, call generate_journal_entries, then verify_balance.

Step 5 — Approval Routing:
Call route_approval to determine the approval path.

Step 6 — Complete:
Call complete_processing to finalize.

Work through these steps methodically. Explain your reasoning before each tool call.
```

**Section B — Attribute extraction guidance:**

Same content as the original spec's §7.3 Section B. This tells the agent how to analyze line items and determine attributes. Includes:

- One-line definition for each attribute.
- The service period extraction rules (same as original: only extract when stated, never fabricate, expand named months/quarters).
- The `is_marketing` instruction (same as original: assess the line item, not the department).

**This section is the target of the feedback loop.** The initial version is intentionally slightly naive in the same two ways as the original:
1. No few-shot example for branded merch override.
2. No explicit guidance on regulatory advisory work.

**Section C — Few-shot examples:**

Same format as the original spec's §7.3 Section C, but now showing the full tool call pattern:

```
Example — analyzing a line item:
Line item: "Annual Platform License (Jan–Dec 2026)" from Cloudware Solutions, Engineering dept
This is a software license billed annually with a service period of Jan to Dec 2026.
→ classify_line_item(is_software=true, billing_frequency="annual", service_period_start="2026-01-01", service_period_end="2026-12-31", ...)

Example — analyzing a line item:
Line item: "Patent filing & prosecution" from Morrison & Burke LLP, Legal dept
This is direct legal work — patent filing is a legal action.
→ classify_line_item(service_type="legal", billing_frequency="one_time", service_period_start=null, service_period_end=null, ...)
```

Start with 2–3 examples. The feedback loop adds more.

**Section D — Constraints:**
```
CRITICAL RULES:
- You NEVER determine the GL account code. The classify_line_item tool does that.
- You NEVER skip the treatment check. Call apply_treatment for every classified line item.
- You NEVER guess dates. If no service period is stated, pass null.
- You ALWAYS call complete_processing as your final action.
- You process ALL line items before generating journal entries.
```

### 7.2 Information Boundary (Updated)

| Agent sees | Agent does NOT see |
|------------|-------------------|
| SOP workflow steps (Step 1–6) | GL codes or account names |
| Attribute definitions and guidance | Priority rule tree logic |
| Few-shot examples (attributes + tool calls) | Approval threshold amounts |
| Full invoice (all line items at once) | Journal entry structure |
| Tool results (including GL codes in results) | How tools make their decisions |

The agent sees GL codes in tool RESULTS (e.g., "classified as 1310, Prepaid Software"). This is fine — it's output, not input. The agent doesn't need this information to make subsequent decisions. If the classification tool returns "UNCLASSIFIED," the agent should call `flag_for_review`.

// WHY: The original information boundary is preserved in intent but adapted for the agent pattern. The agent still doesn't know the rule tree or approval thresholds. It learns the GL code for a line item ONLY after calling the tool — it can't pre-determine or override it. The tool results give the agent enough context to reason about next steps (e.g., "this was classified as a Fixed Asset, which may affect approval routing") without giving it the decision-making authority.

### 7.3 Prompt Versioning for Feedback Loop

```python
def get_system_prompt(refinements: list[str] | None = None) -> str:
    """Build the system prompt. If refinements are provided, append them to Section B."""
    prompt = SECTION_A + SECTION_B
    if refinements:
        prompt += "\n\nAdditional guidance from corrections:\n" + "\n".join(refinements)
    prompt += SECTION_C + SECTION_D
    return prompt
```

The `refinements` parameter is how the feedback loop injects improvements. The base prompt stays the same; corrections add explicit guidance and few-shot examples to Section B.

---

## 8. Conversation Trace Storage

**Adds to:** §5 of original spec (one new table).

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

// WHY: The conversation trace replaces the original spec's `reasoning` field on `ExtractedAttributes` as the primary audit artifact. The trace is richer: it shows not just what attributes the agent chose, but WHY (its reasoning text before each tool call), and how it handled the full workflow. Assessors can read the trace to verify the agent followed the SOP.

---

## 9. Eval System Changes

**Modifies:** §13 of original spec. Labels and accuracy computation are unchanged. Execution flow changes slightly.

### 9.1 Attribute Extraction from Tool Calls

Previously, attributes were extracted from the `ExtractedAttributes` stored by the extraction step. Now, attributes are extracted from the `classify_line_item` tool call parameters in the conversation trace.

```python
def extract_attributes_from_trace(messages: list[dict], line_item_index: int) -> ExtractedAttributes:
    """Find the classify_line_item tool call for this line item in the conversation trace
    and reconstruct ExtractedAttributes from its parameters."""
```

Alternatively (and simpler): the `classify_line_item` tool handler already stores the `ExtractedAttributes` object in the DB via `store_attributes()`. The eval can read from the DB exactly as before. **Use the DB path — it's simpler and already works.**

// WHY: The tool handler stores attributes in the same DB columns with the same format as the original extraction step. Eval code that reads `line_items.extracted_attributes` gets the same JSON blob regardless of whether it was produced by a standalone extraction call or by the `classify_line_item` tool handler. This means the eval runner's comparison logic is unchanged.

### 9.2 Eval Execution (Unchanged)

```python
run_eval(invoices, labels, db, client)  # Same signature
```

Still processes each invoice with `mode="auto"`. Still compares `gl_code`, `treatment`, `approval`, and `key_attributes` against labels. The only change is that `process_invoice` now runs an agent loop instead of a pipeline — but the eval doesn't know or care about that distinction. It reads results from the DB.

---

## 10. Feedback Loop Changes

**Modifies:** §14 of original spec. The engineered weakness, correction flow, and before/after report are unchanged in structure. The target changes from an extraction prompt to the orchestrator's system prompt.

### 10.1 Engineered Weakness (Updated Mechanism, Same Outcome)

**Weakness #1 — Branded merch:** The initial orchestrator system prompt (Section B) does not include a few-shot example showing that branded t-shirts from a marketing vendor should have `is_physical_goods=True, is_branded_merch=True, is_marketing=False`. Expected failure: the agent calls `classify_line_item` with `is_marketing=True` for INV-005 lines 2 and 4 → tool returns 5050 instead of 5000.

**Weakness #2 — Regulatory advisory:** The initial prompt does not explicitly distinguish advisory work about regulatory topics from direct legal actions. Expected failure: the agent calls `classify_line_item` with `service_type="legal"` for INV-002 line 2 → tool returns 5030 instead of 5040.

### 10.2 Correction and Refinement Flow

Identical to original §14 phases A–F. The corrections target the same attributes. The error analysis groups by the same patterns. The prompt refinement adds the same few-shot examples and explicit instructions — but they're injected into the orchestrator's Section B via the `refinements` parameter on `get_system_prompt()`.

### 10.3 Expected Before/After (Unchanged)

The before/after accuracy numbers are the same targets as the original spec:
- Baseline: ~77% GL accuracy (3 misclassified lines from weaknesses #1 and #2)
- After: 100% GL accuracy
- Same delta, same report format, same story

---

## 11. Error Handling (Updated)

**Replaces:** §17 of original spec.

### 11.1 LLM Call Failures

The agent loop makes one API call per iteration (not one per line item). Failure handling:

**Transient API errors (rate limit, timeout, 5xx):**
- Retry the `client.messages.create` call up to 3 times with exponential backoff (1s, 2s, 4s).
- Same messages, same tools. The conversation state is preserved.
- If all 3 retries fail, raise the exception. The outer transaction rolls back. Invoice is set to error.

**Malformed tool calls (agent provides invalid parameters):**
- The tool handler returns a structured error response: `{"error": "line_item_index 5 is out of range (invoice has 3 line items)"}`.
- The error is appended to the conversation as a tool result. The agent sees it and can self-correct.
- If the agent makes 3 consecutive errors on the same tool, the loop should flag the invoice for review and stop.

// WHY: The agent can self-correct from structured error feedback. If it calls `classify_line_item` with `line_item_index=5` on a 3-line invoice, the error tells it exactly what's wrong. This is a genuine advantage of the agent pattern over the pipeline — the pipeline would crash; the agent can reason about the error and try again.

### 11.2 Agent Stuck / Infinite Loop

If the agent reaches `max_iterations` (30) without calling `complete_processing` or `flag_for_review`, the loop terminates. The invoice is set to error with reason `"agent_exceeded_max_iterations"`.

### 11.3 Rule Engine and DB Failures

Unchanged from original spec §17.2 and §17.3. The transaction wrapper handles all DB failures. `UNCLASSIFIED` returns propagate as tool results to the agent, which should call `flag_for_review`.

---

## 12. CLI Changes

**Modifies:** §15 of original spec. Commands and output format are unchanged. Internal wiring changes.

The only change: `process`, `process-all`, `eval`, `shadow`, and `demo` call `agent.process_invoice()` instead of `pipeline.process_invoice()`. The function signature is identical. All output formatting code reads from the same DB tables.

**New command (optional):**

```
python cli.py trace <invoice_id>
```

Prints the full conversation trace for a processed invoice — the agent's reasoning and tool calls. Useful for debugging and for assessors to see the agent's thought process.

---

## 13. Updated LLM Call Configuration

**Replaces:** §7.2 of original spec.

- **Model:** `claude-sonnet-4-20250514` (default, configurable via `AP_AGENT_MODEL`)
- **Temperature:** 0.0
- **Max tokens:** 4096 (increased from 1024 — the agent produces reasoning text + tool calls, needs more room)
- **Tool use:** Anthropic API `tools` parameter with the 8 tool schemas from §5.2

// WHY: Sonnet is still the right model. The agent task is more complex than extraction alone — it's reasoning through a multi-step workflow and making tool calls — but Sonnet handles tool use well. The increased max_tokens accommodates the agent's reasoning text between tool calls. Temperature 0.0 for consistency. Cost: previously ~16 extraction calls × ~500 tokens each ≈ 8K tokens per eval run. Now: ~16 invoices × ~10 iterations × ~300 tokens per turn ≈ 48K tokens per eval run. Still trivial.

---

## 14. What to Update in `/docs` Split Files

For the implementor working from the split docs, here is which files need changes:

| Doc file | Change |
|----------|--------|
| `docs/data-models.md` | **UNCHANGED** |
| `docs/storage.md` | **ADD** `conversation_traces` table |
| `docs/po-matching.md` | **UNCHANGED** (internal logic). Add note that it's now called as a tool. |
| `docs/llm-extraction.md` | **REPLACE** with this spec change's §6 and §7. The standalone extraction step no longer exists. |
| `docs/classification.md` | **UNCHANGED** (internal logic). Add note that it's now called as a tool via `classify_line_item` handler. |
| `docs/treatment.md` | **UNCHANGED** (internal logic). Add note that it's now called as a tool via `apply_treatment` handler. |
| `docs/approval.md` | **UNCHANGED** |
| `docs/journal-entries.md` | **UNCHANGED** |
| `docs/pipeline.md` | **REPLACE** with this spec change's §6 (agent loop) and §11 (error handling). |
| `docs/eval-and-feedback.md` | **MINOR UPDATE** per this spec change's §9 and §10. |
| `docs/cli.md` | **MINOR UPDATE** — add `trace` command, note internal wiring change. |

---

## 15. Implementation Order

For the implementor refactoring an existing codebase:

1. **Create `src/tools.py`** — Define all 8 tool schemas and handlers. Each handler wraps existing functions. Test each handler in isolation by calling it directly with a mock context.

2. **Rewrite `src/prompts.py`** — New orchestrator system prompt with Sections A–D. Keep `get_system_prompt(refinements)` interface for the feedback loop.

3. **Create `src/agent.py`** — Agent loop. Wire up `process_invoice()` with the same signature as the original `pipeline.py`.

4. **Delete `src/attribute_extraction.py`** and `src/pipeline.py`.

5. **Add `conversation_traces` table** to `src/db.py` schema creation.

6. **Update `cli.py`** — Change imports from `pipeline` to `agent`. Add optional `trace` command.

7. **Update `eval/runner.py`** — Verify it still reads attributes from DB (it should, since tool handlers store them identically).

8. **Update `eval/feedback.py`** — Verify `get_system_prompt(refinements)` interface works.

9. **Run eval** — Verify same accuracy results as before.

10. **Run demo** — Verify full end-to-end.

---

## 16. What the Agent Conversation Looks Like

A concrete example for INV-004 (the accrual invoice) to eliminate ambiguity:

```
USER:
Process this invoice:
Invoice ID: INV-004
Vendor: Apex Strategy Group
PO Number: PO-2025-189
Date: 2026-01-15
Department: Operations
Total: $8,700
Service Period: 2025-12-01 to 2025-12-31

Line Items:
  0: "Operational efficiency assessment – Dec 2025" — $7,500, Quantity: 1
  1: "Travel expenses – Dec 2025 on-site visits" — $1,200, Quantity: 1

ASSISTANT:
I'll process this invoice step by step.

Step 1 — PO Matching: The invoice references PO-2025-189. Let me verify it.
[calls lookup_purchase_order(po_number="PO-2025-189")]

TOOL RESULT: {"matched": true, "po_amount": 8700, "tolerance_pct": 0.0}

ASSISTANT:
PO matched with 0% variance. Proceeding to line-item classification.

Step 2 — Line Item 0: "Operational efficiency assessment – Dec 2025" — $7,500
This is an operational assessment — advisory/consulting work. The description says "Dec 2025" which is the service period. The vendor is a strategy consulting firm.
[calls classify_line_item(line_item_index=0, is_physical_goods=false, is_branded_merch=false,
  is_equipment=false, is_software=false, is_cloud_hosting=false, service_type="consulting",
  is_marketing=false, category_hint=null, billing_frequency="one_time",
  service_period_start="2025-12-01", service_period_end="2025-12-31",
  unit_cost=7500, confidence=0.95, reasoning="Advisory assessment work, consulting service type")]

TOOL RESULT: {"gl_code": "5040", "treatment": "expense", "rule_triggered": "Priority 5: consulting → 5040"}

ASSISTANT:
Classified as 5040 (expense). Now checking treatment — the service period is Dec 2025 but the invoice date is Jan 15, 2026, so the service was delivered before the invoice arrived. This may need accrual treatment.
[calls apply_treatment(line_item_index=0)]

TOOL RESULT: {"gl_code": "5040", "treatment": "accrual", "accrual_type": "professional_services"}

ASSISTANT:
Treatment overridden to accrual — will book to 2110 (accrued professional services).

Step 2 — Line Item 1: "Travel expenses – Dec 2025 on-site visits" — $1,200
Travel expenses from Dec 2025 on-site visits.
[calls classify_line_item(line_item_index=1, ..., category_hint="travel",
  service_period_start="2025-12-01", service_period_end="2025-12-31", ...)]

TOOL RESULT: {"gl_code": "5060", "treatment": "expense", "rule_triggered": "Priority 7: travel → 5060"}

[calls apply_treatment(line_item_index=1)]

TOOL RESULT: {"gl_code": "5060", "treatment": "accrual", "accrual_type": "other"}

ASSISTANT:
Both line items are accruals — service delivered in Dec 2025, invoice received Jan 2026. Generating journal entries.
[calls generate_journal_entries()]

TOOL RESULT: {"entries_count": 4, "total_immediate": 8700, ...}

[calls verify_balance()]

TOOL RESULT: {"balanced": true, "expected": 8700, "actual": 8700}

[calls route_approval()]

TOOL RESULT: {"required_level": "dept_manager", "reason": "Invoice total $8,700, between $1K–$10K"}

[calls complete_processing()]

TOOL RESULT: {"status": "pending_approval"}
```

This conversation is stored in `conversation_traces` and is viewable via `python cli.py trace INV-004`.

---

*End of spec change. All decisions are locked. Implement exactly as written. The deterministic tools are the safety layer. The agent is the orchestration layer. Both are auditable.*
