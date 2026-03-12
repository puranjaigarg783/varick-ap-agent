"""Step 1: PO matching — lookup and tolerance validation."""

import sqlite3

from src.models import Invoice, POMatchResult


def match_po(invoice: Invoice, db: sqlite3.Connection) -> POMatchResult:
    if invoice.po_number is None:
        return POMatchResult(matched=False, reason="no_po_provided")

    row = db.execute(
        "SELECT * FROM purchase_orders WHERE po_number = ?", (invoice.po_number,)
    ).fetchone()

    if row is None:
        return POMatchResult(matched=False, reason="po_not_found")

    po_amount = row["amount"]
    tolerance_pct = abs(invoice.total - po_amount) / po_amount

    if tolerance_pct > 0.10:
        return POMatchResult(
            matched=False,
            reason="tolerance_exceeded",
            po_amount=po_amount,
            tolerance_pct=round(tolerance_pct, 4),
        )

    return POMatchResult(
        matched=True,
        po_amount=po_amount,
        tolerance_pct=round(tolerance_pct, 4),
    )
