# Spec Change: Pipeline → Tool-Use Agent

**Status:** Implemented. Supersedes pipeline sections of the original spec.

---

## What Changed and Why

The original architecture was a deterministic pipeline with an LLM at one extraction point. The LLM filled a schema and never participated again. Technically sound, but not an agent — the LLM had no agency over the workflow. The assessment asks for an AI agent, not a pipeline with a smart parser.

**The change:** The LLM becomes the orchestrator. It receives an invoice, reasons through the SOP step by step, and calls deterministic tools to execute each step. Attribute extraction is folded into the tool calls — when the agent calls `classify_line_item`, its parameters ARE the extracted attributes. No separate extraction step.

**What this adds:**
- LLM drives the workflow via Anthropic tool use, making multiple tool calls per invoice
- One conversation per invoice replaces N independent extraction calls
- The conversation trace (reasoning + tool calls + results) becomes the audit trail
- The agent can self-correct from structured error feedback

---

## What Did NOT Change

Every deterministic function is unchanged in internal logic: `classify_line_item()`, `determine_treatment()`, `route_approval()`, `generate_journal_entries()`, `verify_balance()`, `match_po()`. All Pydantic models, data files, eval labels, and unit tests are unchanged. The transaction/rollback pattern for dry-run is unchanged.

The information boundary is preserved: the agent doesn't see the priority rule tree, GL code mappings, or approval thresholds. The tools encapsulate those rules.

---

## Files Added, Removed, Modified

| File | Status |
|------|--------|
| `src/tools.py` | **NEW** — 8 tool schemas + handlers wrapping existing functions |
| `src/agent.py` | **NEW** — Agent loop, replaces `pipeline.py` |
| `src/prompts.py` | **REWRITTEN** — Orchestrator system prompt (Sections A–D) |
| `src/db.py` | **MINOR** — Added `conversation_traces` table |
| `cli.py` | **MINOR** — Calls `agent.process_invoice()`, added `trace` command |
| `eval/runner.py` | **MINOR** — Reads attributes from DB (tool handlers store them identically) |
| `src/pipeline.py` | **DELETED** |
| `src/attribute_extraction.py` | **DELETED** |

---

## Tool Design

8 tools, each wrapping an existing deterministic function:

| Tool | Purpose | Key design point |
|------|---------|-----------------|
| `lookup_purchase_order` | PO validation | Overrides agent-provided PO with invoice's actual PO number |
| `classify_line_item` | Attribute extraction + GL classification | Agent provides 14 attributes as parameters; tool applies rule tree |
| `apply_treatment` | Prepaid/accrual override | Takes only `line_item_index`; reads all data from context |
| `generate_journal_entries` | Entry creation for all lines | No parameters; operates on accumulated context |
| `verify_balance` | Balance check | No parameters; compares immediate entries vs invoice total |
| `route_approval` | Approval routing | No parameters; reads classifications from context |
| `flag_for_review` | Human review escalation | Sets `completed=True`, stops processing |
| `complete_processing` | Terminal gate | Validates all steps done, applies approval gate, stores entries |

**Critical design decision — `classify_line_item`:** The agent's parameters ARE the `ExtractedAttributes` schema flattened into individual fields. Every attribute is named, typed, and required. The tool handler constructs the `ExtractedAttributes` object internally, so all downstream code receives the same typed object as before. Unit cost resolution uses the same deterministic precedence chain: structured invoice data overrides the agent's value.

**Shared `ProcessingContext`:** All tools operate on a mutable context created per invoice. Tools read from and write to this context. The agent doesn't manage state — it triggers tools, reads results, and reasons about what to do next. This prevents the agent from providing inconsistent data between calls (e.g., different service dates in classify vs treatment).

---

## Agent Loop

Same `process_invoice()` signature as the original pipeline. All callers are unaffected.

- Wraps in `BEGIN`/`COMMIT` (or `ROLLBACK` for dry-run) — same transaction pattern
- Iterates: API call → process tool calls → feed results back → repeat
- Terminates on `ctx.completed == True`, `stop_reason == "end_turn"`, or max iterations (30)
- Stores full conversation trace for audit

**Error handling:**
- Transient API errors: 3 retries with exponential backoff
- Malformed tool calls: structured error returned to agent, agent can self-correct
- 3 consecutive errors on the same tool: auto-flag for review
- Max iterations hit: invoice set to error

---

## System Prompt Structure

Four sections, same `get_system_prompt(refinements)` interface for the feedback loop:

- **Section A** — Role and 6-step workflow (PO → classify → treat → entries → approve → complete)
- **Section B** — Attribute extraction guidance. Target of the feedback loop. Initial version is intentionally naive in two ways: no branded merch example, no regulatory advisory distinction
- **Section C** — Few-shot examples showing attribute reasoning → tool call pattern
- **Section D** — Hard constraints (never pick GL codes, never skip treatment, never fabricate dates)

---

## Feedback Loop — Same Story, Different Target

The engineered weaknesses, corrections, error analysis, and before/after report are structurally identical. The only change: prompt refinements are injected into the orchestrator's Section B instead of a standalone extraction prompt. Same two weaknesses, same corrections, same expected accuracy improvement (~77% → 100%).

---

## Conversation Trace

New `conversation_traces` table stores the full messages array per invoice. Viewable via `python cli.py trace <invoice_id>`. This replaces the original `reasoning` field as the primary audit artifact — it shows not just what attributes the agent chose, but the reasoning behind each decision and the full workflow progression.
