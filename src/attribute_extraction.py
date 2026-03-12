"""LLM attribute extraction using Anthropic API with structured output."""

import json
import logging
import os
import time

import anthropic

from src.models import ExtractedAttributes, Invoice, LineItem
from src.prompts import SYSTEM_PROMPT_INITIAL, format_user_message

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"

EXTRACTION_TOOL = {
    "name": "extract_attributes",
    "description": "Extract structured attributes from a line item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_physical_goods": {"type": "boolean", "description": "Tangible/physical item"},
            "is_branded_merch": {"type": "boolean", "description": "Branded merchandise (subset of physical goods)"},
            "is_equipment": {"type": "boolean", "description": "Hardware, machines, devices"},
            "unit_cost_extracted": {"type": ["number", "null"], "description": "Per-unit cost parsed from description, or null"},
            "is_software": {"type": "boolean", "description": "Software license, SaaS, platform fee"},
            "is_cloud_hosting": {"type": "boolean", "description": "Cloud hosting/infrastructure"},
            "service_type": {
                "type": ["string", "null"],
                "enum": ["legal", "consulting", "mixed_legal", None],
                "description": "Type of professional service, or null",
            },
            "is_marketing": {"type": "boolean", "description": "Line item is marketing activity"},
            "category_hint": {
                "type": ["string", "null"],
                "enum": ["travel", "facilities", "training", "telecom", "insurance", "recruiting", "catering", None],
                "description": "Category hint for other items",
            },
            "billing_frequency": {
                "type": ["string", "null"],
                "enum": ["monthly", "annual", "one_time", "usage_based", None],
                "description": "Billing frequency",
            },
            "service_period_start": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD or null"},
            "service_period_end": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD or null"},
            "confidence": {"type": "number", "description": "0.0-1.0 confidence score"},
            "reasoning": {"type": "string", "description": "One sentence explaining key decisions"},
        },
        "required": [
            "is_physical_goods", "is_branded_merch", "is_equipment",
            "is_software", "is_cloud_hosting", "service_type",
            "is_marketing", "category_hint", "billing_frequency",
            "service_period_start", "service_period_end",
            "confidence", "reasoning",
        ],
    },
}


def extract_attributes(
    line_item: LineItem,
    invoice: Invoice,
    client: anthropic.Anthropic,
    system_prompt: str | None = None,
) -> ExtractedAttributes:
    model = os.environ.get("AP_AGENT_MODEL", DEFAULT_MODEL)
    prompt = system_prompt or SYSTEM_PROMPT_INITIAL
    user_msg = format_user_message(invoice, line_item)

    last_error = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=0.0,
                system=prompt,
                messages=[{"role": "user", "content": user_msg}],
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "extract_attributes"},
            )

            for block in response.content:
                if block.type == "tool_use":
                    data = block.input
                    attrs = ExtractedAttributes(**data)
                    attrs = _validate_invariants(attrs)
                    return attrs

            raise ValueError("No tool_use block in response")

        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
            last_error = e
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"Transient API error (attempt {attempt + 1}/3), retrying in {wait}s: {e}")
                time.sleep(wait)
                continue
            raise

        except (anthropic.APIError, ValueError, Exception) as e:
            logger.error(f"Non-retryable extraction error: {e}")
            raise

    raise last_error


def _validate_invariants(attrs: ExtractedAttributes) -> ExtractedAttributes:
    if attrs.is_branded_merch and not attrs.is_physical_goods:
        logger.warning("Invariant fix: is_branded_merch=True requires is_physical_goods=True")
        attrs.is_physical_goods = True
    if attrs.is_equipment and not attrs.is_physical_goods:
        logger.warning("Invariant fix: is_equipment=True requires is_physical_goods=True")
        attrs.is_physical_goods = True
    return attrs


def resolve_unit_cost(line_item: LineItem, attrs: ExtractedAttributes) -> float:
    # Precedence chain: explicit → computed → extracted → amount
    if line_item.unit_cost is not None:
        return line_item.unit_cost
    if line_item.quantity > 1:
        return line_item.amount / line_item.quantity
    if attrs.unit_cost_extracted is not None:
        return attrs.unit_cost_extracted
    return line_item.amount
