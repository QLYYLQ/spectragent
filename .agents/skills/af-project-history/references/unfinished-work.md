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

Preserve this record of work that was explicitly unfinished and the user's later
decision to complete it. Sections §0–§8 retain the historical gaps and decisions;
§9 records the 2026-10-01 implementation. The current JS capture path is connected:
the configured cleaner requests a separate JS execution scope, runs this project's
own extraction script in isolated worlds, resolves CDP node identities, and produces
`DOMState`. It does not copy upstream AGPL `domUtils.js` or silently fall back to
BrowserUse snapshots. See `docs/architecture_design.md` §4.4/§4.12 for the current
contract and `docs/ref_gap_analysis.md` C1 for the decision history.

The verification entry points under `tests/test_core/test_capabilities/` are:

- `test_browser_skyvern_live.py`: real Chrome verification of YAML-selected JS
  extraction, indexed actions, same-process and OOP frames, navigation, tab isolation,
  scope cleanup, sensitive-value masking, serialization, and DOM budgets;
- `test_browser_js_scope.py`: cross-scope execution, session ownership, and rollback;
- `test_browser_skyvern_dom.py`: explicit failure without fallback and frame deadlines.

These capability checks do not complete M18's three-Agent × three-benchmark
integration matrix. Revalidate that separate gate before declaring it complete.

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
