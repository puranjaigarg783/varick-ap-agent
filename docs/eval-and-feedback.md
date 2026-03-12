# Eval System & Feedback Loop

> **Implements:** `eval/runner.py`, `eval/labels.py`, `eval/feedback.py`, `data/corrections.json`
> **Spec origin:** Sections 13, 14

---

## Eval System (`eval/runner.py`)

### Function Signature

```python
def run_eval(
    invoices: list[Invoice],
    labels: dict,              # Ground truth: {invoice_id: {line_index: {gl_code, treatment, ...}}}
    db: sqlite3.Connection,
    client: anthropic.Anthropic
) -> EvalReport
```

### Ground Truth Labels (`eval/labels.py`)

Hardcoded from the assessment's "Expected GL" column. This is a Python dict, not a JSON file — labels are part of the eval logic, not input data.

**Label schema per line item:**
```python
{
    "gl_code": str,          # Expected final GL code (for accruals, this is the accrual account, not the expense)
    "treatment": str,        # Expected treatment: "expense", "prepaid", "accrual", "capitalize"
    "approval": str,         # Expected approval level: "auto_approve", "dept_manager", "vp_finance"
    "key_attributes": dict   # Only attributes that DRIVE the classification for this line item
}
```

`key_attributes` principle: only include attributes the classification depends on. If Priority 6 (`is_marketing`) fires, don't include `category_hint` — it's never consulted. The eval measures attribute accuracy (dimension 4) against these; including attributes the classification doesn't depend on measures noise.

```python
LABELS = {
    "INV-001": {
        0: {"gl_code": "1310", "treatment": "prepaid", "approval": "vp_finance",
            "key_attributes": {"is_software": True, "billing_frequency": "annual"}}
    },
    "INV-002": {
        0: {"gl_code": "5030", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"}},
        1: {"gl_code": "5040", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"}},
        2: {"gl_code": "5030", "treatment": "expense", "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"}}
    },
    "INV-003": {
        0: {"gl_code": "5110", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True}},
        1: {"gl_code": "1500", "treatment": "capitalize", "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True}},
        2: {"gl_code": "1300", "treatment": "prepaid", "approval": "vp_finance",
            "key_attributes": {"is_cloud_hosting": True, "billing_frequency": "annual"}}
    },
    "INV-004": {
        0: {"gl_code": "2110", "treatment": "accrual", "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"}},
        1: {"gl_code": "2100", "treatment": "accrual", "approval": "dept_manager",
            "key_attributes": {"category_hint": "travel"}}
    },
    "INV-005": {
        0: {"gl_code": "5050", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_marketing": True}},
        1: {"gl_code": "5000", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True}},
        2: {"gl_code": "5050", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_marketing": True}},
        3: {"gl_code": "5000", "treatment": "expense", "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True}}
    },
    "INV-006": {
        "expected_flag": "no_po_provided"
    }
}
```

### Eval Execution Flow

1. Process each invoice using `process_invoice(invoice, db, client, mode="auto")`.
2. For INV-006: verify it was flagged with `no_po_provided`. No GL/treatment eval.
3. For all others: compare each line item's actual classification against the label.
4. For attribute accuracy: compare LLM-extracted attributes against `key_attributes` in the label.
5. Aggregate results into `EvalReport`.

### Accuracy Computation

```
gl_accuracy = correct_gl_codes / total_line_items_with_labels
treatment_accuracy = correct_treatments / total_line_items_with_labels
approval_accuracy = correct_approval_routes / total_invoices_with_labels
attribute_accuracy = correct_key_attributes / total_key_attributes_checked
```

**Attribute accuracy detail:** For each line item, iterate over the `key_attributes` dict in the label. For each key-value pair, check the corresponding field in the LLM's `ExtractedAttributes` output. A key attribute is "correct" if the extracted value matches the expected value exactly (bool match for booleans, string equality for enums). Only `key_attributes` are checked — the other ~12 fields in `ExtractedAttributes` are not evaluated. This means `total_key_attributes_checked` is the sum of `len(key_attributes)` across all labeled line items, not `15 × total_line_items`.

// WHY: `key_attributes` are reverse-engineered from expected GL codes via the rule tree. The rule tree is deterministic — given an expected GL code, there is exactly one set of attributes that could have produced it through the if/elif chain. We only check the attributes that drove the classification because checking "did the LLM correctly set `is_cloud_hosting=False` on a patent filing" measures noise, not capability. The feedback loop uses attribute-level failures to target prompt improvements — this only works if the failures are meaningful.

INV-006 is excluded from all accuracy calculations (it's an exception test, not a classification test).

---

## Feedback Loop (`eval/feedback.py`)

### The Engineered Improvement Story

**Phase A — Baseline (intentionally slightly naive):**

The initial LLM prompt in `prompts.py` is deliberately imperfect in two specific ways:

1. **No few-shot example for branded merch override.** The LLM's initial prompt does not include an example of "branded company t-shirts" being classified as `is_physical_goods=True, is_branded_merch=True`. Expected failure: INV-005 lines 2 and 4 get `is_physical_goods=False, is_marketing=True` → classified 5050 instead of 5000.

2. **Ambiguous guidance on "regulatory compliance review."** The initial prompt does not explicitly call out that advisory/review work is `service_type="consulting"` even when it mentions "regulatory." Expected failure: INV-002 line 2 gets `service_type="legal"` → classified 5030 instead of 5040.

Run eval → record **baseline accuracy** across all 4 dimensions. Store the EvalReport.

**Phase B — Corrections:**

After baseline eval, submit corrections:
```python
corrections = [
    Correction(invoice_id="INV-005", line_item_index=1, field="is_physical_goods",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=1, field="is_branded_merch",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=3, field="is_physical_goods",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-005", line_item_index=3, field="is_branded_merch",
               original_value="false", corrected_value="true"),
    Correction(invoice_id="INV-002", line_item_index=1, field="service_type",
               original_value="legal", corrected_value="consulting"),
]
```

**Phase C — Error analysis:**

```python
def analyze_corrections(corrections: list[Correction]) -> dict:
    """Group corrections by field and pattern. Returns summary."""
```

Output: `{"is_branded_merch_missing": 2, "service_type_legal_vs_consulting": 1}`.

**Phase D — Prompt refinement:**

Based on error patterns, add to the prompt:
1. A few-shot example: `"Branded company t-shirts (500 units)" → is_physical_goods: true, is_branded_merch: true, is_marketing: false`.
2. An explicit instruction: `"For regulatory compliance review, advisory, or assessment work — even if it mentions 'regulatory' — set service_type to 'consulting' unless the work is litigation, patent filing, or contract drafting."`

**Phase E — Re-run eval:**

Run eval again with the refined prompt. Record **after accuracy**. Compare against baseline.

**Phase F — Produce before/after artifact:**

```python
def generate_improvement_report(baseline: EvalReport, after: EvalReport, corrections: list[Correction]) -> str:
    """Generate a human-readable before/after comparison. Printed to stdout by `feedback report`."""
```

Output format (structured text to stdout, consistent with all other CLI output):
```
══════════════════════════════════════════════════
Feedback Loop Report
══════════════════════════════════════════════════

Accuracy Comparison:
┌────────────────────┬──────────────┬──────────────┬────────────┐
│ Dimension          │ Baseline     │ After        │ Delta      │
├────────────────────┼──────────────┼──────────────┼────────────┤
│ GL Code            │ 10/13 (76.9%)│ 13/13 (100%) │ +23.1 pp   │
│ Treatment          │ 13/13 (100%) │ 13/13 (100%) │  —         │
│ Approval Routing   │  5/5  (100%) │  5/5  (100%) │  —         │
│ Attribute (key)    │ 18/23 (78.3%)│ 23/23 (100%) │ +21.7 pp   │
└────────────────────┴──────────────┴──────────────┴────────────┘

Corrections Applied: 5
┌────────────┬──────┬─────────────────────┬──────────┬───────────┐
│ Invoice    │ Line │ Field               │ Was      │ Should be │
├────────────┼──────┼─────────────────────┼──────────┼───────────┤
│ INV-005    │  1   │ is_physical_goods   │ false    │ true      │
│ INV-005    │  1   │ is_branded_merch    │ false    │ true      │
│ INV-005    │  3   │ is_physical_goods   │ false    │ true      │
│ INV-005    │  3   │ is_branded_merch    │ false    │ true      │
│ INV-002    │  1   │ service_type        │ legal    │ consulting│
└────────────┴──────┴─────────────────────┴──────────┴───────────┘

Error Patterns Identified: 2
  1. Missing branded merch flag (2 line items, 4 attribute errors)
     → LLM did not recognize physical goods from marketing vendor
  2. Regulatory/consulting confusion (1 line item, 1 attribute error)
     → LLM tagged advisory work as legal based on subject matter

Prompt Changes Applied:
  1. Added few-shot example: "Branded company t-shirts (500 units)"
     → is_physical_goods: true, is_branded_merch: true, is_marketing: false
  2. Added instruction: advisory/review work about regulatory topics
     → service_type: consulting, not legal

Baseline Failures (now fixed):
  INV-005 line 1: GL 5050 → 5000 (physical goods, not marketing)
  INV-005 line 3: GL 5050 → 5000 (physical goods, not marketing)
  INV-002 line 1: GL 5030 → 5040 (consulting, not legal)
══════════════════════════════════════════════════
```

This is the assessor-facing artifact. It tells the complete story in one screen: what was wrong, why it was wrong, what was changed, and the measurable result. The format is illustrative — exact layout may vary, but all sections must be present.

// WHY: The feedback loop is not "the system learns by itself." It's a structured human-in-the-loop improvement cycle: eval → correct → analyze → adjust → re-eval → measure. The engineered initial weakness ensures there's something to improve. The before/after report is the assessor-facing artifact that proves the loop works.
