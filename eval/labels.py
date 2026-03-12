"""Ground truth labels for the 6 labeled invoices."""

LABELS = {
    "INV-001": {
        0: {
            "gl_code": "1310",
            "treatment": "prepaid",
            "approval": "vp_finance",
            "key_attributes": {"is_software": True, "billing_frequency": "annual"},
        },
    },
    "INV-002": {
        0: {
            "gl_code": "5030",
            "treatment": "expense",
            "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"},
        },
        1: {
            "gl_code": "5040",
            "treatment": "expense",
            "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"},
        },
        2: {
            "gl_code": "5030",
            "treatment": "expense",
            "approval": "dept_manager",
            "key_attributes": {"service_type": "legal"},
        },
    },
    "INV-003": {
        0: {
            "gl_code": "5110",
            "treatment": "expense",
            "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True},
        },
        1: {
            "gl_code": "1500",
            "treatment": "capitalize",
            "approval": "vp_finance",
            "key_attributes": {"is_equipment": True, "is_physical_goods": True},
        },
        2: {
            "gl_code": "1300",
            "treatment": "prepaid",
            "approval": "vp_finance",
            "key_attributes": {"is_cloud_hosting": True, "billing_frequency": "annual"},
        },
    },
    "INV-004": {
        0: {
            "gl_code": "2110",
            "treatment": "accrual",
            "approval": "dept_manager",
            "key_attributes": {"service_type": "consulting"},
        },
        1: {
            "gl_code": "2100",
            "treatment": "accrual",
            "approval": "dept_manager",
            "key_attributes": {"category_hint": "travel"},
        },
    },
    "INV-005": {
        0: {
            "gl_code": "5050",
            "treatment": "expense",
            "approval": "vp_finance",
            "key_attributes": {"is_marketing": True},
        },
        1: {
            "gl_code": "5000",
            "treatment": "expense",
            "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True},
        },
        2: {
            "gl_code": "5050",
            "treatment": "expense",
            "approval": "vp_finance",
            "key_attributes": {"is_marketing": True},
        },
        3: {
            "gl_code": "5000",
            "treatment": "expense",
            "approval": "vp_finance",
            "key_attributes": {"is_physical_goods": True, "is_branded_merch": True},
        },
    },
    "INV-006": {
        "expected_flag": "no_po_provided",
    },
}
