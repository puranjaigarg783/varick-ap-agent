"""SQLite setup, seed data loading, and query helpers."""

import json
import os
import sqlite3
from datetime import datetime, timezone

from src.models import (
    ApprovalRecord,
    ClassificationResult,
    Correction,
    ExtractedAttributes,
    Invoice,
    JournalEntry,
    LineItem,
)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ap_agent.db")


def get_connection(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = sqlite3.Row
    return db


def create_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
        DROP TABLE IF EXISTS corrections;
        DROP TABLE IF EXISTS approvals;
        DROP TABLE IF EXISTS journal_entries;
        DROP TABLE IF EXISTS line_items;
        DROP TABLE IF EXISTS invoices;
        DROP TABLE IF EXISTS purchase_orders;

        CREATE TABLE purchase_orders (
            po_number TEXT PRIMARY KEY,
            vendor TEXT NOT NULL,
            amount REAL NOT NULL,
            department TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        CREATE TABLE invoices (
            invoice_id TEXT PRIMARY KEY,
            vendor TEXT NOT NULL,
            po_number TEXT,
            date TEXT NOT NULL,
            department TEXT NOT NULL,
            total REAL NOT NULL,
            service_period_start TEXT,
            service_period_end TEXT,
            status TEXT NOT NULL DEFAULT 'received',
            raw_json TEXT NOT NULL
        );

        CREATE TABLE line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            line_index INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_cost REAL,
            extracted_attributes TEXT,
            gl_code TEXT,
            gl_name TEXT,
            rule_triggered TEXT,
            treatment TEXT,
            classification_json TEXT,
            UNIQUE(invoice_id, line_index)
        );

        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            line_item_index INTEGER NOT NULL,
            date TEXT NOT NULL,
            debit_account TEXT NOT NULL,
            credit_account TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            is_reversal INTEGER NOT NULL DEFAULT 0,
            posted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE approvals (
            invoice_id TEXT PRIMARY KEY REFERENCES invoices(invoice_id),
            required_level TEXT NOT NULL,
            routing_reason TEXT NOT NULL,
            override_applied TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT,
            decided_at TEXT,
            rejection_reason TEXT
        );

        CREATE TABLE corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT NOT NULL,
            line_item_index INTEGER NOT NULL,
            field TEXT NOT NULL,
            original_value TEXT NOT NULL,
            corrected_value TEXT NOT NULL,
            corrected_by TEXT NOT NULL DEFAULT 'human',
            timestamp TEXT NOT NULL
        );
    """)


def load_seed_data(db: sqlite3.Connection) -> None:
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

    with open(os.path.join(data_dir, "purchase_orders.json")) as f:
        pos = json.load(f)
    for po in pos:
        db.execute(
            "INSERT INTO purchase_orders (po_number, vendor, amount, department, status) VALUES (?, ?, ?, ?, ?)",
            (po["po_number"], po["vendor"], po["amount"], po["department"], po.get("status", "open")),
        )

    for filename in ["invoices_labeled.json", "invoices_unlabeled.json"]:
        with open(os.path.join(data_dir, filename)) as f:
            invoices = json.load(f)
        for inv_data in invoices:
            raw_json = json.dumps(inv_data)
            inv = Invoice(**inv_data)
            db.execute(
                """INSERT INTO invoices
                   (invoice_id, vendor, po_number, date, department, total,
                    service_period_start, service_period_end, status, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received', ?)""",
                (
                    inv.invoice_id, inv.vendor, inv.po_number, inv.date,
                    inv.department, inv.total,
                    inv.service_period_start, inv.service_period_end,
                    raw_json,
                ),
            )
            for i, li in enumerate(inv.line_items):
                db.execute(
                    """INSERT INTO line_items
                       (invoice_id, line_index, description, amount, quantity, unit_cost)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (inv.invoice_id, i, li.description, li.amount, li.quantity, li.unit_cost),
                )


def get_invoice(invoice_id: str, db: sqlite3.Connection) -> Invoice:
    row = db.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise ValueError(f"Invoice {invoice_id} not found")
    line_rows = db.execute(
        "SELECT * FROM line_items WHERE invoice_id = ? ORDER BY line_index", (invoice_id,)
    ).fetchall()
    line_items = [
        LineItem(
            description=lr["description"],
            amount=lr["amount"],
            quantity=lr["quantity"],
            unit_cost=lr["unit_cost"],
        )
        for lr in line_rows
    ]
    return Invoice(
        invoice_id=row["invoice_id"],
        vendor=row["vendor"],
        po_number=row["po_number"],
        date=row["date"],
        department=row["department"],
        line_items=line_items,
        total=row["total"],
        service_period_start=row["service_period_start"],
        service_period_end=row["service_period_end"],
    )


def get_all_invoices(db: sqlite3.Connection, labeled_only: bool = False, unlabeled_only: bool = False) -> list[Invoice]:
    rows = db.execute("SELECT invoice_id FROM invoices ORDER BY invoice_id").fetchall()
    invoices = []
    for row in rows:
        inv_id = row["invoice_id"]
        if labeled_only and not inv_id.startswith("INV-"):
            continue
        if unlabeled_only and not inv_id.startswith("UL-"):
            continue
        invoices.append(get_invoice(inv_id, db))
    return invoices


def get_po(po_number: str, db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (po_number,)).fetchone()
    if row is None:
        return None
    return dict(row)


def set_invoice_status(invoice_id: str, status: str, db: sqlite3.Connection) -> None:
    db.execute("UPDATE invoices SET status = ? WHERE invoice_id = ?", (status, invoice_id))


def add_flag(invoice_id: str, flag: str, db: sqlite3.Connection) -> None:
    # Flags are stored as a simple mechanism — we track them via the status and return them
    # in InvoiceProcessingResult. For simplicity, we don't have a separate flags table.
    pass


def store_attributes(invoice_id: str, line_index: int, attrs: ExtractedAttributes, db: sqlite3.Connection) -> None:
    db.execute(
        "UPDATE line_items SET extracted_attributes = ? WHERE invoice_id = ? AND line_index = ?",
        (attrs.model_dump_json(), invoice_id, line_index),
    )


def store_classification(invoice_id: str, line_index: int, classification: ClassificationResult, db: sqlite3.Connection) -> None:
    db.execute(
        """UPDATE line_items SET gl_code = ?, gl_name = ?, rule_triggered = ?,
           treatment = ?, classification_json = ?
           WHERE invoice_id = ? AND line_index = ?""",
        (
            classification.gl_code, classification.gl_name,
            classification.rule_triggered, classification.treatment,
            classification.model_dump_json(),
            invoice_id, line_index,
        ),
    )


def store_entries(entries: list[JournalEntry], db: sqlite3.Connection, posted: bool = False) -> None:
    for entry in entries:
        db.execute(
            """INSERT INTO journal_entries
               (entry_id, invoice_id, line_item_index, date, debit_account,
                credit_account, amount, description, status, is_reversal, posted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.entry_id, entry.invoice_id, entry.line_item_index,
                entry.date, entry.debit_account, entry.credit_account,
                entry.amount, entry.description, entry.status,
                1 if entry.is_reversal else 0,
                1 if posted else 0,
            ),
        )


def store_approval(approval: ApprovalRecord, db: sqlite3.Connection) -> None:
    db.execute(
        """INSERT INTO approvals
           (invoice_id, required_level, routing_reason, override_applied, status,
            decided_by, decided_at, rejection_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            approval.invoice_id, approval.required_level, approval.routing_reason,
            approval.override_applied, approval.status,
            approval.decided_by, approval.decided_at, approval.rejection_reason,
        ),
    )


def store_corrections(corrections: list[Correction], db: sqlite3.Connection) -> None:
    for c in corrections:
        db.execute(
            """INSERT INTO corrections
               (invoice_id, line_item_index, field, original_value, corrected_value, corrected_by, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (c.invoice_id, c.line_item_index, c.field, c.original_value, c.corrected_value, c.corrected_by, c.timestamp),
        )


def get_corrections(db: sqlite3.Connection) -> list[Correction]:
    rows = db.execute("SELECT * FROM corrections ORDER BY id").fetchall()
    return [
        Correction(
            invoice_id=r["invoice_id"],
            line_item_index=r["line_item_index"],
            field=r["field"],
            original_value=r["original_value"],
            corrected_value=r["corrected_value"],
            corrected_by=r["corrected_by"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]


def get_invoice_status(invoice_id: str, db: sqlite3.Connection) -> str:
    row = db.execute("SELECT status FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise ValueError(f"Invoice {invoice_id} not found")
    return row["status"]


def get_approval(invoice_id: str, db: sqlite3.Connection) -> ApprovalRecord | None:
    row = db.execute("SELECT * FROM approvals WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if row is None:
        return None
    return ApprovalRecord(
        invoice_id=row["invoice_id"],
        required_level=row["required_level"],
        routing_reason=row["routing_reason"],
        override_applied=row["override_applied"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        rejection_reason=row["rejection_reason"],
    )


def get_journal_entries(invoice_id: str, db: sqlite3.Connection) -> list[JournalEntry]:
    rows = db.execute(
        "SELECT * FROM journal_entries WHERE invoice_id = ? ORDER BY date, entry_id",
        (invoice_id,),
    ).fetchall()
    return [
        JournalEntry(
            entry_id=r["entry_id"],
            invoice_id=r["invoice_id"],
            line_item_index=r["line_item_index"],
            date=r["date"],
            debit_account=r["debit_account"],
            credit_account=r["credit_account"],
            amount=r["amount"],
            description=r["description"],
            status=r["status"],
            is_reversal=bool(r["is_reversal"]),
        )
        for r in rows
    ]


def get_line_item_classifications(invoice_id: str, db: sqlite3.Connection) -> list[ClassificationResult | None]:
    rows = db.execute(
        "SELECT classification_json FROM line_items WHERE invoice_id = ? ORDER BY line_index",
        (invoice_id,),
    ).fetchall()
    results = []
    for r in rows:
        if r["classification_json"]:
            results.append(ClassificationResult.model_validate_json(r["classification_json"]))
        else:
            results.append(None)
    return results


def get_extracted_attributes(invoice_id: str, line_index: int, db: sqlite3.Connection) -> ExtractedAttributes | None:
    row = db.execute(
        "SELECT extracted_attributes FROM line_items WHERE invoice_id = ? AND line_index = ?",
        (invoice_id, line_index),
    ).fetchone()
    if row is None or row["extracted_attributes"] is None:
        return None
    return ExtractedAttributes.model_validate_json(row["extracted_attributes"])
