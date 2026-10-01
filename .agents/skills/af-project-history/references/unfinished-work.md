# Unfinished work and handoff routing

These documents are intentionally preserved because they contain continuation state,
not merely retrospective history. None outranks `docs/architecture_design.md`. Before
implementing an item, compare its dated baseline with current code, tests, and later
commits; mark it complete only after verifying the resulting behavior.

## Current heavy handoff pair

### `HANDOFF_260918_port_residue.md`

This is the broad 2026-09-18 port-residue audit and the primary index for that round.
It was written at commit `b25a1da`. Later commits completed meaningful parts of it,
including deletion of the model nickname factory, removal of model-name prompt
selection, Anthropic cache-breakpoint repair, token-only usage accounting, and
truncation handling. Therefore, preserve the document but do not treat every finding as
open.

Its durable continuation value is:

- the remaining port-specific behavioral gaps and dead-code candidates;
- the explicit unresolved design questions in §7;
- the evidence and reproduction notes behind each finding;
- the relationship to the SkyvernCleaner sister handoff.

When resuming it, start with the document's proposed order, then classify each item as
still reproducible, completed by a later commit, superseded by architecture, or awaiting
a user decision. Never bulk-apply the old cleanup list.

### `HANDOFF_260918_skyvern_cleaner.md`

This is explicitly unfinished, not dead-code documentation. The current code still
exposes `SkyvernCleaner`, still expects JS-derived elements, and still has no working
end-to-end injection path.

The unresolved gates are:

1. licensing/distribution — do not copy Skyvern's AGPL `domUtils.js` into the
   Apache-licensed package without an approved legal/packaging decision;
2. injection and same-world bootstrap placement;
3. conversion between the upstream positional result and AF's capture shape;
4. an effect test proving the selected cleaner changes the produced `DOMState`, not
   merely that configuration constructed a `SkyvernCleaner` instance;
5. whether to fail loudly or remove the YAML option until the path is real.

Treat this document as the specific blocker record for the cleaner. Do not silently
fall back and call the feature complete. Before choosing an implementation, read the
later delete-vs-complete analysis in `docs/ref_gap_analysis.md` C1.

## Older evidence, not the active queue

- `HANDOFF_260916_findings.md` and `HANDOFF_260916_next.md` form the anti-bot/input
  round's evidence and backlog pair. Keep them for measurements and rationale, but
  current architecture §4.13 and later commits supersede many implementation claims.
  In particular, do not revive N1's per-tick `InputRecorder`: the user later ruled that
  input ticks are framework execution detail and do not belong in the agent trace.
- `HANDOFF_260730_remaining.md` contains the third-round mixed backlog and deferred
  items. Its predecessors `HANDOFF_260728_coupling_and_fixes.md` and
  `HANDOFF_260729_remaining.md` explicitly say that most findings were implemented or
  invalidated and point forward. Use all three only for provenance unless a current
  source links to a specific finding.
