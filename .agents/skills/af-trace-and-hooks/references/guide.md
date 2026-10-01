# Hooks and the trace

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

Two halves that only look separate. Hooks are how anything gets *into* a trace; the
trace is the only thing an evaluator is allowed to read. §4 is the judgement call —
read it before you add a field.

## 1. No emitter, no opt-in

A hook declares what it wants to see; nothing calls it. Two surfaces, and they are
dispatched by two different components:

| Surface | Declared as | Dispatched by |
|---|---|---|
| Lifecycle | `on_run_start` / `on_run_end` / `on_run_error` | `AgentRuntime._dispatch_lifecycle` (`core/runtime.py:638`), directly, in `priority` order |
| Method interception | `__intercepts__ = [Intercept("observe", role="env", advice="after")]` | `Weaver` → `AOPProxy` (`core/aop/weaver.py:105`, `core/aop/proxy.py:168`) |

`AgentRuntime.prepare` wraps four roles (`core/runtime.py:314-320`): `agent`, `env`,
`llm`, and `loop` — the last being the `StepRecorder`. Loop code receives the wrapped
objects and calls plain methods. `ReactLoop` contains no `emit()`, no hook reference,
and no stopwatch.

### The rule that keeps it that way

> **If a semantic event does not correspond to an existing method, model it as an
> Agent/Loop Protocol method. Do not add an emitter sidechannel.**

`core/step_recorder.py` is the worked example and is worth reading in full (it is 91
lines). "A step finished" had no method, so the WAL used to fake it by intercepting
`llm.ainvoke` — a call that is neither one-per-step nor aware of anything that happens
after it, and which reconstructed a two-key skeleton `{step, llm_output}`. The fix was
a Protocol (`StepSink.record_step`, `step_recorder.py:34`), a default in-memory
implementation that does no I/O (`StepRecorder`, `:52`), and the Weaver wrapping it
under `role="loop"`. `ReactLoop` calls `await recorder.record_step(record,
observation=observation)` once (`core/loops/react.py:701`) and does not know anyone is
listening. `TraceWriter` intercepts it; a run with no hooks behaves identically, just
without files.

The same move is available to you. Wanting a new event is almost always evidence that
some component should expose a method it currently keeps in a local variable.

### Adding a hook

1. Subclass `HookBase` (`core/hooks/_registrar.py:35`), set `__registry_key__`.
2. Declare `__intercepts__`. `__init_subclass__` (`_registrar.py:95`) raises
   `MissingHandlerError` at *class creation* if `{advice}_{method}` is missing — the
   typo never reaches a run.
3. Set `priority` from the class constants, not a number:
   `PRIORITY_ENRICH=10` / `PRIORITY_DEFAULT=100` / `PRIORITY_PERSIST=900`
   (`_registrar.py:90-93`). It is a plain `ClassVar`, deliberately not a YAML knob —
   ordering is a property of what the hook does. `hook: [trace_writer, metrics]` with
   no priority once wrote `total_tokens: 0` into every `result.json`.
4. Declare `**kwargs: Any` on `on_run_start`. The `RuntimeHook` Protocol does
   (`core/hooks/base.py:96`) because the payload grows; absorbing it is how a hook fails
   a type-check instead of a run.
5. Enable in YAML (`configs/browser_use_local.yaml:229`).

Three assembly-time checks fire before the first LLM call, all in `prepare()`:
`weaver.raise_for_unclaimed_roles()` (`weaver.py:142`, a role nobody wrapped),
`weaver.audit_intercepts()` (`weaver.py:162`, run **after** `loop.weave()` because
`agent.run` does not exist until then — an intercept that can never fire), and
`validate_advice_shape` (`proxy.py:109`, sync/async mismatch, `blocking=False` on a sync
target or on `around`).

### `blocking` means concurrency, and the corollary bites

`blocking=True` awaits inline; `blocking=False` spawns a background task. Error
isolation is a *consequence*, not a second meaning (`core/hooks/base.py:36-71`).

**A `blocking=False` handler cannot contribute to the trace.** It finishes after the WAL
commit point, so whatever it computes lands on no artifact. `trace_writer` and `metrics`
are both blocking for that reason; `blocking=False` is only for side channels nothing
downstream reads (the designed one is `live_monitor`).

## 2. `around` — the advice for calls that failed (defect F17)

`after` does not fire when the target raises. Usually right; once wrong.

A structured reply that does not match the schema leaves `ainvoke` as
`StructuredOutputValidationError` — and the run *continues*, because the Loop feeds the
model its own text back and takes another step. With only `after`, the WAL grew steps
with no message file at all, and precisely the steps a researcher most wants to read
(the ones where the model got it wrong); `metrics` undercounted by one call and its
tokens, so metrics and `result` disagreed, and the gap widened with every correction a
run needed. That is F17 (`HANDOFF_260916_findings.md:67`, fixed in `cf45ee2`).

The fix is one `around` per affected hook — no fourth advice type
(`docs/architecture_design.md:1267-1269`). Success still flows through `after`
(`around`'s innermost link *is* the before/after chain, `proxy.py:471`
`_apply_around_chain`), so nothing is double-counted.

The two hooks deliberately catch different things:

- `TraceWriter.around_ainvoke` (`core/hooks/trace_writer.py:292`) buffers **every**
  exception. The messages that caused a 429 or a timeout are the same messages, and
  `on_run_end` drains the buffer as *unfinished*.
- `MetricsCollector.around_ainvoke` (`core/hooks/metrics_collector.py:130`) counts
  **only** `StructuredOutputValidationError`. A dead socket carries no usage and no
  reply; counting it would invent a call that never happened.

### The discipline that came with it: never fabricate a reply

`_failed_call` (`trace_writer.py:501`) returns a plain dict:

```python
{"error": type(exc).__name__, "detail": str(exc)}   # + "raw_output" only if the provider had text
```

Not a stand-in `ChatInvokeCompletion`. Whoever reads the trace must be able to tell a
reply the model wrote from one the framework invented, and a completion-shaped object
with an error inside it reads as the former. `raw_output` appears for a schema mismatch
(the provider handed over text) and not for a dead socket (it did not). Apply the same
rule anywhere you record a failure: say it failed, say what came back, invent nothing.

**`around` is also the only place a hook can measure time** — `before`/`after` see one
edge each and neither sees a call that raised. `around_ainvoke` accumulates into
`_pending_llm_ms` in a `finally` (`trace_writer.py:327`), and `_stamp_timings`
(`trace_writer.py:376`) writes `duration_ms` / `llm_latency_ms` onto the `StepRecord` at
the commit point. To add a per-step measurement: put it on `StepRecord`
(`schemas/step_record.py:77,81`) with `None` meaning "not measured", fill it in
`_stamp_timings` from an `around` intercept, never in the Loop — then extend
`test_run_configuration.py::test_the_stamp_does_not_reach_the_prompt` (`:364`), because
`StepRecord` is also the payload of `HistoryObs` and is therefore prompt-reachable.

Two honest limits, documented at `trace_writer.py:390-407`: `duration_ms` for step 0
includes environment setup (`on_run_start` fires *before* `setup_environment` on purpose,
so a setup failure still lands in a trace), and **nothing orders intercept handlers** —
`priority` orders lifecycle dispatch only and `core/aop/` never reads it. Treat the
timing fields as readable only after the commit point.

## 3. WAL ordering is the commit protocol

Per step, in this order (`core/trace/wal.py:6-10`):

1. observation fragment files — `write_observation` (`wal.py:179`)
2. LLM message files — `write_messages` (`wal.py:216`)
3. the step record — `write_step_record` (`wal.py:239`)
4. **`manifest.json` atomic rename — `commit_step` (`wal.py:253`)**

Step 4 is the commit point. `_atomic_write` (`wal.py:57`) is write-tmp → fsync →
`os.replace` → `_fsync_dir` (`:37`) — atomic is not durable without the directory fsync.
Crash recovery reads the manifest and ignores orphan files: a complete, parseable
`steps/007.json` for a step the manifest never listed **did not happen**.

`RunTrace.step()` (`core/trace/reader.py:110`) resolves through the manifest rather than
probing the filename, for exactly that reason — one reader, one answer to "did this step
happen". Do not add an accessor that stats a path.

Two things that are files but not steps: `commit_unfinished_messages` (`wal.py:285`)
lists the prompt and completion of a step the Loop never surrendered (timeout, crash)
*without* advancing `last_step_completed` — claiming a step would put an action-less
phantom in the manifest. And `finalize` (`wal.py:316`) sets the terminal status; a trace
stuck at `running` is a trace no evaluator will touch.

**No SQLite.** Trace schemas are heterogeneous and evolve; querying is lazy accessors on
`RunTrace` (`reader.py:42`) — `config`, `result`, `step_records`, `get_observation`,
`get_messages`, `unfinished_message_files`, `screenshots`, each a `cached_property` or a
path lookup. `result` returning `None` means `result.json` is absent (the run died);
a file that exists and does not parse raises `TraceSchemaError` instead
(`reader.py:80-98`) — "written by an older schema" and "never finished" are different
facts and must not collapse.

On-disk layout: `docs/architecture_design.md:1812-1841` (the doc says `config.yaml`;
the writer produces `config.json` — believe the code).

## 4. What belongs in a trace — the judgement

**The trace records agent behaviour.**

This line was drawn explicitly by the user on 2026-09-16, against a concrete proposal.
N1 in `HANDOFF_260916_next.md:103-110` asked for an `InputRecorder` hook that would
intercept input dispatch and write per-tick mouse and key events into the WAL, so a
behavioural-realism evaluator could read them back. It was rejected, and the reason
generalises:

> A mouse trajectory is **how the framework executed the agent's decision**, not the
> agent's decision.

So the line runs between:

| Record it | Do not record it |
|---|---|
| `trajectory_type: "learned"` — one string, an experimental variable you group runs by | the 847 coordinates it produced |
| `screenshot_format: "jpeg"`, `observation_mode: "vision"` | the encoder's intermediate buffers |
| which action was emitted, with which params, and what it returned | the CDP event sequence that delivered it |
| that a fragment capture failed, and whether it was required | the retry timings inside the capability |

**The test to apply to anything you want to add:** could an agent under test have chosen
differently and produced a different value? If yes, it is behaviour or an experimental
variable — record it. If it is the framework's own execution of a decision already
recorded, it is execution detail — leave it out.

Two corollaries worth knowing before you argue the other way:

- **Volume is not the argument, and neither is "Record Once, Evaluate Many".** The
  principle is real (`docs/architecture_design.md:1774`) and the framework does record
  generously — every negotiated observation fragment is serialized in full, on the
  explicit grounds that storage is cheap and API calls are not (`:2259`). Screenshots
  are `required` in *every* observation mode, including DOM-only runs, because
  `online_mind2web` and `lexbench_browser` grade from them: "mode decides what the model
  sees, not what the run records" (`:835`). The rejection of per-tick input was about
  *kind*, not size.
- **Behavioural-realism verification belongs in the external harness**, which also owns
  the human reference data (829 recorded paths, ~50 Hz, `HANDOFF_260916_next.md:108`).
  The harness clicks *through* af's action interface so the trajectory under test is
  unchanged. That is the shape to reach for whenever a proposed trace field exists to
  verify the framework rather than to score the agent.

If you believe a case genuinely sits on the line, AGENTS.md §3.1 applies: ask, do not
invent a compromise.

## 5. Configuration and negotiation reach the trace

`TraceConfig` (`core/trace/models.py:162`) used to be nine identity strings — enough to
say *which* agent and *which* task, not *how it was set up*. Two runs that differed in
`observation_mode` and mouse strategy were indistinguishable on disk, so every cross-run
comparison depended on an external harness keeping its own notes. That was the blank
spot in the framework's own "Record Once, Evaluate Many" (`HANDOFF_260916_findings.md:123`,
N6 at `HANDOFF_260916_next.md`).

Three layers now (`models.py:165-197`):

- **Identity** — `(benchmark, subset, task_id)` is the key an Evaluator joins ground
  truth by. Resolved from the live objects by `AgentRuntime._component_id`
  (`runtime.py:59`), and it is the **authority**: it exists even for a run assembled
  without the Composer, where `resolved` is `{}`.
- **Configuration** — `resolved` is `ComposedRun.top_level_cfg`, the composed, validated
  Pydantic object: every default filled in, every wired receptor inlined inside its owner
  (`agent[0].llm.provider`). **Not the YAML** — `configs/browser_use_local.yaml` never
  mentions `coordinate_space` or `provider.name` and both are in the object. It is also
  not everything a run resolved: a value a component derives at construction
  (`BrowserCapability` turning a profile name into a watchdog set) lives on the instance
  and is not here. `negotiation` is `TraceNegotiation` (`models.py:72`) — active and
  *required* fragments, active actions, and the `fragment_bindings` / `action_bindings`
  that say what each relative `__wiring__` name meant in *this* environment. Everything
  fully qualified, everything sorted (the upstream results are `frozenset`s and iteration
  order varies between processes).
- **Provenance** — `secrets_recorded`, the one field a reader must check before sharing
  the directory.

`AgentRuntime` passes both explicitly (`runtime.py:466-485`) because it is the only
component holding both, and it dispatches `on_run_start` *after* `prepare()` so the
negotiation results exist, but *before* `setup_environment` so a setup failure still
lands in a trace. `TraceWriter.on_run_start` (`trace_writer.py:145`) accepts them **by
name and derives nothing** — a serializer that rediscovers facts by `getattr` is how the
configuration ended up recorded nowhere in the first place.

### The digest has two fidelities and they must not be confused

`configuration_digest` (`models.py:229`) covers identity + benchmark/subset + resolved
config + negotiation, and deliberately **not** `task` / `task_id` / `extra`, so two tasks
of one sweep share a hash.

It hashes the **unredacted** view. `TraceWriter` builds both (`trace_writer.py:212-215`):
`resolved_full = redact_config(cfg, record_secrets=True)`, which never reaches disk and
exists only to be hashed, and `recorded`, which does. `WALWriter.init_run` takes it as a
keyword-only `config_hash` (`wal.py:140`) because only the caller still has the
unredacted object. The earlier version hashed the post-redaction dict, where every
unmarked value is the same `"<omitted>"` string — so `max_steps`, `temperature`,
`viewport_*` and `dom_max_elements` all collapsed and the digest merged the arms of every
sweep a researcher actually runs, silently, while looking like it worked.

The payload stamps `resolved_fidelity` (`models.py:292`), so **a reader cannot recompute
the manifest's `config_hash` and should not try.** Readers compare two manifests. Pinned
by `test_run_configuration.py::TestConfigHash` (`:253`).

### Recipe: record a new run-scoped fact

1. Add the field to `TraceConfig` with a docstring saying what question it answers that
   identity cannot.
2. Decide whether it joins `configuration_digest`'s payload (`models.py:280`): yes if two
   runs differing in it are different *configurations*; no if it is per-task.
3. Resolve it in `AgentRuntime` — the only component holding the whole run — and add it
   as a keyword to the `on_run_start` dispatch (`runtime.py:466`). Document it on the
   `RuntimeHook` Protocol (`core/hooks/base.py:76`).
4. Accept it **by name** in `TraceWriter.on_run_start`. Do not derive it there.
5. Extend `tests/test_core/test_trace/test_run_configuration.py`. If it can carry
   user-supplied text, extend `SECRET_PATTERNS` in `test_secret_guard.py` too.

## 6. Secrets: deny by default, at field level

`spectragent/core/trace/redaction.py`. The resolved config is the single place every
credential in a run passes through — LLM api keys, CDP serving URLs carrying a token,
cloud-browser session tokens, proxy `user:password@`, Android signing keys — and the list
grows with every backend.

**A field is recorded verbatim only when something in the source says it is safe.**
Everything else becomes `OMITTED = "<omitted>"` (`redaction.py:73`) — *written*, not
dropped, because "this field was set" is reproducibility information and a missing key
reads identically to "never configured". `None` stays `null` for the same reason: an
api key resolved from the environment never enters the config model and must remain
distinguishable from one that was set and withheld (`redaction.py:243-247`).

**The asymmetry is the whole argument.** Forgetting to mark a safe knob costs one field
that anybody adds back with a one-line edit. Forgetting to mark a secret is an
unrecoverable leak into a directory people share, attach to issues and upload alongside
papers. A blocklist of key-shaped *names* inverts that asymmetry and will always lag the
next backend.

Two things make a field safe (`_redact_field`, `redaction.py:237`):

1. **An explicit mark** — `trace_safe("<why this cannot be a credential>", ...)`
   (`redaction.py:102`) in place of the field's `Field(...)`. Example in the wild:
   `TraceWriter.trace_dir` (`trace_writer.py:91-102`).
2. **A closed-set annotation** — `Literal[...]`, an `Enum` subclass, `bool`, `None`
   (`is_closed_set`, `redaction.py:159`). This is a *proof*, not a heuristic: the value
   can only be one of the alternatives the source enumerates, so recording it leaks
   nothing that reading the source would not. It is what makes every conscribe
   discriminator and every mode switch (`observation_mode`, `screenshot_format`,
   `trajectory_type`, `coordinate_space`) readable without anybody marking anything.
   Numbers and free strings are open sets and stay withheld — which is why `max_steps`,
   `temperature` and `screenshot_quality` currently read as `"<omitted>"`. Deleting the
   `is_closed_set` clause at `redaction.py:251` restores the strict reading in one line.

Marking a field now only changes what a trace **says**, never whether two traces compare
equal — the digest is taken before redaction (§5). Nothing is blocked on a mass-marking
pass, and there is a reason not to do one yet: the recipe asks `core/loops/`,
`core/capabilities/`, `core/llm/providers/` and `agents/` to import
`spectragent.core.trace.redaction` *in their field declarations*, which drags `wal` and
`reader` into their import graphs and inverts the rule `models.py:133` states in the same
package ("the trace layer is downstream of everything and must stay importable on its
own"). **The mark's home in
`core/trace/` is provisional** — probably `spectragent/schemas/` or conscribe itself.

`record_secrets: bool = False` (`trace_writer.py:104`) is the escape hatch for a local
run you will not share. It logs a warning, and the trace says so in
`config.secrets_recorded`. It is Config-Driven working as designed — a registrar field,
set in YAML, per run — not a global or an env var.

### The guard

`tests/test_core/test_trace/test_secret_guard.py` is what protects secret fields
**nobody has written yet**. It plants four real-shaped credentials into the real shipped
config, validates against the real composed schema, drives a real `AgentRuntime` with a
real `TraceWriter`, then reads every byte under the trace directory by `rglob`
(`:193`) — walking the filesystem, not the manifest, because a leak in a file nothing
indexes is still a leak. It asserts on *shapes*, not names (`SECRET_PATTERNS`, `:60`):
sk-style keys, bearer tokens, `scheme://user:pass@`, `?token=`, devtools websocket URLs.

Both halves are load-bearing: `TestGuardCanActuallyDetectALeak` (`:268`) re-runs with
`record_secrets=True` and requires the same strings to **appear**, so the guard cannot
pass because the run wrote nothing.

Add a pattern here whenever a new credential *shape* becomes possible. You do not need to
do anything for a new secret-carrying backend: its `api_key` / `session_url` / `proxy_*`
are open-typed strings and record as `"<omitted>"` the day the class lands. That is the
point of the polarity.

### Four traps

- **conscribe silently discards almost all field metadata.** Its extractor takes the
  first `FieldInfo` out of an `Annotated`, rebuilds a fresh `Field` from the attributes
  it knows (`description`, numeric/length constraints) and drops the rest —
  `json_schema_extra` included, and every sibling annotation. The one thing it carries
  verbatim is `FieldInfo.metadata`. So `trace_safe()` **replaces** `Field()` and appends
  to `info.metadata` (`redaction.py:133-138`). Both obvious spellings
  (`Annotated[str, Field(...), TraceSafe(...)]`, `Field(json_schema_extra=...)`) test
  green in a plain Pydantic model and evaporate in the composed one, with the failure
  mode being a field that reads `"<omitted>"` and nothing anywhere saying why.
  `test_config_redaction.py::TestMarkSurvivesConscribe` (`:180`) is that tripwire.
  Corollary: the committed `_composed.py` is **not** a faithful picture of which fields
  are trace-safe — codegen drops the same metadata, while `ConfigComposer` builds its
  model at runtime from the source classes. Do not reason about redaction from the
  generated file.
- **No `from __future__ import annotations` in a module using `trace_safe()`.** Under
  PEP 563 the annotation is a string resolved in the *subclass's* globals, conscribe sees
  `field_info is None`, and the whole field is dropped. `trace_writer.py:17-22` carries
  the note. `tests/test_generated/test_no_dropped_annotated_fields.py` catches it.
- **`TraceConfig.extra` is NOT redacted** (`models.py:210-215`). It is whatever the
  caller passed to `execute(config=...)` — `BenchmarkRunner._task_snapshot` puts
  difficulty/domain/tags there. A plain dict has no declarations to consult, so
  deny-by-default would omit everything. It is documented as caller-curated, and it is a
  hole the guard test cannot cover. Do not put configuration there.
- **Structure is checked before the mark** (`_redact_structure`, `redaction.py:256`), on
  purpose: marking a nested model does not dump its subtree verbatim. If you want a
  subtree recorded, mark its leaves.

## 7. Writing an evaluator

**Trace in, verdict out.** `BaseEvaluator` (`spectragent/evaluator/base.py`) takes a
`RunTrace` and an `EvalReference` and returns an `EvalResult`. It never touches a live
environment, never re-runs the agent, and **never reads in-memory state** — which is
precisely what makes evaluation re-runnable: the same directory can be scored by a dozen
evaluators, months apart, on a machine with no browser installed.

- Prefer `TraceView` (`spectragent/evaluator/trace_view.py`) to raw `RunTrace`. It is the
  shared read-side vocabulary — what was asked, what the agent answered, what it did,
  what it saw — each spread across two or three files. Every grader ported in
  reimplemented those joins with slightly different fallbacks.
- **Observation files are named by the fully-qualified fragment key** —
  `000_browser.dom.json`, not `000_dom.json` (`wal.py:186-202`,
  `docs/architecture_design.md:1926`). The negotiated local name is a per-run artefact of
  which capability won negotiation, so `000_screenshot.png` means a different thing in a
  browser run and a desktop run and no reader could tell. `RunTrace.get_observation`
  takes the qualified key (`reader.py:128`); `RunTrace.screenshots` (`:187`) globs
  `*.screenshot.<ext>` across capabilities, and across every image extension, because a
  run configured for JPEG writes `.jpg` and a judge that silently found no frames would
  score the run text-only.
- **Read `negotiation` before you read steps.** `required_fragments` is what tells you
  whether a capture failure ended the run or was recorded as a `FragmentFailure`. A trace
  without it cannot explain its own degraded steps.
- **Scoring that needs a live environment does not go here.** SWE-bench running `pytest
  FAIL_TO_PASS` in the container, OSWorld evaluating VM predicates — that goes through
  `RuntimeHook.on_run_end(result, env)`, the one moment the environment is both alive and
  reachable by a framework-aware component (`core/hooks/base.py:136`). Such a hook writes
  its findings *into the trace*, and an Evaluator reads them back like any other recorded
  fact.
- Verdicts land in each trace's own `evals/` (`evaluator/runner.py:297-309`), named by
  evaluator slug, so a new judge never requires re-running the agent.
- Fail loudly on an unusable trace. `TraceIncompleteError` with the directory named beats
  a zero score — see `online_mind2web.py:106-133` for the pattern (no task text, no
  screenshots for an image judge).

## File map

| | |
|---|---|
| `core/aop/advice.py` | `Intercept`, `AdviceType`, the `{advice}_{method}` convention |
| `core/aop/weaver.py` | index build, `wrap`, the two assembly-time audits |
| `core/aop/proxy.py` | `AOPProxy`, `_apply_around_chain` (`:471`), `validate_advice_shape` (`:109`) |
| `core/hooks/base.py` | `RuntimeHook` Protocol — lifecycle signatures, `blocking` contract |
| `core/hooks/_registrar.py` | `HookBase`, priority constants, `MissingHandlerError` |
| `core/hooks/trace_writer.py` | the reference hook: `around` for failures, commit at `record_step` |
| `core/step_recorder.py` | "model it as a Protocol method" — the worked example |
| `core/trace/wal.py` | write order, atomic commit, durability |
| `core/trace/models.py` | `TraceManifest`, `TraceConfig`, `TraceNegotiation`, the digest |
| `core/trace/redaction.py` | `trace_safe`, `is_closed_set`, `redact_config` |
| `core/trace/reader.py` | `RunTrace` lazy accessors |
| `evaluator/base.py`, `evaluator/trace_view.py` | the read side |
| `tests/test_core/test_trace/test_secret_guard.py` | the artifact-level guard |
