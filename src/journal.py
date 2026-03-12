"""Step 5: Journal entry generation and balance verification."""

import calendar
import uuid
from datetime import date

from src.models import (
    ClassificationResult,
    ExtractedAttributes,
    Invoice,
    JournalEntry,
    LineItem,
)


def _last_day_of_month(year: int, month: int) -> str:
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, last_day).isoformat()


def generate_journal_entries(
    invoice: Invoice,
    line_item: LineItem,
    line_index: int,
    classification: ClassificationResult,
    attrs: ExtractedAttributes,
) -> list[JournalEntry]:
    entries: list[JournalEntry] = []

    if classification.treatment == "expense":
        # Case 1: Simple expense
        entries.append(JournalEntry(
            entry_id=str(uuid.uuid4()),
            invoice_id=invoice.invoice_id,
            line_item_index=line_index,
            date=invoice.date,
            debit_account=classification.gl_code,
            credit_account="2000",
            amount=line_item.amount,
            description=f"Expense: {line_item.description}",
            status="immediate",
        ))

    elif classification.treatment == "capitalize":
        # Case 2: Capitalize
        entries.append(JournalEntry(
            entry_id=str(uuid.uuid4()),
            invoice_id=invoice.invoice_id,
            line_item_index=line_index,
            date=invoice.date,
            debit_account="1500",
            credit_account="2000",
            amount=line_item.amount,
            description=f"Capitalize: {line_item.description}",
            status="immediate",
        ))

    elif classification.treatment == "prepaid":
        # Case 3: Prepaid — booking + amortization entries
        prepaid_account = classification.gl_code
        expense_account = classification.prepaid_expense_target
        months = classification.amortization_months or 12

        # Booking entry
        entries.append(JournalEntry(
            entry_id=str(uuid.uuid4()),
            invoice_id=invoice.invoice_id,
            line_item_index=line_index,
            date=invoice.date,
            debit_account=prepaid_account,
            credit_account="2000",
            amount=line_item.amount,
            description=f"Prepaid: {line_item.description}",
            status="immediate",
        ))

        # Amortization entries
        monthly_amount = round(line_item.amount / months, 2)
        if attrs.service_period_start:
            start = date.fromisoformat(attrs.service_period_start)
        else:
            start = date.fromisoformat(invoice.date)

        for i in range(months):
            m = start.month + i
            y = start.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            amort_date = _last_day_of_month(y, m)
            month_name = date(y, m, 1).strftime("%b %Y")
            entries.append(JournalEntry(
                entry_id=str(uuid.uuid4()),
                invoice_id=invoice.invoice_id,
                line_item_index=line_index,
                date=amort_date,
                debit_account=expense_account,
                credit_account=prepaid_account,
                amount=monthly_amount,
                description=f"Amortization: {month_name} \u2014 {line_item.description}",
                status="scheduled",
            ))

    elif classification.treatment == "accrual":
        # Case 4: Accrual — booking + reversal
        expense_gl = classification.prepaid_expense_target or classification.gl_code
        accrual_gl = classification.gl_code

        # Determine accrual date: service period end, or invoice date
        spe = attrs.service_period_end or invoice.service_period_end
        accrual_date = spe if spe else invoice.date

        # Accrual booking: DR expense, CR accrual account
        entries.append(JournalEntry(
            entry_id=str(uuid.uuid4()),
            invoice_id=invoice.invoice_id,
            line_item_index=line_index,
            date=accrual_date,
            debit_account=expense_gl,
            credit_account=accrual_gl,
            amount=line_item.amount,
            description=f"Accrual: {line_item.description}",
            status="immediate",
        ))

        # Reversal: DR accrual account, CR AP (2000)
        entries.append(JournalEntry(
            entry_id=str(uuid.uuid4()),
            invoice_id=invoice.invoice_id,
            line_item_index=line_index,
            date=invoice.date,
            debit_account=accrual_gl,
            credit_account="2000",
            amount=line_item.amount,
            description=f"Reversal: {line_item.description}",
            status="pending_payment",
            is_reversal=True,
        ))

    return entries


def verify_balance(invoice: Invoice, entries: list[JournalEntry]) -> bool:
    total_initial = sum(
        e.amount for e in entries
        if e.status == "immediate" and not e.is_reversal
    )
    return abs(total_initial - invoice.total) < 0.01
