# Unfinished work and handoff routing

These documents are intentionally preserved because they contain continuation state,
not merely retrospective history. None outranks `docs/architecture_design.md`. Before
implementing an item, compare its dated baseline with current code, tests, and later
commits; mark it complete only after verifying the resulting behavior.

## Data flywheel line (2026-10-10 to 2026-10-12)

### Later evidence: live runs and the interrupted session (2026-10-14)

The fixture-only and pending-live statements below describe the earlier snapshot, not
the current state. The user subsequently authorized the live runs and made further
decisions in the local `tmp/flywheel/followup/RULINGS_2026-10-13.md` and
`RULINGS_2026-10-14.md`. The latter records a twelve-site LexBench run, a repeat with
site rules, about 1.59 million model tokens used against a 3 million token ceiling,
and the final two work items: JSONP callback rewriting with all 25 site-rule files,
then a zero-model-call virtual-time probe on six sites. The probe is measurement only;
it is not authorization to implement virtual time or change restoration checks.

Claude's latest session stopped at its weekly limit after committing `e8b8308`
(JSONP rewriting, replay-log counts, and the site rules). Its full gate passed:
7,050 tests and 187 generated-config checks; pyright had no new errors against its
existing baseline, not zero errors. The unfinished actions were the replay comparison
and the virtual-time findings, not the older React-layering backlog. Codex resumed
those actions from the existing scripts and traces. The local continuation outputs are
`tmp/flywheel/followup/jsonp_report.md`, `vtime/findings.md` under that same follow-up
directory, and `resume_report.md`; read their final status and evidence before doing
another run. The replay experiment also found that the host's light/dark appearance
changes screenshots; its light-media override is a measurement-only patch, not a
shipped pinned-tier setting.

With that appearance aligned, the previous rules run passed 171/236 checks (72.5%);
the released rules with JSONP rewriting passed 188/236 (79.7%, 62 rewrites). The
same-rules control with rewriting disabled passed 174/236 (73.7%). Each set completed
24 replays, two per site, using only a local scripted model. A follow-up corrected the
documented duplicate-parameter boundary: a recorded callback key appearing more than
once is ambiguous even when only one of its values equals the called name.

The completed virtual-time probe recommends against shipping its current approach:
baseline completed all six sites (28/62 restoration checks passed); both the one-second
and three-second variants completed only one site, with the other five ending on
screenshot failures. Their surviving prefixes (14/21 and 14/22) are not evidence of a
fidelity improvement over the baseline. The recommendation is experimental evidence,
not a new user ruling; volatile-region masking still needs its design discussion.

The earlier implementation boundaries still apply: do not edit `react.py` before the
user's revised design; keep the export side frozen; record agent-cdp requirements in
the local append-only ledger, without changing that upstream repository; do not push
the local commits without current authorization. Preserve failed experimental traces
and compare completed-run counts as well as per-step fidelity.

### Earlier snapshot

Done: eight commits on `main` after `661c924`, `422b006` to `1ad8e47`, covering typed
trace read-back, the archive axis, browser archivers and the recorder connection, offline
replay, branching from recorded checkpoints, SFT export through the student agent, the
metrics chain with the form-submit observation fix, and LLM providers closing the client
each request builds. Architecture §4.14–§4.16 and `docs/execution_dag.md` M27–M33 describe
the result; the `af-data-flywheel` skill is the working guide. Every module was accepted on
fixtures with scripted models; nothing has run against a live site or a paid model. The
local orchestration record (`tmp/flywheel/`: decisions, acceptance reports, the live plan)
and the local PRD (`docs/data_flywheel_prd.md` and its revision log) hold the evidence when
present.

Open user decisions, each surfaced and none taken:

- Dropping `--disable-site-isolation-trials`: `pinned` no longer passes it, because the
  recorder captured out-of-process frames without it and the switch changes their
  observation. It still needs sign-off.
- Failed-teacher branches: a branch whose teacher call failed counts as a branch with no
  document miss, which inflates the rate the strict-miss-policy decision reads. The report
  lists such runs (`model_failures`); excluding them would change the metric's definition.
- The live run: S2b (record and same-path replay of 10 live `online_mind2web` tasks) and
  the full live chain are prepared in `tmp/flywheel/M33_live_plan.md` and
  `tmp/flywheel/m33_live/`, and need the user's route, task count and token ceiling. Until
  they run, the metrics report's recommendations rest on fixture evidence and stay
  `partial`.

Decided by the user on 2026-10-13 (threat model "only me") and implemented: the BrowserUse
DOM observation masks sensitive control values with the shared rule (architecture §4.12);
`storage.json` keeps cookie and Web Storage values as they are, under redaction policy
`af-privacy-1+browser-2` (§4.14, §5.6); typing records
`ActionResult.typed_into_sensitive_field`, which export does not use yet;
`browser.extract_content` of one element masks the same values in the HTML it returns
(§4.12); the flywheel report's privacy measure checks only the two gates, the recorded model
requests and the exported datasets, never `env/` (§4.16).

Decided by the user on 2026-10-13 and implemented (the configuration surface): "not
enabled" is spelled `none`, so the replayability tier `off` is now `none` and the export's
`occlusion: off` is `occlusion: none`, and a test refuses registry keys and option values
YAML 1.1 reads as booleans or null; `trace_writer.archive` is the one archiving switch and
a top-level `archiver:` section is refused; numeric limits say `unlimited`, never `null`;
`actions: []` means no actions; `reasoning_effort` defaults to an explicit `auto`; the
`CHROME_USER_DATA_DIR` fallback is gone; `deterministic_rendering` passes when its seed
equals the tier's (architecture §4.2.1, §4.14).

The `react.py` layering handoff (`HANDOFF_261010_react_loop_layering.md`, local) is still
open. `_UsageTally`, `_merge_token_usage` and most other private helpers in `react.py`
belong to the trace/metrics hook layer or to `AgentLoopBase`; the user's uncommitted TODO
line in `react.py` asks the same question and stays out of commits. M31 placed its new
code by layer and moved nothing, and who owns token accounting (the hook layer, or a
base-Loop helper around the LLM call) awaits the user. The handoff records two accounting
defects, both unfixed: with the `metrics` hook on, a provider that reports no usage turns
the result's `usage=None` into an all-zero `AgentUsage`, which the eval summary then counts
as a measured zero; and after an in-step parse retry, a step that ends on a later failed or
vetoed call records `token_usage=None` although its earlier calls were billed.

Follow-ups of the LLM client fix (`1ad8e47`, architecture §4.7; local report
`tmp/flywheel/H28_fix.md`):

- Reusing connections needs an LLM lifecycle, such as a run-owned `aclose()` on the model
  (architecture §14.3), which is a protocol change for the user. Until then every request
  builds and closes its own client, and `ChatGoogle` on Vertex without explicit credentials
  reloads ADC per request. `ChatOllama` and `ChatMistral` still leave their clients to the
  garbage collector.
- Providers whose `get_client()` ignores an injected `http_client` (deepseek, cerebras,
  vercel, openrouter, `anthropic.aws`) leave the client they built open whenever one is
  injected, because the close rule keys on the field. YAML cannot set the field, so nothing
  in the repository reaches this path.
- The six cloud lite browser backends cache one SDK client per backend instance and close
  only their sessions, never the client. The lite SDKs define no finalizer that schedules
  work on the event loop, so this leaks until collection without the stale-close hang.

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
