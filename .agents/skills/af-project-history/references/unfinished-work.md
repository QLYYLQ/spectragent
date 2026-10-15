# Current handoff and remaining work

Memory updated 2026-10-16 after the implementation accepted on 2026-10-15 at `a6a36d0`.
This file routes continuation; it does not outrank `docs/architecture_design.md`,
`docs/execution_dag.md` or `AGENTS.md`.

## Current entry point

The new implementation handoff is
[implementation/README.md](../../../../tmp/flywheel/followup/implementation/README.md).
Read it before resuming the six review-change workstreams. Their implementation is
**complete**, not awaiting a design discussion or another upstream release.

Evidence beside that handoff:

- `delivery.json`: final scope, versions, formats and limits.
- `final/summary.json`, `pytest.log`, `pytest.xml`: **7882 passed, zero failures/errors/
  skips**, tested at `a6a36d0`; Pyright zero errors/warnings, lint/format passed.
- `branch_delivery.json`: six original feature heads and worktree locations. All six
  working branches were later fast-forwarded to the integrated code. The independent
  commits remain in Git history; old dependency overlays are backups, not patches to apply.
- `final/attempt-*`: earlier collection and integration failures, subsequently resolved.
  They are not current open defects.

These are local, gitignored artifacts. If absent in a fresh clone, use the versioned
architecture, browser/data-flywheel runbooks, code and tests. Do not recreate completed
features merely because a local handoff or experiment trace is missing.

The tested implementation uses agent-cdp **0.5.0**, conscribe **2.0.0**, Ollama **0.6.3**
(minimum 0.6.2), Trace format **5** and AgentResult **3.0**. Old test recordings must be
regenerated; do not add compatibility paths. Later memory-only commits do not change
which code revision actually ran the full acceptance suite.

## Completed boundaries to preserve

- Loop owns execution policy/facts; one recording component owns call accounting and
  persisted scope. No separate metrics hook, no token ledger in ReactLoop or LoopBase.
- Typed execution limits, public step/history/prompt contracts, Runtime-owned identity
  and `execution_ms` replace name-probing and shared private helpers. Task elapsed time
  still includes cleanup and read-back.
- Recording faults do not control execution. Stable sealed segments, sparse manifests,
  selected-channel completeness, bounded background persistence and honest unknown
  usage are implemented. Manifest reconciliation after uncertain writes is implemented.
- Model clients are lazy and reused within an execution; creators close owned resources,
  external transports remain borrowed. Runtime closes its main and explicitly declared
  helper models even if an earlier lifecycle hook fails; judges own their separate use.
- ReAct branch history supports `source` and `restored`, independently of `strict/warn`.
  Contributions use Agent-owned placement and async Hook/Python/helper contracts;
  historical contributions are replayed without new helper calls. Helper output is not
  Agent thought. All restored attempts retain actual provenance and timing.
- Effective ActionSpace identity includes filter/descriptors/guard/bindings. Local Chrome
  endpoint ownership, external-CDP retention, tab/OOPIF and per-binding cleanup are in.
- AF adopted the published agent-cdp 0.5.0 public contracts. Replay rules are scoped,
  bounded by run permissions, explicitly declare exhaustion, and save loaded snapshots.
  Clock/date controls are independent. WARC revisits stay inside a collection batch.

See the handoff for exact APIs and acceptance files; see the architecture for authority.
This is a dated implementation summary, not a second architecture document.

## Active deferred routing

No item below is authorization to start it automatically. Use the user's next task
and the current execution DAG. The original full list is
[deferred_verification.md](../../../../tmp/flywheel/followup/review_changes/deferred_verification.md).

| Remaining scope | Trigger / limit |
|---|---|
| Framework-wide public/private API audit, **including private methods** | Raise it when the user next asks to optimize the framework. Judge semantic owner and actual consumers; this round's local public APIs did not complete a file-by-file audit. |
| Commented YAML template CLI | Still deferred; `spectragent config explain` already exists and is not this feature. See `developer-experience.md`. |
| Richer configuration provenance | Existing explain covers identities, negotiation, AOP, freshness and redacted resolved config. Inspect a concrete need before adding detailed wiring/MRO/field-origin explanations; do not list the whole explainer as unimplemented. |
| Cloud lite SDK lifecycle and cloud recording/replay | Resume only when a real remote-browser need is requested; verify current SDK/public APIs and provider capabilities. |
| Student context, image memory, new export/translation/thought/training strategies | Explicit next phase. Existing export received only necessary cross-cutting adaptations. |
| Shared Chrome multi-run BrowserContext isolation and broad permissions | A separate AF design. Do not reopen this round's already completed local ownership/tab/OOPIF fixes. |
| WebSocket replay and large-body loopback serving | Separate requested capability work; current HTTP replay does not imply support. |
| Login state and OOPIF storage | Design with credential/profile inputs and partitioned storage; unread-frame evidence is not complete capture. |
| Private-trace residual privacy issues | Keep the user's selected threat model; do not turn this into an unsolicited private-directory scan. |
| Linux fsync calibration | Requires the corresponding environment; local measurements do not establish ext4/xfs behavior. |
| Cross-run pools, archive merging, environment mocks and broader replay/training paths | New scope and dependency review, not an implication of this lifecycle or replay work. |
| Wire-level requests and pre-call prompt durability under process kill | Logical request recording is implemented; these stronger guarantees remain deferred. |
| More detailed judge/unevaluable failure classification | Independent judging exists, but does not close every finer classification question. |
| Full Agent × benchmark integration matrix | Still a separate DAG acceptance gate; sampled browser runs and the 7882-test suite do not replace it. |
| Automatic dynamic-region masking / virtual time | Diagnostics do not alter restoration verdicts; the prior probe did not justify shipping virtual time. |
| Jev/System One/probe/cascade | Read the full preserved memo before resuming. Generic helper attribution is not approval or implementation of these research proposals. |
| Remaining port-residue issues | Revalidate individual findings against current design/code/tests; do not batch-execute a dated handoff. |

Internal verification debt is separate from those feature decisions. The uncertain
manifest rename/fsync case is closed by `test_wal_recovery.py` (14 cases present in
the accepted full-suite XML). Do not repeatedly list it as unverified. Broader provider
partial-output coverage, supported-Python matrices, body-read timeout release,
Response-stage OPTIONS, synchronous archive I/O tail latency, real DOM-index retargeting,
nested-worker isolation and nested-receptor coverage need their own evidence; this
handoff does not infer blanket completion. Historical process/connection failures whose
original evidence is missing remain unproven, not newly confirmed bugs.

## Retired from the active queue

| Old entry | Current routing |
|---|---|
| `review_changes/README.md` and the six "awaiting implementation" specifications | Completed requirements/evidence. Start from `implementation/README.md`. |
| `review_reconciliation/index.json` and earlier partition audits | Historical review traceability; old pending labels are not a task scheduler. |
| `HANDOFF_261010_react_loop_layering.md`, `react_synthesis.md` | The approved layering/accounting implementation is complete. The temporary prohibition on editing react.py is over. |
| `tmp/flywheel/H28_fix.md` and per-request-client cleanup follow-ups | Historical failure evidence; execution-owned lifecycle replaced the old mechanism. |
| `agentcdp_current_status.md`, the upstream spec and ledger's old "release pending" status | agent-cdp 0.5.0 is adopted. Retain the append-only ledger for new upstream needs; keep AF-owned CDPClient issues separate. |
| `resume_report.md`, `replay_next/session_status.md`, old live-run plans | Old continuation/experiment records. Earlier paid-run budgets are not ongoing permission. |
| JSONP, virtual-time and `replay_next/report.md` reports | Historical measurements and methodological limits, read only for that research context. |
| September heavy handoffs | Mixed-state evidence, not the default current implementation queue. |

The detailed earlier memory, including dated live measurements and user rulings, is
preserved in [flywheel-history.md](flywheel-history.md). Read it on demand, not before
every new task. `HANDOFF_260918_skyvern_cleaner.md` §9 and the current Skyvern tests document
its implemented JS path; the full benchmark matrix remains independent.

Claude's `memory/MEMORY.md` now routes five obsolete runtime-status records through its
`reference_af_history_index.md`: WAL open items, initial flywheel plan, review-followup
journal, old runtime flow and old conscribe/agent-cdp usage. Their source files remain.
The upstream append-only-ledger convention stays active; its obsolete release status
was updated rather than deleting the convention.
