# LLM Attribute Extraction

> **Implements:** `src/attribute_extraction.py`, `src/prompts.py`
> **Spec origin:** Sections 7, 19

---

## Function Signature

```python
async def extract_attributes(
    line_item: LineItem,
    invoice_context: dict,  # vendor, department, invoice-level service period, etc.
    client: anthropic.Anthropic
) -> ExtractedAttributes
```

---

## LLM Call Configuration

- **Model:** `claude-sonnet-4-20250514` (default, configurable via `AP_AGENT_MODEL` environment variable)
- **Temperature:** 0.0 (deterministic extraction — we want consistency, not creativity)
- **Max tokens:** 1024 (attribute extraction is compact)
- **Tool use / structured output:** Use Anthropic SDK's structured output with the `ExtractedAttributes` Pydantic model as the response schema. This guarantees the response conforms to the schema — no parsing needed.

// WHY: Sonnet, not Haiku, not Opus. The extraction task is well-scoped (bounded by a Pydantic schema) but the hard cases require genuine reading comprehension: distinguishing "advisory work about regulatory topics" from "direct legal work," recognizing physical goods from a marketing agency as NOT marketing activity, parsing ambiguous service descriptions like "Premium Support & Implementation Services." Haiku would nail the obvious cases but fumble these. Opus is overkill — the schema constraint eliminates formatting errors and the prompt is tight. Cost is irrelevant at this volume (~64 LLM calls total including feedback loop re-runs). The model string is configurable via environment variable so the assessor can swap models without code changes.

---

## Prompt Design (`prompts.py`)

**LOCKED DECISION — Prompt structure and information boundary:**

The LLM sees extraction instructions only. It does NOT see the SOP, GL codes, priority rules, approval thresholds, or journal entry structure. If the LLM sees "Priority 1: Physical goods → 5000," it will start reasoning about GL codes instead of extracting attributes. The attribute extraction becomes contaminated by classification reasoning that belongs in the rule engine. The architecture's testability depends on this boundary.

| LLM sees | LLM does NOT see |
|----------|-----------------|
| Attribute schema (field names, types, enums) | GL codes or account names |
| Extraction guidance per attribute | Priority rule tree |
| Few-shot examples (attributes only, no GL codes) | SOP document |
| Invoice context (vendor, dept, date) | Approval thresholds |
| Line item description and amounts | Journal entry structure |

**System prompt** (stable across all calls, changes only during feedback loop):
- Section A: Role and constraints
- Section B: Attribute extraction instructions
- Section C: Few-shot examples

**User message** (varies per line item):
- Invoice context + line item details

Few-shot examples go in the system prompt, not the user message. They are part of the extraction instructions, not part of the input. Placing them in the user message risks the model pattern-matching too aggressively against the examples instead of reasoning about the actual line item.

The system prompt has three sections:

**Section A — Role and constraints:**
```
You are an accounting attribute extractor. Your job is to analyze a line item from a vendor invoice and extract structured attributes. You NEVER determine the GL account code — that is done by a downstream rule engine. You ONLY extract factual attributes about what the line item is. You do not know and should not guess what account codes exist or how they map to attributes.
```

**Section B — Attribute extraction instructions:**
Each attribute gets a one-line definition and a brief "when to set true" guide. This section must be tuned through the feedback loop. The initial version is intentionally slightly naive (see `docs/eval-and-feedback.md`).

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

**Section C — Few-shot examples:**
Start with 2–3 examples in the initial prompt. More are added during the feedback loop iteration. Must include at least one example with null service periods. Format:

```
Line item: "Annual Platform License (Jan–Dec 2026)"
Vendor: Cloudware Solutions | Dept: Engineering
→ is_software: true, billing_frequency: annual, service_period_start: 2026-01-01, service_period_end: 2026-12-31, confidence: 0.95

Line item: "Patent filing & prosecution"
Vendor: Morrison & Burke LLP | Dept: Legal
→ service_type: legal, billing_frequency: one_time, service_period_start: null, service_period_end: null, confidence: 0.95
```

**The user message** for each call:
```
Invoice: {invoice_id} | Vendor: {vendor} | Department: {department}
Invoice date: {date}
Invoice-level service period: {service_period_start} to {service_period_end} (if stated)

Line item: "{description}"
Amount: ${amount} | Quantity: {quantity} | Unit cost: ${unit_cost}

Extract the structured attributes for this line item.
```

---

## Post-Extraction Validation

After the LLM returns `ExtractedAttributes`, run these validation checks:

1. **Invariant check:** If `is_branded_merch = True` but `is_physical_goods = False` → force `is_physical_goods = True`, log a warning.
2. **Invariant check:** If `is_equipment = True` but `is_physical_goods = False` → force `is_physical_goods = True`, log a warning.
3. **Confidence check:** If `confidence < 0.7` → add flag `low_confidence_line:{line_index}` to the invoice. Processing continues, but the flag is surfaced in output.
4. **Unit cost resolution (precedence chain — first non-null wins):**
   1. `line_item.unit_cost` — explicit in the invoice data. Authoritative.
   2. `line_item.amount / line_item.quantity` — if `quantity > 1`, compute it deterministically.
   3. `attrs.unit_cost_extracted` — LLM parsed a per-unit cost from the description. Fallback only.
   4. `line_item.amount` — if `quantity == 1` and nothing else is available, amount IS unit cost.
   Store the resolved unit cost on the line item record.

// WHY: Deterministic data takes precedence over LLM extraction. INV-003 MacBooks have `quantity=3, amount=5400` → step 2 fires → `unit_cost=1800`. The LLM's `unit_cost_extracted` is irrelevant. But `unit_cost_extracted` stays on the schema because in production, invoices arrive as unstructured text where "3x $1,800" is in the description, not in structured fields. The LLM parsing it is a real perception capability worth demonstrating — it just isn't authoritative when structured data exists.

---

## Batching Strategy

Process line items sequentially within an invoice, one LLM call per line item. Do NOT batch multiple line items into a single call.

// WHY: Each line item gets the invoice-level context but its own extraction. This avoids cross-contamination (where the LLM's attributes for line 2 are influenced by its extraction of line 1) and makes it trivial to pinpoint which call produced which attributes.

---

## INV-002 Line 2 — The Regulatory Advisory Edge Case

**Line: "Regulatory compliance review & advisory" — $3,200**
**Expected: 5040 (Consulting)**

This is the hardest classification call in the test set. Summary:

The SOP lists "regulatory" as a legal sub-type, but that refers to *direct regulatory actions* (filing, compliance submissions). "Regulatory compliance review & advisory" is advisory work about a regulatory subject — the nature of the work (advisory) dominates the subject matter (regulatory). The LLM resolves this by extracting `service_type="consulting"`. No rule-level override.

The ambiguity: the SOP lists "regulatory" as a legal sub-type (→ 5030), and "advisory" as consulting (→ 5040). This line item has both signals. The assessment expects 5040.

Resolution: the distinction is between *direct legal actions* and *advisory work about legal topics*. "Litigation, patent filing, contract drafting, regulatory filing" are direct legal actions — you are doing the legal thing. "Regulatory compliance review & advisory" is someone reviewing your posture and advising you — the nature of the work is consulting, the subject matter happens to be regulatory.

The LLM extracts `service_type="consulting"`. The rule engine follows it to 5040. No override, no special case, no description parsing in the rules.

This is architecturally correct: semantic disambiguation is a perception task (LLM's job). The rule engine handles structured predicates, not text interpretation. Adding a rule like "if description contains 'advisory' AND 'regulatory', force 5040" would build a second NLP system inside the rule engine, violating the separation of concerns.

The initial prompt is intentionally naive about this distinction (engineered weakness #2, see `docs/eval-and-feedback.md`). The feedback loop adds explicit guidance: "For work described as review, advisory, assessment, or consultation — even if the subject matter is regulatory, compliance, or legal topics — set service_type to consulting. Only set legal for direct legal actions: litigation, patent filing/prosecution, contract drafting/review, or regulatory filing."

// WHY: This edge case exists as a standalone section because it tests two things simultaneously: (1) the architecture's claim that semantic disambiguation belongs in the LLM layer, and (2) the feedback loop's ability to improve LLM behavior through targeted prompt refinement. Getting it wrong initially and then fixing it is a stronger demo than getting it right by accident.
