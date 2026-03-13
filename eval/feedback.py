"""Feedback loop — corrections analysis, prompt refinement, improvement reporting."""

from dataclasses import dataclass, field as dc_field

from src.models import Correction, EvalReport
from src.prompts import get_system_prompt, SYSTEM_PROMPT_INITIAL


@dataclass
class CorrectionPattern:
    """A detected pattern of recurring corrections across line items."""

    pattern_id: str
    fields: list[str]
    transitions: dict[str, tuple[str, str]]  # field -> (original, corrected)
    affected_count: int
    affected_invoices: list[str] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Field guidance templates — keyed by (field, original_value, corrected_value)
# ---------------------------------------------------------------------------

_FIELD_GUIDANCE: dict[tuple[str, str, str], str] = {
    ("is_physical_goods", "false", "true"): (
        "Tangible, countable items are physical goods even when ordered by "
        "non-physical-goods departments (e.g., Marketing ordering branded t-shirts)."
    ),
    ("is_branded_merch", "false", "true"): (
        'Branded merchandise (t-shirts, gift bags, swag with company logo) → '
        'is_physical_goods: true, is_branded_merch: true, is_marketing: false. '
        'Physical goods from marketing vendors are NOT marketing activity. '
        'Example: "Branded company t-shirts (500 units)" → '
        "is_physical_goods: true, is_branded_merch: true, is_marketing: false."
    ),
    ("service_type", "legal", "consulting"): (
        'For regulatory compliance review, advisory, or assessment work — even '
        'if it mentions "regulatory" — set service_type to "consulting" unless '
        "the work is litigation, patent filing, or contract drafting."
    ),
    ("service_type", "consulting", "legal"): (
        "Direct legal actions (litigation, patent filing, contract drafting, "
        'regulatory filing) are service_type: "legal", not "consulting".'
    ),
    ("is_marketing", "true", "false"): (
        "Assess the LINE ITEM, not the vendor or department. Physical goods "
        "or professional services from marketing departments are NOT marketing "
        "activity. is_marketing: true is only for ad spend, campaigns, "
        "sponsorships, booth rentals."
    ),
    ("is_marketing", "false", "true"): (
        "Ad spend, campaigns, sponsorships, booth rentals, and agency "
        "management fees are marketing activity (is_marketing: true) regardless "
        "of the vendor name."
    ),
    ("is_equipment", "false", "true"): (
        "Hardware, machines, and devices (laptops, servers, monitors, printers) "
        "are equipment. is_equipment: true always implies is_physical_goods: true."
    ),
    ("is_software", "false", "true"): (
        "Software licenses, SaaS subscriptions, and platform fees are software "
        "(is_software: true) even when bundled with support/maintenance."
    ),
    ("billing_frequency", "one_time", "annual"): (
        'Annual licenses and yearly subscriptions are billing_frequency: "annual" '
        "even when paid in a single invoice."
    ),
    ("is_cloud_hosting", "false", "true"): (
        "Cloud infrastructure (AWS, Azure, GCP, Cloudflare hosting) is "
        "is_cloud_hosting: true, not is_software."
    ),
}


def _build_guidance_for_pattern(pattern: CorrectionPattern) -> str:
    """Build guidance text for a single pattern from its transitions."""
    parts = []
    for fld, (orig, corr) in pattern.transitions.items():
        key = (fld, orig, corr)
        if key in _FIELD_GUIDANCE:
            parts.append(_FIELD_GUIDANCE[key])
        else:
            parts.append(
                f"When {fld} was {orig}, it should have been {corr}."
            )
    return " ".join(parts)


def analyze_corrections(corrections: list[Correction]) -> list[CorrectionPattern]:
    """Group corrections into patterns. Returns list of CorrectionPattern."""
    if not corrections:
        return []

    # Group corrections by (invoice_id, line_item_index)
    grouped: dict[tuple[str, int], list[Correction]] = {}
    for c in corrections:
        key = (c.invoice_id, c.line_item_index)
        grouped.setdefault(key, []).append(c)

    # Build field-set signatures to detect compound patterns
    # signature = frozenset of (field, original, corrected) tuples
    sig_to_lines: dict[frozenset, list[tuple[str, int]]] = {}
    for key, corrs in grouped.items():
        sig = frozenset((c.field, c.original_value, c.corrected_value) for c in corrs)
        sig_to_lines.setdefault(sig, []).append(key)

    patterns: list[CorrectionPattern] = []
    used_lines: set[tuple[str, int]] = set()

    # Detect compound patterns first (multiple fields corrected together)
    for sig, lines in sorted(sig_to_lines.items(), key=lambda x: -len(x[0])):
        if len(sig) < 2:
            continue
        fields = sorted(f for f, _, _ in sig)
        transitions = {f: (orig, corr) for f, orig, corr in sig}
        pattern_id = "+".join(fields)
        invoice_ids = sorted(set(inv for inv, _ in lines))
        patterns.append(CorrectionPattern(
            pattern_id=pattern_id,
            fields=fields,
            transitions=transitions,
            affected_count=len(lines),
            affected_invoices=invoice_ids,
        ))
        used_lines.update(lines)

    # Detect single-field patterns from remaining corrections
    single_field_groups: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
    for key, corrs in grouped.items():
        if key in used_lines:
            continue
        for c in corrs:
            field_key = (c.field, c.original_value, c.corrected_value)
            single_field_groups.setdefault(field_key, []).append(key)

    for (fld, orig, corr), lines in single_field_groups.items():
        invoice_ids = sorted(set(inv for inv, _ in lines))
        patterns.append(CorrectionPattern(
            pattern_id=fld,
            fields=[fld],
            transitions={fld: (orig, corr)},
            affected_count=len(lines),
            affected_invoices=invoice_ids,
        ))

    return patterns


def apply_prompt_refinement(corrections: list[Correction]) -> str:
    """Generate a refined system prompt based on correction patterns."""
    patterns = analyze_corrections(corrections)
    if not patterns:
        return SYSTEM_PROMPT_INITIAL

    refinements = [_build_guidance_for_pattern(p) for p in patterns]
    return get_system_prompt(refinements=refinements)


def generate_improvement_report(
    baseline: EvalReport,
    after: EvalReport,
    corrections: list[Correction],
) -> str:
    """Generate a human-readable before/after comparison."""
    patterns = analyze_corrections(corrections)

    def delta(before: float, after_val: float) -> str:
        diff = (after_val - before) * 100
        if abs(diff) < 0.1:
            return " \u2014"
        return f"+{diff:.1f} pp" if diff > 0 else f"{diff:.1f} pp"

    total_li = baseline.total_line_items

    baseline_gl_correct = round(baseline.gl_accuracy * total_li)
    after_gl_correct = round(after.gl_accuracy * after.total_line_items)
    baseline_treat_correct = round(baseline.treatment_accuracy * total_li)
    after_treat_correct = round(after.treatment_accuracy * after.total_line_items)

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

    # Error patterns — data-driven
    lines.append(f"Error Patterns Identified: {len(patterns)}")
    for i, p in enumerate(patterns, 1):
        field_str = " + ".join(p.fields)
        transition_parts = []
        for fld, (orig, corr) in p.transitions.items():
            transition_parts.append(f"{fld}: {orig} \u2192 {corr}")
        transition_str = ", ".join(transition_parts)
        lines.append(f"  {i}. {field_str} ({p.affected_count} line items across {', '.join(p.affected_invoices)})")
        lines.append(f"     \u2192 {transition_str}")
    lines.append("")

    # Prompt changes — dynamically generated
    refinements = [_build_guidance_for_pattern(p) for p in patterns]
    lines.append("Prompt Changes Applied:")
    for i, r in enumerate(refinements, 1):
        lines.append(f"  {i}. {r}")
    lines.append("")

    # Baseline failures
    lines.append("Baseline Failures (now fixed):")
    for r in baseline.results:
        if not r.gl_correct:
            lines.append(f"  {r.invoice_id} line {r.line_item_index}: GL {r.gl_code_actual} \u2192 {r.gl_code_expected}")
    lines.append("\u2550" * 50)

    return "\n".join(lines)
