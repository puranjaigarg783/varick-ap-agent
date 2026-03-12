# My LLM Development Workflow

> A structured approach to building software with AI — where thinking precedes typing, and every phase exists to serve the next.

---

## The Core Principle

The quality of what I build with an LLM is determined almost entirely by the quality of what I bring *into* each conversation. Vague input produces vague output. The pattern that consistently produces better results is simple: **think first, then type.**

I formalize that pattern into three distinct phases — each a focused conversation with a specific AI, a specific purpose, and a specific output. Each phase produces a handoff document that becomes the input for the next. I never skip a phase. I never let one bleed into the next.

I call these phases **Divergent**, **Convergent**, and **Execution**.

---

## Phase 1 — Divergent

**Purpose:** Understand the problem space before any decisions are made.

This is a deliberate thinking session, not a building session. The goal is to surface what I don't know, explore the full landscape of options, and arrive at a strong mental model of what I'm about to build. Decisions made here are tentative. That's intentional.

I open a dedicated chat for this phase and paste the following prompt, followed by the project description:

```
Phase 1 — Divergent Session

Purpose: Phase 1 is a divergent thinking session. The goal is not to make final decisions — it is to fully understand the problem space before any decisions are made. You are exploring options, surfacing unknowns, and building a mental model of what you are about to build.

The job of the human: Describe what they want to build in broad strokes. Answer questions where they can. Acknowledge gaps where they can't. Leave with clarity they didn't have when they started.

The job of the AI: Act as a senior software architect who has seen this type of system built many times. Do not just take the description at face value. Push back. Ask clarifying questions. Surface the decisions the human hasn't thought about yet. Present options with tradeoffs — do not just pick one. The goal is to make the human think, not to give them answers.

What Phase 1 is NOT:
* It is not a spec writing session
* It is not a place to make final decisions
* It is not one directional — the AI should be asking as much as the human

What you leave Phase 1 with: A strong mental model. You know your major components, you've picked a direction for each key decision, and you've identified every unknown that needs to be resolved before building. The output is clarity in your head, not a document.

Tone: Conversational. Exploratory. It should feel like a whiteboard session with a smart engineer who keeps asking "but have you thought about..."

I will now proceed to ask questions. Make sure you understand what's in the document though.
```

The back and forth that follows is messy and wide. That's the point. I'm asking things like:
- What are my options for structuring this?
- What are the tradeoffs between approach A and B?
- One agent or many? Fine-tune or prompt?
- What happens when X goes wrong — who owns exceptions?

Where I don't know the answer, that's the signal — those are the gaps I need to resolve before building anything.

### The Handoff: Divergent → Convergent

At the end of the session I ask:

> *"Summarize everything we've decided and everything that's still open. Format it as a handoff document for the next phase."*

This produces a clean artifact capturing every decision made, every option ruled out and why, and every open question that still needs to be closed. This handoff document, plus the original project description, is what I bring into Phase 2.

I always open Phase 2 in a **separate chat**. Phase 1 gets long and messy — that's its job. I don't want that noise polluting Phase 2, which needs clean context to produce a precise output.

---

## Phase 2 — Convergent

**Purpose:** Take everything explored in Phase 1 and sharpen it into a precise, unambiguous technical specification.

I open a new dedicated chat and paste the following prompt, followed by the Phase 1 handoff document and the original project description:

```
Phase 2 — Architecture / Spec

Purpose: Phase 2 is a convergent thinking session. The goal is to take everything that was explored and decided in Phase 1 and sharpen it into a precise, unambiguous technical specification. By the end of this session there should be zero wiggle room. Every major decision should be locked, every constraint documented, every integration specified.

The job of the human: Bring the Phase 1 handoff document and the original project description. Answer any remaining open questions. Push back if a proposed spec decision doesn't feel right. Leave with a document they could hand to an engineer and have it built exactly as intended.

The job of the AI: Act as a senior software architect writing a spec for a junior engineer who will interpret everything literally. Take every fuzzy Phase 1 decision and make it concrete. For every component, pin down: what triggers it, what it consumes, what it produces, where it fails, and what happens when it does. Do not leave anything to interpretation. If the human gives a vague answer, press them until it is specific.

What good Phase 2 looks like component by component:
* Inputs: exact format, source, frequency, protocol
* Processing logic: exact rules, thresholds, fallback behavior
* Integrations: exact API, auth method, retry strategy, failure handling
* Data: what gets stored, where, schema if relevant
* Human in the loop: exactly when, exactly who, exactly how they are notified and how they respond
* Error states: every failure mode named and handled explicitly

What Phase 2 is NOT:
* It is not a place to re-explore options — that happened in Phase 1
* It is not allowed to leave decisions open — if something is unresolved it must be resolved here before moving on
* It is not a high level overview — vague language like "handle errors appropriately" or "store relevant data" is not acceptable

What you leave Phase 2 with: A written spec document that is so specific there is only one reasonable way to build it. This document will be pasted directly into CLAUDE.md and will serve as Claude Code's persistent context for the entire build. The quality of this document directly determines the quality of everything built after it.

Tone: Precise. Declarative. No maybes, no it-depends, no hand-waving. Every sentence should be a constraint or a decision.

Begin Phase 2 and produce a spec. Keep a live doc of the spec at all times so that I can go back and recall. It is crucial you mention the thinking and reasoning here, since this is a doc the CLAUDE.md will use and it works better when accompanied by reasoning — but also I will return back to reason my choices.

I will then proceed to ask questions to clarify and solidify. They may draw from the architecture decisions doc — could be that you may have already solved it in your proposed Phase 2 live doc. If not, consider and edit.
```

I ask clarifying questions throughout. The AI maintains a live spec document as we go. Every sentence in that spec is a constraint or a decision — no hand-waving, no vague language. I also ask it to include `// WHY:` reasoning blocks throughout — not just what was decided, but why — so that edge cases the spec doesn't anticipate can still be resolved correctly later.

### The Handoff: Convergent → Execution

The output of Phase 2 is a written spec — so specific there is only one reasonable way to build what it describes.

Before I enter Phase 3, I do the following:

1. **Scaffold the project** — create the directory, initialize git, create the folder structure
2. **Split the spec into domain files** under `/docs` — one file per component (data models, classification rules, approval routing, storage, eval system, etc.)
3. **Run `/init`** in Claude Code — generates a `CLAUDE.md` skeleton from the existing structure
4. **Edit `CLAUDE.md` to be a navigation layer** — short, dense, pointing into the `/docs` files. It contains system-wide context, the tech stack, project structure, and pointers — not the full spec

> **Why not dump the full spec into `CLAUDE.md`?**
> `CLAUDE.md` is read on every single prompt in Claude Code. A 1700-line spec in context on every interaction is wasteful and dilutes focus. The navigation layer tells Claude where to look. The domain files contain the detail. When I'm working on a specific component, I reference that doc explicitly — Claude reads it on demand.

---

## Phase 3 — Execution

**Purpose:** Build the system incrementally, one component at a time, with tests passing before moving to the next.

This phase happens inside Claude Code.

**Before entering plan mode I:**
- Set model to **Opus**
- Set thinking effort to **maximum**
- Enable **plan mode** (`Shift+Tab`)

I then paste the following prompt:

```
Read CLAUDE.md, then read every file in docs/. Also read architecture-decisions.md for strategic context.

I need you to produce a detailed, phased implementation plan for building this AP Agent from scratch. No code yet — just the plan.

Constraints on the plan:

1. Build order matters. Each phase must produce something runnable or testable before moving on. No phase should depend on code that hasn't been built yet. Map out the dependency graph.

2. One source file per step within each phase. Each step = one file created or modified, with a clear "done when" criteria I can verify before moving to the next step. If a file is large, split it into sub-steps (e.g., "models.py — input schemas" then "models.py — result schemas").

3. Every phase ends with a verification check. For the deterministic components (classification, treatment, approval, journal entries), this means pytest unit tests — tests/test_rules.py is a deliverable anyway, so write it as you build. For everything else, the verification is a CLI command or a quick smoke-check I can run. Don't write formal tests for components that the eval suite already validates end-to-end.

4. The LLM is expensive. Structure the plan so I can build and test all deterministic components (rules, treatment, approval, journal entries, DB) WITHOUT making any Anthropic API calls. The LLM extraction layer and anything that calls it (pipeline, eval, feedback) should come later.

5. Data files before code that reads them. The JSON files in data/ and the seed data setup should exist before any pipeline code tries to use them.

6. The demo command is the final milestone. `python cli.py demo` running end-to-end with correct output is the definition of done for the whole project.

7. For each phase, tell me:
   * What files are created/modified
   * Which doc(s) to have in context for that phase
   * What the "done" check is
   * What risks or tricky parts to watch for
   * Estimated number of steps

8. Call out the two hardest integration points — where things are most likely to break when components connect — and what I should test at those boundaries.

Produce the plan as a numbered phase list. I'll come back and say "build phase N" to execute each one.
```

I review the sequence Opus produces, push back on anything out of order, and approve it when it looks right. Then I:

- Toggle plan mode off (`Shift+Tab`)
- Switch model to **Sonnet** in the same session — context is preserved
- Say: *"Let's start with phase 1"*

**The build loop:**

After each phase completes I run the verification check. If it passes:
```
Phase N tests pass. Move to phase N+1.
```
If it fails, I fix it within that phase. I never carry broken state forward.

After each phase passes I commit:
```
"commit the phase N changes with a descriptive message, then push"
```

---

## Why This Works

Each phase collapses the solution space in a specific direction:

| Phase | Mode | Output |
|---|---|---|
| Divergent | Exploring | Mental model + handoff doc |
| Convergent | Deciding | Locked spec + `CLAUDE.md` + `/docs` |
| Execution | Building | Working, tested, committed code |

The failure mode most developers fall into is skipping Divergent and Convergent entirely and going straight to Execution with a vague prompt. The LLM fills in the gaps with assumptions. Those assumptions are wrong half the time. You spend the rest of the session correcting them.

The better pattern: thinking first, then typing, produces dramatically better results than typing first and hoping the AI figures it out.

By the time I write the first line of code, there are no open questions.

---

## Model Strategy

| Phase | Model | Effort |
|---|---|---|
| Divergent | Claude.ai | Normal |
| Convergent | Claude.ai | Normal |
| Plan mode | Opus | Maximum |
| Build mode | Sonnet | Normal |

I switch from Opus to Sonnet the moment the plan is approved. Context is preserved within the same Claude Code session — Sonnet picks up exactly where Opus left off.
