# Storage — SQLite Schema & Invoice Data Files

> **Implements:** `src/db.py`, `data/*.json`
> **Spec origin:** Sections 5, 16

Single file: `ap_agent.db`. Created on first run. Seeded with PO data and invoice data.

---

## Tables

```sql
CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    department TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'  -- 'open', 'matched', 'closed'
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
    raw_json TEXT NOT NULL  -- Full invoice JSON for audit trail
);

CREATE TABLE line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
    line_index INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_cost REAL,
    extracted_attributes TEXT,  -- JSON blob of ExtractedAttributes
    gl_code TEXT,
    gl_name TEXT,
    rule_triggered TEXT,
    treatment TEXT,
    classification_json TEXT,  -- Full ClassificationResult JSON for audit
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
    status TEXT NOT NULL,  -- 'immediate', 'scheduled', 'pending_payment'
    is_reversal INTEGER NOT NULL DEFAULT 0,
    posted INTEGER NOT NULL DEFAULT 0  -- 0 = not posted, 1 = posted
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
```

---

## Seed Data — Purchase Orders (`data/purchase_orders.json`)

PO data is stored in `data/purchase_orders.json` as an array of PO objects. Loaded into the `purchase_orders` table by `init-db`. Derived from the invoice test data to make PO matching work.

```
PO-2026-044  | Cloudware Solutions   | $24,000 | Engineering   # INV-001
PO-2026-051  | Morrison & Burke LLP  | $9,500  | Legal         # INV-002
PO-2026-038  | TechDirect Inc.       | $49,900 | Engineering   # INV-003
PO-2025-189  | Apex Strategy Group   | $8,700  | Operations    # INV-004
PO-2026-062  | BrightSpark Agency    | $23,500 | Marketing     # INV-005
# INV-006 has no PO — this is intentional
PO-2026-077  | DataSync Pro          | $24,000 | Engineering   # UL1
PO-2026-081  | WeWork                | $4,500  | Operations    # UL2
PO-2026-090  | TalentBridge Partners | $25,000 | Engineering   # UL3
PO-2026-085  | Global Tech Summit    | $6,800  | Engineering   # UL4
PO-2026-092  | RenewTech             | $4,500  | Operations    # UL5
PO-2026-055  | Cloudflare            | $500    | Engineering   # UL6
PO-2026-088  | Sarah Chen Design LLC | $8,000  | Marketing     # UL7
PO-2026-095  | ModernSpace Builders  | $12,000 | Operations    # UL8
PO-2026-060  | Twilio Inc.           | $2,300  | Engineering   # UL9
PO-2026-098  | Fresh Bites Catering  | $1,500  | Marketing     # UL10
```

**Over-tolerance test PO:** Add one PO with a deliberate mismatch to test the 10% tolerance validation. Specifically:

- Modify `PO-2026-085` (UL4 — Global Tech Summit) to have amount `$5,800` instead of `$6,800`. This is a ~14.7% mismatch. UL4's invoice total is $6,800. This triggers the tolerance check failure.

// WHY: We need both exception paths tested: no PO (INV-006) and PO with tolerance exceeded (UL4 with seeded mismatch). This proves Step 1 validation is real, not just a passthrough.

---

## Labeled Invoices (`data/invoices_labeled.json`)

Each invoice follows the `Invoice` Pydantic schema. The 6 labeled invoices from the assessment are encoded exactly:

- **INV-001:** Cloudware Solutions, PO-2026-044, 2026-01-05, Engineering, 1 line item ($24,000), total $24,000
- **INV-002:** Morrison & Burke LLP, PO-2026-051, 2026-01-20, Legal, 3 line items ($4,500 + $3,200 + $1,800), total $9,500
- **INV-003:** TechDirect Inc., PO-2026-038, 2026-02-01, Engineering, 3 line items ($5,400 + $8,500 + $36,000), total $49,900. MacBook line: `quantity=3, unit_cost=1800`. Server line: `quantity=1, unit_cost=8500`. AWS line: `quantity=1`.
- **INV-004:** Apex Strategy Group, PO-2025-189, 2026-01-15, Operations, 2 line items ($7,500 + $1,200), total $8,700. Service period: 2025-12-01 to 2025-12-31.
- **INV-005:** BrightSpark Agency, PO-2026-062, 2026-02-10, Marketing, 4 line items ($15,000 + $2,000 + $5,000 + $1,500), total $23,500. All lines `quantity=1` — t-shirts and gift bags are lump sum.
- **INV-006:** QuickPrint Co., po_number=null, 2026-02-20, Marketing, 1 line item ($3,800), total $3,800

---

## Unlabeled Invoices (`data/invoices_unlabeled.json`)

10 invoices (UL1–UL10) encoded in the same format. Quantity encoding for multi-unit lines:

- **UL4** line 1: Conference registration — `quantity=2, unit_cost=1800, amount=3600`
- **UL5** line 1: Refurbished Dell 27" monitors — `quantity=10, unit_cost=450, amount=4500`
- All other unlabeled invoice lines: `quantity=1`

**LOCKED DECISION — `quantity` encoding rules:**

`quantity` exists on `LineItem` only. There is no `quantity_extracted` on `ExtractedAttributes`. The rule engine never asks "how many units?" — it only asks "what's the unit cost?" for the Priority 2 equipment threshold ($5K).

Encoding principle: only set `quantity > 1` when the per-unit cost is relevant to a rule engine decision. Specifically, this means equipment/hardware where the $5K threshold applies (INV-003 MacBooks, UL4 conference registrations, UL5 monitors). For physical goods where Priority 1 fires regardless of unit cost (t-shirts, gift bags, brochures), encode as `quantity=1` lump sum — breaking out per-unit cost is noise the rule engine never consumes.

// WHY: The LLM's `unit_cost_extracted` is the fallback for production scenarios with unstructured data. For our test data, we control the JSON and encode quantities where they matter. Lump-summing items where unit cost is irrelevant keeps the data honest about what the system actually uses.
