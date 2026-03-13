"""Agent loop — LLM orchestrates invoice processing via tool calls."""

import json
import logging
import os
import time

import anthropic

from src.db import set_invoice_status, store_conversation_trace
from src.models import Invoice, InvoiceProcessingResult
from src.prompts import format_invoice_for_agent, get_system_prompt
from src.tools import TOOL_SCHEMAS, ProcessingContext, execute_tool

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
MAX_ITERATIONS = 30
MAX_CONSECUTIVE_ERRORS = 3


def process_invoice(
    invoice: Invoice,
    db,
    client: anthropic.Anthropic,
    mode: str = "normal",
    system_prompt: str | None = None,
) -> InvoiceProcessingResult:
    """Process an invoice through the agent loop. Same signature as the old pipeline."""
    db.execute("BEGIN")
    try:
        result = _run_agent(invoice, db, client, mode, system_prompt)

        if mode == "dry_run":
            db.execute("ROLLBACK")
            result.status = "dry_run_complete"
        else:
            db.execute("COMMIT")

        return result

    except Exception:
        db.execute("ROLLBACK")
        try:
            db.execute("BEGIN")
            set_invoice_status(invoice.invoice_id, "received", db)
            db.execute("COMMIT")
        except Exception:
            pass
        raise


def _run_agent(
    invoice: Invoice,
    db,
    client: anthropic.Anthropic,
    mode: str,
    system_prompt: str | None,
) -> InvoiceProcessingResult:
    ctx = ProcessingContext(invoice=invoice, db=db, mode=mode)

    # Pre-flight: reject invoices with no line items
    if not invoice.line_items:
        ctx.status = "error"
        ctx.flags.append("no_line_items")
        set_invoice_status(invoice.invoice_id, "error", db)
        return InvoiceProcessingResult(
            status="error",
            entries=[],
            approval=None,
            flags=ctx.flags,
            error="Invoice has no line items to process",
        )

    prompt = system_prompt or get_system_prompt()
    model = os.environ.get("AP_AGENT_MODEL", DEFAULT_MODEL)

    user_message = format_invoice_for_agent(invoice)
    messages = [{"role": "user", "content": user_message}]

    set_invoice_status(invoice.invoice_id, "received", db)

    tool_calls_count = 0
    consecutive_errors: dict[str, int] = {}

    for iteration in range(MAX_ITERATIONS):
        # API call with retry
        response = _call_api_with_retry(client, model, prompt, messages)

        # Append assistant response
        assistant_content = _serialize_content(response.content)
        messages.append({"role": "assistant", "content": assistant_content})

        # Check if the agent is done (no tool calls, just text)
        if response.stop_reason == "end_turn":
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls_count += 1
                result = execute_tool(block.name, block.input, ctx)

                # Track consecutive errors per tool
                if "error" in result:
                    consecutive_errors[block.name] = consecutive_errors.get(block.name, 0) + 1
                    if consecutive_errors[block.name] >= MAX_CONSECUTIVE_ERRORS:
                        # Force flag for review
                        from src.tools import _handle_flag_for_review
                        _handle_flag_for_review(
                            {"reason": f"Tool {block.name} failed {MAX_CONSECUTIVE_ERRORS} consecutive times: {result['error']}"},
                            ctx,
                        )
                else:
                    consecutive_errors[block.name] = 0

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if ctx.completed:
            break
    else:
        # Max iterations hit
        ctx.status = "error"
        ctx.flags.append("agent_exceeded_max_iterations")
        set_invoice_status(invoice.invoice_id, "error", db)

    # Store conversation trace
    store_conversation_trace(
        invoice.invoice_id, messages, tool_calls_count, iteration + 1, db
    )

    return InvoiceProcessingResult(
        status=ctx.status,
        entries=ctx.journal_entries,
        approval=ctx.approval,
        flags=ctx.flags,
        error="agent_exceeded_max_iterations" if ctx.status == "error" else None,
    )


def _call_api_with_retry(client, model, system_prompt, messages):
    """Call the Anthropic API with retry for transient errors."""
    last_error = None
    for attempt in range(3):
        try:
            return client.messages.create(
                model=model,
                max_tokens=4096,
                temperature=0.0,
                system=system_prompt,
                messages=messages,
                tools=TOOL_SCHEMAS,
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
            last_error = e
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"Transient API error (attempt {attempt + 1}/3), retrying in {wait}s: {e}")
                time.sleep(wait)
                continue
            raise
    raise last_error


def _serialize_content(content) -> list:
    """Serialize response content blocks for message history."""
    serialized = []
    for block in content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return serialized
