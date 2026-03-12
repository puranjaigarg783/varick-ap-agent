# Step 2 — GL Classification

> **Implements:** `src/classification.py`
> **Spec origin:** Section 8

---

## Function Signature

```python
def classify_line_item(attrs: ExtractedAttributes, unit_cost: float) -> ClassificationResult
```

---

## Priority Rule Tree

**Conflict resolution meta-principle:** Evaluate rules in order 1→7. First match wins. Stop. No scoring, no weighting, no tiebreaking. Multiple attributes CAN be true simultaneously — the priority order determines which one matters. This is a hard-coded if/elif chain, not a lookup table, and not a scoring system.

The rules are evaluated **in priority order**:

```python
def classify_line_item(attrs, unit_cost):
    # Priority 1: Physical goods
    if attrs.is_physical_goods and not attrs.is_equipment:
        return ClassificationResult(
            gl_code="5000", gl_name="Office Supplies",
            rule_triggered="Priority 1: Physical goods → 5000",
            treatment="expense"
        )

    # Priority 2: Equipment
    if attrs.is_equipment:
        if unit_cost >= 5000:
            return ClassificationResult(
                gl_code="1500", gl_name="Fixed Assets",
                rule_triggered="Priority 2: Equipment, unit cost ≥ $5K → 1500 (capitalize)",
                treatment="capitalize"
            )
        else:
            return ClassificationResult(
                gl_code="5110", gl_name="Equipment (under $5,000)",
                rule_triggered="Priority 2: Equipment, unit cost < $5K → 5110",
                treatment="expense"
            )

    # Priority 3: Software/SaaS
    if attrs.is_software:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1310", gl_name="Prepaid Software",
                rule_triggered="Priority 3: Software — annual prepayment → 1310",
                treatment="prepaid",
                prepaid_expense_target="5010",
                amortization_months=_compute_amortization_months(attrs)
            )
        else:
            return ClassificationResult(
                gl_code="5010", gl_name="Software & Subscriptions",
                rule_triggered="Priority 3: Software — monthly/usage → 5010",
                treatment="expense"
            )

    # Priority 4: Cloud hosting
    if attrs.is_cloud_hosting:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1300", gl_name="Prepaid Expenses (General)",
                rule_triggered="Priority 4: Cloud hosting — annual prepayment → 1300",
                treatment="prepaid",
                prepaid_expense_target="5020",
                amortization_months=_compute_amortization_months(attrs)
            )
        else:
            return ClassificationResult(
                gl_code="5020", gl_name="Cloud Hosting & Infrastructure",
                rule_triggered="Priority 4: Cloud hosting — monthly/usage → 5020",
                treatment="expense"
            )

    # Priority 5: Professional services
    if attrs.service_type in ("legal", "mixed_legal"):
        return ClassificationResult(
            gl_code="5030", gl_name="Professional Services — Legal",
            rule_triggered=f"Priority 5: Professional services — {attrs.service_type} → 5030",
            treatment="expense"
        )
    if attrs.service_type == "consulting":
        return ClassificationResult(
            gl_code="5040", gl_name="Professional Services — Consulting",
            rule_triggered="Priority 5: Professional services — consulting → 5040",
            treatment="expense"
        )

    # Priority 6: Marketing
    if attrs.is_marketing:
        return ClassificationResult(
            gl_code="5050", gl_name="Marketing & Advertising",
            rule_triggered="Priority 6: Marketing → 5050",
            treatment="expense"
        )

    # Priority 7: Other categories
    # Note: "recruiting" and "catering" are valid category_hint enum values but are
    # intentionally NOT in this map. They fall through to UNCLASSIFIED — fail closed.
    category_map = {
        "travel": ("5060", "Travel & Entertainment"),
        "facilities": ("5070", "Facilities & Maintenance"),
        "training": ("5080", "Training & Development"),
        "telecom": ("5090", "Telecom & Internet"),
        "insurance": ("5100", "Insurance Expense"),
    }
    if attrs.category_hint in category_map:
        code, name = category_map[attrs.category_hint]
        # Special case: insurance with prepaid treatment handled in Step 3
        return ClassificationResult(
            gl_code=code, gl_name=name,
            rule_triggered=f"Priority 7: {attrs.category_hint} → {code}",
            treatment="expense"
        )

    # Fallback: unclassifiable
    return ClassificationResult(
        gl_code="UNCLASSIFIED", gl_name="Unclassified",
        rule_triggered="No matching rule — flagged for human review",
        treatment="expense"
    )
```

---

## Critical Rule Interactions

**Multi-flag resolution — every scenario in the test data:**

| Flags true simultaneously | Rule hit | Result | Why |
|--------------------------|----------|--------|-----|
| `is_physical_goods`, `is_branded_merch` | Priority 1 | 5000 | Physical goods, not equipment → P1 fires. P6 (marketing) never reached. |
| `is_physical_goods`, `is_equipment`, unit < $5K | Priority 2 | 5110 | P1 checks `is_physical_goods AND NOT is_equipment` → skips. P2 fires. |
| `is_physical_goods`, `is_equipment`, unit ≥ $5K | Priority 2 | 1500 | Same skip from P1. P2 fires with capitalize. |
| `is_software`, `billing_frequency=annual` | Priority 3 | 1310 | Single-flag match, no conflict. |
| `is_cloud_hosting`, `billing_frequency=annual` | Priority 4 | 1300 | Single-flag match, no conflict. |
| `is_marketing`, `is_physical_goods` | Priority 1 | 5000 | P1 fires first. P6 never reached. This IS the branded merch override. |
| `is_marketing`, `category_hint=training` | Priority 6 | 5050 | P6 fires first. P7 never reached. |

The if/elif chain IS the conflict resolution. There is no secondary mechanism.

**Physical goods override for branded merch from marketing:**
- INV-005 has "Branded company t-shirts" from a Marketing vendor. The LLM should extract `is_physical_goods=True, is_branded_merch=True`. Priority 1 fires (physical goods → 5000) BEFORE Priority 6 (marketing → 5050) is ever reached. The priority order IS the override mechanism.

**Equipment is a subset of physical goods:**
- Priority 1 checks `is_physical_goods and NOT is_equipment`. Equipment items (which are also physical goods) skip Priority 1 and hit Priority 2 where the $5K threshold applies. This is not two rules competing — it's one rule with an explicit carve-out.

**LOCKED DECISION — INV-002 line 2: "Regulatory compliance review & advisory" → 5040 (consulting)**

This is resolved by the LLM, not the rule engine. No rule-level override exists or is needed. See `docs/llm-extraction.md` for the full analysis.

---

## Amortization Month Computation

```python
def _compute_amortization_months(attrs: ExtractedAttributes) -> int:
    """Compute months between service_period_start and service_period_end."""
    if attrs.service_period_start and attrs.service_period_end:
        start = date.fromisoformat(attrs.service_period_start)
        end = date.fromisoformat(attrs.service_period_end)
        months = (end.year - start.year) * 12 + (end.month - start.month)
        return max(months, 1)  # At least 1 month
    # Default: 12 months if service period not parseable
    return 12
```

// WHY: Default to 12 months because "annual" implies 12 months. But if the LLM extracted specific dates (e.g., "Feb 26–Jan 27" = 12 months), use those.
