"""Step 2: GL classification — priority rule tree."""

from datetime import date

from src.models import ClassificationResult, ExtractedAttributes


def _compute_amortization_months(attrs: ExtractedAttributes) -> int:
    if attrs.service_period_start and attrs.service_period_end:
        start = date.fromisoformat(attrs.service_period_start)
        end = date.fromisoformat(attrs.service_period_end)
        months = (end.year - start.year) * 12 + (end.month - start.month) + 1
        return max(months, 1)
    return 12


def classify_line_item(attrs: ExtractedAttributes, unit_cost: float) -> ClassificationResult:
    # Priority 1: Physical goods (not equipment)
    if attrs.is_physical_goods and not attrs.is_equipment:
        return ClassificationResult(
            gl_code="5000", gl_name="Office Supplies",
            rule_triggered="Priority 1: Physical goods \u2192 5000",
            treatment="expense",
        )

    # Priority 2: Equipment
    if attrs.is_equipment:
        if unit_cost >= 5000:
            return ClassificationResult(
                gl_code="1500", gl_name="Fixed Assets",
                rule_triggered="Priority 2: Equipment, unit cost \u2265 $5K \u2192 1500 (capitalize)",
                treatment="capitalize",
            )
        else:
            return ClassificationResult(
                gl_code="5110", gl_name="Equipment (under $5,000)",
                rule_triggered="Priority 2: Equipment, unit cost < $5K \u2192 5110",
                treatment="expense",
            )

    # Priority 3: Software/SaaS
    if attrs.is_software:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1310", gl_name="Prepaid Software",
                rule_triggered="Priority 3: Software \u2014 annual prepayment \u2192 1310",
                treatment="prepaid",
                prepaid_expense_target="5010",
                amortization_months=_compute_amortization_months(attrs),
            )
        else:
            return ClassificationResult(
                gl_code="5010", gl_name="Software & Subscriptions",
                rule_triggered="Priority 3: Software \u2014 monthly/usage \u2192 5010",
                treatment="expense",
            )

    # Priority 4: Cloud hosting
    if attrs.is_cloud_hosting:
        if attrs.billing_frequency == "annual":
            return ClassificationResult(
                gl_code="1300", gl_name="Prepaid Expenses (General)",
                rule_triggered="Priority 4: Cloud hosting \u2014 annual prepayment \u2192 1300",
                treatment="prepaid",
                prepaid_expense_target="5020",
                amortization_months=_compute_amortization_months(attrs),
            )
        else:
            return ClassificationResult(
                gl_code="5020", gl_name="Cloud Hosting & Infrastructure",
                rule_triggered="Priority 4: Cloud hosting \u2014 monthly/usage \u2192 5020",
                treatment="expense",
            )

    # Priority 5: Professional services
    if attrs.service_type in ("legal", "mixed_legal"):
        return ClassificationResult(
            gl_code="5030", gl_name="Professional Services \u2014 Legal",
            rule_triggered=f"Priority 5: Professional services \u2014 {attrs.service_type} \u2192 5030",
            treatment="expense",
        )
    if attrs.service_type == "consulting":
        return ClassificationResult(
            gl_code="5040", gl_name="Professional Services \u2014 Consulting",
            rule_triggered="Priority 5: Professional services \u2014 consulting \u2192 5040",
            treatment="expense",
        )

    # Priority 6: Marketing
    if attrs.is_marketing:
        return ClassificationResult(
            gl_code="5050", gl_name="Marketing & Advertising",
            rule_triggered="Priority 6: Marketing \u2192 5050",
            treatment="expense",
        )

    # Priority 7: Other categories
    category_map = {
        "travel": ("5060", "Travel & Entertainment"),
        "facilities": ("5070", "Facilities & Maintenance"),
        "training": ("5080", "Training & Development"),
        "telecom": ("5090", "Telecom & Internet"),
        "insurance": ("5100", "Insurance Expense"),
    }
    if attrs.category_hint in category_map:
        code, name = category_map[attrs.category_hint]
        return ClassificationResult(
            gl_code=code, gl_name=name,
            rule_triggered=f"Priority 7: {attrs.category_hint} \u2192 {code}",
            treatment="expense",
        )

    # Fallback: unclassifiable
    return ClassificationResult(
        gl_code="UNCLASSIFIED", gl_name="Unclassified",
        rule_triggered="No matching rule \u2014 flagged for human review",
        treatment="expense",
    )
