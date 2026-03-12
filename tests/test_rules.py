"""Unit tests for deterministic rules — no LLM calls."""

import sqlite3
import uuid
from datetime import date

import pytest

from src.approval import route_approval
from src.classification import classify_line_item, _compute_amortization_months
from src.journal import generate_journal_entries, verify_balance
from src.models import (
    ApprovalRecord,
    ClassificationResult,
    ExtractedAttributes,
    Invoice,
    JournalEntry,
    LineItem,
)
from src.treatment import determine_treatment


def _make_attrs(**overrides) -> ExtractedAttributes:
    defaults = dict(
        is_physical_goods=False,
        is_branded_merch=False,
        is_equipment=False,
        unit_cost_extracted=None,
        is_software=False,
        is_cloud_hosting=False,
        service_type=None,
        is_marketing=False,
        category_hint=None,
        billing_frequency=None,
        service_period_start=None,
        service_period_end=None,
        confidence=0.95,
        reasoning="test",
    )
    defaults.update(overrides)
    return ExtractedAttributes(**defaults)


def _make_invoice(**overrides) -> Invoice:
    defaults = dict(
        invoice_id="TEST-001",
        vendor="Test Vendor",
        po_number="PO-TEST",
        date="2026-02-01",
        department="Engineering",
        line_items=[LineItem(description="Test item", amount=1000.00)],
        total=1000.00,
        service_period_start=None,
        service_period_end=None,
    )
    defaults.update(overrides)
    return Invoice(**defaults)


# ─── Classification Tests ────────────────────────────────────────────

class TestClassification:
    def test_priority_1_physical_goods(self):
        attrs = _make_attrs(is_physical_goods=True)
        result = classify_line_item(attrs, unit_cost=50)
        assert result.gl_code == "5000"
        assert result.treatment == "expense"

    def test_priority_1_branded_merch(self):
        attrs = _make_attrs(is_physical_goods=True, is_branded_merch=True)
        result = classify_line_item(attrs, unit_cost=20)
        assert result.gl_code == "5000"

    def test_priority_1_physical_goods_with_marketing_flag(self):
        """Physical goods override marketing — branded merch case."""
        attrs = _make_attrs(is_physical_goods=True, is_branded_merch=True, is_marketing=True)
        result = classify_line_item(attrs, unit_cost=20)
        assert result.gl_code == "5000"  # P1 fires, P6 never reached

    def test_priority_2_equipment_under_5k(self):
        attrs = _make_attrs(is_equipment=True, is_physical_goods=True)
        result = classify_line_item(attrs, unit_cost=1800)
        assert result.gl_code == "5110"
        assert result.treatment == "expense"

    def test_priority_2_equipment_over_5k(self):
        attrs = _make_attrs(is_equipment=True, is_physical_goods=True)
        result = classify_line_item(attrs, unit_cost=8500)
        assert result.gl_code == "1500"
        assert result.treatment == "capitalize"

    def test_priority_2_equipment_exactly_5k(self):
        attrs = _make_attrs(is_equipment=True, is_physical_goods=True)
        result = classify_line_item(attrs, unit_cost=5000)
        assert result.gl_code == "1500"  # >= 5000

    def test_priority_3_software_monthly(self):
        attrs = _make_attrs(is_software=True, billing_frequency="monthly")
        result = classify_line_item(attrs, unit_cost=500)
        assert result.gl_code == "5010"
        assert result.treatment == "expense"

    def test_priority_3_software_annual(self):
        attrs = _make_attrs(is_software=True, billing_frequency="annual")
        result = classify_line_item(attrs, unit_cost=24000)
        assert result.gl_code == "1310"
        assert result.treatment == "prepaid"
        assert result.prepaid_expense_target == "5010"

    def test_priority_4_cloud_monthly(self):
        attrs = _make_attrs(is_cloud_hosting=True, billing_frequency="monthly")
        result = classify_line_item(attrs, unit_cost=3000)
        assert result.gl_code == "5020"

    def test_priority_4_cloud_annual(self):
        attrs = _make_attrs(is_cloud_hosting=True, billing_frequency="annual")
        result = classify_line_item(attrs, unit_cost=36000)
        assert result.gl_code == "1300"
        assert result.treatment == "prepaid"
        assert result.prepaid_expense_target == "5020"

    def test_priority_5_legal(self):
        attrs = _make_attrs(service_type="legal")
        result = classify_line_item(attrs, unit_cost=4500)
        assert result.gl_code == "5030"

    def test_priority_5_consulting(self):
        attrs = _make_attrs(service_type="consulting")
        result = classify_line_item(attrs, unit_cost=3200)
        assert result.gl_code == "5040"

    def test_priority_5_mixed_legal(self):
        attrs = _make_attrs(service_type="mixed_legal")
        result = classify_line_item(attrs, unit_cost=1800)
        assert result.gl_code == "5030"

    def test_priority_6_marketing(self):
        attrs = _make_attrs(is_marketing=True)
        result = classify_line_item(attrs, unit_cost=15000)
        assert result.gl_code == "5050"

    def test_priority_7_travel(self):
        attrs = _make_attrs(category_hint="travel")
        result = classify_line_item(attrs, unit_cost=1200)
        assert result.gl_code == "5060"

    def test_priority_7_facilities(self):
        attrs = _make_attrs(category_hint="facilities")
        result = classify_line_item(attrs, unit_cost=12000)
        assert result.gl_code == "5070"

    def test_priority_7_training(self):
        attrs = _make_attrs(category_hint="training")
        result = classify_line_item(attrs, unit_cost=1800)
        assert result.gl_code == "5080"

    def test_priority_7_telecom(self):
        attrs = _make_attrs(category_hint="telecom")
        result = classify_line_item(attrs, unit_cost=200)
        assert result.gl_code == "5090"

    def test_priority_7_insurance(self):
        attrs = _make_attrs(category_hint="insurance")
        result = classify_line_item(attrs, unit_cost=5000)
        assert result.gl_code == "5100"

    def test_priority_7_recruiting_falls_through(self):
        attrs = _make_attrs(category_hint="recruiting")
        result = classify_line_item(attrs, unit_cost=25000)
        assert result.gl_code == "UNCLASSIFIED"

    def test_priority_7_catering_falls_through(self):
        attrs = _make_attrs(category_hint="catering")
        result = classify_line_item(attrs, unit_cost=1500)
        assert result.gl_code == "UNCLASSIFIED"

    def test_fallback_unclassified(self):
        attrs = _make_attrs()
        result = classify_line_item(attrs, unit_cost=100)
        assert result.gl_code == "UNCLASSIFIED"

    def test_amortization_months_with_dates(self):
        attrs = _make_attrs(
            is_software=True, billing_frequency="annual",
            service_period_start="2026-01-01", service_period_end="2026-12-31",
        )
        months = _compute_amortization_months(attrs)
        assert months == 12

    def test_amortization_months_default(self):
        attrs = _make_attrs(is_software=True, billing_frequency="annual")
        months = _compute_amortization_months(attrs)
        assert months == 12

    def test_amortization_months_partial_year(self):
        attrs = _make_attrs(
            service_period_start="2026-02-01", service_period_end="2027-01-31",
        )
        months = _compute_amortization_months(attrs)
        assert months == 12


# ─── Treatment Tests ──────────────────────────────────────────────────

class TestTreatment:
    def test_accrual_detected(self):
        """Invoice date after service period end → accrual."""
        attrs = _make_attrs(service_type="consulting", service_period_end="2025-12-31")
        classification = ClassificationResult(
            gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-15")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "accrual"
        assert result.gl_code == "2110"
        assert result.accrual_type == "professional_services"
        assert result.prepaid_expense_target == "5040"

    def test_accrual_non_professional(self):
        """Non-professional service accrual goes to 2100."""
        attrs = _make_attrs(category_hint="travel", service_period_end="2025-12-31")
        classification = ClassificationResult(
            gl_code="5060", gl_name="Travel", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-15")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "accrual"
        assert result.gl_code == "2100"
        assert result.accrual_type == "other"

    def test_no_accrual_when_dates_null(self):
        attrs = _make_attrs(service_type="consulting")
        classification = ClassificationResult(
            gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-15")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "expense"

    def test_no_accrual_when_invoice_date_equals_period_end(self):
        """Strict greater-than: same day is NOT accrual."""
        attrs = _make_attrs(service_type="consulting", service_period_end="2026-01-15")
        classification = ClassificationResult(
            gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-15")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "expense"

    def test_prepaid_via_annual_billing(self):
        attrs = _make_attrs(category_hint="telecom", billing_frequency="annual")
        classification = ClassificationResult(
            gl_code="5090", gl_name="Telecom", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-10")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "prepaid"
        assert result.gl_code == "1300"
        assert result.prepaid_expense_target == "5090"

    def test_insurance_prepaid(self):
        attrs = _make_attrs(category_hint="insurance", billing_frequency="annual")
        classification = ClassificationResult(
            gl_code="5100", gl_name="Insurance", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-01")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "prepaid"
        assert result.gl_code == "1320"
        assert result.prepaid_expense_target == "5100"

    def test_accrual_wins_over_prepaid(self):
        """If both prepaid-eligible and accrual-eligible, accrual wins."""
        attrs = _make_attrs(
            service_type="consulting", billing_frequency="annual",
            service_period_end="2025-12-31",
        )
        classification = ClassificationResult(
            gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense",
        )
        invoice = _make_invoice(date="2026-01-15")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "accrual"

    def test_line_item_dates_override_invoice_level(self):
        """Line-item service_period_end takes precedence over invoice-level."""
        attrs = _make_attrs(service_type="consulting", service_period_end="2025-11-30")
        classification = ClassificationResult(
            gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense",
        )
        # Invoice-level says Dec, but line-item says Nov
        invoice = _make_invoice(date="2025-12-15", service_period_end="2025-12-31")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "accrual"  # Line-item Nov 30 < Dec 15

    def test_already_prepaid_not_overridden(self):
        """Classification already set prepaid (e.g. software annual) — treatment doesn't re-override."""
        attrs = _make_attrs(is_software=True, billing_frequency="annual")
        classification = ClassificationResult(
            gl_code="1310", gl_name="Prepaid Software", rule_triggered="test",
            treatment="prepaid", prepaid_expense_target="5010", amortization_months=12,
        )
        invoice = _make_invoice(date="2026-01-05")
        result = determine_treatment(attrs, classification, invoice)
        assert result.treatment == "prepaid"
        assert result.gl_code == "1310"  # Unchanged


# ─── Approval Tests ───────────────────────────────────────────────────

class TestApproval:
    def test_fixed_asset_override(self):
        invoice = _make_invoice(total=500, department="Engineering")
        classifications = [
            ClassificationResult(gl_code="1500", gl_name="Fixed Assets", rule_triggered="test", treatment="capitalize"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "vp_finance"

    def test_marketing_auto_approve_under_2500(self):
        invoice = _make_invoice(total=2000, department="Marketing")
        classifications = [
            ClassificationResult(gl_code="5050", gl_name="Marketing", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "auto_approve"

    def test_marketing_over_2500_not_auto(self):
        invoice = _make_invoice(total=3000, department="Marketing")
        classifications = [
            ClassificationResult(gl_code="5050", gl_name="Marketing", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "dept_manager"

    def test_engineering_auto_approve_cloud_software(self):
        invoice = _make_invoice(total=4000, department="Engineering")
        classifications = [
            ClassificationResult(gl_code="5010", gl_name="Software", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5020", gl_name="Cloud", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "auto_approve"

    def test_engineering_prepaid_not_auto(self):
        """Prepaid GL codes (1310, 1300) don't qualify for engineering override."""
        invoice = _make_invoice(total=4000, department="Engineering")
        classifications = [
            ClassificationResult(gl_code="1310", gl_name="Prepaid Software", rule_triggered="test", treatment="prepaid"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "dept_manager"

    def test_base_threshold_auto_under_1k(self):
        invoice = _make_invoice(total=500)
        classifications = [
            ClassificationResult(gl_code="5090", gl_name="Telecom", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "auto_approve"

    def test_base_threshold_dept_manager(self):
        invoice = _make_invoice(total=5000)
        classifications = [
            ClassificationResult(gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "dept_manager"

    def test_base_threshold_vp_finance(self):
        invoice = _make_invoice(total=15000)
        classifications = [
            ClassificationResult(gl_code="5050", gl_name="Marketing", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "vp_finance"

    # Expected routes for labeled invoices
    def test_inv001_approval(self):
        invoice = _make_invoice(invoice_id="INV-001", total=24000, department="Engineering")
        classifications = [
            ClassificationResult(gl_code="1310", gl_name="Prepaid Software", rule_triggered="test", treatment="prepaid"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "vp_finance"

    def test_inv002_approval(self):
        invoice = _make_invoice(invoice_id="INV-002", total=9500, department="Legal")
        classifications = [
            ClassificationResult(gl_code="5030", gl_name="Legal", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5040", gl_name="Consulting", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5030", gl_name="Legal", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "dept_manager"

    def test_inv003_approval(self):
        invoice = _make_invoice(invoice_id="INV-003", total=49900, department="Engineering")
        classifications = [
            ClassificationResult(gl_code="5110", gl_name="Equipment", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="1500", gl_name="Fixed Assets", rule_triggered="test", treatment="capitalize"),
            ClassificationResult(gl_code="1300", gl_name="Prepaid", rule_triggered="test", treatment="prepaid"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "vp_finance"

    def test_inv004_approval(self):
        invoice = _make_invoice(invoice_id="INV-004", total=8700, department="Operations")
        classifications = [
            ClassificationResult(gl_code="2110", gl_name="Accrued PS", rule_triggered="test", treatment="accrual"),
            ClassificationResult(gl_code="2100", gl_name="Accrued", rule_triggered="test", treatment="accrual"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "dept_manager"

    def test_inv005_approval(self):
        invoice = _make_invoice(invoice_id="INV-005", total=23500, department="Marketing")
        classifications = [
            ClassificationResult(gl_code="5050", gl_name="Marketing", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5000", gl_name="Supplies", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5050", gl_name="Marketing", rule_triggered="test", treatment="expense"),
            ClassificationResult(gl_code="5000", gl_name="Supplies", rule_triggered="test", treatment="expense"),
        ]
        result = route_approval(invoice, classifications)
        assert result.required_level == "vp_finance"


# ─── Journal Entry Tests ──────────────────────────────────────────────

class TestJournalEntries:
    def test_simple_expense(self):
        invoice = _make_invoice()
        line = LineItem(description="Office supplies", amount=500.00)
        attrs = _make_attrs()
        classification = ClassificationResult(
            gl_code="5000", gl_name="Office Supplies", rule_triggered="test", treatment="expense",
        )
        entries = generate_journal_entries(invoice, line, 0, classification, attrs)
        assert len(entries) == 1
        assert entries[0].debit_account == "5000"
        assert entries[0].credit_account == "2000"
        assert entries[0].amount == 500.00
        assert entries[0].status == "immediate"

    def test_capitalize(self):
        invoice = _make_invoice()
        line = LineItem(description="Server", amount=8500.00)
        attrs = _make_attrs(is_equipment=True, is_physical_goods=True)
        classification = ClassificationResult(
            gl_code="1500", gl_name="Fixed Assets", rule_triggered="test", treatment="capitalize",
        )
        entries = generate_journal_entries(invoice, line, 0, classification, attrs)
        assert len(entries) == 1
        assert entries[0].debit_account == "1500"
        assert entries[0].credit_account == "2000"
        assert entries[0].amount == 8500.00

    def test_prepaid_with_amortization(self):
        invoice = _make_invoice(date="2026-01-05")
        line = LineItem(description="Annual Platform License", amount=24000.00)
        attrs = _make_attrs(
            is_software=True, billing_frequency="annual",
            service_period_start="2026-01-01", service_period_end="2026-12-31",
        )
        classification = ClassificationResult(
            gl_code="1310", gl_name="Prepaid Software", rule_triggered="test",
            treatment="prepaid", prepaid_expense_target="5010", amortization_months=12,
        )
        entries = generate_journal_entries(invoice, line, 0, classification, attrs)
        assert len(entries) == 13  # 1 booking + 12 amortizations

        # Booking entry
        assert entries[0].debit_account == "1310"
        assert entries[0].credit_account == "2000"
        assert entries[0].amount == 24000.00
        assert entries[0].status == "immediate"

        # Amortization entries
        for i, entry in enumerate(entries[1:], 1):
            assert entry.debit_account == "5010"
            assert entry.credit_account == "1310"
            assert entry.amount == 2000.00
            assert entry.status == "scheduled"

    def test_accrual_with_reversal(self):
        invoice = _make_invoice(date="2026-01-15", service_period_end="2025-12-31")
        line = LineItem(description="Consulting assessment", amount=7500.00)
        attrs = _make_attrs(
            service_type="consulting",
            service_period_end="2025-12-31",
        )
        classification = ClassificationResult(
            gl_code="2110", gl_name="Accrued PS", rule_triggered="test",
            treatment="accrual", accrual_type="professional_services",
            prepaid_expense_target="5040",
        )
        entries = generate_journal_entries(invoice, line, 0, classification, attrs)
        assert len(entries) == 2

        # Accrual booking
        assert entries[0].debit_account == "5040"
        assert entries[0].credit_account == "2110"
        assert entries[0].amount == 7500.00
        assert entries[0].status == "immediate"
        assert entries[0].date == "2025-12-31"
        assert entries[0].is_reversal is False

        # Reversal
        assert entries[1].debit_account == "2110"
        assert entries[1].credit_account == "2000"
        assert entries[1].amount == 7500.00
        assert entries[1].status == "pending_payment"
        assert entries[1].is_reversal is True

    def test_balance_verification_pass(self):
        invoice = _make_invoice(total=9500)
        entries = [
            JournalEntry(entry_id="1", invoice_id="TEST", line_item_index=0, date="2026-01-01",
                         debit_account="5030", credit_account="2000", amount=4500,
                         description="test", status="immediate"),
            JournalEntry(entry_id="2", invoice_id="TEST", line_item_index=1, date="2026-01-01",
                         debit_account="5040", credit_account="2000", amount=3200,
                         description="test", status="immediate"),
            JournalEntry(entry_id="3", invoice_id="TEST", line_item_index=2, date="2026-01-01",
                         debit_account="5030", credit_account="2000", amount=1800,
                         description="test", status="immediate"),
        ]
        assert verify_balance(invoice, entries) is True

    def test_balance_verification_fail(self):
        invoice = _make_invoice(total=9500)
        entries = [
            JournalEntry(entry_id="1", invoice_id="TEST", line_item_index=0, date="2026-01-01",
                         debit_account="5030", credit_account="2000", amount=4500,
                         description="test", status="immediate"),
        ]
        assert verify_balance(invoice, entries) is False

    def test_balance_excludes_reversals(self):
        """Reversal entries should not be counted in balance check."""
        invoice = _make_invoice(total=8700)
        entries = [
            JournalEntry(entry_id="1", invoice_id="TEST", line_item_index=0, date="2025-12-31",
                         debit_account="5040", credit_account="2110", amount=7500,
                         description="accrual", status="immediate"),
            JournalEntry(entry_id="2", invoice_id="TEST", line_item_index=0, date="2026-01-15",
                         debit_account="2110", credit_account="2000", amount=7500,
                         description="reversal", status="pending_payment", is_reversal=True),
            JournalEntry(entry_id="3", invoice_id="TEST", line_item_index=1, date="2025-12-31",
                         debit_account="5060", credit_account="2100", amount=1200,
                         description="accrual", status="immediate"),
            JournalEntry(entry_id="4", invoice_id="TEST", line_item_index=1, date="2026-01-15",
                         debit_account="2100", credit_account="2000", amount=1200,
                         description="reversal", status="pending_payment", is_reversal=True),
        ]
        assert verify_balance(invoice, entries) is True

    def test_balance_excludes_scheduled_amortizations(self):
        """Scheduled amortization entries should not be counted in balance check."""
        invoice = _make_invoice(total=24000)
        entries = [
            JournalEntry(entry_id="1", invoice_id="TEST", line_item_index=0, date="2026-01-05",
                         debit_account="1310", credit_account="2000", amount=24000,
                         description="prepaid", status="immediate"),
        ]
        # Add 12 amortization entries
        for i in range(12):
            entries.append(JournalEntry(
                entry_id=str(i + 10), invoice_id="TEST", line_item_index=0,
                date=f"2026-{i+1:02d}-28",
                debit_account="5010", credit_account="1310", amount=2000,
                description="amort", status="scheduled",
            ))
        assert verify_balance(invoice, entries) is True
