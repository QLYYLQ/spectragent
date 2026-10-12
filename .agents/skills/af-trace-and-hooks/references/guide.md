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
| Lifecycle | `on_run_start` / `on_run_end` / `on_run_error` | `AgentRuntime._dispatch_lifecycle` (`core/runtime.py:881`), directly, in `priority` order — on every termination path, external cancellation included (§2) |
| Method interception | `__intercepts__ = [Intercept("observe", role="env", advice="after")]` | `Weaver` → `AOPProxy` (`core/aop/weaver.py:105`, `core/aop/proxy.py:168`) |

`AgentRuntime.prepare` wraps four roles (`core/runtime.py:385-390`): `agent`, `env`,
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
observation=observation)` once (`core/loops/react.py:824`) and does not know anyone is
listening. `TraceWriter` intercepts it; a run with no hooks behaves identically, just
without files.

The same move is available to you. Wanting a new event is almost always evidence that
some component should expose a method it currently keeps in a local variable.

### Adding a hook

1. Subclass `HookBase` (`core/hooks/_registrar.py:39`), set `__registry_key__`.
   **Dataclass or hand-written `__init__`?** A hook with no `__wiring__` receptor may
   stay a `@dataclass` with `Annotated` fields (`metrics` does). A hook that carries a
   receptor must **not** be one: conscribe's metaclass reads
   `inspect.signature(cls.__init__)` at class creation, before `@dataclass` has
   synthesised anything, so a receptor that is a dataclass field looks unreceived and
   gets a `WiredField` over its default (af-add-a-receptor §6). Hand-write `__init__`
   with named `Annotated` params for the knobs, and give the receptor a class-level
   annotation somewhere in the MRO (`archive: list[ArchiverBase]`; use `list[...]` for a
   list receptor, because the Composer reads the *class* annotation, not the signature,
   to choose list / dict / single injection; put it on a mixin when the class body uses
   the name for something else). `TraceWriter` is the worked case (M28 dropped its
   `@dataclass`). `HookBase` itself is `@dataclass(eq=False)`: a hook is a stateful
   object compared by identity, and without it a hand-written subclass inherits a
   field-less `__eq__` under which every two instances are "equal". Compare hooks with
   `is`.
2. Declare `__intercepts__`. `__init_subclass__` (`_registrar.py:122`) raises
   `MissingHandlerError` at *class creation* if `{advice}_{method}` is missing — the
   typo never reaches a run.
3. Set `priority` from the class constants, not a number:
   `PRIORITY_ENRICH=10` / `PRIORITY_DEFAULT=100` / `PRIORITY_PERSIST=900`
   (`_registrar.py:117-120`). It is a plain `ClassVar`, deliberately not a YAML knob —
   ordering is a property of what the hook does. `hook: [trace_writer, metrics]` with
   no priority once wrote `total_tokens: 0` into every `result.json`.
4. Declare `**kwargs: Any` on `on_run_start`. The `RuntimeHook` Protocol does
   (`core/hooks/base.py:101`) because the payload grows; absorbing it is how a hook fails
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
isolation is a *consequence*, not a second meaning (`core/hooks/base.py:37-72`).

**A `blocking=False` handler cannot contribute to the trace.** It finishes after the WAL
commit point, so whatever it computes lands on no artifact. `trace_writer` and `metrics`
are both blocking for that reason; `blocking=False` is only for side channels nothing
downstream reads (the designed one is `live_monitor`).

## 2. `around` — the advice for calls that fail or never finish (defect F17)

`after` does not fire when the target raises. Usually right; twice wrong.

A structured reply that does not match the schema leaves `ainvoke` as
`StructuredOutputValidationError` — and the run *continues*, because the Loop feeds the
model its own text back and takes another step. With only `after`, the WAL grew steps
with no message file at all, and precisely the steps a researcher most wants to read
(the ones where the model got it wrong); `metrics` undercounted by one call and its
tokens, so metrics and `result` disagreed, and the gap widened with every correction a
run needed. That is F17 (`HANDOFF_260916_findings.md:67`, fixed in `cf45ee2`).

The second is cancellation. `CancelledError` is a `BaseException`: neither `after` nor
an `except Exception` ever sees it, so a call a timeout or a Ctrl-C cut off took its
prompt with it, and that prompt is the one exchange that says what the agent was doing
when the run died.

The fix is `around`, with no fourth advice type (`docs/architecture_design.md` §4.6).
The two hooks use it differently:

- `TraceWriter.around_ainvoke` (`core/hooks/trace_writer.py:579`) is the **only** place
  an LLM call is recorded; `TraceWriter` has no `after_ainvoke`. It registers the call
  *before* `proceed()` (`_register_call`, `:725`: the prompt from `args[0]`, the
  requested reply model from `kwargs["output_format"]`, the start time) and settles it
  exactly once: `completed` on return, `failed` on any `Exception`, `cancelled` on a
  `BaseException`. It always re-raises. One advice owns the call from start to end, so
  nothing has to correlate an `after` with an in-flight record, a correlation that would
  break the day a Loop issues calls concurrently. The prompt is a JSON snapshot taken at
  registration (`PendingLLMCall`, `core/trace/encoding.py:444`): a memory that mutates a
  message object afterwards cannot rewrite what the record says the model was asked.
- `MetricsCollector.around_ainvoke` (`core/hooks/metrics_collector.py:126`) counts
  **only** the failures the provider billed: `StructuredOutputValidationError` and
  `ModelOutputTruncatedError`. A dead socket carries no usage and no reply; counting it
  would invent a call that never happened. Its successes still flow through `after`
  (`around`'s innermost link *is* the before/after chain, `proxy.py:471`
  `_apply_around_chain`), so nothing is double-counted.

A registered call whose step never commits (the run timed out, was cancelled or crashed
between the call and `record_step`) is committed at run end as a *trailing* call
(`_flush_trailing`, `trace_writer.py:984`; §3). That needs the runtime's half too:
`AgentRuntime.execute` dispatches `on_run_end` / `on_run_error` on **every** termination
path, and catches `asyncio.CancelledError` to close a cancelled run out with an
`agent_done=ERROR`, `error="Execution cancelled"` result before re-raising it unchanged
(`docs/architecture_design.md` §4.2). Before that, external cancellation reached no
lifecycle callback at all: no `result.json`, a manifest stuck at `running`, and the
in-flight prompt lost however the hook recorded it.

"Unchanged" holds even when the close-out itself fails. A blocking hook that raises
while a cancelled run is being closed out (`TraceWriter.on_run_end` on a full disk) is
logged, and the `CancelledError` is still what the caller sees. Letting the hook's
`OSError` win turned Ctrl-C into an ordinary task failure: a sweep's `except Exception`
recorded it and started the next task, and a caller's `asyncio.timeout` saw `OSError`
instead of `TimeoutError`. A hook whose close-out *awaits* can be cancelled inside it,
and then the same rule is the hook's own: `TraceWriter` holds a cancellation of its
close-out collection, finishes the close-out, logs a flush or `result.json` failure that
happened under it, and re-raises the cancellation, in `on_run_end` and `on_run_error`
alike.

A guard's veto has the same precedence, kept by the runtime alone: a close-out
that fails under a `RunAbort` (the vetoed step's calls cannot be flushed because the disk
stayed full) is logged and attached to the veto as a note, and the `RunAbort` is what
`execute` raises, `raise_on_error=False` or not, so the sweep still stops. The run's own
timeout is not an outside cancellation and does not outrank the veto either. When it
expires while the Loop is still recording the vetoed step (`TraceWriter` collecting the
refused page's evidence, the first thing on the veto's way out that can take long), the
cancellation carries the veto (`loops.errors.RunEnded`), and `execute` closes the run
out as vetoed and raises the `RunAbort` with the timeout as a note; a terminal result
the Loop had built (a dead browser) is returned instead of a timeout result. For a hook
nothing changes: the step is simply not committed, its calls land as trailing and the
close-out collection takes the evidence the cut-off one never did. It is still the
reason `after_record_step` must never swallow a `CancelledError` (af-add-a-loop §3.3).

On every other path a close-out failure propagates as it is,
and the run is closed out **once**: the success path's `_finish` sits in the `try`'s
`else`, so a hook raising in `on_run_end` is not mistaken for the run failing and
dispatched a second `on_run_end`. When you write a blocking hook, its `on_run_end`
failing is the caller's error, not a retry.

### The discipline that came with it: never fabricate a reply

A failed or cancelled call is recorded with a `CallFailure` (`core/trace/models.py:255`),
built by `_failure` (`core/trace/encoding.py:417`) from what the exception carried and
nothing else:

```python
{"error": type(exc).__name__, "detail": str(exc)}
# + "raw_output"      the provider handed over text the validator refused
# + "partial_output"  what a reply cut off at the token ceiling had generated
# + "stop_reason", "usage"   a failed call is billed all the same
# — each one only when the exception carries it
```

Not a stand-in `ChatInvokeCompletion`. Whoever reads the trace must be able to tell a
reply the model wrote from one the framework invented, and a completion-shaped object
with an error inside it reads as the former. `raw_output` appears for a schema mismatch
(the provider handed over text) and not for a dead socket (it did not).
`partial_output` is deliberately not `raw_output`: that name means "as the validator
saw it", and nothing validated a truncated reply. Every truncation raise site passes it
(`openai_base`, `anthropic_base` with serialised `tool_use` input, `google`), because the
Loop aborts the step on truncation and this record is the only place the text survives.
A call still in flight when its step commits or the run closes out is `cancelled` with
no failure record at all: no exception reached the hook. Apply the same rule anywhere
you record a failure: say it failed, say what came back, invent nothing.

The reply schema a structured call asked for lands once per run, content-addressed, at
`messages/output_formats/<sha256>.json`, and every call references it
(`OutputFormatRef`). It is the *requested* schema (the reply model's
`model_json_schema()`), not what the provider compiled and sent, so it is no evidence the
schema was honoured. A class whose schema cannot be generated is recorded as
`output_format: null` with a warning; the trace is never what fails a call.

**`around` is also the only place a hook can measure time** — `before`/`after` see one
edge each and neither sees a call that raised. `around_ainvoke` accumulates into
`_pending_llm_ms` in a `finally` (`trace_writer.py:618`), and `_stamp_timings`
(`trace_writer.py:680`) writes `duration_ms` / `llm_latency_ms` onto the `StepRecord` at
the commit point; each call record also carries its own `latency_ms`. To add a
per-step measurement: put it on `StepRecord`
(`schemas/step_record.py:77,81`) with `None` meaning "not measured", fill it in
`_stamp_timings` from an `around` intercept, never in the Loop — then extend
`test_run_configuration.py::test_the_stamp_does_not_reach_the_prompt` (`:367`), because
`StepRecord` is also the payload of `HistoryObs` and is therefore prompt-reachable.

Two honest limits, documented at `trace_writer.py:697-714`: `duration_ms` for step 0
includes environment setup (`on_run_start` fires *before* `setup_environment` on purpose,
so a setup failure still lands in a trace), and **nothing orders intercept handlers** —
`priority` orders lifecycle dispatch only and `core/aop/` never reads it. Treat the
timing fields as readable only after the commit point.

## 3. WAL ordering is the commit protocol

One commit per step: `WALWriter.commit_step(step, record, *, fragments, llm_calls,
extra_files)` (`core/trace/wal.py:191`), in this order (`wal.py:7-16`):

0. validate everything before writing a byte: path rules, content addresses, a step
   not already committed (`_Plan`, `wal.py:347`);
1. every content file (fragment JSON and artifacts, one record per LLM call, output
   schemas this run has not committed yet, extra files) through `fsio.write_durable`
   (`core/trace/fsio.py:104`): temp file → write and flush → `fsync(file)` →
   `os.replace`, and **no** directory `fsync`;
2. `steps/NNN.json`, plus `steps/NNN.extra.json` when the step carries extra files;
3. `fsio.fsync_dir` (`fsio.py:79`) once per distinct directory, deepest first, plus the
   parent of every directory on the way that is not yet known durable;
4. **`manifest.json` atomic rewrite — the commit point** (`fsio.atomic_write`,
   `fsio.py:153`).

The per-file data `fsync` stays; only the directory `fsync` is batched. Renaming a file
whose bytes never reached the disk lets a power loss leave the manifest listing an empty
file, and then the commit point lies. One directory `fsync` covers every entry written
under that directory, so batching costs no guarantee and saves an `fsync` per file on
a step that carries hundreds of evidence files. Atomic is still not durable without it.

The flush before the data `fsync` is just as load-bearing, and harder to see.
`os.fdopen` buffers up to `st_blksize` (4 KiB on APFS and ext4), so an `fsync` issued
before the flush (or ahead of the write) covers an empty file for everything smaller
than that, which is most of a trace: step records, the early manifests, call records,
verdicts. Their bytes reach the kernel at `close()`, after the `fsync`. The count and the
order of `fsync` calls are identical either way, so a test that counts cannot tell;
`test_fsio.py` and the 1000-file budget test record each file's size at the moment of
its data `fsync`. Do the same in any test that claims a file is durable.

Directory `fsync` errors are **not** swallowed (`fsio.py:29-36`): skipped only on
Windows; only `EINVAL` / `ENOTSUP` / `EOPNOTSUPP` from the `fsync` itself are tolerated
("this filesystem cannot"); everything else raises, `os.open` failing with `EMFILE` /
`EACCES` / `ENOENT` included. The old helper returned silently on all of them.
`TraceWriter` is blocking, so such an error ends the run; a manifest claiming a commit
it is not sure of would be worse. `F_FULLFSYNC` is deliberately off; the `wal.py` module
docstring has the measurement. TODO(linux): every `fsync` cost and the 1000-file commit
budget were calibrated on macOS APFS only; ext4 and xfs have not been measured. The
budget is asserted by the benchmark test alone and `SPECTRAGENT_WAL_COMMIT_BUDGET_S`
overrides it; nothing enforces it at run time.

A failure anywhere before the manifest rename raises with the manifest untouched (on
disk and in the writer's memory), so what was written is an orphan and the same commit
can be retried. Crash recovery reads the manifest and ignores orphans: a complete,
parseable `steps/007.json` for a step the manifest never listed **did not happen**.

"Not yet known durable" in step 3 is what keeps a retry honest. A directory's name is an
entry in its parent, durable only once the parent is `fsync`-ed. The writer remembers
which directories `init_run` or a *successful* commit made durable (`_durable_dirs`,
updated after the manifest rewrite like everything else it remembers), and `fsync`-s the
parent of every other directory a commit writes under. "Created by this call", which is
what `write_durable` reports, is not enough: a failed attempt already created `env/` and
`env/steps/000/`, the retry finds them present, and the manifest would then commit files
under names a power loss can still take back. M28's run end, flushing evidence with
`commit_trailing` after a step commit failed, is the same case.

`init_run` (`wal.py:128`) is the run's first commit, by the same rule: `config.json`
through `write_durable` (which creates the run directory, and `traces/` on a sweep's first
run), the WAL's subdirectories, one directory `fsync` each for the run directory and the
parent of every directory created on the way, and only then the first manifest. A run
exists once its manifest does (`discover_traces` looks for nothing else), so a failure
before it leaves no run, and a run that exists has its config. The `fsync` of `traces/`
looks redundant next to the manifest's own `fsync` of the run directory, and is not: no
later commit `fsync`-s a directory outside the run, so this is the only place the run's
own name becomes durable. `test_wal_batch_commit.py::TestInitRun` pins the order and
every failure before the manifest.

Files get the permissions a plain write would: the temp file is created with
`os.open(..., 0o666)`, which the kernel masks with the umask, and a file being replaced
keeps its mode (set before a byte is written). `tempfile.mkstemp` forced `0o600`, so every
trace file and verdict came out owner-only: a shared `--evals-dir` was unreadable to a
teammate or a dashboard, and a re-score reset a mode set on purpose. Whether a trace is
private is the umask's (or the directory's) decision; reading the umask would race other
threads, because `os.umask` can only set it.

**One module spells the layout.** `core/trace/encoding.py` decides every file name and
every byte that goes in it (`fragment_path`, `call_path`, `encode_fragment`,
`encode_llm_call`, …); `TraceWriter`, `RunTrace` and the test fixtures
(`tests/_helpers/trace_builder.py`) all go through it, because hand-written fixtures are
how test traces drifted from what production writes.

The WAL checks the paths (`_Plan`, `wal.py:347-520`): POSIX relative with no empty, `.`
or `..` segment (`encoding.check_relative_path`, which the reader applies too), on every
channel, the names `encoding.py` decides included. A fragment file is named by its key, a
`FragmentFailure`'s `fragment_id` is free text, and "under `observations/`" is a prefix
test that `observations/000_x/../../../escaped.json` passes, so that check is not
redundant with the directory rule. An extra file may not take a name the WAL owns
(`manifest.json`, `config.json`, `result.json`, anything under
`steps/`, `observations/`, `messages/`, `evals/`), judged by the first segment, so
`result.json/evidence.txt` cannot turn the result into a directory the close-out then
fails to write; a path is a file or a directory of the run, never both (`env/a` with
`env/a/b`, in one commit or across two, and no file on a WAL directory such as
`messages/output_formats`), or the commit would die half-way with `NotADirectoryError`;
a committed path is immutable; a content-addressed path names its data's full sha256
(whole, or sharded `ab/cdef…`) and is written once per run however many commits list it,
since deduplication skips the write, never that check. All of these compare paths case-
and normalization-insensitively, through one key (`encoding.fold_path`, Unicode canonical
caseless matching), because macOS's APFS stores `A.txt`/`a.txt`, and `café` precomposed
and decomposed, as one file.

Two things that are files but not steps: `commit_trailing(*, llm_calls, extra_files)`
(`wal.py:259`) commits the calls of a step the Loop never surrendered (timeout,
cancellation, crash), and whatever a producer flushes at run end, by the same
protocol, *without* a step record and without advancing `last_step_completed`:
claiming a step would put an action-less phantom in the manifest. Each call appends one
`manifest.trailing` entry. And `finalize` (`wal.py:303`) sets the terminal status; a
trace stuck at `running` is a trace no evaluator will touch.

Fragments and calls are few per step, so the manifest indexes them itself:
`fragments: {key: {file, artifact, failed}}` and `llm_calls`. Extra files are not:
they are listed in `steps/NNN.extra.json` (path, size, sha256, content_addressed,
meta) and the manifest holds only that path. It is rewritten whole on every commit and
read eagerly by every reader, so inlining a step's hundred evidence files would make it
grow quadratically with the run. `failed: true` exists because a `FragmentFailure` has
no discriminator in its JSON and validates silently as an empty `ScreenshotObs`,
`DOMObs` or `BrowserMetaObs`; the flag is written by the one component that knew which
it was.

**Environment evidence rides the same commit** (`docs/architecture_design.md`
§5.2–§5.4). When a run binds archivers, `TraceWriter.after_record_step` pulls
`env.collect_archive()` *before* the manifest commit, checks the batch, and hands it to
that step's `commit_step(..., extra_files=...)` through `encoding.encode_archive_file`.
The layout, every name spelled in `encoding.py` (`archive_step_path`,
`archive_trailing_path`, `archive_blob_path`, `archive_run_path`):

```
env/                                   # only when archivers were bound and something was collected
├── run.<capability_id>.json           # a capability's run record (fingerprint …): version 0
├── run.<capability_id>.<k>.json       # version k ≥ 1, written when the content changed
├── blobs/sha256/<ab>/<rest>           # content-addressed files, written once per run
├── steps/NNN/<archiver_id>/<name>     # what step NNN's commit took
└── trailing/KK/<archiver_id>/<name>   # what the KK-th commit_trailing took
```

Every archive file's `ExtraFileRecord.meta["archive"]` is an `ArchiveMeta`
(`archiver_id`, `name`, `media_type`, `scope`, `captured_at`, `cycle`, `ids`,
`run_version`). A blob shared by ten steps is one file listed in ten indexes. A run
record is never rewritten (a committed path is immutable), so a changed re-emission
becomes the next version and an identical one is dropped. Versions keep **capture order**,
not commit order: collections are numbered, and an owed batch committed at close-out
after a later collection's record already landed does not commit its own, older record,
because as the next version it would read back as the latest fingerprint.

The close-out gives **every owed thing its own `commit_trailing`**, in order: the calls
still registered; each batch a failed step commit left behind (two steps' batches can
share a file name, so each needs its own `env/trailing/KK/`, where `KK` is the entry's
position in `manifest.trailing`, the same number as `steps/trailing_KK.extra.json`); the
tail collected at run end. A commit that fails costs only what it carried, and that item
is dropped, not retried: `AgentRuntime` dispatches no `on_run_error` after `on_run_end`
raised, and the veto and cancel close-outs do not retry either (a write the disk just
refused rarely succeeds a few milliseconds later). The rest still lands, so a batch no
retry can write never takes the unfinished step's calls or the tail down with it. The
first failure is raised once everything was tried. The manifest then says `error`, as it
does whenever a collection failed (at close-out or at any step), whatever the result
reports. The WAL knows none of this; archive files are ordinary `TraceFile`s.

Every batch, the tail included, is checked before the WAL sees it: bound archivers only
(content-addressed bodies too), single-segment `archiver_id` and `name` of at most 255
bytes of UTF-8, every indexed string UTF-8-encodable, paths unique under `fold_path`,
`check_relative_path`. What becomes owed is therefore never a batch that would fail the
same way at every retry.

### The read side

`RunTrace` (`core/trace/reader.py:104`) resolves **every** file through the manifest;
the commit point is also the reader's only directory. No accessor probes a file name or
globs a directory, so whatever the manifest does not list is invisible to all of them
alike. The one exception is `evals()` (M31): verdicts are written *after* the run, by
`EvaluationRunner`, and never enter the manifest, so it lists `evals/*.json`, with the
same containment rule as every listed path. It used to be otherwise: `step()` went
through the manifest while the observation, message and screenshot reads probed and
globbed, so a frame written for a
step that never committed was in the trace for one accessor and not another. Do not add
an accessor that stats a path; `tests/test_core/test_trace/test_crash_window.py` holds
every accessor to this, for a crash before the rename and for hand-planted orphans.

**And it opens nothing outside the run directory.** The manifest and the extra indexes
are data, and traces travel (§6): a crafted one could list `/home/me/.ssh/id_rsa` as
step 0's screenshot, and `screenshots` handed that path to an image judge, which
base64-encoded the file into its request. Every listed path now goes through `_listed`
(`reader.py:550`) before anything opens it: the WAL's own rule
(`encoding.check_relative_path`), then containment once symlinks are resolved
(`(trace_dir / rel).resolve()` inside `trace_dir.resolve()`). The three names a run
gives itself (`manifest.json`, `config.json`, `result.json`) must not lead out either.
A violation is a `TraceSchemaError` whose `.path` is the file that *listed* the path;
the file it points at is never opened. `ExtraFileRecord.path` is checked when the index
is read, because `extra_files` hands those paths to callers. A symlink that stays inside
the run reads normally. `test_reader_errors.py::TestAPathTheTraceListsStaysInsideIt` has
a row per accessor; add one when you add an accessor.

**No SQLite.** Trace schemas are heterogeneous and evolve; querying is lazy, typed
accessors, and what comes back is the type that was written:

| Accessor | Returns | Not there |
|---|---|---|
| `config` / `result` | `TraceConfig` / `AgentResult` | `result` is `None` when `result.json` is absent |
| `step_records` / `step(idx)` / `step_count` | `StepRecord`s, in manifest order | `step` → `None` |
| `observation_fragment(step, key)` | the class registered under the qualified key, rebuilt by `from_trace` — or the `FragmentFailure` recorded in its place | `None` |
| `screenshot(step)` / `screenshots` | the bytes / every committed frame's `Path`, step order | `None` / skipped |
| `llm_messages(step)` | every `RecordedLLMCall` of the step, start order | `[]` |
| `trailing_calls` | the calls of the step never surrendered | `[]` |
| `extra_files(step)` / `trailing_extra_files` | `ExtraFileRecord`s | `[]` |
| `read_extra(path)` / `output_schema(sha256)` | bytes / `dict`, checked against size and sha256 | `KeyError` |
| `archive_files(step, archiver_id)` | that step's step-scoped `ArchiveRecord`s for the archiver (content-addressed ones included), index order | `[]` |
| `iter_archive(archiver_id, *, include_trailing=False)` | `(step, ArchiveRecord)` in step order, then `(None, record)` per trailing batch when asked | nothing yielded |
| `blob(sha256)` | bytes of a committed content-addressed archive file (`read_extra(archive_blob_path(sha256))`) | `KeyError` |
| `archive_run_file(capability_id)` | the `ArchiveRecord` of the highest committed run-record version; bytes via `read_extra(record.path)` | `None` |
| `archive_lines(path, model)` (M30) | each non-blank line of a committed line-delimited archive file (`index.jsonl`, `replay.jsonl`) validated as the caller's `model`, in file order; bytes via `read_extra`; a line that is not JSON or does not validate → `TraceSchemaError` naming the file and line. With the accessors above it is how `RunTrace` satisfies `core/archive/source.py`'s `ArchiveReader`, the replay's read side | `KeyError` |
| `evals()` (M31) | `verdict_slug` → `EvalResult` for every `evals/<slug>.json`, sorted by slug and walked in sorted order (which bad file is named first does not depend on the file system's listing order); not cached (a verdict written later is seen); `evals/` or a file in it resolving outside the run → `TraceSchemaError`, as is a file that does not read as an `EvalResult` | `{}` |
| `committed_files` / `read_committed(path)` (M33) | every listing of a committed file in commit order — the run's own three files, what the manifest lists, what each committed extra index lists (a shared blob once per commit that lists it) — as `CommittedFile` (`kind`, `step`, `fragment`, the index entry, the `ArchiveRecord` when it is evidence), each path checked as it is listed; the bytes through that file's own accessor's checks. What consumers that account for a whole run (M33's storage and archive bytes) read instead of walking the manifest's fields themselves | `KeyError` (`read_committed`) |

"Not there" and "not readable" never collapse. Anything the manifest lists that cannot
be read — corrupt JSON, bytes that are not UTF-8, an empty file, a schema mismatch, a
listed file missing on disk, bytes that disagree with their index — raises
`TraceSchemaError` naming the file (`.path`), through one helper (`_parse` /
`_read_model`, `reader.py:710-725`). It parses with `json.loads` after a strict UTF-8
decode and validates in pydantic's Python mode, never `validate_json`: that parser
refuses nesting past 200 levels while pydantic-core's serializer, the writer, emits up
to ~254, so a DOM page about a hundred elements deep, or a deep structured reply,
committed fine and could never be read back. `result.json` is the one file allowed to be
absent, because nothing lists it: `None` means the run died before writing it, while a
file that exists and does not parse raises, because "written by an older schema" and
"never finished" are different facts. Other `OSError`s (permissions, I/O) propagate;
they say nothing about the trace. An evaluation process may never have imported the module that registers a
fragment class, so a registry miss runs `discover` over `spectragent.schemas` and
`spectragent.core.capabilities` once before giving up (`_fragment_class`, `:635`).

**The trace has a format version.** `TraceManifest.format_version` is 2 since the
per-step index; a manifest without the field reads as 1. The accessors whose on-disk
shape changed refuse a format-1 trace with `TraceSchemaError` (`_require_indexed`,
`reader.py:532`), since its fragment failures carry no marker, while `config`, `result`
and the step records stay readable. No shim (AGENTS.md §0.4): re-scoring an old trace
with an image judge means re-running the task. A newer version than the build reads is
refused when `RunTrace` is constructed.

**Steps have an origin (M31).** `StepRecord.origin` is `policy` (a step the run's own
agent decided; every step of every older trace reads back as `policy`, which is right for
them), `restored` (a branch run's prefix: a copy of the source run's step, with the live
observation the restorer re-reached and its `restoration` check) or `terminal` (the
observation after the run ended: no model call, no action). Both new fields are defaulted,
so neither `AgentResult.schema_version` nor `format_version` moved (§4.4's BACKWARD rule).
A reader that means "what the agent did" filters on `origin == "policy"`.
`AgentMetrics.steps` already counts only those, and `TraceView.action_history` keeps the
restored actions apart (`restored_action_history`, each labelled `[restored]`) while
`screenshots` keeps every frame, the terminal one included. Neither field reaches a prompt
(`HistoryObs` renders actions, results and replies only).

On-disk layout, every file name and every record model: `docs/architecture_design.md`
§5.3.

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
  principle is real (`docs/architecture_design.md:19`) and the framework does record
  generously — every negotiated observation fragment is serialized in full, on the
  explicit grounds that storage is cheap and API calls are not (`:3147`). Screenshots
  are `required` in *every* observation mode, including DOM-only runs, because
  `online_mind2web` and `lexbench_browser` grade from them: "mode decides what the model
  sees, not what the run records" (`:1006`). The rejection of per-tick input was about
  *kind*, not size.
- **Behavioural-realism verification belongs in the external harness**, which also owns
  the human reference data (829 recorded paths, ~50 Hz, `HANDOFF_260916_next.md:108`).
  The harness clicks *through* af's action interface so the trajectory under test is
  unchanged. That is the shape to reach for whenever a proposed trace field exists to
  verify the framework rather than to score the agent.

### The second category: environment evidence (ruling D2)

On 2026-10-09 the user added one category beside agent behaviour (user ruling D2,
recorded in `docs/architecture_design.md` §5.1): **environment evidence**, meaning the
state and resources of the world the agent acted in: response bodies, storage
snapshots, raw DOM captures, the environment fingerprint. It passes the test above on
its own terms: had the agent chosen differently these values would differ, and they are
the world the decision was made in, not the framework's execution of it. Three
conditions come with it, and they are what keep it from becoming a back door for
execution detail:

- **explicit opt-in** — only a run whose trace hook has a non-empty `archive` records
  any; the default is none, and a run without it has no `env/` directory at all;
- **its own subtree** — `env/`, read through its own accessors (§3), never mixed into
  steps, observations or messages;
- **its own privacy marking** — `TraceConfig.env_evidence_privacy` (written only when
  archivers were bound; *which* were bound is `negotiation.active_archivers`), plus the
  capability's run record (§5, §6).

The 2026-09-16 line still holds in full: an archive stores resources and state, **not
the CDP event sequence** that delivered them, and **input ticks are still not
recorded**. "It is environment evidence" is not an argument for a mouse trajectory:
the trajectory is how the framework executed a decision, not the world the decision was
made in. Producing evidence is the capability's job and only on pull
(`collect_archive`); persisting it is the trace hook's, and the capability never learns
a step number or the `env/` layout.

If you believe a case genuinely sits on the line, AGENTS.md §3.1 applies: ask, do not
invent a compromise.

### What the browser's evidence looks like on disk (M29)

`docs/architecture_design.md` §4.14 is the authority; this is the reader's map. Every
file is read back through `RunTrace.archive_files(step, key)` / `blob(sha)` /
`archive_run_file("browser")`, never by globbing `env/`.

| Archiver | Step files | What is in them |
|---|---|---|
| `browser.network` | `exchanges.warc.gz` | WARC/1.1, one gzip member per record (warcio). A `request` record per exchange (stripped headers, redacted POST body) linked by `WARC-Concurrent-To` to a `response` record — only when the body is known (`ok`, `redirect`, `websocket`); a payload already written this run is a `revisit` (`identical-payload-digest`). Bodies are stored **decoded**; `Content-Encoding` / `Transfer-Encoding` are dropped and `Content-Length` is the decoded length. |
| | `index.jsonl` | One JSON object per line, three kinds. `exchange` — exactly once per request hop, when it settles: WARC offsets, method, url, status, resource type, frame / loader / session / target, initiator (no stack), timing, the raw request facts (headers, POST sha256 / length / content type), `payload_sha256`, `revisit_of`, `content_range`, `body_status` ∈ `ok` / `redirect` / `too_large` / `streamed_later` / `websocket` / `incomplete`, and a `redaction` ledger (policy `af-privacy-1+browser-1`). `pending` — still in flight at this collection, status only (also one per collection for a written `streamed_later` record whose body is still streaming, `waiting_for: streamed_body`); a record whose **last** line is `pending` was incomplete at run end. `body` — a `streamed_later` body refetched, pointing back at its `record_id`, with the blob's sha256. **No match key** — replay derives keys from the raw facts. |
| | `websocket.jsonl` | Frames: `record_id` of the handshake, direction, opcode, wall time, payload (text) or `payload_base64`. |
| | `body-<sha>.bin` (content-addressed) | A refetched large body. |
| | `replay.jsonl` (a replay run only, M30) | Written instead of the four files above: one `ReplayLogEntry` per serving decision of the step (`hit`, `exhausted`, `synthetic`, `miss`, and `summary` for folded repeats), with the matched `record_id` and level, or the miss's class, reason, answer and nearest candidates; a main-frame document miss is `diverged`. Read it with `archive_lines` or the browser's `replay_log` / `iter_replay_log` / `diverged_steps` (architecture §4.14, "回放日志"). |
| `browser.storage` | `storage.json` | Cookies (values `sha256:<hex>` unless `allow_session_state`), local storage per origin, session storage per tab and origin (values kept, credential-named keys redacted — so the archiver always declares `contains_session_state`), its ledger, and what it could not read: `unreadable_tabs`, `unreadable_origins`, `unread_frames` (out-of-process iframes, not read in v1). **Written only when the state changed**: a step's state is the latest `storage.json` at or before it. |
| `browser.dom_raw` | `dom-<cycle>-<tab>.json.gz` (content-addressed) | Gzip canonical JSON per committed cycle: the redacted `TargetCapture`, DPR, viewport, cleaner key (`self_capture` for a vision run), source, tab, url / loader id, ledger. Cycle and capture time are in the record's provenance, not the bytes, so an unchanged document is stored once. `archive/dom_raw.capture_from_document` turns it back into a `TargetCapture` for an offline re-clean. |
| (capability) | `env/run.browser.json` (run record) | The environment fingerprint, versioned on change. |

Two rules a reader must not miss: an exchange line is written **once** (pending lines
before it carry no evidence), and a `body` line can name a record whose `exchange` line
came in an earlier step.

## 5. Configuration and negotiation reach the trace

`TraceConfig` (`core/trace/models.py:437`) used to be nine identity strings — enough to
say *which* agent and *which* task, not *how it was set up*. Two runs that differed in
`observation_mode` and mouse strategy were indistinguishable on disk, so every cross-run
comparison depended on an external harness keeping its own notes. That was the blank
spot in the framework's own "Record Once, Evaluate Many" (`HANDOFF_260916_findings.md:123`,
N6 at `HANDOFF_260916_next.md`).

Three layers now (`models.py:437-524`):

- **Identity** — `(benchmark, subset, task_id)` is the key an Evaluator joins ground
  truth by. Resolved from the live objects by `core/runtime.py::_component_id`
  (`runtime.py:62`), and it is the **authority**: it exists even for a run assembled
  without the Composer, where `resolved` is `{}`.
- **Configuration** — `resolved` is `ComposedRun.top_level_cfg`, the composed, validated
  Pydantic object: every default filled in, every wired receptor inlined inside its owner
  (`agent[0].llm.provider`). **Not the YAML** — `configs/browser_use_local.yaml` never
  mentions `coordinate_space` or `provider.name` and both are in the object. It is also
  not everything a run resolved: a value a component derives at construction
  (`BrowserCapability` turning a profile name into a watchdog set) lives on the instance
  and is not here. `negotiation` is `TraceNegotiation` (`models.py:302`) — active and
  *required* fragments, active actions, and the `fragment_bindings` / `action_bindings`
  that say what each relative `__wiring__` name meant in *this* environment, plus the
  archive axis's `active_archivers` / `archive_bindings` (archiver key → `capability_id`).
  Everything fully qualified, everything sorted (the upstream results are `frozenset`s
  and iteration order varies between processes).
- **Provenance** — `secrets_recorded`, the one field a reader must check before sharing
  the directory, and beside it `env_evidence_privacy` (which privacy markers the bound
  archivers declared; whether any were bound is `negotiation.active_archivers`, and the
  old `env_evidence_recorded` boolean that duplicated it is gone).

Plus two per-task facts. `task_setup` (`TaskSetupRecord`) is the environment setup
context `execute()` handed to `setup_environment` (`start_url` …, a relative
`replay_archive` already made absolute), value-level redacted with its ledger; a branch
task (M31) re-creates its source environment from it, never from a redacted one.
`branch` (`BranchProvenance`, M31) says where a branch run came from.

**A per-task or per-feature field is written only when it has a value** (user ruling,
2026-10-13): `Field(exclude_if=...)` keeps `branch`, `task_setup` and
`env_evidence_privacy` out of `config.json` when they are `None`, and `extra` when it is
empty, so a plain run carries no `"branch": null` placeholders. An absent key reads back
as the default; no reader changes and no format bump.

`AgentRuntime` passes both explicitly (`runtime.py:586-609`) because it is the only
component holding both, and it dispatches `on_run_start` *after* `prepare()` so the
negotiation results exist, but *before* `setup_environment` so a setup failure still
lands in a trace. `TraceWriter.on_run_start` (`trace_writer.py:268`) accepts them **by
name and derives nothing** — a serializer that rediscovers facts by `getattr` is how the
configuration ended up recorded nowhere in the first place.

### The digest has two fidelities and they must not be confused

`configuration_digest` (`models.py:526`) covers identity + benchmark/subset + resolved
config + negotiation, and deliberately **not** `task` / `task_id` / `extra` /
`task_setup` / `branch` (per task) nor `secrets_recorded` / `env_evidence_privacy` (provenance),
so two tasks of one sweep share a hash — including two that differ only in `start_url`, or
two branches of one sweep (`test_run_configuration.py::TestBranchProvenance`).
Archive settings move the hash through the two existing roads and nothing else: the
binding through `negotiation`, the archivers' knobs through `resolved` (once YAML can
set them, M29). The two `TraceNegotiation` fields M28 added also mean a trace written
after M28 hashes differently from a pre-M28 trace of the same configuration; compare
manifests written by one framework version.

It hashes the **unredacted** view. `TraceWriter` builds both (`trace_writer.py:382-383`):
`resolved_full = redact_config(cfg, record_secrets=True)`, which never reaches disk and
exists only to be hashed, and `recorded`, which does. `WALWriter.init_run` takes it as a
keyword-only `config_hash` (`wal.py:128`) because only the caller still has the
unredacted object. The earlier version hashed the post-redaction dict, where every
unmarked value was the same `"<omitted>"` string — so `max_steps`, `temperature`,
`viewport_*` and `dom_max_elements` all collapsed and the digest merged the arms of every
sweep a researcher actually runs, silently, while looking like it worked. Numbers are
recorded raw now (§6), but an unmarked free-form string — the model name, a
`user_agent` — still collapses, which is why the digest stays on the unredacted view.

The payload stamps `resolved_fidelity` (`models.py:598`), so **a reader cannot recompute
the manifest's `config_hash` and should not try.** Readers compare two manifests. Pinned
by `test_run_configuration.py::TestConfigHash` (`:264`).

### Recipe: record a new run-scoped fact

1. Add the field to `TraceConfig` with a docstring saying what question it answers that
   identity cannot. A per-task or per-feature field gets `exclude_if` so it is written
   only when it has a value.
2. Decide whether it joins `configuration_digest`'s payload (`models.py:586`): yes if two
   runs differing in it are different *configurations*; no if it is per-task.
3. Resolve it in `AgentRuntime` — the only component holding the whole run — and add it
   as a keyword to the `on_run_start` dispatch (`runtime.py:586`). Document it on the
   `RuntimeHook` Protocol (`core/hooks/base.py:77`).
4. Accept it **by name** in `TraceWriter.on_run_start`. Do not derive it there.
5. Extend `tests/test_core/test_trace/test_run_configuration.py`. If it can carry
   user-supplied text in a new credential shape, extend `SECRET_PATTERNS`
   (`spectragent/core/trace/secret_patterns.py`) too.

M28 applied the recipe three times, and each is a worked example:

- **Archive negotiation** — `TraceNegotiation.active_archivers` / `archive_bindings`;
  joins the digest (through `negotiation`); resolved in `AgentRuntime.prepare()`, passed
  as `archive_negotiation=`; `TraceNegotiation.from_results(observation, action,
  archive)` returns `None` only when all three are `None`.
- **`env_evidence_privacy`** — provenance, so not in the digest; resolved in the runtime
  (the sorted union of the bound archivers' `privacy_markers`, `None` when none were
  bound), accepted by name. A sibling boolean `env_evidence_recorded` was deleted
  later: it only restated whether `archive_negotiation` was `None`.
- **`task_setup`** — per task, so not in the digest; resolved in `execute()` as the
  `env_context` it routes to `setup_environment` (`None` when there is none), accepted by
  name and redacted in `TraceWriter.on_run_start` (§6). It can carry user text — a start
  URL with a token — so the secret guard plants one.

M31 applied it once more:

- **`branch`** (`BranchProvenance`: source run id, the source trace relative to the
  sources root, source benchmark / subset / task id, checkpoint step, branch index and
  group) — per task (the task id is `<source_run_id>/s<t>/b<i>`), so not in the digest;
  the sweep-level knobs (`continue_steps`, the teacher's sampling, the restorer's
  `verify`) move the hash through `resolved` as usual. It arrives as the third part of
  `TaskLoader.get_task_context(id)` (`TaskContext{env, loop, branch}`), is passed as
  `execute(branch=…)` and on to `on_run_start(branch=…)`, and is recorded as given. Not
  named "provenance": that word is the `secrets_recorded` / `env_evidence_privacy` layer.

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

Two things make a field safe (`_redact_field`, `redaction.py:234`):

1. **An explicit mark** — `trace_safe("<why this cannot be a credential>", ...)`
   (`redaction.py:102`) in place of the field's `Field(...)`. Example in the wild:
   `TraceWriter.trace_dir` (`trace_writer.py:165-176`).
2. **An annotation that is safe by type** (`is_safe_by_type`): a closed set —
   `Literal[...]`, an `Enum` subclass, `bool`, `None` (`is_closed_set`) — or a number,
   `int` / `float` (and any union made only of those and closed sets, such as
   `int | None` or `int | Literal["unlimited"]`). A closed set is a *proof*, not a
   heuristic: the value can only be one of the alternatives the source enumerates, so
   recording it leaks nothing that reading the source would not; it is what makes every
   conscribe discriminator and every mode switch (`observation_mode`,
   `screenshot_format`, `trajectory_type`, `coordinate_space`) readable without anybody
   marking anything. Numbers are recorded raw by the user's ruling of 2026-10-13: a
   count, a limit, a size or a sampling knob (`max_steps`, `max_actions_per_step`,
   `temperature`, `viewport_width`) is what a reader compares runs by, and it lets a
   branch compare the numeric agent and memory knobs at assembly. Free strings stay
   withheld (the model name, `extend_system_message`), and so does a container of
   values (a list of strings, a tuple of numbers).

Marking a field now only changes what a trace **says**, never whether two traces compare
equal — the digest is taken before redaction (§5). With numbers raw, the marks left to
add are for free strings, and nothing is blocked on a mass-marking pass. There is a
reason not to do one yet: the recipe asks `core/loops/`,
`core/capabilities/`, `core/llm/providers/` and `agents/` to import
`spectragent.core.trace.redaction` *in their field declarations*, which drags `wal` and
`reader` into their import graphs and inverts the rule `models.py:375` states in the same
package ("the trace layer is downstream of everything and must stay importable on its
own"). **The mark's home in
`core/trace/` is provisional** — probably `spectragent/schemas/` or conscribe itself.

`record_secrets: bool = False` (`trace_writer.py:177`) is the escape hatch for a local
run you will not share. It logs a warning, and the trace says so in
`config.secrets_recorded`. It is Config-Driven working as designed — a registrar field,
set in YAML, per run — not a global or an env var.

### The guard

`tests/test_core/test_trace/test_secret_guard.py` is what protects secret fields
**nobody has written yet**. It plants four real-shaped credentials into the real shipped
config, validates against the real composed schema, drives a real `AgentRuntime` with a
real `TraceWriter`, then reads every byte under the trace directory by `rglob`
(`:318`) — walking the filesystem, not the manifest, because a leak in a file nothing
indexes is still a leak. It asserts on *shapes*, not names: sk-style keys, bearer
tokens, `scheme://user:pass@`, `?token=`, devtools websocket URLs. The shapes live in
production code, `spectragent/core/trace/secret_patterns.py` (`SECRET_PATTERNS`,
`find_secrets(text)`; it imports only `re`), because they have a second reader: the
training export scans every text byte it is about to write with them and fails closed
on a hit (M32, `spectragent/export/validate.py::check_credentials`). There is one list,
and no copy of it in a test.

Both halves are load-bearing: `TestGuardCanActuallyDetectALeak` (`:401`) re-runs with
`record_secrets=True` and requires the same strings to **appear**, so the guard cannot
pass because the run wrote nothing.

Add a pattern there whenever a new credential *shape* becomes possible. You do not need to
do anything for a new secret-carrying backend: its `api_key` / `session_url` / `proxy_*`
are open-typed strings and record as `"<omitted>"` the day the class lands. That is the
point of the polarity.

**The guard reads `env/` too.** Environment evidence is free-form bytes no field
declaration covers, and archivers may gzip it. So the scan decompresses a file whose
first bytes are `1f 8b` or whose name ends in `.gz` before decoding it, repeatedly for
a gzip inside a gzip (falling back to the raw bytes when decompression fails). The
guard's run binds an archiving fake capability (`secret_guard_archive`) so `env/` exists
in the redacted fixture: its archiver `secret_guard_archive.evidence` writes benign plain
and `.gz` files. The positive control binds `secret_guard_archive.leak`, which plants
`API_KEY` in one plain and one `.gz` file, and asserts the key is found in **each** file,
which proves the gzip path. The framework never redacts evidence itself (browser header
stripping and form masking are the capability's, M29), so this guard is the
framework's only check on `env/`.

**`task_setup` is redacted by value, not by declaration.** It is free-form per-task
data with no field declarations, so deny-by-default would omit every value and leave a
branch task nothing to rebuild its environment from. It goes through
`core/archive/privacy.py::redact_value` instead: a mapping key that is credential-shaped
(`is_credential_name`: the field-name pattern
`password|passwd|pwd|otp|token|secret|card|cvv` as a substring, or a trailing `key` /
`apikey` / `sig` / `signature` / `auth` / `credential` segment) has its value replaced by
`<redacted>` (a `null` stays `null`); a string that is an absolute URL (`scheme://…`) has
its userinfo replaced whole and the non-empty value of every credential-named query or
`#k=v` fragment parameter replaced, every other byte preserved. Each replacement is a
`RedactionEntry(location=<JSON pointer>, field, rule)` in the record's `RedactionLedger`
(`policy` = `PRIVACY_POLICY`). `record_secrets` disables it exactly as it disables config
redaction.

The guard routes
`START_URL = "https://shop.example/landing?ref=guard&token=Zk3Lm9QpRt7Vx2BnQ8"` through
`env_context`: absent from the redacted run, whose ledger holds `{location:
"/start_url", rule: "url_query", field: "token"}`, and present with
`record_secrets=True`. `<redacted>` is deliberately not `"<omitted>"` (replaced by a
rule vs. set and withheld) and matches none of `SECRET_PATTERNS`. The residual, stated
in `docs/architecture_design.md` §5.6: a secret under a name neither rule recognises is
recorded as is. Changing a rule means bumping `PRIVACY_POLICY`. A test pins a digest of
the rule sources (both name patterns, the placeholder, the URL pattern and the source of
every rule function, the URL rules included), so even a cosmetic edit to those functions
trips it; re-pin the current policy's row then, and say so.

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
  `test_config_redaction.py::TestMarkSurvivesConscribe` (`:176`) is that tripwire.
  Corollary: the committed `_composed.py` is **not** a faithful picture of which fields
  are trace-safe — codegen drops the same metadata, while `ConfigComposer` builds its
  model at runtime from the source classes. Do not reason about redaction from the
  generated file.
- **No `from __future__ import annotations` in a module using `trace_safe()`.** Under
  PEP 563 the annotation is a string resolved in the *subclass's* globals, conscribe sees
  `field_info is None`, and the whole field is dropped. `trace_writer.py:38-43` carries
  the note. `tests/test_generated/test_no_dropped_annotated_fields.py` catches it.
- **`TraceConfig.extra` is NOT redacted** (`models.py:492-497`). It is whatever the
  caller passed to `execute(config=...)` — `BenchmarkRunner._task_snapshot` puts
  difficulty/domain/tags there. A plain dict has no declarations to consult, so
  deny-by-default would omit everything. It is documented as caller-curated, and it is a
  hole the guard test cannot cover. Do not put configuration there.
- **Structure is checked before the mark** (`_redact_structure`, `redaction.py:251`), on
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
  `000_browser.dom.json`, not `000_dom.json` (`fragment_path`,
  `core/trace/encoding.py:78`; `docs/architecture_design.md` §5.5). The negotiated local
  name is a per-run artefact of which capability won negotiation, so
  `000_screenshot.png` means a different thing in a browser run and a desktop run and no
  reader could tell.
- **Read fragments typed.** `RunTrace.observation_fragment(step, key)` (`reader.py:198`)
  takes the qualified key and hands back the fragment's own class, rebuilt by
  `FragmentBase.from_trace` (a `DOMObs` whose `element_tree` is a `DOMState` and whose
  `selector_map` holds `DOMElementNode`s with their bounds, a `ScreenshotObs` with its
  `image_bytes`), or the `FragmentFailure` recorded in its place when the capture
  failed. Test the type (`isinstance(frag, DOMObs)`): a failure is not `None`, and is
  never an empty fragment of the class the key names. Do not write a second decoder for
  a fragment's JSON; if `from_trace` loses something you need, that is the fragment's
  bug.
- **Screenshots come from the manifest's fragment index**, not a glob.
  `RunTrace.screenshot(step)` (`reader.py:236`) is one step's bytes;
  `RunTrace.screenshots` (`:251`) is every committed step's frame as a `Path`, in step
  order: any capability's `screenshot` fragment, whatever image suffix the header gave
  it, because a run configured for JPEG writes `.jpg` and a judge that silently found no
  frames would score the run text-only. A frame written for a step that never committed
  is not a frame of the run, and neither is a capture that came back empty: the
  resolver raises on one (it becomes a `FragmentFailure`), and a `ScreenshotObs` still
  holding `b""` writes no artifact, so the step has no frame. A 0-byte file instead
  would read as damage and fail `screenshots` for the whole run.
- **Every LLM call of a step is there**, in start order: `RunTrace.llm_messages(step)`
  (`reader.py:271`) returns parse retries, refused structured replies (`raw_output`),
  truncations (`partial_output`) and calls cut off by a cancellation (prompt only) as
  `RecordedLLMCall`s; a step that never committed has its calls in `trailing_calls`.
- **Read `negotiation` before you read steps.** `required_fragments` is what tells you
  whether a capture failure ended the run or was recorded as a `FragmentFailure`. A trace
  without it cannot explain its own degraded steps.
- **Scoring that needs a live environment does not go here.** SWE-bench running `pytest
  FAIL_TO_PASS` in the container, OSWorld evaluating VM predicates — that goes through
  `RuntimeHook.on_run_end(result, env)`, the one moment the environment is both alive and
  reachable by a framework-aware component (`core/hooks/base.py:158`). Such a hook writes
  its findings *into the trace*, and an Evaluator reads them back like any other recorded
  fact.
- Verdicts land in each trace's own `evals/` (`evaluator/runner.py:290-313`), named by
  evaluator slug, so a new judge never requires re-running the agent. They are written
  with `fsio.atomic_write` like every other trace file, so a re-score that dies half way
  leaves the previous verdict whole, never a torn one the next pass reads as corrupt.
  They get a plain write's permissions, so a results directory another user reads stays
  readable.
- Fail loudly on an unusable trace. `TraceIncompleteError` with the directory named beats
  a zero score — see `online_mind2web.py:104-125` for the pattern (no task text, no
  screenshots for an image judge). The reader already fails loudly on its side: a
  `TraceSchemaError` is a damaged trace, or a format-1 trace read through a format-2
  accessor, and re-running the evaluation will never change it.

**The training export is the other offline consumer** (M32, `spectragent/export/`,
arch §4.15): the same rules, a different product. It reads source runs only through
`RunTrace` (committed steps, `observation_fragment`, `llm_messages`, `evals()`), opens
`env/` only for occlusion evidence (`browser.dom_raw`) and the replay log's document
misses, and never copies an `env/` file into a dataset (a test compares content hashes).
A run recorded with `record_secrets: true` is never a source. It shares the recorded
step readers with branching rather than decoding a step itself
(`core/loops/restore/source.py::recorded_observation` / `recorded_step_info`), and its
output carries no wall clock, absolute path or machine name, so two exports of one input
are byte-identical.

## File map

| | |
|---|---|
| `core/aop/advice.py` | `Intercept`, `AdviceType`, the `{advice}_{method}` convention |
| `core/aop/weaver.py` | index build, `wrap`, the two assembly-time audits |
| `core/aop/proxy.py` | `AOPProxy`, `_apply_around_chain` (`:471`), `validate_advice_shape` (`:109`) |
| `core/hooks/base.py` | `RuntimeHook` Protocol — lifecycle signatures, `blocking` contract |
| `core/hooks/_registrar.py` | `HookBase`, priority constants, `MissingHandlerError` |
| `core/hooks/trace_writer.py` | the reference hook: `around` registers every call, one `commit_step` at `record_step`, trailing calls at run end; with a non-empty `archive`, the pull of `env.collect_archive()` at each commit point and once at close-out |
| `core/archive/_registrar.py` | `Archiver`, `ArchiverRegistrar`, `ArchiverBase` (identity and declarations only — no capture method) |
| `core/archive/types.py` | `ArchiveFile`, `ArchiveProvenance`, `ArchiveBatch`, `ArchiveContractError` |
| `core/archive/protocols.py` | `ArchivingCapability` (capability side), `ArchiveConsumer` (the one attribute the runtime knows) |
| `core/archive/privacy.py` | `PRIVACY_POLICY`, the shared field-name / credential-name patterns, `redact_url`, `redact_value`, `RedactionLedger`, privacy marker constants |
| `core/negotiation/archive.py` | `negotiate_archive`, `ArchiveNegotiationResult` (`archive`), `IncompatibleArchiveError` |
| `core/step_recorder.py` | "model it as a Protocol method" — the worked example |
| `core/trace/encoding.py` | the one on-disk rule: file names, `TraceFile`, `encode_fragment` / `decode_fragment`, `PendingLLMCall`, `encode_llm_call`, output schemas, the `env/` paths and `encode_archive_file` |
| `core/trace/fsio.py` | `write_durable`, `fsync_dir` and its errno policy, `atomic_write`, plain-write permissions |
| `core/trace/wal.py` | the commit protocol, path rules (file-or-directory included), content-addressed dedup, durable-directory tracking, `commit_trailing` |
| `core/trace/models.py` | `TraceManifest` and its per-step index, `RecordedLLMCall`, `CallFailure`, `TraceConfig`, `TraceNegotiation`, `ArchiveMeta` / `ArchiveRecord`, `TaskSetupRecord`, the digest |
| `core/trace/redaction.py` | `trace_safe`, `is_safe_by_type`, `is_closed_set`, `redact_config` |
| `core/trace/reader.py` | `RunTrace` — typed accessors resolved through the manifest (the archive ones through the extra indexes), `_listed` (containment), `_read_model` (`json.loads`), the format gate |
| `core/trace/errors.py` | `TraceSchemaError` — every way a committed trace is unreadable |
| `evaluator/base.py`, `evaluator/trace_view.py` | the read side |
| `tests/_helpers/trace_builder.py` | builds traces through the real encoder and `WALWriter` — use it, do not hand-write sidecars (`archive_file(...)`, `build_archive_trace(base)` for `env/`) |
| `tests/_helpers/archive_fakes.py` | the fake archiving capability (`contract_archive`), fake archivers, `ArchiveConsumerHook`, `CollectSpy` (the raw env as the trace hook sees it, every batch kept), `make_archive_run(...)` — unique `contract_archive*` keys, because registries are global |
| `tests/_helpers/archive_contract.py` | the shared archive contract harness: the capability-side clauses (no step, unbound archives nothing, prefix routing and clearing, hand over and forget, bound archivers only, provenance, a broken collection raises) and, through `TraceWriter` driven directly, commit → read back → crash window → tail → fail-closed collection → zero-cost empty archive → run records kept per capability beside a second archiving capability; a capability opts in with a factory in its `HARNESS_FACTORIES` |
| `tests/test_core/test_archive/test_archive_boundaries.py` | the layering of §10 on source text: runtime / negotiation / environments never name `TraceWriter` or a concrete archiver module, `core/archive` imports none of its dependents (one exception by exact module name: `core/archive/source.py`, the replay opener, imports `core.trace.reader`), `core/trace` imports only `core.archive.privacy`; no money field in the new models; the browser's evidence readers import neither `core.trace` nor `spectragent.flywheel` |
| `tests/test_core/test_runtime/test_archive_binding_errors.py` | every archive binding error raised by `prepare()` naming the parties, and by `execute()` before `on_run_start` and before setup |
| `tests/test_core/test_trace/test_crash_window.py` | every accessor ignores what the manifest does not list |
| `tests/test_core/test_trace/test_reader_errors.py` | every reader input × every corruption → `TraceSchemaError` naming the file; every listed path × every way out of the run directory |
| `tests/test_core/test_trace/test_fsio.py` | the directory `fsync` errno policy, plain-write permissions, every data `fsync` holding the file's bytes |
| `tests/test_core/test_trace/test_wal_batch_commit.py` | `init_run`'s order, commit order, one `fsync` per directory, the path rule on every channel, injected failures, the 1000-file budget |
| `tests/test_core/test_trace/test_secret_guard.py` | the artifact-level guard |
