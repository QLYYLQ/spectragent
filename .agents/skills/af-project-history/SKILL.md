---
name: af-project-history
description: Resume AF/All4One work from its current implementation handoff, remaining-work ledger, durable user decisions, and historical evidence. Use when continuing earlier work or revisiting browser-input, anti-bot, LLM-routing, data-flywheel, decision-model, operations, or visual-system context. Never substitute memory for current architecture or code inspection.
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

- Formal Jev integration, implemented on `feature/jev-add` on 2026-10-19: start with
  [jev-integration-handoff.md](references/jev-integration-handoff.md) for current delivery
  evidence and preserved earlier decisions. Independent review is implemented; calibrated
  dynamic replay participation remains a separate future task.
- Current accepted implementation, the new local handoff, genuinely deferred work and
  retired continuation routes: [unfinished-work.md](references/unfinished-work.md).
  Current local evidence starts at `tmp/framework_expansion/delivery.json`, with
  `review/final.json`, `review/cursor.json` and `final/626612a/summary.json` beside it.
  The five framework extensions plus cursor presentation are merged and frozen-tested;
  the earlier `a6a36d0` six-package handoff remains completed history. Real-site
  acceptance is separate. If local evidence is absent, use the tracked summary and
  re-check the current architecture/code/tests, not old pending labels.
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
- Earlier flywheel discussion, dated live experiments and heavy-handoff provenance,
  only when that history is needed: [flywheel-history.md](references/flywheel-history.md).
  Its old pending labels and temporary freezes are superseded, not active instructions.
- The dated Jev/System One feasibility study and its historical registrar/attribution questions:
  [jev-system-one-study.md](references/jev-system-one-study.md)
- Human–code-agent development rationale and implemented config explanation/template tooling:
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
