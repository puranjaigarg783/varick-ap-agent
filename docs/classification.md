# GL Classification

> **Implements:** `src/classification.py`

---

## Priority Rule Tree

**Conflict resolution:** Rules are evaluated in order 1→7. First match wins. Stop. No scoring, no weighting. Multiple attributes CAN be true simultaneously — the priority order determines which one matters. This is a hard-coded if/elif chain in `classify_line_item()`.

| Priority | Condition | GL Code | Account |
|----------|-----------|---------|---------|
| 1 | `is_physical_goods` and NOT `is_equipment` | 5000 | Office Supplies |
| 2 | `is_equipment`, unit < $5K | 5110 | Equipment (under $5K) |
| 2 | `is_equipment`, unit ≥ $5K | 1500 | Fixed Assets (capitalize) |
| 3 | `is_software`, annual | 1310 | Prepaid Software |
| 3 | `is_software`, other | 5010 | Software & Subscriptions |
| 4 | `is_cloud_hosting`, annual | 1300 | Prepaid Expenses |
| 4 | `is_cloud_hosting`, other | 5020 | Cloud Hosting |
| 5 | `service_type` = legal/mixed_legal | 5030 | Prof Services — Legal |
| 5 | `service_type` = consulting | 5040 | Prof Services — Consulting |
| 6 | `is_marketing` | 5050 | Marketing & Advertising |
| 7 | `category_hint` in {travel, facilities, training, telecom, insurance} | 5060–5100 | Category-specific |
| — | No match | UNCLASSIFIED | Flagged for review |

`recruiting` and `catering` are valid `category_hint` values but intentionally have no mapping — they fall through to UNCLASSIFIED (fail closed).

---

## Multi-Flag Resolution

| Flags true simultaneously | Rule hit | Result | Why |
|--------------------------|----------|--------|-----|
| `is_physical_goods`, `is_branded_merch` | P1 | 5000 | Not equipment → P1 fires. P6 never reached. |
| `is_physical_goods`, `is_equipment`, unit < $5K | P2 | 5110 | P1 checks `NOT is_equipment` → skips. P2 fires. |
| `is_physical_goods`, `is_equipment`, unit ≥ $5K | P2 | 1500 | Same. P2 fires with capitalize. |
| `is_marketing`, `is_physical_goods` | P1 | 5000 | P1 fires first. This IS the branded merch override. |
| `is_marketing`, `category_hint=training` | P6 | 5050 | P6 fires first. P7 never reached. |

**LOCKED DECISION — INV-002 line 2: "Regulatory compliance review & advisory" → 5040 (consulting).** Resolved by the LLM setting `service_type="consulting"`, not by a rule override. See `docs/llm-extraction.md`.

---

## Amortization Months

When classification triggers prepaid treatment, `_compute_amortization_months` computes months from `service_period_start` to `service_period_end`. Default: 12 months (implied by "annual" billing when no dates are parsed).
