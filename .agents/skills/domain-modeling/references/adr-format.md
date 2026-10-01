# ADR Format

ADRs live in `docs/adr/` and use sequential numbering: `0001-slug.md`, `0002-slug.md`, etc.

Create the `docs/adr/` directory lazily — only when the first ADR is needed.

## Route it before you write it

`docs/design_discussion.md` already holds the big 难题 (Agent-Loop 注册体系, Capability 扩展体系, Action 体系): the options weighed, the one adopted, and why the rest were rejected.

- **Decision is a sub-branch of an existing 难题** → extend that section in `docs/design_discussion.md`. Do not open a competing ADR.
- **Decision contradicts `docs/architecture_design.md`** → stop. That's a doc disagreement; per AGENTS.md §3.1 the move is to ask, not to record a decision that outflanks the source of truth.
- **Decision is new, self-contained, and session-scoped** → ADR.

## Template

```md
# {决策标题}

{1–3 句：什么背景、决定了什么、为什么。}
```

That's it. An ADR can be a single paragraph. The value is in recording *that* a decision was made and *why* — not in filling out sections.

## Optional sections

Only include these when they add genuine value. Most ADRs won't need them.

- **Status** frontmatter (`proposed | accepted | deprecated | superseded by ADR-NNNN`) — useful when decisions are revisited
- **Considered Options** — only when the rejected alternatives are worth remembering
- **Consequences** — only when non-obvious downstream effects need to be called out
- **相关** — link the 难题 section in `docs/design_discussion.md` or the `docs/architecture_design.md` section this refines

## Numbering

Scan `docs/adr/` for the highest existing number and increment by one.

## When to offer an ADR

All three of these must be true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will look at the code and wonder "why on earth did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If a decision is easy to reverse, skip it — you'll just reverse it. If it's not surprising, nobody will wonder why. If there was no real alternative, there's nothing to record beyond "we did the obvious thing."

### What qualifies here

- **Registry shape.** A new registrar, or a discriminator going flat vs nested (`loop_id` flat, `model_type.provider` nested). Which one a registry gets is a real trade-off and it's invisible from the code.
- **Contract boundaries.** What belongs in an Agent method slot versus Loop orchestration versus a Capability method. Every one of these is expensive to move later.
- **Negotiation semantics.** Anything changing how the three-party observation or action negotiation resolves — especially relative-vs-qualified key rules, or what happens on an ambiguous binding.
- **Trace / WAL format.** File naming, the commit point, what a crash-recovery reader is allowed to assume. Evaluators read the trace, so the format is a public contract even though nothing imports it.
- **Dependency choices that carry lock-in.** `conscribe`, `agent-cdp`, `bubus`, the cloud-browser lite SDKs. Not every library — the ones that would take a month to swap out.
- **Deliberate deviations from the obvious path.** Anything a reasonable reader would assume the opposite of. These stop the next session from "fixing" something intentional.
- **Explicit no-s.** "No SQLite for traces." "No `# deprecated` re-exports." A recorded no is worth as much as a yes — it's what stops the same suggestion returning in three months.

### What doesn't

Implementation detail that the code already states plainly, anything already covered by a 难题 section, and anything the seven principles in AGENTS.md §2.1 already decide for you. If a principle dictates the answer, there was no trade-off — don't write an ADR to restate it.
