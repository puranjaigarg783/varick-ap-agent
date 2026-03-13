# Eval System & Feedback Loop

> **Implements:** `eval/runner.py`, `eval/labels.py`, `eval/feedback.py`, `data/corrections.json`

---

## Eval System

`run_eval()` processes each labeled invoice via `process_invoice(mode="auto")`, then compares results against ground truth in `eval/labels.py`.

Four accuracy dimensions:
- **GL Code** — correct final GL code per line item
- **Treatment** — correct treatment (expense, prepaid, accrual, capitalize)
- **Approval Routing** — correct approval level per invoice
- **Attribute (key)** — correct extraction of classification-driving attributes only

### Key Attributes Principle

Only attributes that DRIVE the classification are checked. If Priority 6 (`is_marketing`) fires, `category_hint` isn't evaluated — it was never consulted. This ensures attribute-level failures are meaningful for feedback targeting.

INV-006 is excluded from accuracy calculations (exception test for missing PO, not a classification test).

---

## Engineered Weakness

The initial prompt is deliberately imperfect in two ways:

1. **No few-shot example for branded merch.** Expected failure: INV-005 lines 1/3 get `is_marketing=True` instead of `is_physical_goods=True, is_branded_merch=True` → classified 5050 instead of 5000.

2. **Ambiguous regulatory/consulting guidance.** Expected failure: INV-002 line 1 gets `service_type="legal"` instead of `"consulting"` → classified 5030 instead of 5040.

---

## Feedback Loop Phases

1. **Baseline eval** — Run with naive prompt, record accuracy
2. **Corrections** — 5 human corrections targeting the two weaknesses (stored in `data/corrections.json`)
3. **Error analysis** — `analyze_corrections()` groups corrections into patterns (compound patterns like `is_physical_goods + is_branded_merch` detected together, single-field patterns for the rest)
4. **Prompt refinement** — `apply_prompt_refinement()` dynamically generates guidance from correction patterns and injects via `get_system_prompt(refinements=...)`
5. **Re-eval** — Run with refined prompt, record improved accuracy
6. **Before/after report** — `generate_improvement_report()` produces the comparison artifact

The feedback loop is not "the system learns by itself." It's a structured human-in-the-loop improvement cycle: eval → correct → analyze → adjust → re-eval → measure.
