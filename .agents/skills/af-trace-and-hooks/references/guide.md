# Hooks and the trace

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

Two halves that only look separate. Hooks are how anything gets *into* a trace; the
trace is the only thing an evaluator is allowed to read. §4 is the judgement call —
read it before you add a field.

## 1. Public AOP boundaries

Hooks declare `__intercepts__`; Loop code calls public methods, never a hook or emitter.
Runtime dispatches `on_run_start/end/error` and wraps agent/environment/model/loop roles.
The loop role can include both the execution strategy and StepSink. Audit their public
methods as a union. `TraceWriter` is the single recording component; no separate metrics
hook or Loop usage tally exists.

`execute_step` starts a step; `record_step` stops its execution clock and seals its facts.
`observe_terminal` gives terminal observation its own boundary. `around_ainvoke` registers
calls before invoking and settles success, failure and cancellation without changing the
result or exception. Every recording error is isolated; actual execution errors propagate.

Keep handlers blocking for snapshot and handoff order. Bounded WAL persistence and retries
belong to the recording worker, not to the Agent's execution wait chain. Do not set the
whole hook `blocking=False`. Use declared `Annotated` configuration fields and regenerate
with the one config generator after changing them.

## 2. One invocation ledger and explicit scope

`RecordingPlan` selects metrics/errors/trajectory/observations independently; `archive` is
the existing receptor for environment evidence. Plan selection happens before copying or
encoding. Metrics-only must never snapshot prompts, replies, screenshots or DOM. Result
persistence also omits trajectory fields in that mode.

`RunCallContext` is instantiated by Runtime per execution and explicitly distributed.
Its task-local scope carries caller, purpose and step without any global ContextVar.
Main decisions use `agent_decision`; prompt helpers use `prompt_helper`. Calls receive a
stable run/step/call identity before invocation. Restore steps create no new model calls.

`RecordedCallMetrics` stores provider usage and status independently of complete messages.
Failure usage counts; missing usage stays missing. `TokenUsage.from_llm_usage/combine` is
the shared step/online/offline counting rule. Preserve cache read/write/image counts and
explicit zero. No recording means None; measured no calls means zero. Known subtotals
must travel with `UsageCoverage`, including every missing call. Never estimate prices.

Step execution time excludes extra recording snapshot work. Capture, encoding and WAL
work is associated with the original step; `recording_timing(step)` reads a later committed
measurement, avoiding a self-referential final timing field. Runtime alone times
`execution_ms`; task drivers own total elapsed time.

## 3. WAL, immutable segments and reader contracts

A sealed segment keeps its original step, calls, observations, action results and handed
over archive batch even when its commit fails. Only a genuinely unfinished step's calls
are trailing. Retry waits are bounded and background-only; buffer exhaustion describes
a lost range and does not stop Agent execution. Public `flush()` allows a bounded explicit
inspection wait without closing the run; normal step handoff does not call it.

WAL validates paths first, durably writes contents and records, fsyncs distinct directories,
then atomically replaces the manifest. Orphans remain invisible. If rename succeeds but
a later fsync fails, reconcile actual disk state before any following write; never let
old memory roll the manifest back. Deduplication and archive versions follow reconciled
commits. Trailing segment IDs distinguish retries from reused list positions.

The manifest retains sparse committed steps, expected range, missing steps, continuous
`complete_through`, and historical problems. Repaired holes can restore current complete
status without erasing history. `result.json` records execution outcome; recording health
never replaces it. Current format is 5 and result schema is 3.0, with no compatibility shim.

Readers only follow committed indexes and validated paths inside the run directory.
`RunTrace.call_metrics` and `trailing_call_metrics` do not decode prompts or pictures.
`require_recording()` distinguishes unselected scope from corruption. Viewing and totals
can use saved steps after a gap; history-dependent branch/export consumers must remain
within their verified complete prefix and require the relevant selected channels.

Archive pull happens at the segment boundary, with raw environment and no step argument.
Collection batch ownership is not action causality; preserve each event's capture time
and IDs. Cross-batch references cannot point at uncommitted objects. Replay source reads
are environment operations and must not be swallowed as current-run recording failures.

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
| `browser.network` | `exchanges.warc.gz` | WARC/1.1, one gzip member per record (warcio). A `request` record per exchange (stripped headers, redacted POST body) linked by `WARC-Concurrent-To` to a `response` record — only when the body is known (`ok`, `redirect`, `websocket`); a payload already written in this collection batch is a `revisit` (`identical-payload-digest`). Bodies are stored **decoded**; `Content-Encoding` / `Transfer-Encoding` are dropped and `Content-Length` is the decoded length. |
| | `index.jsonl` | One JSON object per line, three kinds. `exchange` — exactly once per request hop, when it settles: WARC offsets, method, url, status, resource type, frame / loader / session / target, initiator (no stack), timing, the raw request facts (headers, POST sha256 / length / content type), `payload_sha256`, `revisit_of`, `content_range`, `body_status` ∈ `ok` / `redirect` / `too_large` / `streamed_later` / `websocket` / `incomplete`, and a `redaction` ledger (policy `af-privacy-1+browser-2`). `pending` — still in flight at this collection, status only (also one per collection for a written `streamed_later` record whose body is still streaming, `waiting_for: streamed_body`); a record whose **last** line is `pending` was incomplete at run end. `body` — a `streamed_later` body (over the inline tier, streamed while the page read it) once its request ended, pointing back at its `record_id`: `ok` with the blob's sha256, or `incomplete` / `too_large` with a `note`. **No match key** — replay derives keys from the raw facts. |
| | `websocket.jsonl` | Frames: `record_id` of the handshake, direction, opcode, wall time, payload (text) or `payload_base64`. |
| | `body-<sha>.bin` (content-addressed) | A streamed large body (at most 64 MiB; a larger one is `too_large`). |
| `browser.replay_log` (a replay run only; `browser.network` is refused there) | `replay.jsonl` | One `ReplayLogEntry` per serving decision of the step (`hit`, `exhausted`, `synthetic`, `miss`, and `summary` for folded repeats), with the matched `record_id` and level, or the miss's class, reason, answer and nearest candidates; a main-frame document miss is `diverged`. Read it with `archive_lines` or the browser's `replay_log` / `iter_replay_log` / `diverged_steps`, which find it under this archiver (architecture §4.14, "回放日志"). |
| `browser.storage` | `storage.json` | Cookies, local storage per origin, session storage per tab and origin, all values as they are (so the archiver always declares `contains_session_state`), an empty ledger naming the policy, and what it could not read: `unreadable_tabs`, `unreadable_origins`, `unread_frames` (out-of-process iframes, not read in v1). **Written only when the state changed**: a step's state is the latest `storage.json` at or before it. |
| `browser.dom_raw` | `dom-<cycle>-<tab>.json.gz` (content-addressed) | Gzip canonical JSON per committed cycle: the redacted `TargetCapture`, DPR, viewport, cleaner key (`self_capture` for a vision run), source, tab, url / loader id, ledger. Cycle and capture time are in the record's provenance, not the bytes, so an unchanged document is stored once. `archive/dom_raw.capture_from_document` turns it back into a `TargetCapture` for an offline re-clean, the Skyvern ref maps keyed by integers again. |
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
  the sweep-level knobs (`continue_steps`, `history_source`, prompt contribution configuration, the teacher's sampling,
  the restorer's `verify`) move the hash through `resolved` as usual. It arrives as the third part of
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
  `TraceSchemaError` is a damaged trace, or a trace of another format, and re-running
  the evaluation will never change it.

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
| `core/trace/encoding.py` | the one on-disk rule: file names, `TraceFile`, `encode_fragment` / `decode_fragment`, `PendingLLMCall`, `encode_llm_call`, output schemas, prompt images (`store_prompt_images` / `restore_prompt_images` / `prompt_image_text`), the `env/` paths and `encode_archive_file` |
| `core/trace/fsio.py` | `write_durable`, `fsync_dir` and its errno policy, `atomic_write`, plain-write permissions |
| `core/trace/wal.py` | the commit protocol, path rules (file-or-directory included), content-addressed dedup, durable-directory tracking, `commit_trailing` |
| `core/trace/models.py` | `TraceManifest` and its per-step index, `RecordedLLMCall`, `PromptImageRef`, `CallFailure`, `TraceConfig`, `TraceNegotiation`, `ArchiveMeta` / `ArchiveRecord`, `TaskSetupRecord`, the digest |
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
| `tests/test_core/test_trace/test_prompt_images.py` | prompt images stored once: the 12-step growing history, lossless round trips (non-standard base64 included), what is left alone, the commit order, and every way a referenced image can be damaged |
| `tests/test_core/test_trace/test_reader_errors.py` | every reader input × every corruption → `TraceSchemaError` naming the file; every listed path × every way out of the run directory |
| `tests/test_core/test_trace/test_fsio.py` | the directory `fsync` errno policy, plain-write permissions, every data `fsync` holding the file's bytes |
| `tests/test_core/test_trace/test_wal_batch_commit.py` | `init_run`'s order, commit order, one `fsync` per directory, the path rule on every channel, injected failures, the 1000-file budget |
| `tests/test_core/test_trace/test_secret_guard.py` | the artifact-level guard |
