"""Step 4: Approval routing and human-in-the-loop actions."""

import sqlite3
from datetime import datetime, timezone

from src.models import ApprovalRecord, ClassificationResult, Invoice


def route_approval(invoice: Invoice, line_classifications: list[ClassificationResult]) -> ApprovalRecord:
    # Rule 1: Fixed Asset override
    if any(c.gl_code == "1500" for c in line_classifications):
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="vp_finance",
            routing_reason="Line item classified as Fixed Asset (1500)",
            status="pending",
        )

    # Rule 2: Marketing override
    if invoice.department == "Marketing" and invoice.total <= 2500:
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="auto_approve",
            routing_reason=f"Invoice total ${invoice.total:,.0f} \u2264 $2.5K",
            override_applied="Marketing auto-approve \u2264 $2.5K",
            status="pending",
        )

    # Rule 3: Engineering cloud/software override
    if (
        invoice.department == "Engineering"
        and invoice.total <= 5000
        and all(c.gl_code in {"5010", "5020"} for c in line_classifications)
    ):
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="auto_approve",
            routing_reason=f"Invoice total ${invoice.total:,.0f} \u2264 $5K, all lines Cloud/Software",
            override_applied="Engineering auto-approve \u2264 $5K, all lines Cloud/Software",
            status="pending",
        )

    # Rule 4: Base thresholds
    if invoice.total <= 1000:
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="auto_approve",
            routing_reason=f"Invoice total ${invoice.total:,.0f} \u2264 $1K",
            status="pending",
        )
    elif invoice.total <= 10000:
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="dept_manager",
            routing_reason=f"Invoice total ${invoice.total:,.0f} between $1K\u2013$10K",
            status="pending",
        )
    elif invoice.total > 10000:
        return ApprovalRecord(
            invoice_id=invoice.invoice_id,
            required_level="vp_finance",
            routing_reason=f"Invoice total ${invoice.total:,.0f} > $10K",
            status="pending",
        )

    # Rule 5: Fail closed (unreachable under valid input)
    return ApprovalRecord(
        invoice_id=invoice.invoice_id,
        required_level="denied",
        routing_reason="No matching approval rule \u2014 denied (fail closed)",
        status="pending",
    )


def approve(invoice_id: str, decided_by: str, db: sqlite3.Connection) -> bool:
    status = db.execute("SELECT status FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if status is None or status["status"] != "pending_approval":
        return False

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? WHERE invoice_id = ?",
        (decided_by, now, invoice_id),
    )
    db.execute("UPDATE invoices SET status = 'posted' WHERE invoice_id = ?", (invoice_id,))
    db.execute(
        "UPDATE journal_entries SET posted = 1 WHERE invoice_id = ? AND status = 'immediate'",
        (invoice_id,),
    )
    return True


def reject(invoice_id: str, decided_by: str, reason: str, db: sqlite3.Connection) -> bool:
    status = db.execute("SELECT status FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if status is None or status["status"] != "pending_approval":
        return False

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """UPDATE approvals SET status = 'rejected', decided_by = ?, decided_at = ?,
           rejection_reason = ? WHERE invoice_id = ?""",
        (decided_by, now, reason, invoice_id),
    )
    db.execute("UPDATE invoices SET status = 'rejected' WHERE invoice_id = ?", (invoice_id,))
    return True
