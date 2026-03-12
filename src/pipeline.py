"""Pipeline orchestrator — wires all steps together."""

import logging
import sqlite3

import anthropic

from src.approval import approve, route_approval
from src.attribute_extraction import extract_attributes, resolve_unit_cost
from src.classification import classify_line_item
from src.db import (
    set_invoice_status,
    store_approval,
    store_attributes,
    store_classification,
    store_entries,
)
from src.journal import generate_journal_entries, verify_balance
from src.models import (
    ApprovalRecord,
    ClassificationResult,
    Invoice,
    InvoiceProcessingResult,
)
from src.po_matching import match_po
from src.treatment import determine_treatment

logger = logging.getLogger(__name__)


def process_invoice(
    invoice: Invoice,
    db: sqlite3.Connection,
    client: anthropic.Anthropic,
    mode: str = "normal",
    system_prompt: str | None = None,
) -> InvoiceProcessingResult:
    db.execute("BEGIN")
    try:
        result = _process_invoice_inner(invoice, db, client, mode, system_prompt)

        if mode == "dry_run":
            db.execute("ROLLBACK")
            result.status = "dry_run_complete"
        else:
            db.execute("COMMIT")

        return result

    except Exception as e:
        db.execute("ROLLBACK")
        try:
            db.execute("BEGIN")
            set_invoice_status(invoice.invoice_id, "received", db)
            db.execute("COMMIT")
        except Exception:
            pass
        raise


def _process_invoice_inner(
    invoice: Invoice,
    db: sqlite3.Connection,
    client: anthropic.Anthropic,
    mode: str,
    system_prompt: str | None,
) -> InvoiceProcessingResult:
    flags = []

    # Step 1: PO Matching
    po_result = match_po(invoice, db)
    if not po_result.matched:
        set_invoice_status(invoice.invoice_id, "flagged_for_review", db)
        flags.append(po_result.reason)
        return InvoiceProcessingResult(
            status="flagged_for_review",
            flags=flags,
            error=f"PO match failed: {po_result.reason}",
        )

    set_invoice_status(invoice.invoice_id, "po_matched", db)

    # Step 2: Attribute Extraction + Classification (per line item)
    all_classifications = []
    all_entries = []
    has_unclassifiable = False

    for i, line_item in enumerate(invoice.line_items):
        try:
            attrs = extract_attributes(line_item, invoice, client, system_prompt)
        except Exception as e:
            logger.error(f"Extraction failed for {invoice.invoice_id} line {i}: {e}")
            flags.append(f"extraction_failed")
            set_invoice_status(invoice.invoice_id, "flagged_for_review", db)
            return InvoiceProcessingResult(
                status="flagged_for_review",
                flags=flags,
                error=f"LLM extraction failed: {e}",
            )

        store_attributes(invoice.invoice_id, i, attrs, db)

        if attrs.confidence < 0.7:
            flags.append(f"low_confidence_line:{i}")

        # Resolve unit cost
        unit_cost = resolve_unit_cost(line_item, attrs)

        # Classification
        classification = classify_line_item(attrs, unit_cost)

        # Treatment override
        classification = determine_treatment(attrs, classification, invoice)

        store_classification(invoice.invoice_id, i, classification, db)
        all_classifications.append(classification)

        if classification.gl_code == "UNCLASSIFIED":
            has_unclassifiable = True
            flags.append(f"unclassifiable_line:{i}")

        # Journal entries
        entries = generate_journal_entries(invoice, line_item, i, classification, attrs)
        all_entries.extend(entries)

    if has_unclassifiable:
        set_invoice_status(invoice.invoice_id, "flagged_for_review", db)

    set_invoice_status(invoice.invoice_id, "classified", db)

    # Step 3: Balance verification
    if not verify_balance(invoice, all_entries):
        flags.append("balance_check_failed")
        logger.warning(f"Balance check failed for {invoice.invoice_id}")

    # Step 4: Approval routing
    approval = route_approval(invoice, all_classifications)
    store_approval(approval, db)

    # Step 5: Store entries with posted=0
    store_entries(all_entries, db, posted=False)

    # Step 6: Approval gate
    set_invoice_status(invoice.invoice_id, "pending_approval", db)

    if mode == "auto" or approval.required_level == "auto_approve":
        approve(invoice.invoice_id, "system", db)
        return InvoiceProcessingResult(
            status="posted",
            entries=all_entries,
            approval=approval,
            flags=flags,
        )
    elif mode == "shadow":
        return InvoiceProcessingResult(
            status="shadow_complete",
            entries=all_entries,
            approval=approval,
            flags=flags,
        )
    else:
        return InvoiceProcessingResult(
            status="pending_approval",
            entries=all_entries,
            approval=approval,
            flags=flags,
        )
