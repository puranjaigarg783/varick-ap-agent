"""Eval system — runs pipeline on labeled invoices and compares against ground truth."""

import sqlite3
from datetime import datetime, timezone

import anthropic

from eval.labels import LABELS
from src.db import get_extracted_attributes, get_line_item_classifications, get_approval
from src.models import EvalReport, EvalResult, Invoice
from src.pipeline import process_invoice


def run_eval(
    invoices: list[Invoice],
    db: sqlite3.Connection,
    client: anthropic.Anthropic,
    system_prompt: str | None = None,
) -> EvalReport:
    results = []
    failure_summary: dict[str, int] = {}
    total_key_attrs = 0
    correct_key_attrs = 0
    correct_gl = 0
    correct_treatment = 0
    correct_approval = 0
    total_line_items = 0
    total_invoices_with_approval = 0

    for invoice in invoices:
        inv_id = invoice.invoice_id
        if inv_id not in LABELS:
            continue

        label = LABELS[inv_id]

        # Handle INV-006 special case (expected flag, no classification)
        if "expected_flag" in label:
            proc_result = process_invoice(invoice, db, client, mode="auto", system_prompt=system_prompt)
            if label["expected_flag"] in (proc_result.flags or []) or proc_result.status == "flagged_for_review":
                pass  # Flag correctly raised
            else:
                key = "missing_expected_flag"
                failure_summary[key] = failure_summary.get(key, 0) + 1
            continue

        # Process the invoice
        proc_result = process_invoice(invoice, db, client, mode="auto", system_prompt=system_prompt)

        # Get classifications from DB
        classifications = get_line_item_classifications(inv_id, db)
        approval = get_approval(inv_id, db)

        # Get expected approval level (same for all lines in an invoice)
        first_line_label = next(iter(label.values()))
        expected_approval = first_line_label.get("approval", "")
        actual_approval = approval.required_level if approval else "unknown"
        approval_correct = actual_approval == expected_approval
        if approval_correct:
            correct_approval += 1
        total_invoices_with_approval += 1

        for line_idx, line_label in label.items():
            if not isinstance(line_idx, int):
                continue

            total_line_items += 1
            expected_gl = line_label["gl_code"]
            expected_treatment = line_label["treatment"]

            actual_gl = "unknown"
            actual_treatment = "unknown"
            if line_idx < len(classifications) and classifications[line_idx] is not None:
                actual_gl = classifications[line_idx].gl_code
                actual_treatment = classifications[line_idx].treatment

            gl_correct = actual_gl == expected_gl
            treatment_correct = actual_treatment == expected_treatment

            if gl_correct:
                correct_gl += 1
            else:
                key = f"gl_mismatch_{expected_gl}_got_{actual_gl}"
                failure_summary[key] = failure_summary.get(key, 0) + 1

            if treatment_correct:
                correct_treatment += 1

            # Check key attributes
            attribute_errors = []
            attrs = get_extracted_attributes(inv_id, line_idx, db)
            if attrs and "key_attributes" in line_label:
                for attr_name, expected_val in line_label["key_attributes"].items():
                    total_key_attrs += 1
                    actual_val = getattr(attrs, attr_name, None)
                    if actual_val == expected_val:
                        correct_key_attrs += 1
                    else:
                        attribute_errors.append(f"{attr_name}: expected {expected_val}, got {actual_val}")
                        key = f"{attr_name}_mismatch"
                        failure_summary[key] = failure_summary.get(key, 0) + 1

            results.append(EvalResult(
                invoice_id=inv_id,
                line_item_index=line_idx,
                gl_code_expected=expected_gl,
                gl_code_actual=actual_gl,
                gl_correct=gl_correct,
                treatment_expected=expected_treatment,
                treatment_actual=actual_treatment,
                treatment_correct=treatment_correct,
                approval_expected=expected_approval,
                approval_actual=actual_approval,
                approval_correct=approval_correct,
                attribute_errors=attribute_errors,
            ))

    gl_accuracy = correct_gl / total_line_items if total_line_items > 0 else 0.0
    treatment_accuracy = correct_treatment / total_line_items if total_line_items > 0 else 0.0
    approval_accuracy = correct_approval / total_invoices_with_approval if total_invoices_with_approval > 0 else 0.0
    attribute_accuracy = correct_key_attrs / total_key_attrs if total_key_attrs > 0 else 0.0

    return EvalReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_line_items=total_line_items,
        gl_accuracy=gl_accuracy,
        treatment_accuracy=treatment_accuracy,
        approval_accuracy=approval_accuracy,
        attribute_accuracy=attribute_accuracy,
        results=results,
        failure_summary=failure_summary,
    )
