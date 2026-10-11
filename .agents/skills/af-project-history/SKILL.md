---
name: af-project-history
description: Resume durable or unfinished AF/All4One work from project decisions, heavy handoffs, JEV research, experiment results, and roadmap history. Use when continuing earlier work or revisiting browser-input, anti-bot, LLM-routing, data-flywheel, decision-model, operations, or visual-system context. Never substitute it for current architecture or code inspection.
---

# AF project history

Recover the useful history that previously lived in Claude project memory without
turning dated observations into permanent truth.

## Authority order

1. `docs/architecture_design.md` defines the current target design.
2. `docs/execution_dag.md` defines dependency order; revalidate its implementation
   status against current code and tests.
3. `AGENTS.md` defines mandatory repository working agreements.
4. These references preserve rationale, user rulings, measured snapshots, and
   historical intent. They never override the three sources above.

If a reference conflicts with a current source, report the conflict. Do not invent a
compromise or silently refresh the historical claim.

## Read only the relevant reference

- Project purpose, user-set design boundaries, and naming preferences:
  [project-decisions.md](references/project-decisions.md)
- The 2026-09-17 ordered roadmap snapshot and its invalidated assumptions:
  [roadmap-snapshot.md](references/roadmap-snapshot.md)
- Mouse humanization, anti-bot experiments, and learned-trajectory research:
  [browser-input-research.md](references/browser-input-research.md)
- LLM gateway, structured-output, cache-accounting, and dependency findings:
  [llm-and-runtime-findings.md](references/llm-and-runtime-findings.md)
- Local execution conventions, ignored visual assets, and migration exclusions:
  [operations-and-exclusions.md](references/operations-and-exclusions.md)
- The data-flywheel line's open decisions and follow-ups, the active unfinished handoff
  pair, and older handoff routing: [unfinished-work.md](references/unfinished-work.md)
- The dated Jev/System One feasibility study and unresolved registrar/attribution choice:
  [jev-system-one-study.md](references/jev-system-one-study.md)
- Human–code-agent development rationale and the deferred configuration explainer:
  [developer-experience.md](references/developer-experience.md)

## Use the history safely

1. Label a remembered item as a user ruling, measured result, historical roadmap, or
   abandoned proposal.
2. Re-read the relevant current design section and inspect current code/tests before
   making an implementation claim.
3. Re-verify provider behavior, dependency APIs, model capabilities, prices, and
   machine configuration when the current task depends on them.
4. Never treat an old release or publishing preference as present authorization for
   `git push`, package upload, credential use, or another external mutation.
5. Never copy credentials, secret-file locations, private IPs, or raw transcripts into
   repository artifacts.
6. When the user asks for current status or to continue an indexed item, read the
   primary document before answering. Read the complete Jev memo; for a long handoff,
   read its status/how-to-read section, the relevant finding, corrections, unresolved
   questions, and proposed order. A filename containing `remaining` or `next` does not
   prove the item is still open.
