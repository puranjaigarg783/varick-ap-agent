"""Tool schemas and handlers for the AP agent."""

import logging

from pydantic import BaseModel

from src.approval import approve, route_approval
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
    ClassificationResult,
    ExtractedAttributes,
    Invoice,
    JournalEntry,
    POMatchResult,
    ApprovalRecord,
)
from src.po_matching import match_po
from src.treatment import determine_treatment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Processing context — mutable state container shared across tool calls
# ---------------------------------------------------------------------------

class LineProcessingResult(BaseModel):
    attributes: ExtractedAttributes
    unit_cost: float
    classification: ClassificationResult
    treatment_applied: bool = False


class ProcessingContext:
    def __init__(self, invoice: Invoice, db, mode: str = "normal"):
        self.invoice = invoice
        self.db = db
        self.mode = mode
        self.po_result: POMatchResult | None = None
        self.line_results: dict[int, LineProcessingResult] = {}
        self.journal_entries: list[JournalEntry] = []
        self.approval: ApprovalRecord | None = None
        self.flags: list[str] = []
        self.status: str = "received"
        self.completed: bool = False


# ---------------------------------------------------------------------------
# Tool schemas — for the Anthropic API `tools` parameter
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "lookup_purchase_order",
        "description": "Look up a purchase order by number and validate the amount against the invoice total within 10% tolerance. Call this first for every invoice. If the invoice has no PO number, call flag_for_review instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {
                    "type": "string",
                    "description": "The purchase order number from the invoice",
                }
            },
            "required": ["po_number"],
        },
    },
    {
        "name": "classify_line_item",
        "description": "Classify a line item using the extracted attributes you have determined. You provide what the line item IS (physical goods, equipment, software, etc.) and the rule engine determines the correct GL account. You NEVER determine the GL code yourself — this tool does that. Call this once per line item after analyzing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_item_index": {
                    "type": "integer",
                    "description": "Zero-based index of the line item in the invoice",
                },
                "is_physical_goods": {
                    "type": "boolean",
                    "description": "True if this is a tangible physical item: supplies, stationery, toner, merch, monitors, etc.",
                },
                "is_branded_merch": {
                    "type": "boolean",
                    "description": "True if this is branded merchandise: t-shirts, swag, gift bags with company branding. Must be a subset of physical goods.",
                },
                "is_equipment": {
                    "type": "boolean",
                    "description": "True if this is hardware, machines, or devices: laptops, servers, monitors. Equipment is a subset of physical goods.",
                },
                "is_software": {
                    "type": "boolean",
                    "description": "True if this is a software license, SaaS subscription, or platform fee",
                },
                "is_cloud_hosting": {
                    "type": "boolean",
                    "description": "True if this is cloud infrastructure: AWS, Azure, GCP, Cloudflare, hosting",
                },
                "service_type": {
                    "type": ["string", "null"],
                    "enum": ["legal", "consulting", "mixed_legal", None],
                    "description": "For professional services only. 'legal' = direct legal actions (litigation, patent filing, contract drafting, regulatory filing). 'consulting' = advisory, review, strategy, assessment, implementation, creative/design services — includes advisory work ABOUT legal/regulatory topics. 'mixed_legal' = engagement contains both. null = not a professional service.",
                },
                "is_marketing": {
                    "type": "boolean",
                    "description": "True if the LINE ITEM itself is marketing activity: ad spend, campaigns, sponsorships, booth rentals, agency management fees. Assess the line item content, NOT the invoice department. Physical goods from marketing vendors/departments are NOT marketing.",
                },
                "category_hint": {
                    "type": ["string", "null"],
                    "enum": ["travel", "facilities", "training", "telecom", "insurance", "recruiting", "catering", None],
                    "description": "For items that don't match the categories above. null if no category applies.",
                },
                "billing_frequency": {
                    "type": ["string", "null"],
                    "enum": ["monthly", "annual", "one_time", "usage_based", None],
                    "description": "How this item is billed. 'annual' = yearly upfront payment. 'monthly' = recurring monthly. 'one_time' = single deliverable. 'usage_based' = pay per use. null if unclear.",
                },
                "service_period_start": {
                    "type": ["string", "null"],
                    "description": "ISO date YYYY-MM-DD. Only provide when the text contains a specific date range, named month, or named quarter. Never fabricate dates. null if no period stated.",
                },
                "service_period_end": {
                    "type": ["string", "null"],
                    "description": "ISO date YYYY-MM-DD. Same rules as service_period_start.",
                },
                "unit_cost": {
                    "type": "number",
                    "description": "Per-unit cost for this line item. If the invoice specifies quantity and amount, compute amount/quantity. If the description mentions per-unit pricing (e.g., '3x $1,800'), use that. Otherwise, use the line item amount.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Your confidence in these attributes, 0.0 to 1.0. Below 0.7 means you are unsure.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One sentence explaining your key attribute decisions for this line item.",
                },
            },
            "required": [
                "line_item_index", "is_physical_goods", "is_branded_merch", "is_equipment",
                "is_software", "is_cloud_hosting", "service_type", "is_marketing",
                "category_hint", "billing_frequency", "service_period_start",
                "service_period_end", "unit_cost", "confidence", "reasoning",
            ],
        },
    },
    {
        "name": "apply_treatment",
        "description": "Check if a classified line item needs prepaid or accrual treatment. Call this after classify_line_item for each line item. The tool checks if the billing is annual (prepaid) or if the service period ended before the invoice date (accrual).",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_item_index": {
                    "type": "integer",
                    "description": "Zero-based index of the line item (must have been classified already)",
                }
            },
            "required": ["line_item_index"],
        },
    },
    {
        "name": "generate_journal_entries",
        "description": "Generate journal entries for all classified line items. Call this once after all line items have been classified and treatment-checked.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "verify_balance",
        "description": "Verify that the generated journal entries balance against the invoice total. Call this after generate_journal_entries.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "route_approval",
        "description": "Determine the approval routing for this invoice based on the total amount, department, and line item classifications. Call this after balance verification passes.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "flag_for_review",
        "description": "Flag this invoice for manual human review. Use this when: the invoice has no PO number, the PO amount exceeds tolerance, or a line item cannot be classified. Processing stops after this call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the invoice needs review, e.g. 'No purchase order number provided' or 'PO amount exceeds 10% tolerance'",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "name": "complete_processing",
        "description": "Signal that you have finished processing this invoice. Call this as your final action after all line items are classified, entries generated, balance verified, and approval routed.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_lookup_po(tool_input: dict, ctx: ProcessingContext) -> dict:
    agent_po = tool_input.get("po_number")
    response: dict = {}

    if agent_po and agent_po != ctx.invoice.po_number:
        logger.warning(
            f"Agent passed po_number={agent_po!r} but invoice has "
            f"po_number={ctx.invoice.po_number!r}; using invoice value"
        )
        response["warning"] = (
            f"po_number '{agent_po}' does not match invoice "
            f"po_number '{ctx.invoice.po_number}'. Using invoice value."
        )

    result = match_po(ctx.invoice, ctx.db)
    ctx.po_result = result
    if result.matched:
        set_invoice_status(ctx.invoice.invoice_id, "po_matched", ctx.db)
    response.update({
        "matched": result.matched,
        "reason": result.reason,
        "po_amount": result.po_amount,
        "tolerance_pct": result.tolerance_pct,
    })
    return response


def _validate_invariants(attrs: ExtractedAttributes) -> ExtractedAttributes:
    if attrs.is_branded_merch and not attrs.is_physical_goods:
        logger.warning("Invariant fix: is_branded_merch=True requires is_physical_goods=True")
        attrs.is_physical_goods = True
    if attrs.is_equipment and not attrs.is_physical_goods:
        logger.warning("Invariant fix: is_equipment=True requires is_physical_goods=True")
        attrs.is_physical_goods = True
    return attrs


def _resolve_unit_cost(line_item, agent_unit_cost: float) -> float:
    """Resolve unit cost with structured data taking precedence over agent value."""
    if line_item.unit_cost is not None:
        return line_item.unit_cost
    if line_item.quantity > 1:
        return line_item.amount / line_item.quantity
    if agent_unit_cost is not None:
        return agent_unit_cost
    return line_item.amount


def _handle_classify_line_item(tool_input: dict, ctx: ProcessingContext) -> dict:
    idx = tool_input["line_item_index"]
    invoice = ctx.invoice

    if idx < 0 or idx >= len(invoice.line_items):
        return {"error": f"line_item_index {idx} is out of range (invoice has {len(invoice.line_items)} line items)"}

    line_item = invoice.line_items[idx]

    # Construct ExtractedAttributes from agent's parameters
    attrs = ExtractedAttributes(
        is_physical_goods=tool_input["is_physical_goods"],
        is_branded_merch=tool_input["is_branded_merch"],
        is_equipment=tool_input["is_equipment"],
        unit_cost_extracted=tool_input.get("unit_cost"),
        is_software=tool_input["is_software"],
        is_cloud_hosting=tool_input["is_cloud_hosting"],
        service_type=tool_input["service_type"],
        is_marketing=tool_input["is_marketing"],
        category_hint=tool_input["category_hint"],
        billing_frequency=tool_input["billing_frequency"],
        service_period_start=tool_input["service_period_start"],
        service_period_end=tool_input["service_period_end"],
        confidence=tool_input["confidence"],
        reasoning=tool_input["reasoning"],
    )

    # Validate invariants
    attrs = _validate_invariants(attrs)

    # Resolve unit cost — structured data takes precedence
    unit_cost = _resolve_unit_cost(line_item, tool_input.get("unit_cost", line_item.amount))

    # Classify
    classification = classify_line_item(attrs, unit_cost)

    # Store in context
    ctx.line_results[idx] = LineProcessingResult(
        attributes=attrs,
        unit_cost=unit_cost,
        classification=classification,
        treatment_applied=False,
    )

    # Store in DB
    store_attributes(invoice.invoice_id, idx, attrs, ctx.db)
    store_classification(invoice.invoice_id, idx, classification, ctx.db)

    # Flags
    if attrs.confidence < 0.7:
        ctx.flags.append(f"low_confidence_line:{idx}")

    if classification.gl_code == "UNCLASSIFIED":
        ctx.flags.append(f"unclassifiable_line:{idx}")

    return {
        "line_item_index": idx,
        "gl_code": classification.gl_code,
        "gl_name": classification.gl_name,
        "rule_triggered": classification.rule_triggered,
        "treatment": classification.treatment,
        "amortization_months": classification.amortization_months,
    }


def _handle_apply_treatment(tool_input: dict, ctx: ProcessingContext) -> dict:
    idx = tool_input["line_item_index"]

    if idx not in ctx.line_results:
        return {"error": f"line_item_index {idx} has not been classified yet"}

    lr = ctx.line_results[idx]
    updated = determine_treatment(lr.attributes, lr.classification, ctx.invoice)

    # Update context
    lr.classification = updated
    lr.treatment_applied = True

    # Update DB
    store_classification(ctx.invoice.invoice_id, idx, updated, ctx.db)

    return {
        "line_item_index": idx,
        "gl_code": updated.gl_code,
        "gl_name": updated.gl_name,
        "treatment": updated.treatment,
        "accrual_type": updated.accrual_type,
        "prepaid_expense_target": updated.prepaid_expense_target,
        "amortization_months": updated.amortization_months,
    }


def _handle_generate_entries(tool_input: dict, ctx: ProcessingContext) -> dict:
    all_entries = []
    for idx in sorted(ctx.line_results.keys()):
        lr = ctx.line_results[idx]
        line_item = ctx.invoice.line_items[idx]
        entries = generate_journal_entries(
            ctx.invoice, line_item, idx, lr.classification, lr.attributes
        )
        all_entries.extend(entries)

    ctx.journal_entries = all_entries

    immediate = [e for e in all_entries if e.status == "immediate" and not e.is_reversal]
    scheduled = [e for e in all_entries if e.status == "scheduled"]
    reversals = [e for e in all_entries if e.is_reversal]

    return {
        "entries_generated": len(all_entries),
        "immediate_bookings": len(immediate),
        "scheduled_amortizations": len(scheduled),
        "reversals": len(reversals),
        "total_immediate_amount": sum(e.amount for e in immediate),
    }


def _handle_verify_balance(tool_input: dict, ctx: ProcessingContext) -> dict:
    balanced = verify_balance(ctx.invoice, ctx.journal_entries)
    immediate = [e for e in ctx.journal_entries if e.status == "immediate" and not e.is_reversal]
    computed_total = sum(e.amount for e in immediate)

    if not balanced:
        ctx.flags.append("balance_check_failed")

    return {
        "balanced": balanced,
        "invoice_total": ctx.invoice.total,
        "computed_total": round(computed_total, 2),
    }


def _handle_route_approval(tool_input: dict, ctx: ProcessingContext) -> dict:
    classifications = [ctx.line_results[i].classification for i in sorted(ctx.line_results.keys())]
    approval_record = route_approval(ctx.invoice, classifications)
    ctx.approval = approval_record

    store_approval(approval_record, ctx.db)

    return {
        "required_level": approval_record.required_level,
        "routing_reason": approval_record.routing_reason,
        "override_applied": approval_record.override_applied,
    }


def _handle_flag_for_review(tool_input: dict, ctx: ProcessingContext) -> dict:
    reason = tool_input["reason"]
    ctx.flags.append(reason)
    ctx.status = "flagged_for_review"
    ctx.completed = True
    set_invoice_status(ctx.invoice.invoice_id, "flagged_for_review", ctx.db)
    return {"status": "flagged_for_review", "reason": reason}


def _handle_complete_processing(tool_input: dict, ctx: ProcessingContext) -> dict:
    # Verify all steps were completed (skip if flagged for review)
    if ctx.status != "flagged_for_review":
        expected = len(ctx.invoice.line_items)
        classified = len(ctx.line_results)
        if classified < expected:
            return {
                "error": f"Only {classified}/{expected} line items classified. "
                "Classify all line items before completing."
            }
        untreated = [i for i, lr in ctx.line_results.items() if not lr.treatment_applied]
        if untreated:
            return {
                "error": f"Line items {untreated} have not had treatment applied. "
                "Call apply_treatment for each classified line item."
            }
        if not ctx.journal_entries:
            return {
                "error": "No journal entries generated. "
                "Call generate_journal_entries before completing."
            }

    # Store entries
    store_entries(ctx.journal_entries, ctx.db, posted=False)

    # Set pending_approval first
    set_invoice_status(ctx.invoice.invoice_id, "pending_approval", ctx.db)

    # Approval gate
    if ctx.mode == "auto" or (ctx.approval and ctx.approval.required_level == "auto_approve"):
        approve(ctx.invoice.invoice_id, "system", ctx.db)
        ctx.status = "posted"
    elif ctx.mode == "shadow":
        ctx.status = "shadow_complete"
    else:
        ctx.status = "pending_approval"

    ctx.completed = True
    return {"status": ctx.status}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "lookup_purchase_order": _handle_lookup_po,
    "classify_line_item": _handle_classify_line_item,
    "apply_treatment": _handle_apply_treatment,
    "generate_journal_entries": _handle_generate_entries,
    "verify_balance": _handle_verify_balance,
    "route_approval": _handle_route_approval,
    "flag_for_review": _handle_flag_for_review,
    "complete_processing": _handle_complete_processing,
}


def execute_tool(tool_name: str, tool_input: dict, ctx: ProcessingContext) -> dict:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return handler(tool_input, ctx)
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {e}")
        return {"error": str(e)}
