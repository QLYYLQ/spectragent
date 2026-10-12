---
name: domain-modeling
description: Build and sharpen this project's domain model. Use when the user wants to pin down domain terminology or a ubiquitous language, record an architectural decision, or when another skill needs to maintain the domain model.
---

# Domain Modeling

Actively build and sharpen the project's domain model as you design. This is the *active* discipline — challenging terms, inventing edge-case scenarios, and writing the glossary and decisions down the moment they crystallise. (Merely *reading* `docs/glossary.md` for vocabulary is not this skill — that's a one-line habit any skill can do. This skill is for when you're changing the model, not just consuming it.)

## Where things land

This repo already has a doc hierarchy with a clear authority order. Slot into it; do not start a competing one.

| Doc | Owns | Your relationship to it |
|---|---|---|
| `docs/architecture_design.md` | **Source of truth** for the target design (AGENTS.md §0.1) | Read-only while exploring. If a proposal contradicts it, surface the conflict. After the user explicitly ratifies a changed target, update the authoritative section before writing subordinate ADRs that depend on it. |
| `docs/design_discussion.md` | The big 难题 — options weighed, one adopted, the others explicitly rejected | Extend the relevant 难题 section when a decision belongs to one. Do not open a competing ADR for it. |
| `docs/execution_dag.md` | M01–M33 order, priorities, status | Read-only. |
| `docs/glossary.md` | Ubiquitous language — terms only, zero implementation | **You own this.** Create lazily, on the first resolved term. Format: [GLOSSARY-FORMAT.md](references/glossary-format.md). |
| `docs/adr/NNNN-slug.md` | Session-scoped decisions too small to be a 难题 | **You own this.** Create lazily, on the first ADR. Format: [ADR-FORMAT.md](references/adr-format.md). |

`spectragent/` is a single context. There is no root `CONTEXT.md` and no `CONTEXT-MAP.md` — do not create either, and do not add doc files to the repo root; it is already crowded.

## During the session

### Challenge against the glossary

When the user uses a term that conflicts with the language already in `docs/glossary.md`, call it out immediately. "The glossary defines Resolver as X, but you seem to mean Y — which is it?"

### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. This repo has several words that are genuinely ambiguous until pinned: *capability* vs *environment*, *action* vs *tool*, *fragment* vs *observation*, *registry key* vs *wiring name*. When one shows up unqualified, ask which is meant before designing on top of it.

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force the user to be precise about the boundaries between concepts. The target scope is the fixture set worth reaching for: browser (WebArena), SWE (SWE-bench), tool-use (τ-bench), multi-tool (GAIA), desktop (OSWorld), mobile, GPU (KernelBench). A design that only holds for browser is a design that isn't finished — walk it through a second domain before agreeing it works.

### Cross-reference the design first, the code second

This repo is mid-refactor: the design doc outranks the code, and everything under `spectragent/` is disposable (AGENTS.md §0.1). So when the user states how something works, check `docs/architecture_design.md` first and the code second — and read a mismatch in the right direction:

- **User contradicts `architecture_design.md`** → surface it and ask which one is moving. The doc is allowed to change; it changes deliberately, not by drift.
- **Code contradicts `architecture_design.md`** → the code is wrong, and that's a work item — not an argument against the user.
- **User contradicts the code and the doc is silent** → the doc has a gap. This is usually the most valuable thing a session finds; flag it as a candidate ADR or glossary entry.

### Update the glossary inline

When a term is resolved, update `docs/glossary.md` right there. Don't batch these up — capture them as they happen.

`docs/glossary.md` must be totally devoid of implementation details. It is not a spec, not a scratch pad, not a home for implementation decisions. It is a glossary and nothing else.

### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If any of the three is missing, skip the ADR.

Before writing one, route it: if the decision is a sub-branch of an existing 难题 in `docs/design_discussion.md`, extend that section instead. A three-line ADR that quietly contradicts a hundred-line 难题 analysis is exactly the doc-disagreement AGENTS.md §3.1 tells you to stop and ask about.

## Language

Prose in 中文, matching `docs/`. Identifiers, registry keys and protocol names stay verbatim in English — `__wiring__`, `__required_slots__`, `guarded_act`, `fragment_id`, `browser.click`. Agent-facing instructions (this file and AGENTS.md) stay in English.
