# LLM Agent — Prompt Design & Configuration

> **Implements:** `src/prompts.py`

---

## Information Boundary

The agent sees workflow instructions and attribute extraction guidance. It does NOT see the priority rule tree, GL code mappings, approval thresholds, or journal entry structure. The tools encapsulate those rules.

| Agent sees | Agent does NOT see |
|------------|-------------------|
| SOP workflow steps (Step 1–6) | GL codes or account names |
| Attribute definitions and guidance | Priority rule tree logic |
| Few-shot examples | Approval threshold amounts |
| Full invoice (all line items at once) | Journal entry structure |
| Tool results (including GL codes) | How tools make their decisions |

The agent sees GL codes in tool RESULTS (output, not input). It can't pre-determine or override them.

---

## System Prompt Structure

Four sections in `src/prompts.py`:

- **Section A** — Role and SOP workflow (Steps 1–6)
- **Section B** — Attribute extraction guidance (target of feedback loop)
- **Section C** — Few-shot examples (tool call format)
- **Section D** — Critical constraints (never pick GL codes, never skip treatment, never fabricate dates)

---

## Prompt Versioning

```python
def get_system_prompt(refinements: list[str] | None = None) -> str
```

The `refinements` parameter injects feedback loop improvements into Section B. The base prompt stays the same; corrections add explicit guidance. See `docs/eval-and-feedback.md` for the engineered weakness strategy.

---

## INV-002 — The Regulatory Advisory Edge Case

**Line: "Regulatory compliance review & advisory" — Expected: 5040 (Consulting)**

The SOP lists "regulatory" as a legal sub-type, but that refers to *direct regulatory actions* (filing, compliance submissions). "Regulatory compliance review & advisory" is advisory work about a regulatory subject — the nature of the work (advisory) dominates the subject matter (regulatory).

Resolution: the agent provides `service_type="consulting"`. The tool follows it to 5040. No rule override needed. This is architecturally correct — semantic disambiguation is the LLM's job.

The initial prompt is intentionally naive about this distinction (engineered weakness #2). The feedback loop adds explicit guidance.

---

## LLM Configuration

- **Model:** `claude-sonnet-4-20250514` (configurable via `AP_AGENT_MODEL` env var)
- **Temperature:** 0.0
- **Max tokens:** 4096
