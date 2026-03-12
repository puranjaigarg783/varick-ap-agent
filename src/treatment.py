"""Step 3: Prepaid and accrual treatment overrides."""

from datetime import date

from src.models import ClassificationResult, ExtractedAttributes, Invoice
from src.classification import _compute_amortization_months


def _is_accrual(attrs: ExtractedAttributes, invoice: Invoice) -> bool:
    """Check if service period ended before invoice date. Strict greater-than."""
    spe = attrs.service_period_end or invoice.service_period_end
    if spe is None:
        return False
    return date.fromisoformat(invoice.date) > date.fromisoformat(spe)


def determine_treatment(
    attrs: ExtractedAttributes,
    classification: ClassificationResult,
    invoice: Invoice,
) -> ClassificationResult:
    result = classification.model_copy()

    # 1. Insurance prepaid check (specific account 1320)
    if (
        attrs.category_hint == "insurance"
        and attrs.billing_frequency == "annual"
        and result.treatment != "prepaid"
    ):
        months = _compute_amortization_months(attrs)
        if attrs.service_period_start and attrs.service_period_end:
            start = date.fromisoformat(attrs.service_period_start)
            end = date.fromisoformat(attrs.service_period_end)
            diff_months = (end.year - start.year) * 12 + (end.month - start.month) + 1
            if diff_months <= 1:
                # Service period is 1 month or less — contradicts prepaid
                pass
            else:
                result.gl_code = "1320"
                result.gl_name = "Prepaid Insurance"
                result.treatment = "prepaid"
                result.prepaid_expense_target = "5100"
                result.amortization_months = months
                result.rule_triggered = "Treatment: Insurance annual prepayment \u2192 1320"
        else:
            # No dates — annual implies >1 month, default to prepaid
            result.gl_code = "1320"
            result.gl_name = "Prepaid Insurance"
            result.treatment = "prepaid"
            result.prepaid_expense_target = "5100"
            result.amortization_months = months
            result.rule_triggered = "Treatment: Insurance annual prepayment \u2192 1320"

    # 2. General prepaid check (only if not already prepaid from classification)
    if (
        attrs.billing_frequency == "annual"
        and result.treatment == "expense"
    ):
        months = _compute_amortization_months(attrs)
        should_prepay = True
        if attrs.service_period_start and attrs.service_period_end:
            start = date.fromisoformat(attrs.service_period_start)
            end = date.fromisoformat(attrs.service_period_end)
            diff_months = (end.year - start.year) * 12 + (end.month - start.month) + 1
            if diff_months <= 1:
                should_prepay = False
        if should_prepay:
            original_gl = result.gl_code
            result.gl_code = "1300"
            result.gl_name = "Prepaid Expenses (General)"
            result.treatment = "prepaid"
            result.prepaid_expense_target = original_gl
            result.amortization_months = months
            result.rule_triggered = f"Treatment: General annual prepayment \u2192 1300 (amortize to {original_gl})"

    # 3. Accrual check — accrual wins over prepaid
    if _is_accrual(attrs, invoice):
        if attrs.service_type in ("legal", "consulting", "mixed_legal"):
            accrual_type = "professional_services"
            accrual_gl = "2110"
            accrual_name = "Accrued Professional Services"
        else:
            accrual_type = "other"
            accrual_gl = "2100"
            accrual_name = "Accrued Expenses (General)"

        # Store the original expense GL for journal entry use
        if result.treatment == "prepaid" and result.prepaid_expense_target:
            expense_gl = result.prepaid_expense_target
        else:
            expense_gl = result.gl_code

        result.treatment = "accrual"
        result.accrual_type = accrual_type
        result.prepaid_expense_target = expense_gl  # Reuse for the expense GL in accrual entries
        result.gl_code = accrual_gl
        result.gl_name = accrual_name
        result.rule_triggered = f"Treatment: Accrual \u2014 service period ended before invoice date \u2192 {accrual_gl}"
        result.amortization_months = None

    return result
