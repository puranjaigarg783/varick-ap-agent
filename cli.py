#!/usr/bin/env python3
"""CLI entry point for the AP Agent."""

import argparse
import json
import os
import sys

import anthropic

from src.db import (
    create_tables,
    get_all_invoices,
    get_approval,
    get_connection,
    get_conversation_trace,
    get_corrections,
    get_extracted_attributes,
    get_invoice,
    get_invoice_status,
    get_journal_entries,
    get_line_item_classifications,
    load_seed_data,
    store_corrections,
)
from src.models import Correction, Invoice
from src.agent import process_invoice
from src.approval import approve as do_approve, reject as do_reject


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def _print_section(title: str, step: str | None = None):
    print()
    print("\u2550" * 50)
    if step:
        print(f"[{step}] {title}")
    else:
        print(title)
    print("\u2550" * 50)
    print()


def _print_processing_result(invoice: Invoice, result, db):
    print(f"  Invoice: {invoice.invoice_id} | Vendor: {invoice.vendor}")
    print(f"  PO: {invoice.po_number or 'None'} | Dept: {invoice.department} | Total: ${invoice.total:,.2f}")
    print(f"  Status: {result.status}")

    if result.flags:
        print(f"  Flags: {', '.join(result.flags)}")

    if result.error:
        print(f"  Error: {result.error}")
        print()
        return

    # Line item classifications
    classifications = get_line_item_classifications(invoice.invoice_id, db)
    if classifications and any(c is not None for c in classifications):
        print()
        print(f"  {'#':<4} {'Description':<45} {'Amount':>10} {'GL':>6} {'Treatment':<10}")
        print(f"  {'-'*4} {'-'*45} {'-'*10} {'-'*6} {'-'*10}")
        for i, li in enumerate(invoice.line_items):
            c = classifications[i] if i < len(classifications) else None
            gl = c.gl_code if c else "—"
            treat = c.treatment if c else "—"
            desc = li.description[:45]
            print(f"  {i:<4} {desc:<45} ${li.amount:>9,.2f} {gl:>6} {treat:<10}")

    # Approval
    if result.approval:
        print()
        print(f"  Approval: {result.approval.required_level}")
        print(f"  Reason: {result.approval.routing_reason}")
        if result.approval.override_applied:
            print(f"  Override: {result.approval.override_applied}")

    # Journal entries summary
    if result.entries:
        immediate = [e for e in result.entries if e.status == "immediate" and not e.is_reversal]
        scheduled = [e for e in result.entries if e.status == "scheduled"]
        reversals = [e for e in result.entries if e.is_reversal]
        print()
        print(f"  Journal Entries: {len(result.entries)} total")
        print(f"    Immediate bookings: {len(immediate)}")
        if scheduled:
            print(f"    Scheduled (amortization): {len(scheduled)}")
        if reversals:
            print(f"    Reversals (pending payment): {len(reversals)}")

        # Show immediate entries
        print()
        print(f"  {'Date':<12} {'Debit':>6} {'Credit':>6} {'Amount':>12} {'Description':<40}")
        print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*12} {'-'*40}")
        for e in immediate:
            desc = e.description[:40]
            print(f"  {e.date:<12} {e.debit_account:>6} {e.credit_account:>6} ${e.amount:>11,.2f} {desc}")
        if reversals:
            for e in reversals:
                desc = e.description[:40]
                print(f"  {e.date:<12} {e.debit_account:>6} {e.credit_account:>6} ${e.amount:>11,.2f} {desc} [reversal]")
        if scheduled and len(scheduled) <= 4:
            for e in scheduled:
                desc = e.description[:40]
                print(f"  {e.date:<12} {e.debit_account:>6} {e.credit_account:>6} ${e.amount:>11,.2f} {desc}")
        elif scheduled:
            e = scheduled[0]
            desc = e.description[:40]
            print(f"  {e.date:<12} {e.debit_account:>6} {e.credit_account:>6} ${e.amount:>11,.2f} {desc}")
            print(f"  ... ({len(scheduled) - 1} more amortization entries)")

    print()


def _print_eval_report(report):
    total = report.total_line_items
    gl_correct = round(report.gl_accuracy * total)
    treat_correct = round(report.treatment_accuracy * total)

    print(f"  Total line items evaluated: {total}")
    print(f"  GL Code Accuracy:    {gl_correct}/{total} ({report.gl_accuracy * 100:.1f}%)")
    print(f"  Treatment Accuracy:  {treat_correct}/{total} ({report.treatment_accuracy * 100:.1f}%)")
    print(f"  Approval Accuracy:   {report.approval_accuracy * 100:.1f}%")
    print(f"  Attribute Accuracy:  {report.attribute_accuracy * 100:.1f}%")

    # Show failures
    failures = [r for r in report.results if not r.gl_correct]
    if failures:
        print()
        print("  Failures:")
        for r in failures:
            print(f"    {r.invoice_id} line {r.line_item_index}: expected GL {r.gl_code_expected}, got {r.gl_code_actual}")

    attr_errors = [r for r in report.results if r.attribute_errors]
    if attr_errors:
        print()
        print("  Attribute errors:")
        for r in attr_errors:
            for err in r.attribute_errors:
                print(f"    {r.invoice_id} line {r.line_item_index}: {err}")

    print()


def cmd_init_db(args):
    db = get_connection()
    create_tables(db)
    load_seed_data(db)
    count = db.execute("SELECT count(*) FROM invoices").fetchone()[0]
    po_count = db.execute("SELECT count(*) FROM purchase_orders").fetchone()[0]
    print(f"Database initialized: {count} invoices, {po_count} purchase orders loaded.")
    db.close()


def cmd_process(args):
    db = get_connection()
    client = _get_client()
    invoice = get_invoice(args.invoice_id, db)
    result = process_invoice(invoice, db, client, mode=args.mode)
    _print_processing_result(invoice, result, db)
    db.close()


def cmd_process_all(args):
    db = get_connection()
    client = _get_client()
    invoices = get_all_invoices(db)
    for invoice in invoices:
        print(f"Processing {invoice.invoice_id}...")
        result = process_invoice(invoice, db, client, mode=args.mode)
        _print_processing_result(invoice, result, db)
    db.close()


def cmd_approve(args):
    db = get_connection()
    decided_by = args.by or "cli_user"
    success = do_approve(args.invoice_id, decided_by, db)
    if success:
        print(f"Invoice {args.invoice_id} approved by {decided_by}.")
    else:
        print(f"Cannot approve {args.invoice_id} — not in pending_approval status.")
    db.close()


def cmd_reject(args):
    db = get_connection()
    decided_by = args.by or "cli_user"
    success = do_reject(args.invoice_id, decided_by, args.reason, db)
    if success:
        print(f"Invoice {args.invoice_id} rejected by {decided_by}. Reason: {args.reason}")
    else:
        print(f"Cannot reject {args.invoice_id} — not in pending_approval status.")
    db.close()


def cmd_eval(args):
    from eval.runner import run_eval

    db = get_connection()
    client = _get_client()
    invoices = get_all_invoices(db, labeled_only=True)

    # Re-init for clean eval
    create_tables(db)
    load_seed_data(db)

    report = run_eval(invoices, db, client)
    _print_eval_report(report)
    db.close()


def cmd_shadow(args):
    db = get_connection()
    client = _get_client()
    invoices = get_all_invoices(db, unlabeled_only=True)

    _print_section("Shadow Mode — Processing Unlabeled Invoices")
    print("  Processing unlabeled invoices in shadow mode (proposals only, not posted)...")
    print()

    for invoice in invoices:
        print(f"  Processing {invoice.invoice_id} ({invoice.vendor})...")
        result = process_invoice(invoice, db, client, mode="shadow")
        _print_processing_result(invoice, result, db)
    db.close()


def cmd_feedback(args):
    if args.feedback_cmd == "apply-corrections":
        db = get_connection()
        with open(args.file) as f:
            raw = json.load(f)
        corrections = [Correction(**c) for c in raw]
        store_corrections(corrections, db)
        print(f"Applied {len(corrections)} corrections.")
        db.close()

    elif args.feedback_cmd == "analyze":
        from eval.feedback import analyze_corrections

        db = get_connection()
        corrections = get_corrections(db)
        patterns = analyze_corrections(corrections)
        print("Error patterns identified:")
        for pattern, count in patterns.items():
            print(f"  {pattern}: {count}")
        db.close()

    elif args.feedback_cmd == "report":
        from eval.feedback import apply_prompt_refinement, generate_improvement_report
        from eval.runner import run_eval

        db = get_connection()
        client = _get_client()

        # Baseline
        create_tables(db)
        load_seed_data(db)
        invoices = get_all_invoices(db, labeled_only=True)
        baseline = run_eval(invoices, db, client)

        # Get corrections and refined prompt
        corrections = get_corrections(db)
        if not corrections:
            # Load from file if not in DB
            data_dir = os.path.join(os.path.dirname(__file__), "data")
            with open(os.path.join(data_dir, "corrections.json")) as f:
                raw = json.load(f)
            corrections = [Correction(**c) for c in raw]

        refined_prompt = apply_prompt_refinement(corrections)

        # Re-init and run with refined prompt
        create_tables(db)
        load_seed_data(db)
        store_corrections(corrections, db)
        invoices = get_all_invoices(db, labeled_only=True)
        after = run_eval(invoices, db, client, system_prompt=refined_prompt)

        report = generate_improvement_report(baseline, after, corrections)
        print(report)
        db.close()


def cmd_status(args):
    db = get_connection()
    if args.invoice_id:
        invoice_ids = [args.invoice_id]
    else:
        rows = db.execute("SELECT invoice_id FROM invoices ORDER BY invoice_id").fetchall()
        invoice_ids = [r["invoice_id"] for r in rows]

    for inv_id in invoice_ids:
        status = get_invoice_status(inv_id, db)
        print(f"  {inv_id}: {status}")

        approval = get_approval(inv_id, db)
        if approval:
            print(f"    Approval: {approval.required_level} ({approval.status})")
            if approval.decided_by:
                print(f"    Decided by: {approval.decided_by} at {approval.decided_at}")

        entries = get_journal_entries(inv_id, db)
        if entries:
            posted = sum(1 for e in entries if e.status == "immediate")
            print(f"    Journal entries: {len(entries)} ({posted} immediate)")
    print()
    db.close()


def cmd_trace(args):
    db = get_connection()
    trace = get_conversation_trace(args.invoice_id, db)
    if trace is None:
        print(f"No trace found for {args.invoice_id}")
        db.close()
        return

    print(f"Conversation trace for {args.invoice_id}")
    print(f"  Tool calls: {trace['tool_calls_count']}")
    print(f"  Iterations: {trace['iterations']}")
    print(f"  Timestamp: {trace['timestamp']}")
    print()

    messages = json.loads(trace["messages"])
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                print(f"[USER] {content[:200]}{'...' if len(content) > 200 else ''}")
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        result_str = item.get("content", "")
                        try:
                            parsed = json.loads(result_str)
                            print(f"[TOOL RESULT] {json.dumps(parsed, indent=2)[:300]}")
                        except (json.JSONDecodeError, TypeError):
                            print(f"[TOOL RESULT] {result_str[:300]}")
            print()

        elif role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            print(f"[AGENT] {block['text']}")
                        elif block.get("type") == "tool_use":
                            input_str = json.dumps(block.get("input", {}), indent=2)
                            print(f"[TOOL CALL] {block['name']}({input_str[:200]})")
            elif isinstance(content, str):
                print(f"[AGENT] {content}")
            print()

    db.close()


def cmd_demo(args):
    from eval.feedback import apply_prompt_refinement, generate_improvement_report
    from eval.runner import run_eval

    client = _get_client()

    # Step 1: Init DB
    _print_section("Initializing database...", "1/7")
    db = get_connection()
    create_tables(db)
    load_seed_data(db)
    count = db.execute("SELECT count(*) FROM invoices").fetchone()[0]
    print(f"  Loaded {count} invoices.")
    db.close()

    # Step 2: Baseline Eval
    _print_section("Baseline Eval (naive prompt)", "2/7")
    db = get_connection()
    invoices = get_all_invoices(db, labeled_only=True)
    baseline = run_eval(invoices, db, client)
    _print_eval_report(baseline)
    db.close()

    # Step 3: Shadow Mode
    _print_section("Shadow Mode — Unlabeled Invoices", "3/7")
    db = get_connection()
    unlabeled = get_all_invoices(db, unlabeled_only=True)
    for invoice in unlabeled:
        print(f"  Processing {invoice.invoice_id} ({invoice.vendor})...")
        try:
            result = process_invoice(invoice, db, client, mode="shadow")
            _print_processing_result(invoice, result, db)
        except Exception as e:
            print(f"    Error: {e}")
            print()
    db.close()

    # Step 4: Apply Corrections
    _print_section("Applying Corrections", "4/7")
    db = get_connection()
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    with open(os.path.join(data_dir, "corrections.json")) as f:
        raw = json.load(f)
    corrections = [Correction(**c) for c in raw]
    store_corrections(corrections, db)
    print(f"  Applied {len(corrections)} corrections.")
    for c in corrections:
        print(f"    {c.invoice_id} line {c.line_item_index}: {c.field} {c.original_value} -> {c.corrected_value}")
    db.close()

    # Step 5: Error Analysis
    _print_section("Error Analysis", "5/7")
    from eval.feedback import analyze_corrections
    patterns = analyze_corrections(corrections)
    print("  Error patterns identified:")
    for pattern, count in patterns.items():
        print(f"    {pattern}: {count}")

    # Step 6: Refined Eval
    _print_section("Refined Eval (improved prompt)", "6/7")
    db = get_connection()
    create_tables(db)
    load_seed_data(db)
    store_corrections(corrections, db)
    refined_prompt = apply_prompt_refinement(corrections)
    invoices = get_all_invoices(db, labeled_only=True)
    after = run_eval(invoices, db, client, system_prompt=refined_prompt)
    _print_eval_report(after)
    db.close()

    # Step 7: Before/After Report
    _print_section("Before/After Report", "7/7")
    report = generate_improvement_report(baseline, after, corrections)
    print(report)


def main():
    parser = argparse.ArgumentParser(description="AP Agent CLI")
    subparsers = parser.add_subparsers(dest="command")

    # init-db
    sub = subparsers.add_parser("init-db", help="Initialize database with seed data")
    sub.set_defaults(func=cmd_init_db)

    # process
    sub = subparsers.add_parser("process", help="Process a single invoice")
    sub.add_argument("invoice_id", help="Invoice ID to process")
    sub.add_argument("--mode", choices=["normal", "dry_run", "shadow", "auto"], default="normal")
    sub.set_defaults(func=cmd_process)

    # process-all
    sub = subparsers.add_parser("process-all", help="Process all invoices")
    sub.add_argument("--mode", choices=["normal", "dry_run", "shadow", "auto"], default="normal")
    sub.set_defaults(func=cmd_process_all)

    # approve
    sub = subparsers.add_parser("approve", help="Approve a pending invoice")
    sub.add_argument("invoice_id")
    sub.add_argument("--by", help="Approver name", default=None)
    sub.set_defaults(func=cmd_approve)

    # reject
    sub = subparsers.add_parser("reject", help="Reject a pending invoice")
    sub.add_argument("invoice_id")
    sub.add_argument("--reason", required=True, help="Rejection reason")
    sub.add_argument("--by", help="Rejector name", default=None)
    sub.set_defaults(func=cmd_reject)

    # eval
    sub = subparsers.add_parser("eval", help="Run eval suite on labeled invoices")
    sub.set_defaults(func=cmd_eval)

    # shadow
    sub = subparsers.add_parser("shadow", help="Process unlabeled invoices in shadow mode")
    sub.set_defaults(func=cmd_shadow)

    # feedback
    sub = subparsers.add_parser("feedback", help="Feedback loop commands")
    feedback_sub = sub.add_subparsers(dest="feedback_cmd")
    apply_sub = feedback_sub.add_parser("apply-corrections")
    apply_sub.add_argument("file", help="Path to corrections JSON file")
    feedback_sub.add_parser("analyze")
    feedback_sub.add_parser("report")
    sub.set_defaults(func=cmd_feedback)

    # status
    sub = subparsers.add_parser("status", help="Show invoice status")
    sub.add_argument("invoice_id", nargs="?", default=None)
    sub.set_defaults(func=cmd_status)

    # trace
    sub = subparsers.add_parser("trace", help="View agent conversation trace")
    sub.add_argument("invoice_id", help="Invoice ID to view trace for")
    sub.set_defaults(func=cmd_trace)

    # demo
    sub = subparsers.add_parser("demo", help="Run full demo sequence")
    sub.set_defaults(func=cmd_demo)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
