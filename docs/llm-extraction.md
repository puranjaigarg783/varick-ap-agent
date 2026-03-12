# LLM Agent — System Prompt, Extraction Guidance & Configuration

> **Implements:** `src/prompts.py`
> **Spec origin:** Sections 7, 13 of `spec-change-agent-refactor.md`
> **For agent loop, tool schemas, and error handling:** see `docs/pipeline.md`

---

## What Changed

The LLM is no longer a single-purpose attribute extractor called once per line item. It is now the **orchestrator** of the entire invoice workflow. It receives a full invoice, reasons through the SOP step by step, and calls deterministic tools to execute each step. Attribute extraction is folded into the agent's `classify_line_item` tool call — the parameters it passes ARE the extracted attributes. There is no separate extraction step or file.

**Removed:** `src/attribute_extraction.py`. The `extract_attributes()` function no longer exists. The agent's reasoning IS the extraction.

**This doc covers** the system prompt that drives the agent's behavior, the information boundary between the agent and its tools, the LLM configuration, and the few-shot examples and extraction guidance that the feedback loop targets.

---

## LLM Call Configuration

- **Model:** `claude-sonnet-4-20250514` (default, configurable via `AP_AGENT_MODEL` environment variable)
- **Temperature:** 0.0 (deterministic — we want consistency, not creativity)
- **Max tokens:** 4096 (the agent produces reasoning text + tool calls and needs room)
- **Tool use:** Anthropic API `tools` parameter with the 8 tool schemas defined in `docs/pipeline.md`

// WHY: Sonnet is still the right model. The agent task is more complex than extraction alone — it's reasoning through a multi-step workflow and making tool calls — but Sonnet handles tool use well. Temperature 0.0 for consistency. Cost: ~16 invoices × ~10 iterations × ~300 tokens per turn ≈ 48K tokens per eval run. Still trivial. The model string is configurable via environment variable so the assessor can swap models without code changes.

---

## Information Boundary

**LOCKED DECISION — Information boundary preserved, scope expanded:**

The agent sees workflow instructions and attribute extraction guidance. It does NOT see the priority rule tree, GL code mappings, approval thresholds, or journal entry structure. The tools encapsulate those rules. The agent sees GL codes only in tool results (output, not input) — it can't pre-determine or override them.

| Agent sees | Agent does NOT see |
|------------|-------------------|
| SOP workflow steps (Step 1–6) | GL codes or account names |
| Attribute definitions and guidance | Priority rule tree logic |
| Few-shot examples (attributes + tool calls) | Approval threshold amounts |
| Full invoice (all line items at once) | Journal entry structure |
| Tool results (including GL codes in results) | How tools make their decisions |

// WHY: The agent sees GL codes in tool RESULTS (e.g., "classified as 1310, Prepaid Software"). This is fine — it's output, not input. The agent doesn't need this information to make subsequent decisions. If the classification tool returns "UNCLASSIFIED," the agent should call `flag_for_review`.

---

## System Prompt Design (`src/prompts.py`)

The system prompt has four sections.

### Section A — Role and Workflow

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

### Section B — Attribute Extraction Guidance

Each attribute gets a one-line definition and a brief "when to set true" guide. **This section is the target of the feedback loop.** The initial version is intentionally slightly naive (see `docs/eval-and-feedback.md`).

Must include this explicit instruction for service periods:
```
For service_period_start and service_period_end:
- Only extract when the line item text or invoice context contains a specific date range, named month, or named quarter.
- Never infer or fabricate dates. If no period is stated or implied, return null for both.
- Extract single-month periods when stated: "Mar 2026" → 2026-03-01 / 2026-03-31.
- Expand named quarters: "Q1 2026" → 2026-01-01 / 2026-03-31.
- If only a year range is given: "Jan–Dec 2026" → 2026-01-01 / 2026-12-31.
- If no dates at all: null / null. Do not guess.
```

Must include this explicit instruction for is_marketing:
```
For is_marketing:
- Assess the LINE ITEM, not the invoice department. The department field is context, not a classification signal.
- is_marketing = true means the line item IS marketing activity: ad spend, campaigns, sponsorships, booth rentals, agency management fees.
- is_marketing = false for tangible/physical goods even if purchased by or for the Marketing department.
```

// WHY: The initial prompt states the general principle (assess the line item, not the department) but does NOT enumerate specific physical goods like t-shirts or gift bags. That specificity is intentionally withheld for the engineered weakness (see `docs/eval-and-feedback.md`). The LLM may still misclassify branded merch from marketing vendors without a few-shot example showing the pattern. The feedback loop adds the example and the specificity.

### Section C — Few-shot Examples

Start with 2–3 examples. More are added during the feedback loop iteration. Must include at least one example with null service periods. Format shows the full tool call pattern:

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

Few-shot examples go in the system prompt, not the user message. They are part of the extraction instructions, not part of the input.

### Section D — Constraints

```
CRITICAL RULES:
- You NEVER determine the GL account code. The classify_line_item tool does that.
- You NEVER skip the treatment check. Call apply_treatment for every classified line item.
- You NEVER guess dates. If no service period is stated, pass null.
- You ALWAYS call complete_processing as your final action.
- You process ALL line items before generating journal entries.
```

---

## Prompt Versioning for Feedback Loop

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

## INV-002 Line 2 — The Regulatory Advisory Edge Case

**Line: "Regulatory compliance review & advisory" — $3,200**
**Expected: 5040 (Consulting)**

This is the hardest classification call in the test set. Summary:

The SOP lists "regulatory" as a legal sub-type, but that refers to *direct regulatory actions* (filing, compliance submissions). "Regulatory compliance review & advisory" is advisory work about a regulatory subject — the nature of the work (advisory) dominates the subject matter (regulatory). The agent resolves this by providing `service_type="consulting"` when calling `classify_line_item`. The tool's rule tree follows it to 5040. No override, no special case, no description parsing in the rules.

The ambiguity: the SOP lists "regulatory" as a legal sub-type (→ 5030), and "advisory" as consulting (→ 5040). This line item has both signals. The assessment expects 5040.

Resolution: the distinction is between *direct legal actions* and *advisory work about legal topics*. "Litigation, patent filing, contract drafting, regulatory filing" are direct legal actions — you are doing the legal thing. "Regulatory compliance review & advisory" is someone reviewing your posture and advising you — the nature of the work is consulting, the subject matter happens to be regulatory.

This is architecturally correct: semantic disambiguation is a perception task (LLM's job). The rule engine handles structured predicates, not text interpretation. Adding a rule like "if description contains 'advisory' AND 'regulatory', force 5040" would build a second NLP system inside the rule engine, violating the separation of concerns.

The initial prompt is intentionally naive about this distinction (engineered weakness #2, see `docs/eval-and-feedback.md`). The feedback loop adds explicit guidance to Section B: "For work described as review, advisory, assessment, or consultation — even if the subject matter is regulatory, compliance, or legal topics — set service_type to consulting. Only set legal for direct legal actions: litigation, patent filing/prosecution, contract drafting/review, or regulatory filing."

// WHY: This edge case exists as a standalone section because it tests two things simultaneously: (1) the architecture's claim that semantic disambiguation belongs in the LLM layer, and (2) the feedback loop's ability to improve LLM behavior through targeted prompt refinement. Getting it wrong initially and then fixing it is a stronger demo than getting it right by accident.
