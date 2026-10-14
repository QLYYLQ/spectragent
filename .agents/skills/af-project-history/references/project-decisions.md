# Project purpose and durable decisions

These notes preserve explicit user intent. Confirm that the current architecture docs
have not superseded them before implementing a change.

## Current generated data is disposable test data

Explicit user ruling, 2026-10-14: all data generated so far is test data and can be
discarded or regenerated at low cost. Framework consistency takes priority over
keeping these artifacts usable. This includes current recordings, traces, environment
archives, branches, exported samples, evaluation outputs and experiment results.

Do not introduce adapters, compatibility branches, parallel old/new paths, migration
machinery or extra design concepts merely to preserve these data. When a contract or
schema changes, update the test fixtures or generate fresh data to match the coherent
new design; do not bend the framework around existing artifacts. A future requirement
to support durable production data or external consumers must be established explicitly,
not inferred from the mere existence or volume of today's test data.

This ruling removes a compatibility constraint; it is not a request to clean files now.
User-owned source artwork and other protected assets remain governed by their own rules.

## Research workflow is the product

AF/Spectragent targets agent researchers, especially two workflows:

- ablations across agent, loop, observation, action, memory, and environment axes;
- running and authoring benchmarks with comparable, reusable traces.

The WAL is therefore more than crash recovery. It is the substrate for later case
intersection/difference analysis and experiment visualization. Judge architecture by
whether it supports reproducible research, not by how quickly it produces a demo.

`conscribe` and `agent-cdp` are in-house dependencies the user can evolve with the
framework. This explains why their wiring and resolver models are first-class design
choices rather than immutable third-party constraints.

## What belongs in a trace

User ruling, 2026-09-16: per-tick mouse and keyboard coordinates are framework
execution detail, not agent behavior, and do not belong in `RunTrace`.

- Record the selected strategy as an experimental variable.
- Do not persist hundreds of generated input coordinates merely because hooks can see
  them.
- Keep behavioral-realism evaluation in the external harness that owns the human
  reference paths.
- Agent-level step and LLM latency remain trace material.

## An action may use its own LLM

User ruling, 2026-09-16: inject such an LLM through `__wiring__`; never discover an
agent's model by reflection.

Where the receptor lives encodes experimental meaning:

- Capability-owned helper LLM: environment furniture, held constant across subjects.
- Agent-owned helper LLM: part of the subject under test and compared with it.

In either case, nested calls need action-level trace and token attribution. Nested
actions must pass through the negotiated guarded action boundary. The general
multi-round action design was deliberately deferred; `docs/actions_that_call_an_llm.md`
records the active boundary.

## Record tokens, not estimated money

User ruling, 2026-09-19: faithfully normalize provider-reported token counters, but do
not synthesize monetary cost. Price depends on model, serving surface, tier, context
bracket, billing mode, and sometimes time, and a gateway may hide the serving surface.
An authoritative-looking zero or guessed cost is worse than an absent field.

Prompt caching still matters as API-efficiency behavior. Preserve distinct read/write
cache counters when a provider reports them. The current tracked analysis is
`docs/ref_gap_analysis.md`; this note preserves why that boundary was chosen.

## Commit and branch names

The active rule lives in `AGENTS.md` §3.6. Its historical rationale was that names must
explain the resulting change without a planning document:

- Do not put execution-DAG module IDs such as `M05` in commit subjects.
- Do not add `Co-Authored-By` trailers.
- Name branches for what changes, not process words such as `handoff`.
- Do not rename historical branches merely to enforce this convention retroactively.
