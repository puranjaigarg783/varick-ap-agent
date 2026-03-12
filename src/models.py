"""All Pydantic schemas for the AP Agent pipeline."""

from pydantic import BaseModel


class LineItem(BaseModel):
    description: str
    amount: float
    quantity: int = 1
    unit_cost: float | None = None


class Invoice(BaseModel):
    invoice_id: str
    vendor: str
    po_number: str | None
    date: str
    department: str
    line_items: list[LineItem]
    total: float
    service_period_start: str | None = None
    service_period_end: str | None = None


class ExtractedAttributes(BaseModel):
    is_physical_goods: bool
    is_branded_merch: bool
    is_equipment: bool
    unit_cost_extracted: float | None = None
    is_software: bool
    is_cloud_hosting: bool
    service_type: str | None = None
    is_marketing: bool
    category_hint: str | None = None
    billing_frequency: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    confidence: float
    reasoning: str


class POMatchResult(BaseModel):
    matched: bool
    reason: str | None = None
    po_amount: float | None = None
    tolerance_pct: float | None = None


class ClassificationResult(BaseModel):
    gl_code: str
    gl_name: str
    rule_triggered: str
    treatment: str
    prepaid_expense_target: str | None = None
    amortization_months: int | None = None
    accrual_type: str | None = None


class JournalEntry(BaseModel):
    entry_id: str
    invoice_id: str
    line_item_index: int
    date: str
    debit_account: str
    credit_account: str
    amount: float
    description: str
    status: str
    is_reversal: bool = False


class ApprovalRecord(BaseModel):
    invoice_id: str
    required_level: str
    routing_reason: str
    override_applied: str | None = None
    status: str
    decided_by: str | None = None
    decided_at: str | None = None
    rejection_reason: str | None = None


class InvoiceStatus(BaseModel):
    invoice_id: str
    status: str
    flags: list[str] = []
    error: str | None = None


class InvoiceProcessingResult(BaseModel):
    status: str
    entries: list[JournalEntry] = []
    approval: ApprovalRecord | None = None
    flags: list[str] = []
    error: str | None = None


class EvalResult(BaseModel):
    invoice_id: str
    line_item_index: int
    gl_code_expected: str
    gl_code_actual: str
    gl_correct: bool
    treatment_expected: str
    treatment_actual: str
    treatment_correct: bool
    approval_expected: str
    approval_actual: str
    approval_correct: bool
    attribute_errors: list[str] = []


class EvalReport(BaseModel):
    timestamp: str
    total_line_items: int
    gl_accuracy: float
    treatment_accuracy: float
    approval_accuracy: float
    attribute_accuracy: float
    results: list[EvalResult]
    failure_summary: dict[str, int] = {}


class Correction(BaseModel):
    invoice_id: str
    line_item_index: int
    field: str
    original_value: str
    corrected_value: str
    corrected_by: str = "human"
    timestamp: str
