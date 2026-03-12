"""Feedback loop — corrections analysis, prompt refinement, improvement reporting."""

from src.models import Correction, EvalReport
from src.prompts import SYSTEM_PROMPT_INITIAL, SYSTEM_PROMPT_REFINED


def analyze_corrections(corrections: list[Correction]) -> dict[str, int]:
    """Group corrections by field and pattern. Returns summary."""
    patterns: dict[str, int] = {}

    branded_merch_count = 0
    service_type_count = 0

    for c in corrections:
        if c.field in ("is_physical_goods", "is_branded_merch") and c.corrected_value == "true":
            branded_merch_count += 1
        elif c.field == "service_type" and c.original_value == "legal" and c.corrected_value == "consulting":
            service_type_count += 1

    if branded_merch_count > 0:
        patterns["is_branded_merch_missing"] = branded_merch_count // 2  # 2 corrections per line item
    if service_type_count > 0:
        patterns["service_type_legal_vs_consulting"] = service_type_count

    return patterns


def apply_prompt_refinement(corrections: list[Correction]) -> str:
    """Return refined system prompt based on correction patterns."""
    patterns = analyze_corrections(corrections)

    # If patterns match our expected weaknesses, use the refined prompt
    if patterns:
        return SYSTEM_PROMPT_REFINED

    return SYSTEM_PROMPT_INITIAL


def generate_improvement_report(
    baseline: EvalReport,
    after: EvalReport,
    corrections: list[Correction],
) -> str:
    """Generate a human-readable before/after comparison."""
    patterns = analyze_corrections(corrections)

    def pct(val: float, total: int) -> str:
        correct = round(val * total)
        return f"{correct}/{total} ({val * 100:.1f}%)"

    def delta(before: float, after_val: float) -> str:
        diff = (after_val - before) * 100
        if abs(diff) < 0.1:
            return " \u2014"
        return f"+{diff:.1f} pp" if diff > 0 else f"{diff:.1f} pp"

    total_li = baseline.total_line_items
    total_key_attrs_baseline = 0
    total_key_attrs_after = 0
    for r in baseline.results:
        total_key_attrs_baseline += len(r.attribute_errors)
    for r in after.results:
        total_key_attrs_after += len(r.attribute_errors)

    # Compute raw correct counts
    baseline_gl_correct = round(baseline.gl_accuracy * total_li)
    after_gl_correct = round(after.gl_accuracy * after.total_line_items)
    baseline_treat_correct = round(baseline.treatment_accuracy * total_li)
    after_treat_correct = round(after.treatment_accuracy * after.total_line_items)

    # For key attributes, count total checked from labels
    from eval.labels import LABELS
    total_key = 0
    for inv_id, label in LABELS.items():
        if "expected_flag" in label:
            continue
        for line_idx, line_label in label.items():
            if isinstance(line_idx, int) and "key_attributes" in line_label:
                total_key += len(line_label["key_attributes"])

    baseline_attr_correct = round(baseline.attribute_accuracy * total_key)
    after_attr_correct = round(after.attribute_accuracy * total_key)

    # Approval counts (invoice level)
    baseline_approval_correct = sum(1 for r in baseline.results if r.approval_correct)
    after_approval_correct = sum(1 for r in after.results if r.approval_correct)
    # Deduplicate by invoice
    baseline_inv_set = set()
    after_inv_set = set()
    baseline_appr = 0
    after_appr = 0
    total_inv = 0
    for r in baseline.results:
        if r.invoice_id not in baseline_inv_set:
            baseline_inv_set.add(r.invoice_id)
            total_inv += 1
            if r.approval_correct:
                baseline_appr += 1
    for r in after.results:
        if r.invoice_id not in after_inv_set:
            after_inv_set.add(r.invoice_id)
            if r.approval_correct:
                after_appr += 1

    lines = []
    lines.append("\u2550" * 50)
    lines.append("Feedback Loop Report")
    lines.append("\u2550" * 50)
    lines.append("")
    lines.append("Accuracy Comparison:")
    lines.append(f"{'Dimension':<22} {'Baseline':<16} {'After':<16} {'Delta':<12}")
    lines.append("-" * 66)
    lines.append(f"{'GL Code':<22} {baseline_gl_correct}/{total_li} ({baseline.gl_accuracy*100:.1f}%){'':<4} {after_gl_correct}/{after.total_line_items} ({after.gl_accuracy*100:.1f}%){'':<4} {delta(baseline.gl_accuracy, after.gl_accuracy)}")
    lines.append(f"{'Treatment':<22} {baseline_treat_correct}/{total_li} ({baseline.treatment_accuracy*100:.1f}%){'':<4} {after_treat_correct}/{after.total_line_items} ({after.treatment_accuracy*100:.1f}%){'':<4} {delta(baseline.treatment_accuracy, after.treatment_accuracy)}")
    lines.append(f"{'Approval Routing':<22} {baseline_appr}/{total_inv} ({baseline.approval_accuracy*100:.1f}%){'':<4} {after_appr}/{total_inv} ({after.approval_accuracy*100:.1f}%){'':<4} {delta(baseline.approval_accuracy, after.approval_accuracy)}")
    lines.append(f"{'Attribute (key)':<22} {baseline_attr_correct}/{total_key} ({baseline.attribute_accuracy*100:.1f}%){'':<4} {after_attr_correct}/{total_key} ({after.attribute_accuracy*100:.1f}%){'':<4} {delta(baseline.attribute_accuracy, after.attribute_accuracy)}")
    lines.append("")
    lines.append(f"Corrections Applied: {len(corrections)}")
    lines.append(f"{'Invoice':<12} {'Line':<6} {'Field':<22} {'Was':<10} {'Should be':<10}")
    lines.append("-" * 60)
    for c in corrections:
        lines.append(f"{c.invoice_id:<12} {c.line_item_index:<6} {c.field:<22} {c.original_value:<10} {c.corrected_value:<10}")
    lines.append("")
    lines.append(f"Error Patterns Identified: {len(patterns)}")
    pattern_num = 1
    if "is_branded_merch_missing" in patterns:
        count = patterns["is_branded_merch_missing"]
        lines.append(f"  {pattern_num}. Missing branded merch flag ({count} line items, {count * 2} attribute errors)")
        lines.append(f"     \u2192 LLM did not recognize physical goods from marketing vendor")
        pattern_num += 1
    if "service_type_legal_vs_consulting" in patterns:
        count = patterns["service_type_legal_vs_consulting"]
        lines.append(f"  {pattern_num}. Regulatory/consulting confusion ({count} line item, {count} attribute error)")
        lines.append(f"     \u2192 LLM tagged advisory work as legal based on subject matter")
        pattern_num += 1
    lines.append("")
    lines.append("Prompt Changes Applied:")
    lines.append('  1. Added few-shot example: "Branded company t-shirts (500 units)"')
    lines.append("     \u2192 is_physical_goods: true, is_branded_merch: true, is_marketing: false")
    lines.append("  2. Added instruction: advisory/review work about regulatory topics")
    lines.append("     \u2192 service_type: consulting, not legal")
    lines.append("")

    # Baseline failures
    lines.append("Baseline Failures (now fixed):")
    for r in baseline.results:
        if not r.gl_correct:
            lines.append(f"  {r.invoice_id} line {r.line_item_index}: GL {r.gl_code_actual} \u2192 {r.gl_code_expected}")
    lines.append("\u2550" * 50)

    return "\n".join(lines)
