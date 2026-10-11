# Configs

End-to-end YAML samples that drive `ConfigComposer` → `AgentRuntime`.

需要直接启动三种 browser agent、切换本地/云浏览器或跑数据集时，先看 [Browser agent 运行手册](../docs/browser_agent_runbook.md)。

## Layout

| File | Purpose |
|---|---|
| `browser_use_local.yaml` | BrowserUseAgent + ReactLoop + LocalChrome backend |
| `browser_use_skyvern.yaml` | Same agent/loop, switched to the hosted Skyvern backend |
| `skyvern_agent_local.yaml` | SkyvernAgent + ReactLoop + LocalChrome backend |
| `ui_tars_local.yaml` | UITarsAgent + ReactLoop + LocalChrome backend |
| `flywheel/record_ui_tars_pinned.yaml` | A **recording** run (data flywheel): UITarsAgent, `replayability: pinned`, the three browser archivers on the trace hook — see [Recording environment evidence](#recording-environment-evidence) |
| `flywheel/replay_ui_tars_archive.yaml` | Its **offline replay**: the same agent and tier with `network_source: archive` (isolated, strict misses) and `base_time: from_archive`; the recorded run arrives as `--replay-archive DIR` — see [Replaying a recorded run](#replaying-a-recorded-run) |
| `flywheel/branch_ui_tars_archive.yaml` | **Branching** recorded runs (data flywheel, M31): `agent_loop: branch` (the `prefix_replay` restorer) over the replay browser block, tasks from `task_loader: trace_branches` — see [Branching a recorded run](#branching-a-recorded-run) |
| `flywheel/export_sft.yaml` | **Exporting** recorded runs as a training dataset (data flywheel, M32): one `exporter:` block, no agent, no environment — see [Exporting training data](#exporting-training-data) |
| `flywheel/ui_tars_student.yaml` | The export's **student**: UITarsAgent with `coordinate_space: normalized_1000`, the recording's screenshot pipeline, no hooks; assembled by the export, never run by it |

## Quick start

```bash
# Validate compose pipeline only (no real LLM / Chrome required)
uv run spectragent-run --config configs/browser_use_local.yaml --dry-run

# Run end-to-end (needs OPENAI_API_KEY + a Chrome binary)
uv run spectragent-run --config configs/browser_use_local.yaml \
    --start-url https://example.com --task "report the page title"
```

`--dry-run` exercises:

1. YAML schema validation against `ConfigComposer.top_level_type`.
2. `ConfigComposer.compose()` — instantiates every layer, setattr-injects all wired receptors (including `capability.backend`, `agent.llm`, `agent.memory`).
3. Surface report (`agent`, `loop`, `model`, `environment`, `capabilities` and their backends).
4. `AgentRuntime.prepare()` — loop slots plus observation/action negotiation.

It does **not** start a browser or call the LLM — useful for CI smoke tests.

## YAML shape

conscribe `inline_wiring=True` lets every wired child be inlined inside its owning entry, so the agent block carries its loop / llm / memory and the environment carries its capabilities:

```yaml
agent:
  - agent_id: browser_use
    id: main
    # ...agent fields...
    agent_loop: { loop_id: react, max_steps: 30 }
    llm:
      model_type: openai
      model: gpt-4o
      provider: { name: direct }
    memory: { memory_id: simple, max_messages: 200 }

environment:
  - env_type: browser
    capabilities:
      - capability_id: browser
        profile: standard
        mouse_trajectory: { trajectory_type: instant }
        typing_rhythm: { rhythm_type: instant }
        replayability: { replayability_id: "off" }
        network_source: { source_id: live }
        screenshot_format: png      # jpeg + screenshot_quality shrinks a
                                    # vision step's image ~2-4x
        backend:                    # ← execution-layer config (IoC)
          provider_id: local_chrome
          headless: true

hook:
  - hook_id: trace_writer
    trace_dir: traces
```

Notes:

- A wiring entry becomes a runtime receptor **only** when the owning class declares a slot for it. `observation` / `action` on the agent and `observation` on the capability have no slot: they are declaration-only constraints the negotiation engine reads off `__wiring__`. They are optional in the schema, and *populating* one is a hard `UnresolvedWiringError` — the value would otherwise be silently dropped.
- Mode 1 wirings (`agent_loop`, `llm`, `memory`) become full discriminated unions of every registered config — you write the **complete child config** inline, no top-level reference list needed.
- Single-instance receptors (`llm`, `agent_loop`, `capability.backend`, `capability.mouse_trajectory`, `capability.typing_rhythm`, `capability.dom_cleaner`, `capability.dom_serializer`, `capability.replayability`, `capability.network_source`, and `miss_policy` inside an `archive` network source) are **required**; list-shaped ones (`environment.capabilities`, `capability.actions`) are optional and fall back to a sensible default when omitted.
- A strategy is always a receptor block, never a name on `profile:`. `profile:` says which watchdogs run and which CDP round trips they may spend — nothing else. Naming a strategy there is what made `mouse_trajectory`, then `dom_cleaner`, then `dom_serializer` arrive default-constructed with every configured knob discarded.
- `browser.dom` has a cost surface of its own, off by default: `dom_max_elements`, `dom_max_serialized_chars`, `dom_interactive_only`, `dom_viewport_only`. When any of them cuts, the observation fragment records the cut in `budget`, so a trace never shows a truncated page as a sparse one.
- The two input-humanization receptors are independent axes and each is its own off-switch: `mouse_trajectory: { trajectory_type: instant }` and `typing_rhythm: { rhythm_type: instant }` are the legacy instant jump and the legacy single bulk `Input.insertText`. There is no boolean beside either — the strategy *is* the switch.
- `replayability` has the same shape (`docs/architecture_design.md` §4.14): `{ replayability_id: "off" }` pins nothing and opens no second CDP connection (quote `"off"`: YAML 1.1 — PyYAML — reads a bare `off` as `false`, which no tier matches); `pinned` (knobs `base_time`, `random_seed`, `timezone`, `locale`, `device_scale_factor`, `user_agent`, `reduced_motion`) makes a recording replayable and needs a backend that launches its own browser (`local_chrome`).
- `network_source` is the third axis of that shape (§4.14 "回放"): `{ source_id: live }` is the network itself and changes nothing; `archive` answers every request from a recorded run's archive at the Fetch Request stage and never sends one to the network (see [Replaying a recorded run](#replaying-a-recorded-run)). Whether the browser must be isolated from the network is the source's own `isolation` knob — there is no provider boolean beside it.

## Per-action configuration

Action side-effects live on the action *instance*, so a capability's `actions:` list is where they are tuned. Each entry routes by `action_id` and is validated against that action's own generated config model:

```yaml
capabilities:
  - capability_id: browser
    mouse_trajectory: { trajectory_type: windmouse, gravity: 12.0 }
    actions:
      - action_id: browser.navigate
        wait_until: networkidle
        post_delay_ms: 1500       # absorb JS-timer DOM mutations post-load
      - action_id: browser.scroll
        default_amount: 600
      - action_id: browser.click
      - action_id: browser.type
      - action_id: browser.done
    backend: { provider_id: local_chrome }
```

The list is also an **ablation switch**: only the actions it names are instantiated, advertised to the model and executable. Omit the block entirely to get everything `BrowserCapability.__wiring__` offers. Narrow it and an agent that *requires* a dropped action fails action negotiation before the run starts — that is the intended "config errors surface at negotiation, not at the first LLM call" guarantee.

`action_id` uses the fully-qualified registry key (`browser.scroll`, not `scroll`).

## What browser_use sees: `observation_mode`

`browser_use` has one knob for the whole surface, and it replaces `use_vision`:

```yaml
agent:
  - agent_id: browser_use
    observation_mode: dom          # dom | dom_vision | vision
```

| mode | in the model's context | click verbs | DOM extracted |
|---|---|---|---|
| `dom` (default) | the element tree as text | `browser.click` (index) | yes |
| `dom_vision` | that plus a screenshot | `browser.click` **and** `browser.click_coordinate`, independently | yes |
| `vision` | a screenshot, plus URL / title / tabs | `browser.click_coordinate` only | **no** |

`use_vision: false` was `dom`; `use_vision: true` was `dom_vision`.

Three things follow from the enum, so there is nothing else to keep in sync:

- **what the prompt describes** — one prompt tree, one surface fragment spliced in, so a DOM/vision ablation differs by that fragment and nothing else;
- **what the run negotiates** — `vision` never asks for `browser.dom`, so no DOM is extracted at all (not extracted and then hidden), and index-addressed actions are not activated;
- **what the model may emit** — the output schema is compiled from the negotiated actions, so a verb this mode does not have cannot be written.

The mode is never inferred from the environment. `observation_mode: dom` against `profile: vision_only` fails at negotiation, before the browser is launched:

```
IncompatibleFragmentError: Required observation fragments not provided by environment.
Missing: ['dom']. Requested by: dom ← browser_use(observation_mode='dom').
Environment provides: ['browser.browser_meta', 'browser.screenshot'].
```

The requester is named because the fix is not obvious without it: `observation_mode` and `profile` point at opposite edits, and a mistyped profile must not quietly turn a DOM experiment into a vision one.

Screenshots are captured and recorded in **every** mode: `online_mind2web` and `lexbench_browser` score runs from the screenshots in the trace. The mode decides what the model sees, not what the run records.

For a vision run, prefer `profile: standard` over `vision_only`: negotiation already skips DOM extraction, and `vision_only` additionally drops the SecurityWatchdog, so domain allow/deny lists silently stop applying.

## DOM cleaners

`capability.dom_cleaner` picks how the page becomes the element tree a DOM agent reads (`docs/architecture_design.md` §4.12). Three are registered:

| `cleaner_type` | what it does | knobs |
|---|---|---|
| `browser_use` (default) | CDP `DOM.getDocument` + `DOMSnapshot` + accessibility tree; paint-order occlusion drops elements another element fully covers | `descendants_occlude`, `max_occlusion_nodes`, `include_shadow_dom`, `enable_compound_components` |
| `skyvern` | a page-side JS extraction, frames and shadow roots included; no paint order, so a link a sticky header covers is still indexed | `max_text_length`, `include_purgeable` |
| `skyvern_hit_test` | `skyvern`, then the page itself is asked per indexed element whether a click at its centre (or at one of a 5x5 grid of points inside it) reaches it; the answer is recorded on the element as `hit_test` | the two above, plus `covered_elements: drop \| keep` |

```yaml
dom_cleaner:
  cleaner_type: skyvern_hit_test
  covered_elements: drop     # drop (default): an element no probe reached gets no index
                             # keep: indexed as with skyvern; the hit test is still recorded
```

`drop` changes what the agent sees — that is the point of choosing it: an agent can no longer click a link hidden under a sticky header (the defect `skyvern` has). An element outside the viewport has no hit test and keeps its index; off-screen is not covered. The `skyvern` cleaner itself is unchanged, byte for byte. The recorded `hit_test` is also what lets the training export check that a point it teaches is not covered (`docs/architecture_design.md` §4.15).

## Vision agents

A vision agent (`ui_tars`, or `browser_use` in `dom_vision` / `vision`) aims at the screenshot the step showed. Two settings decide whether that is still the page it acts on.

**When the screenshot is taken.** The next observation is captured as soon as the step's last action returns, and an action returns `post_delay_ms` after its effect was dispatched (default `0`). A widget that is still animating then — a reCAPTCHA spinner, an image challenge fading in — is captured mid-animation, and the next reply aims at what is no longer there. Give the coordinate actions a settle delay:

```yaml
capabilities:
  - capability_id: browser
    actions:
      - action_id: browser.navigate          # setup() opens start_url with it
      - action_id: browser.click_coordinate
        post_delay_ms: 1500                  # reCAPTCHA spinner and fades settle
      - action_id: browser.type_coordinate
      - action_id: browser.scroll
      - action_id: browser.send_keys
      - action_id: browser.drag
      - action_id: browser.wait
      - action_id: browser.done
```

The list is the ablation switch described above, so it has to name every action the run needs: `ui_tars` requires `click_coordinate`, `type_coordinate` and `done`, and a task with a `start_url` needs `browser.navigate`, because the capability opens the landing page by executing it.

**What a point is.** `ui_tars` tells the model which grid to write points on and converts each point into the capability's units (CSS pixels for the browser) using the size of the screenshot that step showed, so the device scale factor and viewport size need no setting of their own. Pick the grid the model really answers on — `screenshot_pixels` (the default) or `normalized_1000` (0-1000 on both axes, x first):

```yaml
agent:
  - agent_id: ui_tars
    coordinate_space: normalized_1000
```

`browser_use` takes the same field, with the same two values, and uses it the same way — it is a property of the model, not of the agent class.

Which grid a given model uses is not something to guess from its name: check a few of its answers against known element positions before a run. A point off the grid is clamped onto the edge of the viewport.

## Switching backend

Edit only `environment[0].capabilities[0].backend`:

```yaml
backend:
  provider_id: skyvern       # was: local_chrome
  api_key: sk-...                 # provider-specific fields
  timeout_minutes: 30
```

Every other section stays identical. Trace output (`./traces/<run_id>/config.yaml`) records which `provider_id` was used so cross-backend runs are easy to diff.

## Recording environment evidence

A run records the world it acted in — every request and response with its body, cookies
and Web Storage, the raw DOM of each observation cycle, the environment fingerprint —
when its trace hook lists archivers (`docs/architecture_design.md` §4.14, §5.4). The
evaluation configs above record none. `flywheel/record_ui_tars_pinned.yaml` is the
shipped recording config:

```yaml
environment:
  - env_type: browser
    capabilities:
      - capability_id: browser
        replayability: { replayability_id: pinned, random_seed: 7 }   # replayable recording
        network_source: { source_id: live }                           # recording: the real network
        backend: { provider_id: local_chrome, headless: true }        # must launch its own browser
        # ... the rest as in any capability block

hook:
  - hook_id: trace_writer
    trace_dir: traces
    archive:                          # [] = record nothing
      - archiver_id: browser.network  # WARC/1.1 + index.jsonl per step
      - archiver_id: browser.storage  # cookies (values hashed) + local/session storage
      - archiver_id: browser.dom_raw  # raw DOM of each committed cycle
```

- Evidence lands in `traces/<run>/env/` and is read through `RunTrace.archive_files` /
  `blob` / `archive_run_file`.
- `archive` omitted while the file has a top-level `archiver:` section adopts every
  archiver of that section; with neither, nothing is recorded. Write `archive: []` to be
  explicit.
- Each archiver has knobs (`max_inline_body_bytes`, `strip_request_headers`,
  `drain_timeout_s`, `allow_session_state`, …), validated against the generated schema;
  a mistyped `archiver_id` is refused.
- `browser.network` refuses `replayability: "off"` unless it says
  `require_replayable: false` (an archive of an unpinned run may not replay). Only
  `local_chrome` can record (`NETWORK_CAPTURE`); a cloud backend, an explicit `cdp_url`,
  or a non-fresh profile without `allow_session_state: true` on every archiver is refused
  before anything launches, each problem naming both sides.

## Replaying a recorded run

`network_source: { source_id: archive }` replays a recorded run offline
(`docs/architecture_design.md` §4.14 "回放"): every request the browser makes is matched
against the run's `browser.network` archive and answered from it; what the archive does
not hold is answered by the `miss_policy` receptor — never by the network. The source
run's directory is per task, not per config: it arrives through the task context's
`replay_archive`.

```yaml
network_source:
  source_id: archive
  isolation: required               # default; none_acknowledged = run without network isolation
  shift_dates: none                 # none (default, pairs with base_time: from_archive) | all
  matcher:
    levels: [exact, normalized]     # default; add fuzzy for same-path query-ratio matching
    fuzzy_threshold: 0.5            # lowest 2M/T score a fuzzy hit needs (inclusive)
    site_rules: configs/replay_rules   # per-site <domain>.yaml rules (see its README)
  miss_policy: { miss_policy_id: strict }   # required in an archive block
  # max_fulfill_body_bytes: 67108864   (≤ 72 MiB)   fulfill_timeout_s: 10.0   log_repeat_limit: 3
```

- `matcher` is typed: a misspelled key (`levelz`), an unknown level or a threshold
  outside `[0, 1]` is refused at validation, like a misspelled `source_id`.
- `strict` fails a document it cannot serve (a main-frame one marks the step diverged),
  answers beacons, pings, reports and analytics hosts (`extra_benign_hosts` adds more)
  with 204, and every other subresource with 404 + `x-replay-miss: 1`.
- `isolation: required` needs a backend that launches its own browser and declares
  `NETWORK_ISOLATION` (`local_chrome`); replay also needs `browser.network` bound on the
  trace hook, which records the replay log (`replay.jsonl`, one per step).

`flywheel/replay_ui_tars_archive.yaml` replays a run recorded with
`flywheel/record_ui_tars_pinned.yaml`:

```bash
uv run spectragent-run --config configs/flywheel/replay_ui_tars_archive.yaml \
    --start-url URL --task "the recorded task" --replay-archive traces/<recorded-run>
```

A relative `--replay-archive` is resolved against the working directory before it enters
the task context, so the replay's trace records the source run by absolute path
(`task_setup`) — what a fork of the replay rebuilds its environment from.

- **Before Chrome starts**, setup opens the source run (only what its manifest committed)
  and compares its environment fingerprint with this configuration: the tier and its
  parameters (`base_time` aside), the shim, the viewport, the launch flags (without the
  network routing switches on either side). Any mismatch refuses the run, naming both
  sides, together with the configuration's own cooperation problems. A different Chrome
  version, an unpinned user agent or a changed redaction policy only warns — the warnings
  land in the replay's own `env/run.browser.json` (`network_source.warnings`).
- **`base_time: from_archive`** starts the page clock at the recording's T0; it is refused
  with `network_source: live`. `shift_dates: none` (default) serves recorded dates as they
  were and pairs with it; `all` shifts `Date` / `Expires` / `Last-Modified` and cookie
  expiry together and pairs with `base_time: run_start`.
- **The replay log**: every decision (hit, exhausted repeat, synthesised CORS preflight,
  miss) goes into the step's `env/steps/NNN/browser.network/replay.jsonl`; a step whose log
  holds a main-frame document miss has diverged from the recording. Read it with
  `spectragent.core.capabilities.browser.replay.replay_log` / `iter_replay_log` /
  `diverged_steps` over `spectragent.core.archive.source.open_archive(run_dir)`.
- Not replayed (v1): WebSocket traffic and a nested dedicated worker's script (both are
  logged as misses); bodies over `max_fulfill_body_bytes`.

## Branching a recorded run

A branch task puts a recorded run back where it stood before step *t* and lets the agent
decide that step again (`docs/architecture_design.md` §4.1 `BranchLoop`, §4.10
`trace_branches`). `flywheel/branch_ui_tars_archive.yaml` branches runs recorded with
`flywheel/record_ui_tars_pinned.yaml`; point its `task_loader.data_dir` at the directory
the recordings are in and run the sweep:

```bash
uv run spectragent-bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
uv run spectragent-bench --config configs/flywheel/branch_ui_tars_archive.yaml --list   # the task ids only
```

```yaml
agent_loop:
  loop_id: branch
  max_steps: 30
  max_actions_per_step: 1           # must be the source run's (checked before any action)
  continue_steps: 0                 # 0: decide step t only; N: N more ReAct steps after it
  terminal_observation: true        # default: one more observation after the run ends
  restorer:
    restorer_id: prefix_replay
    verify: strict                  # warn: record a failed check and go on
    screenshot_max_distance: 5      # largest Hamming distance of the 64-bit screenshot dHashes
    pace: none                      # recorded_llm_latency: wait as long as the source waited

task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded       # the sources root (default: traces)
    # sources: ["*"]                # globs relative to data_dir, sorted
    step_selector: { selector_id: all }   # every_k {k, offset} | explicit {steps: [2, 5]}
    branches_per_step: 1            # b0, b1, ... per selected step
    require_status: completed       # error | any (reads the manifest status)
    # require_eval: { evaluator_id: online_mind2web, label: 1, slug: null }
    # require_archive: [browser.network]   (default; [] requires none)
    on_unbranchable: raise          # skip: leave such a source out, with its reason
```

- **The tasks.** Every committed policy step of every selected source run whose prefix is
  committed and whose observation was recorded is a candidate; the `step_selector` picks
  among them. A task id is `<source_run_id>/s<t>/b<i>`; its context carries the source's
  setup context (`start_url` …), `replay_archive` (the source run directory, absolute) and
  the `checkpoint` — nothing on the command line. `require_status` / `require_eval` filter
  sources; a source that cannot be branched (format 1, itself a branch or a replay run, no
  `browser.network` archive or environment fingerprint, no unredacted setup context and no
  source benchmark to re-resolve it from, a run id another source shares) stops the sweep
  before anything runs, or is left out with `on_unbranchable: skip`. The sources are read
  through the trace reader only — what their manifests committed — never `result.json`.
- **Before any setup**, the branch loop opens the source and refuses a mismatch naming both
  sides: another agent, a different closed-set agent knob (`prompt_template`,
  `coordinate_space` …) or memory strategy, a different observation or action negotiation,
  a source loop other than `react`, a source that is itself a branch. The teacher's model
  and sampling may differ from the source's.
- **The run.** The context up to step *t* is rebuilt from the source trace — the teacher
  sees exactly what the source agent saw — and every recorded model call is replayed through
  the agent's parser and checked against the recording. The browser replays the source's
  archive; `prefix_replay` re-executes the recorded actions (element indexes re-found by
  position and signature within `retarget_tolerance_px`, tab ids by tab order and URL) and
  verifies every step: URL, interactive-element signatures, screenshot hash distance, main
  -frame document misses, action outcomes. The restored steps are recorded as
  `origin: restored` with their check; step *t* and the `continue_steps` after it are the
  teacher's (`origin: policy`); the terminal observation is `origin: terminal`.
- **No scoring yet**: `trace_branches` has no eval loader, so a configured evaluator is
  refused before the sweep starts — pass `--no-eval`.

## Exporting training data

`spectragent-export` turns recorded runs into a dataset a student model is trained on
(`docs/architecture_design.md` §4.15). The student is an ordinary agent: its run YAML
(`student_run_config`) is composed and assembled — never run, no browser, no model call —
and every sample is rendered by its own `prepare` / `build_react_prompt` / memory and
written by its `format_react_output`, so the training format is the format it is served
in. Point its `llm` at the trained model and run the same YAML to evaluate it.

```bash
uv run spectragent-export --config configs/flywheel/export_sft.yaml
```

```yaml
exporter:
  - exporter_id: openai_messages     # {"id", "messages"} records; assistant turns carry weight
    sources_root: traces             # null: traces in the working directory
    sources: ["recorded/*"]          # globs, sorted by path; branch runs included
    filters:
      require_status: completed      # error | any
      require_eval: { evaluator_id: online_mind2web, label: 1, slug: null }
      exclude_restoration_diverged: true
      max_document_misses: 0         # replay-log main-frame document misses; null: no limit
      exclude_sites: []              # host names, suffix-matched
    split: { by: site, validation_fraction: 0.1, salt: af-split-1 }   # site | task, never step
    decontamination: { against: null, match: either }   # null: the evaluated benchmarks; []: none
    translator:
      translator_id: index_to_coordinate   # identity: the teacher's actions as recorded
      occlusion: check               # "off" (quoted) skips the check
      occlusion_unknown: raise       # untranslatable: drop a step with no occlusion evidence
      untranslatable: drop_step      # truncate | keep_as_text | fail
    student_run_config: configs/flywheel/ui_tars_student.yaml
    thought_source: teacher          # none
    thought_index_refs: drop         # keep
    length: { max_tokens: 65536 }    # image_tokens: qwen_vl (default) | fixed + fixed_image_tokens
    output_dir: datasets/sft_v1      # refused if it exists
# optional: where decontamination and the leak scan read benchmark data (knobs and all)
eval_loader:
  - { benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web }
task_loader:
  - { benchmark_name: online_mind2web, subset: All, data_dir: ~/agent-data/browser/online_mind2web }
```

- **What becomes a sample.** Every policy step that ran at least one action, of every
  source that is readable, format 2, not recorded with `record_secrets`, and passes the
  filters (left-out sources are listed with their reasons in the manifest). A branch run
  contributes its policy steps, with its source run's prefix as context and its source
  run's verdicts as labels; branches of one checkpoint whose targets are identical are
  flagged `duplicate_of`, not removed.
- **Translation.** `index_to_coordinate` turns a click or type on an element index into the
  coordinate action at a visible point of the element on the recorded screenshot; with
  `occlusion: check` it needs evidence that the point is not covered — record with the
  `skyvern_hit_test` cleaner or bind `browser.dom_raw` (a BrowserUse run's paint order
  catches partial cover only). A step the student cannot take or write is handled by
  `untranslatable` (counted by reason in the manifest).
- **The output**: `<output_dir>/{train,validation}.jsonl`, the same samples with their
  provenance in `*.samples.jsonl`, `images/<sha256><suffix>` (each image once) and
  `manifest.json` (no clock, no absolute path — a source that does not read back is named
  by its run-relative file; the reader's message is in the log). Everything is checked first — images,
  coordinates, reference-answer leaks into the prompt, credential-shaped strings, length —
  and written to `<output_dir>.partial`, renamed only when every check passed. The same
  input exports to the same bytes.
- **Record for the student you train.** The student renders from what was recorded: a
  vision student needs the screenshot, at the student's viewport, format and DPR (the
  export refuses a mismatch). Recording with a vision agent and `max_actions_per_step: 1`
  (`flywheel/record_ui_tars_pinned.yaml`) loses nothing; a DOM teacher's index actions need
  occlusion evidence (above), and a step with more actions than the student's
  `max_actions_per_step` is `exceeds_action_budget`.
- **Decontamination default.** `against: null` checks against the benchmarks the sources
  were evaluated on — the benchmark of each source `require_eval` selected (a branch run's
  source task's), never the evaluator's id (`exact_match` grades any benchmark) — so runs
  recorded on that benchmark's own tasks are all excluded and the export fails with
  `ExportEmptyError` saying so. Name a disjoint evaluation set, or write `against: []`.
  Without `require_eval`, nothing is checked (a warning, and `configured: false` in the
  manifest).
- **Benchmark data.** Decontamination and the reference-leak scan read benchmarks through
  the `eval_loader:` / `task_loader:` blocks of the export YAML itself (matched by
  `benchmark_name`, every knob — `data_dir`, `subset` — as configured); a benchmark with no
  block there is read by its registered loader's defaults
  (`$SPECTRAGENT_BENCHMARK_DATA`, see [Benchmark data](#benchmark-data)), never beside a
  configured one. A decontamination reference that names a `subset` reads only the blocks
  of that subset; without one it reads every block of the benchmark — so when you add a
  block for the subset you recorded on (for the leak scan), name the evaluation subset in
  `against`.
- **Length.** The student keeps every turn's screenshot, so a sample grows with its segment:
  the last step of a full 30-step segment at 1280x720 is ~36k image tokens before text.
  `max_tokens: 65536` holds that; for a shorter trainer context use a student with fewer
  `max_steps` — a longer record fails the export by name, it is never truncated.

## Benchmark data

Benchmark data is **not** part of the distribution and is not guaranteed to be
in a checkout — `pip install spectragent` gives you the framework and the
prompt tree, not the datasets. A loader that cannot find its data says so at
construction (`BenchmarkDataError: No data_info.json at ...`) rather than
loading zero tasks, because a run that scores nothing and a run that had
nothing to score are otherwise indistinguishable.

Point the loaders at your own copy in one of two ways:

```bash
# Whole-root relocation: every task_loader and eval_loader in the run.
# Expects <root>/<env_type>/<benchmark>/data_info.json, e.g.
#   ~/agent-data/browser/browsecomp/data_info.json
export SPECTRAGENT_BENCHMARK_DATA=~/agent-data
```

```yaml
# Per-loader override, for a benchmark that lives somewhere of its own.
# Beats the environment variable; `~` is expanded.
task_loader:
  - benchmark_name: browsecomp
    data_dir: ~/somewhere/else/browsecomp
```

Precedence is `data_dir:` > `$SPECTRAGENT_BENCHMARK_DATA` > the in-package
`spectragent/benchmark_data/` directory. Tests that read shipped data skip
themselves — naming the dataset — when none of the three resolves.

## Regenerating the schema

After registering a new action / capability / provider:

```bash
uv run python spectragent/scripts/generate_all.py
```

This rewrites `spectragent/generated/_composed.py` and per-class `.pyi` stubs.
