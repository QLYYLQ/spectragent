# The data flywheel

> This guide names symbols and files, never line numbers: find them with `rg` and read the
> current implementation before editing. The defaults quoted below come from the generated
> config (`spectragent/generated/_composed.py`); after a class changes, regenerate before
> trusting them.

The flywheel turns ordinary agent runs into training data in five stages. Every stage after
the first reads only what an earlier stage committed to disk, so a stage can be rerun, or
moved to another machine, given the run directories it reads; the export and the report
need no browser or model at all.

```
record   spectragent bench | spectragent run    configs/flywheel/record_ui_tars_pinned.yaml
           replayability: pinned, network_source: live, trace_writer.archive: three archivers
           -> traces/<run>/env/  (WARC + index.jsonl, storage.json, raw DOM, run.browser.json)
replay   spectragent run --replay-archive DIR    configs/flywheel/replay_ui_tars_archive.yaml
           network_source: archive (isolated, strict misses), base_time: from_archive,
           trace_writer.archive: browser.replay_log in browser.network's place
           -> env/steps/NNN/browser.replay_log/replay.jsonl
branch   spectragent bench --no-eval             configs/flywheel/branch_ui_tars_archive.yaml
           agent_loop: branch (restorer: prefix_replay), task_loader: trace_branches
           -> branch runs: restored steps, the policy step(s), a terminal observation
export   spectragent export                      configs/flywheel/export_sft.yaml
           exporter: openai_messages, its translator, student_run_config
           -> datasets/<name>/{train,validation}.jsonl, *.samples.jsonl, images/, manifest.json
measure  spectragent flywheel run | report      a chain spec, not a run YAML
           record -> same_path -> branch -> export x2 -> report/{report.json,report.md}
```

## 1. The pipeline, and who owns each part

| Layer | Owns | Where |
|---|---|---|
| Browser capability | Capture (the recorder connection, each bound archiver's run-time session, redaction before a file is built), the replayability tier, replay serving and miss answers, retargeting and restoration checks, the student capture check, and every reader of its own evidence formats | `core/capabilities/browser/`: `recorder.py`, `archive/`, `replay/`, `restoration.py`, `capture_check.py`, `index_points.py`, `archive_stats.py`, `cooperation.py`, `navigation.py`, `settle.py` |
| Archive axis | `ArchiverRegistrar`, the `ArchivingCapability` and `ArchiveConsumer` Protocols, `ArchiveFile` / `ArchiveBatch`, the shared privacy rules and ledger, the replay read side (`ArchiveReader`, `open_archive`) | `core/archive/` |
| Negotiation and runtime | `negotiate_archive` (right after memory negotiation), `bind_archivers` as the last step of `prepare()` on the raw environment, `seed_messages`, typed Loop inputs, the branch provenance handed to the trace | `core/negotiation/archive.py`, `core/runtime.py` |
| Recording component, WAL, reader | Archive demands, pulling `collect_archive()` at commit points, call/step attribution, usage, the `env/` subtree, immutable run-record versions, gaps and recording health; manifest-based reading and shared context reconstruction | `core/hooks/`, `core/trace/` |
| Loop | ReAct branching (configured history reconstruction, restore, policy/continuation steps and terminal observation), the restorer axis, fresh execution state (`LoopRun`, `ReactRun`, `BranchRun`), loop-fragment builders and the checkpoint-specific `SourceRun` wrapper | `core/loops/branch.py`, `core/loops/restore/`, `core/loops/run.py`, `core/loops/react.py`, `core/loops/_loop_fragments.py` |
| Prompt intervention | Stable function references, run/step contribution generation and explicitly wired helper calls; Agent-owned placement and history formatting | `core/hooks/prompt_contribution.py`, `schemas/prompt.py`, `agents/` |
| Task loading | The `trace_branches` loader, the step-selector axis, `RequireEval` | `core/data_loader/task/` |
| Export | `ExportRunner`, the exporter and translator axes, the student renderer, validation, split, benchmark loaders | `spectragent/export/` |
| Flywheel | The chain, the metrics, the report and `spectragent flywheel`; a package, not a registry | `spectragent/flywheel/` |

The capability never learns a step number and never touches the WAL. Whatever step *t*'s
commit collects belongs to step *t* (architecture §4.6, "记录器从环境拉取证据"). A capability
reads a source run only through `ArchiveReader`, which `RunTrace` satisfies structurally;
`core/archive/source.py` is the one module of `core/archive` that imports `core.trace`.

**Current scope (the user's ruling of 2026-10-13).** This phase delivers teacher
trajectories: record, replay, branch. The student side (how its messages are assembled,
"the student sees a screenshot and emits coordinates") moves to the next phase, and the
export package is frozen: `spectragent/export/`, `spectragent export`,
`configs/flywheel/export_sft.yaml` and `configs/flywheel/ui_tars_student.yaml` get no new
features and no new design decisions, only the adaptations a cross-cutting change needs to
keep their tests green (architecture §4.15). Independent task judging was re-enabled by the user on
2026-10-14 as a separate stage (`spectragent eval`, architecture §4.11). It reads committed
traces and explicit references, reports valid-verdict coverage separately from task
correctness, and never changes restoration checks. No flywheel stage requires a verdict:
filters remain optional and off by default, and decontamination checks only an explicit
benchmark list, empty by default. Judge errors and missing evidence are unevaluable,
not graded task failures. Time-sensitive tasks are judged against their recorded date
and evidence, never today's website or the model's current knowledge.

### The layering rule

Put each responsibility in the layer that owns it. For every new or moved function or class,
ask which layer that is (a concrete Loop or `AgentLoopBase`, the runtime, a hook, the WAL and
trace reader, the capability or its backend), and treat a misplacement as a defect in its own
right. Never park Loop-generic, runtime or trace logic in `ReactLoop`, and never as
module-level private helpers of one concrete class. If the right owner does not exist yet,
say so instead of parking the code.

The shipped flywheel code gives each kind of new piece a precedent:

- Browser evidence formats are read by pure functions in the browser package that take an
  `ArchiveReader` or the rows it yields: `replay/archive_index.py::iter_index_lines`,
  `replay/replay_log.py::iter_decisions` / `document_miss_total` / `diverged_steps`, and
  `archive_stats.py` (`body_completeness`, `revisit_saving`, `challenge_documents`,
  `site_rule_candidates`). The export and the metrics call them and never parse `index.jsonl`
  or `replay.jsonl` themselves. Rule 5 of
  `tests/test_core/test_archive/test_archive_boundaries.py` forbids those readers to import
  `core.trace` or `spectragent.flywheel`.
- The translator only arranges the call; the browser geometry it uses lives in
  `core/capabilities/browser/index_points.py`.
- `capability.py` delegates: restoration to `restoration.py`, the capture comparison to
  `capture_check.py`, the recording's run-time side to `archive/run.py::BrowserArchiveRun`,
  every cooperation rule to `cooperation.py`, replay consistency to `replay/consistency.py`.
- A restorer restores the environment. Its `RestorationContext` carries no model, memory or
  recorder, so it cannot do more.
- Each shared read has one copy: `core/trace/sources.py` (`match_runs`, `task_identity`,
  `unreadable_reason`) for the loader, the export and the metrics; `core/trace/usage.py`
  (`recorded_calls`, `recorded_usage`) for the calls a run recorded and the tokens they
  reported, read by the bench's and the chain's token limit, the report's `token_use` and
  the privacy measure;
  `core/trace/reconstruction.py` (`recorded_observation`, `recorded_step_info`,
  `recorded_form`, `first_difference`) for branch replay and student rendering;
  `core/trace/secret_patterns.py` for the secret guard, the export's credential scan and
  the privacy metric.
- Execution state and common outcome handling belong to `LoopRun` and `AgentLoopBase`;
  ReAct-specific retry state belongs to `ReactRun`. Model-call accounting belongs to the
  recording component, and Runtime resolves identity and measures `execution_ms` once.
  `core/loops/restore/source.py::SourceRun` wraps trace reads with checkpoint-specific
  errors; it exposes no forwarding aliases for the shared reconstruction functions.
  ReAct consumers use public `execute_step`, `cap_actions`, `remember_rejected` and
  `remember_reply`, without accessing another class's private helpers.

## 2. Configuring each stage

The shipped files in `configs/flywheel/` are the starting points, and `configs/README.md`
explains each knob with examples. Every file in `configs/*.yaml` and `configs/flywheel/*.yaml`
must compose and pass `prepare()` (`tests/test_core/test_composer/test_shipped_config_integration.py`;
the export YAML only composes), so a file that is not a run root, such as a chain spec, goes
elsewhere.

### 2.1 Record

```yaml
environment:
  - env_type: browser
    capabilities:
      - capability_id: browser
        replayability: { replayability_id: pinned, random_seed: 7 }
        network_source: { source_id: live }
        backend: { provider_id: local_chrome, headless: true }
        # mouse_trajectory, typing_rhythm, dom_cleaner, dom_serializer: as in any block
hook:
  - hook_id: trace_writer
    trace_dir: traces
    archive:
      - archiver_id: browser.network
      - archiver_id: browser.storage
      - archiver_id: browser.dom_raw
```

| Block | Knobs (default) |
|---|---|
| `pinned` | `base_time` (`run_start`; `from_archive` is for replays), `random_seed` (1, at least 1), `timezone` (`UTC`), `locale` (`en-US`), `device_scale_factor` (1.0), `user_agent` (null: keep the browser's own and pin only `Accept-Language`; a normal Chrome identifier must name the installed Chrome's major version), `reduced_motion` (true) |
| `browser.network` | `max_inline_body_bytes` (5 000 000, at most 64 MiB: a larger body streams while the page reads it, any type except a document; over 64 MiB is `too_large`), `body_read_timeout_s` (10), `command_timeout_s` (1), `drain_timeout_s` (2), `max_post_data_size` (65536), `strip_request_headers` (null; added to `Cookie`, `Authorization`, `Proxy-Authorization`), `require_replayable` (true), `allow_session_state` (false) |
| `browser.storage` | `allow_session_state` (false: refuse a non-fresh profile; values are stored as they are either way), `command_timeout_s` (1) |
| `browser.dom_raw` | `allow_session_state` (false), `self_capture_timeout_s` (10) |

Run it like any benchmark config, or record one task:

```bash
uv run spectragent bench --config configs/flywheel/record_ui_tars_pinned.yaml \
    --benchmark online_mind2web --no-eval        # plus --subset, --task-id, --limit
uv run spectragent run --config configs/flywheel/record_ui_tars_pinned.yaml \
    --start-url URL --task "..."
```

- The recorder connection, a second CDP client the capability owns, opens only when an
  archiver is bound, the tier is not `none`, or the network source intercepts. With none of
  the three, nothing extra is opened or sent.
- Whatever opens the recorder connection (a bound archiver, a tier other than `none`, the
  `archive` network source) needs a browser the capability launched itself. It is refused
  with an explicit `cdp_url` (the capability knob, or the task context's `cdp_url`), with an
  injected control client (`_inject_cdp_client`; a test that needs the recorder swaps only
  the transport, with `_inject_cdp_factory`), without a backend, and on a provider that does
  not declare `NETWORK_CAPTURE` or declares it without implementing
  `NetworkCaptureProvider.persistent_profile`. No cloud provider declares it (`lexmount`,
  `browser_use`, `hyperbrowser`, `agentbay`, `airtop`, `skyvern`). `local_chrome` passes;
  it is the one provider declaring `NETWORK_CAPTURE`, `LAUNCH_ARGS` and `NETWORK_ISOLATION`.
- The browser starts from a fresh profile when the provider's `persistent_profile()` returns
  None; for `local_chrome` that means no `user_data_dir`, the `Default` profile directory
  and no `--user-data-dir` in `extra_args`. The profile is YAML only: the old
  `CHROME_USER_DATA_DIR` fallback is gone, so `config.json` always shows it. Otherwise every
  bound archiver needs `allow_session_state: true`. A capability `profile` whose
  `watchdogs_exclude` lists `archive` refuses every bound archiver, because nothing would
  drain the archive scope.
- Each problem names both sides, and each check reports all of its problems in one
  `BrowserCooperationError`. Every row is decided by configuration and task data, so all of
  them are raised at `prepare()`, before the trace is opened, on a `spectragent run
  --dry-run` too: `BrowserCapability.check_setup` (through `Environment.check_setup`, with
  the task's setup context and the archivers about to be bound) runs the whole check, the
  tier's rows, the network source's rows and a replay's consistency included.
  `bind_archivers` re-checks the binding rows before it binds, and setup re-checks
  everything before Chrome starts (architecture §4.14, "协作检查一览"). `spectragent bench`
  runs `prepare()` once for the first task before the sweep (the pre-flight): a refused
  configuration runs no task, opens no trace and exits 2.
- `browser.network` has `require_replayable` (default true), so binding it under
  `replayability: none` raises `BrowserCooperationError` at `prepare()`. To archive an evaluation run as it is, write
  `- {archiver_id: browser.network, require_replayable: false}` in `trace_writer.archive`.
  That archive was recorded with unpinned randomness and clocks and may not replay.
  `browser.storage` and `browser.dom_raw` have no such knob.
- `pinned` needs a fresh profile and refuses provider knobs that contradict it: a different
  value passes nowhere, an equal one everywhere (`deterministic_rendering`, whose V8 seed
  must equal `random_seed`; another `force_device_scale_factor` or `user_agent`; an
  `extra_args` entry setting one of its switches differently). It changes how live sites
  behave and is visible to anti-bot checks, so evaluation and anti-bot runs stay on `none`.
- `pinned` also launches Chrome with `TZ=<timezone>` (`ReplayabilityProtocol.launch_env()`,
  handed to the provider with the switches, written first in `launch_flags` and so in the
  digest). Chrome keeps one owner of the time zone and locale overrides per renderer
  process, the frame that set them first; once it leaves (an iframe removed, an opener that
  moved to another site) the other frames fall back to the browser's defaults within
  0.2–0.3 s, and a cross-site popup that keeps its opener reads the default locale from its
  first document. `TZ` makes the default zone the pinned one. The default locale is the
  host's (on macOS neither `--lang` nor `LANG` changes it), so with archivers bound setup
  measures both defaults right after launch, before any override, and the fingerprint
  records them as `browser_defaults`.
- `pinned`'s user agent override carries the browser's own client hints
  (`client_hints.py`): setup reads them right after launch, before the recorder prepares
  any target, in a temporary background `chrome://version` tab (`navigator.userAgentData`
  exists in secure contexts only; the first tab's `about:blank` is not one), whenever the
  identifier is Chrome's own form (`user_agent: null`, or `Mozilla/5.0 (<platform>)
  AppleWebKit/537.36 (KHTML, like Gecko) Chrome/<version> Safari/537.36` with nothing after
  it). Pages then read the brands, platform and form this Chrome reports by itself, on every
  request, the first request of a tab that cannot reach its opener included. A Chrome
  identifier naming another major version, platform or form than the browser is refused at
  setup (`BrowserCooperationError`, naming both; not at `prepare()`: it depends on the
  launched browser), and so is a read that fails. Any other string carries none (brands
  empty, no `Sec-CH-UA`). With `--user-agent` at launch (a set `user_agent`) Chrome itself
  reports empty high-entropy values, and the override carries them as Chrome reports them.
  The fingerprint records what was sent (`replayability.client_hints`; `page.userAgentData`
  is its low-entropy part), and a replay compares it after launch: refused when the tier pins
  an identifier, a warning when it does not; a pinned source recorded before the field
  existed is refused at `prepare()`.
- `pinned` measures the browser's current media theme after launch, before any business
  target is prepared (`color_scheme.measure_color_scheme`, an isolated world). It pins
  that `light` or `dark` value on every frame target, including new tabs and OOPIFs, in the
  same `Emulation.setEmulatedMedia` command as reduced motion. The run plan records it as
  `replayability.color_scheme`, not a YAML knob. Replay takes that source value before
  launch and never compares or re-measures its host's theme. A missing or invalid source
  value is refused at `prepare()`; a failed live measurement refuses recording. Do not
  invent a light default, edit old archives, or put this state on a Loop or TraceWriter.
  `none` reads and changes nothing; a site's own account or cookie theme remains its own.
- Key events carry no `nativeVirtualKeyCode` (architecture §4.13): on macOS a key the page
  left unhandled (Enter submitting a form natively, most often) made Chrome open
  `chrome://settings/help` in front of the agent's tab, which then stayed a background page
  for the rest of the recording.
- Tabs a page opens are prepared before their first request whatever opened them: the
  recorder writes a waiting target's whole batch and then its resume, and only then waits
  for the replies (a tab its opener cannot reach — `target=_blank` without `rel`,
  `rel=noopener`, `window.open(..., 'noopener')` — answers its page-side commands only after
  it runs; architecture §4.14, "逐 target 的准备流水线"). `create_tab(url)` creates the tab at
  `about:blank` and navigates it once the recorder prepared it.
- The agent's focus follows a tab its actions opened (`new_tabs: follow`, the capability's
  default; architecture §4.14 "页面新开的标签页"): the observation after the actions shows
  the newest tab the focused tab opened during them, unless Chrome reported it opened without
  the user's activation (`Page.windowOpen{userGesture: false}`: a popup a page opens as it
  loads or from a timer, one click's second window). A focused tab that closes itself hands
  the focus to the tab that opened it, under `stay` too. Every fragment of that capture
  carries `focus_moved{reason, from_tab, to_tab}`; record, replay and branch configs keep
  one `new_tabs` value.
- Evidence lands in `traces/<run>/env/`. A run that binds no archiver has no `env/` at all.

### 2.2 Replay

```yaml
replayability: { replayability_id: pinned, random_seed: 7, base_time: from_archive }
network_source:
  source_id: archive
  isolation: required                 # none_acknowledged: Fetch interception alone
  shift_dates: none                   # independent of base_time; all four combinations are valid
  matcher: { levels: [exact, normalized], fuzzy_threshold: 0.5, site_rules: configs/replay_rules }
  miss_policy: { miss_policy_id: strict }   # extra_benign_hosts: [...]
  # max_fulfill_body_bytes (64 MiB, at most 72 MiB), fulfill_timeout_s (10)
  log_mode: full                     # summary: fold repeats after log_repeat_limit (3)
```

```bash
uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml \
    --start-url URL --task "the recorded task" --replay-archive traces/<recorded-run>
```

- The source run arrives per task as `replay_archive`, a run directory. A relative one is
  relative to the launch directory: `AgentRuntime.execute` makes it absolute
  (`schemas/task.py::resolve_env_context_paths`) before it is recorded as `task_setup` and
  handed to setup, for every caller — `spectragent run`, a task loader, a Python caller
  building `env_context` itself; `prepare()` checks the absolute one. The `archive` source
  without it is a `BrowserCooperationError` row at `prepare()`
  (`network_source: archive ↔ task context: no replay_archive`), and so is `live` with it.
  A path that is not a committed run directory (not a directory, or no `manifest.json`)
  raises `ArchiveOpenError` from `open_archive`, also at `prepare()`.
- A replay opens the recorder connection, so the ownership rules of §2.1 apply: it runs
  only on `local_chrome`, never on a cloud provider or an explicit `cdp_url`.
- A replay binds `browser.replay_log` where a recording binds `browser.network`; the other
  archivers stay. Each archiver has one meaning (the user's ruling of 2026-10-13):
  `browser.network` always records live traffic, `browser.replay_log` the replay server's
  decisions, one `replay.jsonl` per step and nothing else (its only knob is
  `allow_session_state`). The cooperation check matches them to the source by need, never
  by id, and names both sides in each row:
  - an `archive` source without an archiver with the `replay_log` need:
    `network_source: archive ↔ bound archivers [...]: none records the replay log`, raised
    at `prepare()` whether other archivers are bound or none are;
  - an `archive` source with an archiver with the `fetch_response` need (`browser.network`):
    `network_source: archive ↔ archiver browser.network: the archiver records live
    traffic, and a replay has none ...; bind browser.replay_log instead`;
  - a `live` source with `browser.replay_log`: `network_source: live ↔ archiver
    browser.replay_log: ... a live source replays nothing`.
- At `prepare()`, `check_setup` opens the source through its manifest and compares its
  fingerprint (`env/run.browser.json`, read by `archive_index.load_fingerprint`) with this
  configuration. The source's public `check_config(reader)` also validates rule levels,
  scope conflicts and exhaustion coverage from committed request index metadata; it reads
  no WARC bodies. Setup compares again before Chrome starts,
  then loads the whole archive to serve it: the tier and its parameters
  except `base_time`, `shim_sha256`, the viewport, the launch-flag digest with the
  network-routing switches stripped on both sides (`TZ` included), and the browser's
  defaults: each value this host's OS can say before launch
  (`LaunchArgsProvider.planned_browser_defaults`: the time zone by ICU's rule, the locale on
  macOS through CoreFoundation's choice for Chrome's bundle) against the recording's
  `browser_defaults`. After launch, before any target is prepared, setup measures Chrome's
  own defaults and compares every value again (`replay/consistency.py::browser_default_problems`),
  which decides what the OS could not say. A mismatch refuses the run, strictly, with no
  switch; a source with no `browser_defaults` is refused. A different
  Chrome version, an unpinned user agent or a changed redaction policy only warns, in the
  replay's own fingerprint. A run that was itself a replay is refused as a source.
- `isolation: required` hands `--host-resolver-rules=MAP * ~NOTFOUND` (no exception: exact
  `localhost` is cut too, so a run recorded against a local site replays from its archive
  with the site still up) and `--no-proxy-server` to a provider declaring
  `NETWORK_ISOLATION`, and refuses
  `proxy_server`, `proxy_bypass_list`, routing switches in `extra_args`, and a `startup_url`
  other than `about:blank`.
- `strict` fails documents (a main-frame miss marks its step diverged), and answers beacons,
  reports and analytics hosts with 204 and every other subresource with 404, both marked
  `x-replay-miss: 1`. A CORS preflight the archive lacks gets a synthesised 204. Not replayed
  in v1, and logged as misses: WebSockets, a nested dedicated worker's script, bodies over
  `max_fulfill_body_bytes`.
- Every rule file contains a `rules` list. Explicit scopes constrain host, URL/path,
  method and effective port. Highest priority wins; equal-priority matches are refused,
  and files do not implicitly select host suffixes. Rules never merge. `exhaustion` is
  required (`repeat_last` or `miss`); the published baseline explicitly selects reuse,
  while the framework supplies no fallback. Every served candidate must choose the same
  rule identity as its request. `matcher.levels` is an upper bound, tried in fixed strength
  order, including Range; a stronger sequence's exhaustion never selects a weaker resource.
  Canonicalisation performs formatting only: no global timestamp, size, random-number or
  callback guesses. Use scoped `ignore_*` and `keep_*` declarations with counterexamples.
  Rules are read once for the actual replay and frozen. Their exact text, origin, per-file
  and set digests enter the existing run fingerprint; `recorded_site_rules(reader)` verifies
  and reconstructs them through committed archive accessors without rereading the source.
  The shipped Ctrip body `id` omission now applies only to its ad endpoint.
- `log_mode: full` retains each event. Explicit `summary` mode folds semantically identical
  events beyond `log_repeat_limit`, preserving count, category and first/last occurrence
  times. `iter_decisions` returns represented counts and keeps folding metadata; batch step
  ownership does not imply that step's action caused every asynchronous request.
- JSONP callback names (`replay/jsonp.py`, architecture §4.14 "JSONP 回调名"): when the
  archive serves a recorded JSONP answer to a request asking another callback name, the
  responder writes the requested name over the recorded one at the call the body opens with,
  and the hit's log entry carries `callback_rewrite` (`parameter`, `recorded`, `requested`).
  Only provable wrappers: a `Script` / `XHR` / `Fetch` request, a JavaScript, JSON,
  `text/plain`, `text/html` or `application/octet-stream` answer (or none), a body opening
  with `NAME(`, `typeof NAME === 'function' && NAME(`, `NAME && NAME(` or one of them inside
  `try {` (whitespace, comments, a leading `;` and a BOM allowed before), exactly one recorded
  query parameter whose value is `NAME`, and a requested value that is itself an identifier
  path. Everything else is served byte for byte. That is what makes ignoring a random-part
  callback parameter in a site rule safe (`douban.com.yaml` `callback`, `le.com.yaml`
  `jsonp`).
- Read the log with `replay_log(reader, step)`, `iter_replay_log(reader)` or
  `diverged_steps(reader)` over `spectragent.core.archive.source.open_archive(run_dir)`.
  They find it under `browser.replay_log`; a file named `replay.jsonl` under another
  archiver is not read.

### 2.3 Branch, and same-path replay

```yaml
agent:
  - agent_id: ui_tars                  # the source run's agent, closed-set and numeric knobs
    agent_loop:
      loop_id: branch
      max_steps: 30
      max_actions_per_step: 1          # must equal the source's; checked at assembly
      continue_steps: 0                # ReAct steps after the policy step
      terminal_observation: true       # the branch default
      history_source: source          # or restored, independent of strict/warn
      restorer:
        restorer_id: prefix_replay
        verify: strict                 # warn: record failed checks and go on
        screenshot_max_distance: 5     # 0-64, Hamming distance of 64-bit dHashes
        pace: none                     # recorded_llm_latency: wait as the source waited
    llm: { ..., temperature: 0.8 }     # the teacher; model and sampling may differ (shipped: 0.8)
# the browser block is the replay block plus retarget_tolerance_px (2.0 CSS px), and
# trace_writer.archive is the replay's: browser.replay_log, browser.storage, browser.dom_raw
task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded          # the sources root (null: traces)
    sources: null                      # globs under data_dir (null: ["*"])
    step_selector: { selector_id: all }   # every_k {k, offset} | explicit {steps} | last
    branches_per_step: 3               # the shipped value; the branches differ only by sampling
    agent_outcome: any                 # done | max_steps | error | timeout | vetoed (result.json)
    require_eval: null                 # {evaluator_id, label: 1, slug: null}
    require_archive: null              # null: [browser.network]; []: none
    on_unbranchable: raise             # skip
```

```bash
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --list    # task ids only
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
```

- A task id is `<source_run_id>/s<t>/b<i>`. Its context carries the source's setup context
  (re-resolved through the source benchmark's loader when it is missing or carries
  redactions), `replay_archive`, the `checkpoint` Loop input, and the branch provenance that
  only the trace receives.
- Candidates are committed policy steps whose prefix is committed, whose observation was
  recorded and that the source's trace is complete through (`complete_through`). The trace's
  own ledger is a hard condition, not a filter: a trace never closed (`writing`) has no
  candidate; an `incomplete` one still gives every checkpoint before its hole. How the agent
  ended is the filter, `agent_outcome` (`any` by default; it reads `result.json`). A source
  that cannot be branched (it does not read back — an older trace format included — is
  itself a branch or replay run, lacks a `require_archive` archiver or its fingerprint, has
  no recoverable setup context, or shares its `run_id`) stops the sweep with every reason at
  once, or is left out under `on_unbranchable: skip`.
- Before any setup, `BranchLoop.weave` raises `IncompatibleCheckpointError`, naming both
  values, for a source trace never closed or not complete through the checkpoint, another agent, a different closed-set or numeric agent knob, a different
  memory strategy or memory knob (`max_messages`, `window_size`), a different
  `agent_loop.max_actions_per_step` (the one loop setting compared; `max_steps` and the
  rest are the branch's own), a different observation or action negotiation, a source loop
  other than `react`, or a source that is itself a branch. Numbers are recorded raw, so
  they are compared here; a free-form string nobody marked trace-safe is recorded as
  `"<omitted>"`, and the seed check and the replay of the recorded calls catch it, still
  before any action runs. The `llm` block is never compared.
- Teacher sampling (architecture §4.1 "teacher 采样"). The branches of one checkpoint get
  identical inputs (the branch index reaches only the trace), so sampling alone makes them
  differ: the shipped branch config runs the teacher at `temperature: 0.8` with
  `branches_per_step: 3`; the same-path config keeps the recording's model and default
  temperature, and nothing requires the teacher to differ from the source. The metrics
  report's duplicate-branch rate says whether the spread is enough.
- `history_source: source` reconstructs and compares the exact recorded decision prompts;
  only `purpose=agent_decision` calls pass through the parser. `restored` instead uses
  actual observations, retargeted actions and results through the Agent's public async
  `build_restored_history` contract. Missing results or unsupported history are errors
  regardless of `verify: warn`. Both decide on the live restored checkpoint observation.
- Explicit `prompt_contribution` hooks wrap the async `build_react_prompt` slot. A stable
  Python function receives `PromptRequest`, optionally uses its wired helper, and returns
  text or `PromptContribution`; the Agent owns system/turn placement. The default scope
  generates once at a branch's first policy step; each-policy-step mode still reuses the
  same contribution for parse retries. History reads recorded contributions and never
  regenerates them. Helper calls carry `purpose=prompt_helper`, count toward experiment
  usage, and never become Agent replies or teacher thoughts. Code references, parameters,
  scope and helper configuration are experimental inputs, not recorder settings.
- `trace_branches` has no eval loader: pass `--no-eval`. A configured evaluator is refused
  before the sweep starts.
- Write branch runs outside the sources glob; the shipped file sets the trace writer's
  `trace_dir` to `traces/branches`. A branch run is unbranchable, so under
  `on_unbranchable: raise` it would stop the next sweep.
- Focus moves are not restored on their own: `prefix_replay` re-executes the recorded actions
  and observes as the recording did, so a tab the recording followed is followed again by the
  same rule, before the same observation, and the check compares the tab captured. A move not
  reproduced shows as `url_mismatch` or `screenshot_distance`. `browser.switch_tab` is
  re-targeted by the tab's position in `browser_meta.tabs`, which lists tabs in the order they
  opened, the same order in both runs.
- Same-path replay is a branch sweep (`configs/flywheel/same_path_ui_tars_archive.yaml`):
  `step_selector: last`, `continue_steps: 0`, `verify: warn` so every step gets a check,
  `branches_per_step: 2` for replay determinism, `agent_outcome: any`,
  `on_unbranchable: skip`. It costs one teacher call per task, for the last step.

### 2.4 Export

```yaml
exporter:
  - exporter_id: openai_messages
    sources_root: traces               # null: traces
    sources: ["recorded/*"]            # null: ["*"]; branch runs included
    filters:
      agent_outcome: any               # done | max_steps | error | timeout | vetoed
      require_eval: null               # optional: {evaluator_id, label: 1, slug: null}
      exclude_restoration_diverged: true
      max_document_misses: 0           # unlimited: no replay-log filter
      exclude_sites: []
    split: { by: site, validation_fraction: 0.1, salt: af-split-1 }
    decontamination: { against: [], match: either }     # against: [{benchmark, subset}, ...]
    translator:
      translator_id: index_to_coordinate
      occlusion: check                 # none: no occlusion check
      occlusion_unknown: raise         # untranslatable
      untranslatable: drop_step        # truncate | keep_as_text | fail
    student_run_config: configs/flywheel/ui_tars_student.yaml
    thought_source: teacher            # none
    thought_index_refs: drop           # keep
    length: { max_tokens: 65536 }      # image_tokens: qwen_vl (default) | fixed + fixed_image_tokens
    output_dir: datasets/sft_v1
# optional, read by decontamination and the reference-leak scan:
# eval_loader: [{ benchmark_name: ..., subset: ..., data_dir: ... }]
# task_loader: [{ benchmark_name: ..., subset: ..., data_dir: ... }]
```

`uv run spectragent export --config configs/flywheel/export_sft.yaml` prints the samples per
split and the drops by reason, and exits non-zero on any failure.

- One `exporter` block per document. The student YAML holds one agent on a ReAct-family loop
  and no hooks; it is assembled with `prepare()` and never run. Its agent must define
  `format_react_output`; the teacher's class must define `parse_react_thought` when
  `thought_source: teacher`; the student's environment-source fragments must be a subset of
  what the source recorded; its screenshot pipeline (viewport, format, DPR) must match the
  recording's.
- A branch run takes its source run's verdicts and context prefix, so the source run must be
  among the matched sources, or the branch is filtered out. Branches of one checkpoint with
  identical targets are flagged `duplicate_of`, not removed.
- Independent evaluation is optional and the export does not need it: `filters.require_eval`
  defaults to `null` (the shipped export too), so a run the teacher failed is exported as
  well, labelled by `labels.step_status` and any verdicts.
- `decontamination.against` is an explicit list of `{benchmark, subset}` refs and nothing is
  inferred from verdicts. The default `[]` decontaminates nothing (the manifest says
  `decontaminated: false`); `null` is refused at composition. Listing the benchmark the
  sources were recorded on excludes them all (`ExportEmptyError`). Each ref is read
  through this document's `eval_loader:` / `task_loader:` blocks with that
  `benchmark_name`, else through that benchmark's registered loader with its defaults;
  `subset: null` takes every loader the document configures for the benchmark, or the
  registered loader's default subset. `match: task_id` drops a source recorded on that
  benchmark whose task id its eval loader lists; `site_instruction` drops one whose
  start-URL host and normalised instruction (case-folded, whitespace collapsed) match a task
  of its task loader; `either` drops on either test. A benchmark whose loaders cannot load
  fails the export (`ExportConfigError`).
- `occlusion: none` consults no occlusion evidence. A `browser.click` or `browser.type` on a
  covered element is taught at the centre of the element's visible box as if nothing covered
  it (its translation record says `occlusion: unchecked`), and `occlusion_unknown` never
  applies.
- `length` must fit the student. The shipped student keeps every turn's screenshot, about
  36k image tokens for a 30-step segment at 1280×720. A longer record fails by name; nothing
  is truncated.
- An existing `output_dir` is refused. Output goes to `<output_dir>.partial/` and is renamed
  only after every check passes. The same inputs export to the same bytes.

### 2.5 Measure: the chain and the report

Architecture §4.16 specifies the chain, the metrics and the report's rules. The chain spec
is a plain YAML file (`ChainSpec`, `extra="forbid"`). It is not a run root, so it stays out
of `configs/flywheel/`.

```yaml
name: first-live
root: traces/chains/first-live        # everything lands under it (traces/ is git-ignored)
evidence: live                         # live | rehearsal
tasks: {benchmark: online_mind2web, subset: All, task_ids: [...], limit: null}   # record / control only
token_limit: 2000000                   # every run stage together; unlimited (the default): no limit
record:    {config: <recording YAML>, timeout: 600, evaluate: false}   # timeout defaults to 300
control:   {config: <the recording with archive: []>}                  # optional overhead control
same_path: {config: configs/flywheel/same_path_ui_tars_archive.yaml}
branch:    {config: <branch YAML>}
export:    {config: <export YAML>, repeat: 2}                          # 1: no determinism check
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}  # value or env, exactly one
```

```bash
set -a; . ./.env; set +a              # neither spectragent bench nor the chain reads .env
uv run spectragent flywheel run --spec chain.yaml [--stages record,same_path]
uv run spectragent flywheel report --spec chain.yaml [--out DIR]
uv run spectragent flywheel report --root traces/chains/first-live --evidence live [--name N] [--out DIR]
uv run python -m tests._helpers.flywheel_rehearsal --out <new dir>   # fixture rehearsal, no spend
```

- Each stage calls an existing CLI's `main(argv)` in this process. The chain writes only path
  fields: `trace_writer.trace_dir` for each run stage, the `trace_branches` loader's
  `data_dir`, and the exporter's `sources_root`, `sources` and `output_dir`. A stage YAML
  missing one of those blocks, or holding two, is refused before anything runs. Everything
  else (agent, tier, archivers, selector, `verify`) is the stage YAML's own.
- A chain's export YAML uses `filters.require_eval: null` (the chain does not evaluate) and
  `decontamination: {against: []}` (the sources are the benchmark's own tasks) — both the
  defaults, and what the shipped `export_sft.yaml` writes.
- A run stage succeeds when the bench exits 0 or 1 and wrote at least one new run; a task
  with an infrastructure failure is data. The bench exits 3 when a guard vetoed the sweep
  (the vetoed task is the summary's last row): the chain stops there and the vetoed stage's
  runs never feed the next one. It exits 4 when the sweep reached the token limit the chain
  handed it (below). `same_path` and `branch` refuse `evaluate: true`. With the export selected,
  an existing `datasets/first` or `datasets/second` is a `ChainConfigError` before any paid
  stage runs.
- The token limit (the user's ruling of 2026-10-14). `token_limit` is the tokens every run
  stage may use together, a number of at least 1 or `unlimited` (the default: the chain
  counts nothing and passes no limit, exactly as before). Tokens are counted from the call
  records, never from `result.json`'s `metrics.usage`, without restoring full prompts or images:
  `core/trace/usage.py::recorded_usage` adds every committed step's calls and the trailing
  ones, a completed call's `completion.usage` and a failed call's `failure.usage`, each by
  the step record's rule (`TokenUsage.from_llm_usage`: `total_tokens`, else prompt plus
  completion). A call whose provider reported no usage adds no tokens and is counted and
  named (`traces/<role>/<run>: step S call K (status)`), never read as zero. Before the first
  stage the chain counts every run already under the root, so a stage-by-stage run stays
  inside one limit; each run stage gets what is left as `spectragent bench --token-limit`,
  which adds each finished task's tokens and, once its total reaches the limit, lets that
  task finish, starts no other and exits 4 (`TOKEN_LIMIT_EXIT_CODE`). Then no later run or
  export stage starts, the report still runs when selected and states the stop
  (`report.json` `token_use.limit_reached`, the first lines of `report.md`), `chain.json`
  records `stopped_by_token_limit{stage, tokens_used, token_limit, stages_not_started,
  calls_without_usage}` and each run stage's `token_use`, and `spectragent flywheel run`
  exits 4. A sweep runs one task at a time, so a task is never cut off and a sweep overshoots
  by at most one task. The bench's pre-flight refuses a limit when no hook writes a trace.
  The evaluator's judge calls are not in any trace and are not counted.
- Exit codes: 0 done, 1 a stage failed (`ChainStageError`, recorded in `<root>/chain.json`;
  for `report`, also a published dataset that does not read back or an `--out` that cannot be
  written, named in the message), 2 a bad spec or argument, 4 the chain stopped at its token
  limit (`run`; the report is still computed when selected).
- Every metric carries its denominator. A zero denominator gives `null` with a reason, never
  0. A measure that skips a run's damaged evidence names it in its `skipped`, and `report.md`
  lists those too. Every measure a recommendation rests on counts the distinct sites its data
  came from (`sites`).
- Each recommendation is `supported`, `partial` or `insufficient`, with `sites` and a
  `status_reason` for anything below `supported`. `supported` needs live evidence from at
  least `SUPPORTED_MIN_SITES` (10) sites; fewer is at most `partial` ("insufficient sample"),
  and rehearsal evidence is at most `partial`. A 2–3-task live run is a smoke test. Thresholds
  are named constants in `flywheel/report.py` (`<what>_MIN_<quantity>` /
  `<what>_MAX_<quantity>`), written into each rule by name and value, and stay in code until
  a live run over enough sites calibrates them. The per-step archive overhead budget,
  `ARCHIVE_MAX_P50_OVERHEAD_MS` (300 ms), is defined once in `core/archive/budget.py` and read
  by both the report and the browser's overhead benchmark.
- The decisions are `miss_policy`, `recording_coverage`, `site_rules` and `retention`; the
  report format is `af-flywheel-report-7` (`-4` added `archive_overhead.settle_wait`: per
  role, the p50 / p95 of the settle wait before each observation, the share the cap ended
  and the observations whose page state was unknown, read from the `settle` record on every
  browser fragment through `settle.py::recorded_settle`; `-5` added `token_use`: the limit
  from the spec, every role's recorded tokens and calls, the calls without usage, and
  `limit_reached`; `report --root` has no spec, so its `limit` is `null`; `-6` added each
  site's `callback_rewrites`: the replayed responses, same-path and branch, served under the
  JSONP callback name the page asked for, counted from the replay log with folded repeats
  weighted). Report keys,
  headings and rule texts never use a planning code (`test_report_schema.py` pins it).
- The privacy measure checks only the two gates of the threat model "only me" (the user's
  ruling of 2026-10-13): what was sent to the model (`model_requests`: the text in the
  messages of every recorded request of every readable run, read through `RunTrace`; inline
  base64 image data is not text and is skipped) and the exported datasets (`datasets`: every
  file under `datasets/`, gzip peeled). `env/` and the rest of a trace stay on the recording
  machine and are not searched, so a site's own public key in its pages is no leak. Each gate
  counts planted values and `SECRET_PATTERNS` shapes, with the requests or files holding
  them; a run whose trace or one of whose recorded calls does not read back is skipped whole
  and named.
- The chain does not apply the report's site exclusions. `sections.sites.yaml` in
  `report.json` has the `filters.exclude_sites` shape: add it to the export YAML and export
  again by hand.
- An empty branch executed no action from its checkpoint on (`metrics.empty_branch`: its
  teacher call failed, its reply ran no action, or the run ended before the teacher decided).
  It is left out of the strict share (`branch_coverage`, `miss_policy`), the
  recording-coverage input and the per-site branch share, and counted by reason in
  `metrics.branch_coverage.empty` (the user's ruling of 2026-10-13). The duplicate rate does
  not compare a branch whose checkpoint step ran no action (`not_compared`).
- The live plan (a first run over 10 tasks, then the full chain) is local,
  `tmp/flywheel/M33_live_plan.md`. It has not run and needs the user's approval of route,
  task count and token ceiling; the ceiling is the spec's `token_limit`, a concrete number in
  every live spec (2000000 for the twelve-site run of 2026-10-14).

## 3. Extension recipes

### 3.0 Steps every recipe shares

A new strategy on an existing axis:

1. Subclass the axis base in the axis package and set `__registry_key__`. Pick a key that
   YAML 1.1 reads as a string: never on / off / yes / no / true / false / null / ~ in any
   case (`tests/test_core/test_composer/test_yaml_safe_values.py` refuses them). "Not
   enabled" is spelled `none`.
2. Make sure an import registers it. Each package's `__init__` imports its implementations,
   and an owner's module imports whatever its `__wiring__` lists.
3. Add the key to the owner's `__wiring__` list when the owner restricts keys. Every
   flywheel owner does, except `ExporterBase` (`"translator": "action_translator"`) and
   `TraceWriter` (`"archive": "archiver"`), which have no key list to update: their
   receptor takes any *registered* key, and validation still refuses an unregistered one.
4. Declare knobs as `Annotated` constructor parameters or dataclass fields with real
   constraints. A strategy never reaches its knobs through a name someone else looks up
   (af-add-a-receptor §0).
5. Run `uv run spectragent config generate`, then `uv run pytest tests/test_generated -q`, and prove the
   knob reached the owner's generated block (af-regenerate-and-verify §5).
6. Update the key lists in the registrar's rows of `AGENTS.md` §2.2 and architecture §6,
   and the architecture sections in the recipe's Design section row.
   `tests/test_docs/test_registrar_coverage.py` checks that each registrar has a row with
   the right discriminator, not which keys it names, so nothing else catches a stale list.
7. Run the axis's tests (each recipe lists them), `test_receptor_hygiene.py` and
   `tests/test_generated/test_no_dropped_annotated_fields.py`.

A new axis, that is, a new registrar:

- `create_registrar(...)`, a Protocol, a base, and a `default_*()` factory for owners built
  by hand. The receptor goes on its owner as af-add-a-receptor §2 lays out; the layering rule
  (§1) picks the package.
- Add it to `collect_registrars()` in `spectragent/core/registries.py`, leaves before
  owners. An empty registry cannot be a Composer layer, so create and document it now and
  collect it with its first implementation (af-add-a-receptor §2, the `ArchiverRegistrar`
  case).
- Add its rows to `AGENTS.md` §2.2 and architecture §6 in the same change; the coverage
  test fails otherwise.
- A required single-instance receptor reaches every hand-written block of its owner:
  shipped configs, `configs/README.md`, test configs, skill examples. `rg` an existing
  receptor key of the same owner to find them all.

### 3.1 Archiver

| | |
|---|---|
| Registrar | `ArchiverRegistrar` (`archiver_id`, dotted `<capability>.<id>`), `core/archive/_registrar.py`; collected as `archiver` |
| Design section | Architecture §4.5 ("归档：第三条 provides 轴"), §4.14 (the recorder connection; "Browser 归档器：配置实例与运行期会话" and one subsection per archiver), §5.6 (privacy), §6 |
| Protocol, base | `Archiver` (`archiver_id`, `privacy_markers`). `ArchiverBase` holds identity and declarations only, never a capture method. Browser: `BrowserArchiverBase` and `BrowserArchiveSession` in `core/capabilities/browser/archive/base.py` |
| Demand | `TraceWriter.__wiring__["archive"]`, a list receptor; every key fully qualified |
| Provides | the capability's `__wiring__["archiver"]` list (browser: `BrowserCapability`) |
| Tests to copy | `tests/_helpers/browser_archive.py`: add the archiver to the harness's default set in `archiving_capability` and give `BrowserDriver.produce` something for it to capture (fire its events with `self.recorder.fire(method, params, self.page_session())`, as `exchange` does), then run `tests/test_core/test_archive/test_archive_contract.py`. Also `test_browser_archiving.py::TestTiming`, `test_browser_archive_network.py`, `test_browser_archive_storage.py`, `test_composer/test_archive_yaml_surface.py` (add the class to `test_every_archiver_knob_reaches_the_generated_config`), live `test_browser_archive_live.py` |

For a browser archiver:

- Hand-write `__init__` with `Annotated` knobs, `allow_session_state` among them. Every
  browser archiver has it, and the non-fresh-profile check reads it.
- Declare `needs`, the capture channels the capability opens for archivers (`ArchiveNeed` in
  `archive/base.py`: `fetch_response`, `raw_dom`, `observation_cycle`, `replay_log`). They
  open at setup from the union of the bound archivers' needs; binding after setup cannot
  open one. A new channel extends `ArchiveNeed` and the capability's setup. The
  cooperation check also reads two of them: `fetch_response` marks live traffic (refused
  under an `archive` source) and `replay_log` the replay's log (required under an `archive`
  source, refused under `live`).
- An archiver that only consumes CDP events (`Runtime.consoleAPICalled`, say) needs no
  channel. It declares `needs = frozenset()`, as `browser.storage` does, lists the events in
  its session's `event_methods()`, and sends what enables them from `target_commands`
  (`NetworkSession` sends `Network.enable`). The recorder subscribes to the union of
  `event_methods()` over the participants present when it opens. Never list
  `Target.attachedToTarget` or `Target.detachedFromTarget`: every participant receives those
  anyway.
- `open(ctx)` returns this run's `BrowserArchiveSession` and is pure construction, with no CDP
  call. The configured instance never holds a session, scope or buffer.
- The session is a recorder participant. Every member defaults to a no-op in
  `RecorderParticipantBase`, and `collect()` returns `[]`, so override only what you use:
  `target_commands`, `fetch_patterns`, `event_methods`, `admit_event`, `on_event`,
  `on_paused` (with `pause_budget_s`), `wants_cycles` and `on_cycle`, then `collect()` and
  `aclose()`.
- `admit_event(method, params, session_id) -> RecorderEvent | None` runs synchronously in the
  client's receive loop. Return small metadata only, because the archive scope keeps its last
  1000 events; return `None` to admit nothing; never modify `params`, which the other
  participants share. This is where bytes and credential values leave the event: bodies go
  to the session's side store, keyed by the returned event's `event_id`, and stripped header
  values never enter the scope. `on_event` then receives what this session admitted, plus
  the target bookkeeping, synchronously on the archive scope's loop. A raise in either
  method fails the next collection (`ArchiveChannel.errors`).
- `collect() -> list[ArchiveFile]` hands over everything captured since the last collection,
  swaps its buffers out in one synchronous statement, and raises when it cannot collect.
  `ArchiveFile` takes `archiver_id`, `name`, `media_type`, `data`, `meta`,
  `content_addressed=False` and `scope="step"`. `name` is one safe path segment, a step
  file's `archiver_id` is `<capability>.<id>`, `meta` is
  `ArchiveProvenance(captured_at, cycle=None, ids={})`, `content_addressed=True` stores the
  bytes once per run under their sha256, and `scope="run"` is for capability run records
  (JSON, never content-addressed).
- A batch that breaks the contract fails the run. `ArchiveFile.__post_init__` refuses a bad
  file at construction, the environment refuses a file under another capability's prefix,
  and `TraceWriter._checked` refuses a batch with a file from an unbound archiver, two files
  on one path, or two run records of one capability. The clauses in
  `tests/_helpers/archive_contract.py` state the rest.
- Redact before you build an `ArchiveFile`, with ledger entries: the shared rules are in
  `core/archive/privacy.py`, the browser's in `archive/redaction.py` under
  `BROWSER_REDACTION_POLICY`, which a rule change bumps. When the evidence can carry session
  state, override `privacy_markers` as
  `frozenset({MARKER_BROWSER_REDACTED, MARKER_SESSION_STATE})`, as `browser.network` and
  `browser.storage` do. The base returns only `MARKER_BROWSER_REDACTED` (the capture applied
  the browser's rules), so an override must keep it. Report withheld fields through
  `redaction_facts()`, which enters the fingerprint.
- `requires_replayable` defaults to False. Override it when an archive of an unpinned run is
  of no use; `browser.network` returns its `require_replayable` knob, so a user can still
  archive a `none` run, and a new archiver with the same need takes the same knob.
- Import the class in `core/capabilities/browser/archive/__init__.py` and add its key to
  `BrowserCapability.__wiring__["archiver"]`. conscribe resolves those keys only when it
  builds a config (`spectragent config generate` and every `ConfigComposer`), and one key that does not
  resolve makes it drop the capability's whole wiring without an error (§5). With the
  import, a broken module fails as an `ImportError`; without it, `discover()` skips the
  module with a WARNING. The capability iterates the bound instances and never branches on
  an archiver id (receptor hygiene Rule 2).
- Put the reader of the new format next to the archiver, as a pure function over
  `ArchiveReader`. Consumers reach the files only through `RunTrace.archive_files` /
  `iter_archive` / `blob` / `archive_run_file` / `archive_lines`.

A new archiving capability implements `bind_archivers` and `collect_archive`
(`CapabilityBase` raises `NotImplementedError` on a non-empty bind) and passes the contract
in af-add-a-capability, "If your capability provides archivers": write an `ArchiveHarness`
factory and add it to `HARNESS_FACTORIES` in `tests/_helpers/archive_contract.py`, keyed by
the capability's `capability_id` (`TestTheFactories` checks the key). The completeness guard
in `test_archive_contract.py` fails any shipped capability that declares archivers without a
factory. A new browser archiver needs no factory: the `browser` entry covers it once it is
in the browser harness (the table above). The registrar rows exist and `archiver` is
already collected; regenerate all the same.

### 3.2 Network source

| | |
|---|---|
| Registrar | `NetworkSourceRegistrar` (`source_id`), `core/capabilities/browser/replay/network_source.py`; collected as `network_source` |
| Design section | Architecture §4.14 ("回放：网络来源与 miss 策略" and the subsections after it; "协作检查一览") |
| Protocol, base | `NetworkSourceProtocol`: `intercepts`, `isolation`, `open_replay(reader) -> RecordedArchive`, `serve(recorded, ctx) -> ReplayServer`, `fingerprint()`. `NetworkSourceBase` carries the live network's defaults; `default_network_source()` is `live` |
| Owner | `BrowserCapability.__wiring__["network_source"]`, a required single-instance receptor |
| Tests to copy | `test_browser_replay_receptors.py`, `test_browser_replay_cooperation.py`, `test_browser_replay_consistency.py`, `test_browser_replay_isolation.py`, `test_browser_replay_server.py`, live `test_browser_replay_live.py` |

- The `archive` source never lets a request reach the network. That is its own rule, not a
  framework law (architecture §4.14): the recorder fails every unanswered Request-stage
  pause once a participant declares `serves_requests`, and `TestNeverContinue` in
  `test_browser_replay_server.py` pins it for `archive`. A source that falls back to the
  network on a miss would be a new source with a rule of its own; design it with the user
  first.
- Isolation is the source's own property. Do not add a provider boolean beside it.
- Rules about whether a configuration can work go into `browser/cooperation.py`: one
  function, every problem naming both sides, all reported together, at `prepare()` (through
  `check_setup`) and again at `bind_archivers` and setup. What must match the source run
  goes into `replay/consistency.py`, read against the source's fingerprint only.
- A source with a receptor of its own (`archive` carries `miss_policy`) is not a dataclass.
  It hand-writes `__init__`, declares the receptor as a class annotation with a `default_*()`
  factory, uses `from __future__ import annotations` for the stub, and types nested knobs as
  pydantic dataclasses (§4).

### 3.3 Miss policy

| | |
|---|---|
| Registrar | `MissPolicyRegistrar` (`miss_policy_id`), `replay/miss_policy.py`; collected as `miss_policy` |
| Design section | Architecture §4.14 ("回放：网络来源与 miss 策略") |
| Protocol, base | `MissPolicyProtocol.decide(miss: ReplayMiss) -> MissDecision`, pure. `MissPolicyBase` has no default `decide`; `default_miss_policy()` is `strict` |
| Owner | `ArchiveNetworkSource.__wiring__["miss_policy"]`, nested as capability, `network_source`, `miss_policy`. Import the module in `archive_source.py` so the key exists when the wiring is read |
| Tests to copy | `test_browser_replay_miss_policy.py`, the server tests (`TestMisses`, `TestNeverContinue`), the nesting checks in `test_browser_replay_receptors.py` |

- A decision names the request class (`document`, `benign`, `subresource`), the answer
  (`fulfill` with status and headers, or `fail` with an error reason) and whether the replay
  diverged. There is no "continue".
- Keep the conventions `strict` follows: an answer the replay made up carries
  `x-replay-miss: 1`; a document fails with `Failed`, because a 404 or `BlockedByClient` cuts
  off Chrome's http fallback; a main-frame document miss is `diverged`.
- WebSocket handshakes and requests that bypass Fetch never reach the policy.
- A miss policy cannot forward a request: the `archive` network source never puts a
  request on the network, and that is the source's own rule, not a framework law
  (architecture §4.14). Falling back to the live network on a miss is therefore not a
  miss policy; it would be a new network source, a new `NetworkSourceRegistrar` key
  (§3.2), designed with the user before anyone writes it.

### 3.4 Replayability tier

| | |
|---|---|
| Registrar | `ReplayabilityRegistrar` (`replayability_id`), `replay/replayability.py`; collected as `replayability` |
| Design section | Architecture §4.14 ("`replayability`：回放档位"; "协作检查一览") |
| Protocol, base | `ReplayabilityProtocol`: `replayable`, `needs_archive_clock`, `needs_client_hints`, the `device_scale_factor` property, `launch_args()`, `launch_env()`, `plan(*, t0_ms, browser_user_agent, client_hints=None) -> ReplayabilityPlan`. `ReplayabilityBase`; `default_replayability()` is `none` |
| Owner | `BrowserCapability.__wiring__["replayability"]`, a required single-instance receptor |
| Tests to copy | `test_browser_replayability.py` (`TestRegistryAndGeneratedConfig`, `TestPinnedSwitches`, `TestPinnedPlan`, `TestShim`, `TestCooperation`), `test_browser_client_hints.py`, `test_browser_replay_consistency.py` |

- The tier is the only source of its Chrome switches and launch environment (both through
  `LAUNCH_ARGS`; `pinned`'s environment is `TZ`) and of its shim. Its run plan
  (`ReplayabilityPlan`) is a recorder participant that sends per-target commands and reports
  fingerprint fields (`replayability_args`, `replayability_env`); the configured instance
  keeps no run state. A participant's commands go out before the target runs but their
  replies may come after it does; never make a later command depend on an earlier reply.
- Record and replay must use the same tier, switches, seed, user agent, client hints and
  viewport. The consistency check compares the tier, the parameters its plan fingerprints
  (all but `base_time`) and `shim_sha256` before launch, and the client hints the plan's
  user agent override carries after launch (`client_hint_problems`), so a new tier
  fingerprints every parameter that changes what a page sees. A tier whose plan overrides
  the user agent says `needs_client_hints` and puts what it cannot honour in the plan's
  `problems`; setup refuses those before any target is prepared.
- Provider conflicts and the fresh-profile rule live in `cooperation.py`.

### 3.5 Restorer

| | |
|---|---|
| Registrar | `RestorerRegistrar` (`restorer_id`), `core/loops/restore/_registrar.py`; collected as `restorer` |
| Design section | Architecture §4.1 ("`BranchLoop`：从检查点分叉", the restorer), §5.3 (`RestorationCheck`); the browser side is §4.14, "恢复：重定位与校验" |
| Protocol, base | `Restorer.restore(ctx) -> AsyncGenerator[RestoredStep, None]`. `RestorerBase` is a dataclass with `Annotated` fields that keeps nothing per run; `default_restorer()` is `prefix_replay` |
| Owner | `BranchLoop.__wiring__["restorer"]`, annotated only on the plain mixin `_BranchLoopReceptors` |
| Tests to copy | `test_restore_prefix_replay.py`, `test_branch_loop.py`, `test_branch_context_identity.py`, `test_composer/test_branch_yaml_surface.py`, live `test_browser_branch_live.py` |

- Restore the environment and nothing else, through `RestorationContext` (`env`, `act_fn`,
  `negotiated_obs`, `source`, `checkpoint_step`).
- Yield one `RestoredStep` per step *k* < *t* as soon as it is restored and verified, then
  step *t*'s verified observation. The Loop commits each one as it arrives, so an
  interrupted restore still leaves its earlier steps in the trace.
- Call `env.verify_restoration` exactly once per restored observation, in order: a
  capability may report a count as the increase since its previous call. Judge with
  `restore/checks.py::judge`, whose reason order is fixed (architecture §5.3,
  `RestorationCheck`).
- Under `strict`, raise `RestorationDivergedError(check, observation)` at the first failed
  check, before any further action. Let `RunAbort` propagate;
  `test_abort_handler_hygiene.py` scans `core/loops/` recursively.
- A snapshot restorer (`capability_checkpoint`) needs the `RestorableCapability` protocol the
  design names (architecture §4.1) and nobody has built. Design that first.

### 3.6 Step selector

| | |
|---|---|
| Registrar | `StepSelectorRegistrar` (`selector_id`), `core/data_loader/task/step_selector/__init__.py`; collected as `step_selector` |
| Design section | Architecture §4.10 (the `trace_branches` loader and its selectors) |
| Protocol, base | `StepSelector.select(candidates) -> list[int]`, ascending and without repeats. `StepSelectorBase` is a dataclass with `Annotated` fields; `default_step_selector()` is `all` |
| Owner | `TraceBranchesTaskLoader.__wiring__["step_selector"]` |
| Tests to copy | `test_step_selectors.py`, `test_trace_branches_loader.py` |

- A selector is a pure choice: no I/O, nothing kept per source. It may return steps that are
  not candidates; the loader skips them and records the reason under `<source>#s<t>`.
- `Field(min_length=...)` on a top-level knob does not survive generation, so enforce such a
  bound at construction (`ExplicitSteps.__post_init__`).

### 3.7 Action translator

Recipes 3.7 to 3.9 extend the export package, which is frozen (§1, "Current scope"). Raise
any of them with the user before starting.

| | |
|---|---|
| Registrar | `ActionTranslatorRegistrar` (`translator_id`), `spectragent/export/translate/_registrar.py`; collected as `action_translator` |
| Design section | Architecture §4.15 ("翻译器") |
| Protocol, base | `ActionTranslator.translate(sample, trace) -> Translation`. `ActionTranslatorBase` is not a dataclass: knobs are keyword parameters of a hand-written `__init__`, `untranslatable` included. A translator without knobs of its own writes no `__init__`; one with knobs writes the whole signature. `default_translator()` is `identity` |
| Owner | `ExporterBase.__wiring__["translator"]`, any registered key; import the module in `spectragent/export/__init__.py` |
| Tests to copy | `tests/test_export/test_translate/` (`test_identity.py`, `test_index_to_coordinate.py`, `test_registrar.py`), `test_browser_index_points.py`, live `tests/test_export/test_export_live.py` |

- A translator does the student-independent half of the conversion. Whether the student can
  write the result is decided when it renders.
- Report what cannot be translated as an `Untranslatable` reason and leave the
  `untranslatable` policy to the renderer. `occlusion_unknown: raise` is the one exception.
- Pass every produced action through `ActionTranslatorBase.validated`. An
  `InvalidTranslationError` is a defect, never an untranslatable step.
- Domain knowledge goes in the capability's package, as pure functions over fragments and
  documents with no `core.trace` import (`index_points.py`). Evidence is read through
  `RunTrace`, as in `archive_files(step, "browser.dom_raw")`.
- A new reason extends `UntranslatableReason` in `schemas/sample.py`, which must stay
  domain-neutral (`test_shared_module_is_domain_neutral.py`).

### 3.8 Exporter

| | |
|---|---|
| Registrar | `ExporterRegistrar` (`exporter_id`), `spectragent/export/_registrar.py`; collected as `exporter`, an optional top-level layer of the run root |
| Design section | Architecture §4.15 ("配置：运行根的两个可选层", "格式化：`openai_messages`") |
| Protocol, base | `Exporter`: `exporter_id`, a read-only `translator`, the job fields, `root()`, `format(rendered, images) -> ExportRecord`. `ExporterBase` hand-writes a keyword `__init__` with every job field; `translator` is annotation-only with a Python default |
| Owner | none; the exporter is the export document's top-level block, and `ExportRunner.from_run` takes exactly one |
| Tests to copy | `tests/test_export/test_registrar.py`, `test_openai_messages.py`, `test_runner.py`, `test_validate.py`, `test_export_boundaries.py` |

- Exporters differ only in record format. Implement `format`, store images through
  `ImageStore.put(data, media_type)`, and leave sources, filters, rendering, validation,
  split and the manifest to `ExportRunner`. The manifest's shape is
  `schemas/dataset.py::DatasetManifest`, which the metrics read it back through; a new key
  goes into that model (and `DATASET_FORMAT` moves when a reader must tell the shapes
  apart).
- `format(rendered, images)` returns `ExportRecord(sample_id, record, images)`. `record` is
  the JSON object the runner writes, with sorted keys, as one line of `<split>.jsonl`.
  `images` lists, in order of appearance, the path `images.put(data, media_type)` returned
  for each image part: `images/<sha256><suffix>`, relative to the dataset root.
  `check_images` fails a record whose count differs from the context's image parts. A
  LLaMA-Factory record also needs as many `<image>` placeholders as images (architecture
  §4.15).
- The runner owns every file name: `<split>.jsonl`, `<split>.samples.jsonl`, `images/` and
  `manifest.json`. An exporter has no way to add a file, so a format whose trainer wants one
  beside the records (LLaMA-Factory registers datasets in a `dataset_info.json`) needs a
  runner change; raise it with the user before writing one.
- Keep the output deterministic: no wall-clock field, absolute path or host information, and
  no file taken from `env/`.
- Keys are flat (`openai_messages`; the planned `llamafactory`). An exporter without knobs of
  its own writes no `__init__`, and conscribe reads `ExporterBase`'s. One with knobs writes
  the complete signature (the base's job fields plus its own) and hands the job fields to
  `super().__init__`. Neither redeclares `translator`: the receptor is inherited. List
  defaults are `None`, and nested knob types are pydantic dataclasses in a module of their
  own (`export/settings.py`), for the reasons in §4.
- Every exporter module uses `from __future__ import annotations`, with knobs or without:
  each registered exporter gets a `.pyi` stub for the inherited receptor
  (`openai_messages.pyi`), which puts it under the §4 rule for receptor-carrying classes.
- Whether a format variant (LLaMA-Factory's alpaca and sharegpt layouts) is a knob of one
  exporter or a second exporter is not decided; ask the user before writing the second.
- Import the class in `spectragent/export/__init__.py`.

### 3.9 A student agent: `format_react_output`, and a teacher's `parse_react_thought`

These are two optional ReAct slots on `ReactLoop.__optional_slots__`, with no registrar of
their own. The exporter validates them right before it calls them; `prepare()` never does
(af-add-a-loop §8). A new student format is a new Agent class, or a new `coordinate_space`
value on an existing one.

- `format_react_output(actions, *, thought=None) -> str` is the inverse of
  `parse_react_output`: it writes the reply this agent would be asked for when served. It
  reads per-step agent state, so the order is `prepare`, then the step's
  `await build_react_prompt(obs, context=ReactPromptContext(...))`, then
  `format_react_output`, then `parse_react_output`. Historical rendering passes
  `phase="history"` and recorded contributions; it never regenerates them with a helper
  model. The same input gives the same text.
- `StudentRenderer.render` is async. Its callers await it; the synchronous
  `ExportRunner.run` drives the entire async rendering phase with one `asyncio.run`.
  Segment seeding still uses `AgentRuntime.seed_messages`, and assistant history uses
  the public `ReactLoop.remember_reply`. No environment or model is started.
- What it cannot write raises `UnrepresentableActionError(action_name, params, detail)`
  (`core/loops/errors.py`). Never drop a parameter silently.
- It must satisfy the round-trip definition of architecture §4.15: the parsed name resolves
  to the action's name under the student's guard; `format(parse(format(a))) == format(a)`,
  thought included; every point lies within the student grid's `quantum`; every other
  parameter the action sets comes back unchanged.
- `parse_react_thought(llm_output) -> tuple[str, str] | None` is a pure `@staticmethod`. The
  export calls it on the teacher's class, found in `AgentRegistrar` by the source trace's
  `agent_id`.
- `tests/test_agents/test_format_react_output_round_trip.py` checks every registered agent
  class that defines `format_react_output`, at DPR 1 and 2 and for every `coordinate_space`
  value, with parameters from `tests/_helpers/action_params.py::variant_params`. There is
  nothing to register. Give the student a shipped YAML of the
  `configs/flywheel/ui_tars_student.yaml` shape (one agent, a ReAct loop, no hooks, the
  recording's screenshot pipeline) so the shipped-config gate covers it, and check its length
  budget the way `tests/test_export/test_validate.py` does for the shipped pair.

## 4. Invariants and traps

- The archive demand comes only from `trace_writer.archive`, fully qualified. What a run
  records is chosen per run on the trace hook, not declared by an agent, so there is no
  agent-side relative name to bind (architecture §4.5). Never put an archive entry in an
  agent's `__wiring__`. An `archiver:` key on a capability block is refused at compose
  time. A run binds at most one archive consumer.
- `trace_writer.archive` is the one switch for archiving (the user's ruling of 2026-10-13).
  Omitted, `null` or `[]`, nothing is recorded. Archivers are listed only there:
  `TraceWriter.__inline_only__` makes the Composer refuse a top-level `archiver:` section
  (`InlineOnlyLayerError`, whose message says to list them under `trace_writer.archive`),
  and an omitted `archive` never adopts archivers another block built (architecture
  §4.2.1). A hook whose `archive` is
  empty is not an archive consumer: no negotiation, no `bind_archivers`, no
  `collect_archive`, no `env/`. Two hooks with non-empty archives, one archiver in two
  hooks, and one key listed twice are each a problem in one `IncompatibleArchiveError` at
  `prepare()`, so two hooks cannot split the evidence.
- Required single-instance receptors are required in YAML: `replayability` and
  `network_source` in every capability block, `miss_policy` in every
  `network_source: {source_id: archive}` block (the replay's network source, unrelated to
  the `trace_writer.archive` list of archivers), `restorer` in every `branch` loop block,
  `step_selector` in every `trace_branches` block, `translator` in every exporter block.
  The `default_*()` factories serve objects built by hand only.
- "Not enabled" is spelled `none` (the same ruling): `replayability_id: none`,
  `occlusion: none`. The old `off` fails validation, bare or quoted (PyYAML is YAML 1.1 and
  reads a bare `off` as `false`). In a site rule `fuzzy: on` is read as `true`, which is what
  that bool key means. A numeric limit is never `null`: no limit is `unlimited`
  (`max_document_misses`, the capability's `dom_max_*`, the chain's `token_limit`), and `[]`
  always means empty.
- A replay serves recorded bytes, changed in two ways only, both in
  `responder.serve_record`: a site rule's `response_echo`, and the JSONP callback name
  (`jsonp.rewrite_callback`). Either way the body's `Content-Length` is recomputed, the
  `max_fulfill_body_bytes` cap is checked on the bytes served, and the recorded body digests
  (`Content-MD5`, `Digest`, `Content-Digest`, `Repr-Digest`) are dropped; `ETag` stays (a
  replay disables the cache, nothing revalidates). Never write into a served script anything
  but an identifier the request itself carried, and never touch a body the wrapper grammar
  does not prove: an unrecognised shape is served as recorded and stays visible as a hit
  without `callback_rewrite`.
- The recorder never awaits a reply before it resumes a waiting target: it writes the whole
  batch, then `Runtime.runIfWaitingForDebugger`, in order from one task (`CDPClient.post`),
  then waits for every reply for longer than it may itself hold the target's first document.
  A target without a page process answers its page-side commands only once that document
  commits, and applies them before its first script anyway. Waiting before the resume is
  what held every no-opener tab 1 s and logged 10 false failures; there is no special case
  for service workers.
- `pinned` requires a fresh profile, `local_chrome`, no explicit `cdp_url`, no provider knob
  that contradicts the tier, a `random_seed` of at least 1, and `base_time: from_archive`
  only with the `archive` source. `--disable-back-forward-cache` is part of the tier's
  determinism contract. The form-submit observation stall was fixed in the observation layer
  (`navigation.py` and the screenshot command bound); do not drop the switch to fix a timing
  bug.
- Every observation waits for the page to settle before it captures, in recording and in
  replay alike (`settle.py`, architecture §4.14): the main frame's document loaded
  (`MainFrameLoad`, fed by the same `NavigationFeed`), then `settle_quiet_ms` (500) of no DOM
  mutation and no resource finishing, measured in the `af-settle` isolated world from the
  moment the wait reaches the page; at most `settle_max_wait_s` (5; `0` turns it off), then
  it captures as it is. The wait belongs to the observation, ahead of the one capture every
  fragment comes from, so a DOM-only run waits exactly as a vision run; it never takes or
  compares a screenshot. Each browser fragment carries the capture's `PageSettle`
  (`BrowserCaptureFragment.settle`; left out of the trace JSON when `None`). Restoration
  compares the recorded and restored observations as they are: there is no capture taken
  for verification only. Cost: at least one quiet window per observation, the cap on every
  step of a page that never goes quiet.
- The focus moves only at an observation, never inside an action (`tabs.py`, architecture
  §4.14 "页面新开的标签页"): the agent's action opens the follow window, the first fragment of
  the next observation decides (after one `Target.getTargets`, before the navigation and
  settle waits, which then run on the tab captured), the end of the waits decides again from
  events alone, and the capture closes the window. Every action of one step therefore acts
  on the tab the step observed. The decision reads the control connection's target discovery
  (`TabRegistry`), so it is the same with or without the recorder, in recording, replay and
  branching. Each fragment of the capture carries `focus_moved` (`None` and left out of the
  trace JSON when nothing moved); agents never render it.
- Execution outcome and recording health are independent. A failed collection or WAL
  commit loses only its declared segment/channel; later steps are still attempted and
  the Agent continues by its execution policy. The manifest records actual missing
  steps and the continuous complete prefix. Viewing and known usage subtotals may use
  saved steps after holes; branch/export require their declared scope and complete
  history. Unfinished calls remain trailing; sealed failed steps keep their identities.
  Archive-source reads remain environment errors. Offline replay never forwards misses
  to the live network.
  Loop code neither inspects recorder health nor catches arbitrary handoff errors;
  real execution errors, cancellation and vetoes keep their own propagation rules.
- Privacy defaults (threat model "only me", the user's 2026-10-13 ruling: the trace and
  `env/` stay on the recording machine; the gates are the prompt sent to a model provider
  and dataset export): `Cookie`, `Authorization`, `Proxy-Authorization` and the names in
  `strip_request_headers` are stripped at every source. `Set-Cookie` is kept for replay, so
  `browser.network` declares `contains_session_state`. `storage.json` keeps cookie and Web
  Storage values as they are, so `browser.storage` declares session state too, and its
  `allow_session_state` means only what it means on every archiver: accept a non-fresh
  profile. POST fields and raw-DOM input values are redacted with ledger entries
  (`BROWSER_REDACTION_POLICY` `af-privacy-1+browser-2`). The DOM observation path is
  redacted by the same rule: the BrowserUse cleaner replaces a sensitive control's value,
  the text below it and an inner node's accessible name with `<redacted>` while it builds
  its tree, at the raw-DOM redaction's positions, so an offline re-clean of `dom_raw`
  matches the live one. So is an action result that goes into the prompt:
  `browser.extract_content` of one element returns its outer HTML with the same values
  withheld (`dom/sensitivity.withhold_sensitive_markup`, placeholder `&lt;redacted&gt;`).
  The typing actions judge the focused element before they type and
  record `ActionResult.typed_into_sensitive_field` (`null` when they cannot see it); no
  agent renders it, and export does not use it yet. A non-fresh profile needs
  `allow_session_state: true` on every bound archiver. Privacy markers are declarations
  recorded in `TraceConfig.env_evidence_privacy`, not proofs.
- Read through the manifest only: the `RunTrace` accessors, `archive_lines` for line-shaped
  evidence, `committed_files` / `read_committed` for byte accounting, and `ArchiveReader`
  from `open_archive` inside a capability. Files the manifest does not list are invisible
  and are never served by a replay; `evals()` is the one accessor that lists a directory. A
  replay leaves its source directory unchanged.
- Tokens, never money. No field of the archive, negotiation, trace, export or flywheel
  models is about price. `test_archive_boundaries.py`, `test_export_boundaries.py` and
  `test_flywheel_boundaries.py` scan field names against `price|cost|usd|dollar`, so even a
  `cost` that means something else fails.
- Evidence belongs to the step whose commit collected it. A request a page sends after it
  loaded can land in the next step's `index.jsonl` or `replay.jsonl`; the metrics label such
  misses `terminal` or `trailing`.
- An export and a report computed twice from the same inputs are byte-identical. They hold no
  wall clock and no absolute path; a skipped source is named by its run-relative file, and
  the reader's own message goes to the log.
- The conscribe 1.3 workarounds in place, with their reasons (details in af-add-a-receptor
  §6 and §7):
  - Nested knob types are pydantic dataclasses, never `BaseModel` (`MatcherConfig`,
    `ExportFilters`, `SplitSettings`, `Decontamination`, `BenchmarkRef`, `LengthBudget`,
    `RequireEval`). Code generation assumes every `BaseModel` is a submodel it emitted itself,
    writes no import, and `_composed.py` then fails with `NameError` on import.
  - The stub of a receptor-carrying class types a same-module sibling as `Any`, and a `.pyi`
    wins for type checkers, so such a class's knob types live in a module of their own
    (`require_eval.py`, `export/settings.py`).
  - Modules whose classes get a stub for an injected receptor use
    `from __future__ import annotations` (`capability.py`, `archive_source.py`, `branch.py`,
    `trace_branches.py`, the exporter modules), because the stub writer renders an evaluated
    `Annotated[..., Field(...)]` as a `FieldInfo(...)` repr, an invalid stub. Every other
    module that declares `Annotated` knobs must not use it (`trace_writer.py`,
    `prefix_replay.py`, the archiver, selector, translator and miss-policy modules): a string
    annotation that does not resolve in the defining module's globals drops the knob without
    a word. `branch.py` imports `Annotated` and `Field` under exactly those names because the
    strings it inherits from `react.py` resolve there.
  - A list knob with a non-empty default is `list[str] | None = None`, `None` meaning the
    documented default (`sources`, `require_archive`). A list default in a signature is a
    shared mutable default, and `tuple[str, ...]` is generated as `tuple[str, Ellipsis]`,
    which pyright rejects.
  - `Field(min_length=...)` on a top-level knob is dropped from the generated config; enforce
    the bound at construction.
  - A receptor must not be a parameter of a dataclass-synthesised `__init__`, because
    conscribe replaces its default with a `WiredField`. Owners hand-write `__init__`
    (`TraceWriter`, `ArchiveNetworkSource`, `TraceBranchesTaskLoader`, `ExporterBase`) or
    annotate the receptor on a plain mixin (`BranchLoop`). Rule 3 of
    `test_receptor_hygiene.py` checks it.

## 5. Troubleshooting

| Symptom | Look at |
|---|---|
| `IncompatibleArchiveError: ... has no capability 'x' (its capabilities: [...])` | The key's prefix names no capability in this environment |
| `IncompatibleArchiveError: ... provides archivers: [...] (capability 'c' declares ...)` | That capability's `__wiring__["archiver"]` lacks the key. Architecture §4.5 lists every message verbatim |
| `IncompatibleArchiveError` with `lists archiver ... more than once`, `is requested by more than one hook` or `more than one hook requests archivers` | v1 binds the archive of one hook. Put every archiver in one `trace_writer.archive` list, once each. All the problems arrive in one error at `prepare()` |
| Validation: `Input tag 'network' found using 'archiver_id' does not match any of the expected tags` | Archive keys are never relative: write `browser.network`. Unlike action and fragment names, nothing binds a relative archiver name, because the key is the registry key itself and its prefix is the providing capability. The same message names a mistyped key, or an archiver whose class never registered (next row) |
| Every browser block fails validation with `Extra inputs are not permitted` on `replayability`, `network_source`, `mouse_trajectory` and the other receptors | A key in `BrowserCapability.__wiring__`, such as a new archiver's, names a class that never registered, and conscribe dropped the capability's whole wiring. Look in the log for `Failed to import module`: `discover()` skips a module whose import raises `ImportError` or `SyntaxError` |
| Validation says `replayability_id` or `occlusion` is `False` | The old name `off`, unquoted; write `none` |
| `InlineOnlyLayerError: The top-level 'archiver' section is not accepted ...` | Move the entries into `trace_writer.archive` |
| `Field required` for `step_selector`, `restorer`, `translator`, `miss_policy`, `network_source` or `replayability` | Write the receptor block (§4) |
| `BrowserCooperationError` at `prepare()` (or setup, for a caller that skipped `prepare()`) | Architecture §4.14, "协作检查一览"; `browser/cooperation.py` (`binding_problems`, `cooperation_problems`), run by `BrowserCapability.check_setup`. Each line names both sides |
| `spectragent bench` exits 3: `the sweep was vetoed at ...` | A guard (`RunAbort`) stopped the sweep — during setup or the run — at the task named; its row says `vetoed: ...` and its `result.json` says `agent_done: vetoed`. Look at the vetoed run's last steps (a challenge page, a risk-control response), not at the configuration |
| A summary row says `infrastructure failure: ...` | The environment failed, the model stayed unavailable after the provider's retries, or the run's trace is `incomplete` / never closed (its manifest's `incomplete_reason` says what was lost). The three counters above the rows are the agent's ledger, the environment's and the trace's |
| `spectragent bench` exits 4: `the sweep reached its token limit after task ...` | The tokens the sweep's traces recorded reached `--token-limit`: the named task finished, the rest never started. The table's `tokens:` line is the total; `no usage reported (not counted)` names calls without usage, `tokens not counted` a task whose trace does not read back |
| `spectragent flywheel` exits 4: `stopped at the token limit ...` | The runs under the chain root reached `token_limit`; read `chain.json`'s `stopped_by_token_limit` (`stage: null`: reached before this invocation). Raise the limit, then run only the stages not started (`--stages`) |
| `spectragent bench` exits 2: `the pre-flight check refused the configuration for task ...` | The first task's `prepare()` refused the stage YAML before any task ran; the original error follows the colon and names both sides. Check one task with `spectragent run --dry-run` (`--replay-archive DIR` for a replay) |
| `BrowserCooperationError` naming `browser.network` or `browser.replay_log` beside the network source | A replay binds `browser.replay_log` in `browser.network`'s place; a live run binds no `browser.replay_log` (§2.2) |
| A replay refused at `prepare()`, naming the tier, shim, viewport or launch flags | `replay/consistency.py::consistency_problems`. The replay YAML must match the recording's tier, seed, user agent, viewport and provider switches |
| A replay refused naming `browser default timezone` or `locale` (`read from this host before launch` at `prepare()`, or `measured after launch` from setup) | The host's browser falls back to another time zone or locale than the recording's (a frame no override reaches reads it). Replay on a host with the recording's system language; `pinned`'s zone comes from `TZ`. `recorded no browser defaults`: record the source again |
| A recorder failure `Emulation.setLocaleOverride: ... already in effect` on a popup or iframe | Genuine and expected: the target shares a renderer process whose locale another frame owns (architecture §4.14). A failure `... got no reply within` on a new tab is not expected: the preparation must not wait for replies before the resume |
| `ArchiveOpenError` | `replay_archive` is not a run directory with a committed manifest |
| `ReplayArchiveError` | An index line or WARC record of the source does not read back; the source run is damaged |
| `RecorderLostError` | The recorder connection dropped, and observation, action and collection fail closed. Look at Chrome, not at the archive |
| A collection raises about dropped or failed events | The archive channel counts emitted against handled events, and `ArchiveChannel.errors` counts sessions that raised in `admit_event` or `on_event`. The step stays uncommitted |
| Many `no_match` misses on near-identical URLs of one host | The candidates in `replay.jsonl` (`iter_replay_log`) and the report's site-rule candidates. Add `ignore_query` or `ignore_body_paths` to `configs/replay_rules/<domain>.yaml` |
| A main-frame document miss (`diverged`) | The page asked for a document the recording never fetched, such as a time-stamped URL. A restored step shows `document_miss` |
| `IncompatibleCheckpointError` | The branch's agent, closed-set or numeric knobs, memory, `max_actions_per_step`, negotiation, seed messages or replayed calls differ from the source's; the error names both values. Fix the branch YAML |
| `RestorationDivergedError` | `verify: strict` met a failed check; read `StepRecord.restoration.reasons`. `verify: warn` records every step's check for diagnosis |
| A restored step right after a click shows `url_mismatch` against a URL from another tab | Compare the step's `focus_moved` in the source and the branch (any browser fragment of that step's observation): the branch's `new_tabs` differs from the recording's, or the tab opened later than the observation captured in one of the runs. Record, replay and branch keep one `new_tabs` |
| An agent keeps clicking a link that opens a new tab and sees no change | The run has `new_tabs: stay`, or the tab never finished its first navigation within 10 s (a WARNING `the focus cannot move to tab ...`), or Chrome reported it opened without the user's activation (the tab is listed in `browser_meta.tabs`, the trace has no `focus_moved`) |
| A branch sweep refuses its sources, or lists no tasks | The reasons in `loader.skipped` (`spectragent bench --list`); `on_unbranchable: skip`. Branch and replay runs are never sources |
| The bench refuses before starting: no eval loader for `trace_branches` | Pass `--no-eval` |
| `ExportEmptyError` | The counts in the message; most often a `require_eval` over runs nobody evaluated, or `decontamination.against` listing the benchmark the sources were recorded on (§2.4) |
| `ExportConfigError` | More than one exporter block, a student YAML with hooks, several agents or a non-ReAct loop, or decontamination data that cannot be read |
| `IncompatibleError ... needed for training export` | The student lacks `format_react_output`, or the teacher lacks `parse_react_thought` under `thought_source: teacher` |
| `IncompatibleFragmentError` naming the source run | The student negotiates a fragment the source never recorded, such as a DOM student over a vision recording |
| `StudentCaptureMismatchError` | The student's viewport, screenshot format or DPR differs from the recording's |
| `OcclusionEvidenceMissingError` | Bind `browser.dom_raw`, record with the `skyvern_hit_test` cleaner, or set `occlusion_unknown: untranslatable`; `occlusion: none` also avoids it, at the cost §2.4 names |
| `RoundTripError` | The student's formatter breaks the round-trip definition; fix the formatter |
| `SampleTooLongError` | `length.max_tokens` against the student's `max_steps` and its screenshot in every turn |
| `CredentialLeakError`, `ReferenceLeakError` | A credential-shaped string in the output, or the reference answer in seed or task text |
| `ChainConfigError`, `ChainStageError` | A bad spec (exit 2) or a failed stage (exit 1), named in `<root>/chain.json` |
| The pre-flight refuses `--token-limit`: `this configuration writes no trace` | No hook exposes a run directory, so no token can be counted: add a `trace_writer` hook or drop the limit |
| A metric is `null` | Its denominator was zero; the `reason` says why. No data gives `insufficient`, never a default verdict |
| The observation after a form submit stalls | Navigations in flight are awaited before capture (`navigation.py`). A navigation a page timer starts after that check still costs up to 10 s per attempt |
| A replay log hit on a JSONP request, yet the page lacks what it loads (hot searches, an ad, search results) | The entry's `callback_rewrite`: absent while the requested name differs from the record's means the answer is outside the wrapper grammar (a callback in the path, `window.NAME(`), or the request hit a record of the same endpoint without a callback. `sections.sites.by_site.<site>.callback_rewrites` counts the rewrites per site |
| Recorded and replayed screenshots differ, or an agent decided on a half-rendered page | Read the step's `settle` record on its browser fragments: `capped` means the page never went quiet within `settle_max_wait_s` (a carousel, a video, a poll); `unknown` means its state could not be read (`detail`). The wait sees no request still in flight with nothing else changing (architecture §4.14) |
| A model call waits until its timeout late in a long process | A provider that leaves its SDK client to the garbage collector (architecture §4.7). A provider closes every client it builds before the request returns |
