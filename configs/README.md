# Configs

End-to-end YAML samples that drive `ConfigComposer` → `AgentRuntime`.

需要直接启动三种 browser agent、切换本地/云浏览器或跑数据集时，先看 [Browser agent 运行手册](../docs/browser_agent_runbook.md)。录制、离线回放、分叉、导出训练数据和度量报告的操作步骤见 [数据飞轮运行手册](../docs/data_flywheel_runbook.md)。

## Layout

| File | Purpose |
|---|---|
| `browser_use_local.yaml` | BrowserUse (`browser_use.dom`) + ReactLoop + LocalChrome backend |
| `browser_use_skyvern.yaml` | Same agent/loop, switched to the hosted Skyvern backend |
| `skyvern_agent_local.yaml` | Skyvern (`skyvern.dom`; `skyvern.dom_vision` adds the screenshot) + ReactLoop + LocalChrome backend, with a bounded memory |
| `ui_tars_local.yaml` | UITarsAgent + ReactLoop + LocalChrome backend |
| `flywheel/record_ui_tars_pinned.yaml` | A **recording** run (data flywheel): UITarsAgent, `replayability: pinned`, the three browser archivers on the trace hook — see [Recording environment evidence](#recording-environment-evidence) |
| `flywheel/replay_ui_tars_archive.yaml` | Its **offline replay**: the same agent and tier with `network_source: archive` (isolated, strict misses) and `base_time: from_archive`; the recorded run arrives as `--replay-archive DIR` — see [Replaying a recorded run](#replaying-a-recorded-run) |
| `flywheel/branch_ui_tars_archive.yaml` | **Branching** recorded runs (data flywheel): `agent_loop: branch` (the `prefix_replay` restorer) over the replay browser block, tasks from `task_loader: trace_branches`, the teacher at temperature 0.8 with three branches per step — see [Branching a recorded run](#branching-a-recorded-run) |
| `flywheel/same_path_ui_tars_archive.yaml` | **Same-path replay** (data flywheel): the branch sweep restoring every recording to its last step (`step_selector: last`, `continue_steps: 0`, `verify: warn`, two tasks per source) — the metrics chain's fidelity measure, see [Measuring the flywheel](#measuring-the-flywheel) |
| `flywheel/export_sft.yaml` | **Exporting** recorded runs as a training dataset (data flywheel): one `exporter:` block, no agent, no environment — see [Exporting training data](#exporting-training-data) |
| `flywheel/ui_tars_student.yaml` | The export's **student**: UITarsAgent with `coordinate_space: normalized_1000`, the recording's screenshot pipeline, no hooks; assembled by the export, never run by it |

## Quick start

```bash
# Validate compose pipeline only (no real LLM / Chrome required)
uv run spectragent run --config configs/browser_use_local.yaml --dry-run

# Run end-to-end (needs OPENAI_API_KEY + a Chrome binary)
uv run spectragent run --config configs/browser_use_local.yaml \
    --start-url https://example.com --task "report the page title"
```

`--dry-run` exercises:

1. YAML schema validation against `ConfigComposer.top_level_type`.
2. `ConfigComposer.compose()` — instantiates every layer, setattr-injects all wired receptors (including `capability.backend`, `agent.llm`, `agent.memory`).
3. Surface report (`agent`, `loop`, `model`, `environment`, `capabilities` and their backends).
4. `AgentRuntime.prepare()` — loop slots plus observation/action negotiation, and the environment's setup check: everything setup would refuse (the browser's cooperation rows, a task-context key no capability accepts), with `--start-url` / `--replay-archive` as the task context. A replaying config needs `--replay-archive DIR` here too, and its consistency with that recorded run is checked from the run's environment fingerprint.

It does **not** start a browser or call the LLM — useful for CI smoke tests. `spectragent bench` makes the same check once for its first task before running any (the pre-flight); a refused config runs no task, opens no trace and exits 2.

## YAML shape

conscribe `inline_wiring=True` lets every wired child be inlined inside its owning entry, so the agent block carries its loop / llm / memory and the environment carries its capabilities:

```yaml
agent:
  - agent_id: browser_use.dom
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
        replayability: { replayability_id: none }
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
- Single-instance receptors (`llm`, `agent_loop`, `capability.backend`, `capability.mouse_trajectory`, `capability.typing_rhythm`, `capability.dom_cleaner`, `capability.dom_serializer`, `capability.replayability`, `capability.network_source`, and `miss_policy` inside an `archive` network source) are **required**; list-shaped ones (`environment.capabilities`, `capability.actions`) are optional: omitted (or `null`) they adopt the matching instances of a top-level section, and with none the owner's default applies (every compatible capability; every wired action). `[]` always means empty: `actions: []` builds no action at all.
- A strategy is always a receptor block, never a name on `profile:`. `profile:` says which watchdogs run and which CDP round trips they may spend — nothing else. Naming a strategy there is what made `mouse_trajectory`, then `dom_cleaner`, then `dom_serializer` arrive default-constructed with every configured knob discarded.
- `browser.dom` has a cost surface of its own, off by default: `dom_max_elements`, `dom_max_serialized_chars` (both a number or `unlimited`, the default — a numeric limit is never written `null`), `dom_interactive_only`, `dom_viewport_only`. When any of them cuts, the observation fragment records the cut in `budget`, so a trace never shows a truncated page as a sparse one.
- The two input-humanization receptors are independent axes and each is its own off-switch: `mouse_trajectory: { trajectory_type: instant }` and `typing_rhythm: { rhythm_type: instant }` are the legacy instant jump and the legacy single bulk `Input.insertText`. There is no boolean beside either — the strategy *is* the switch.
- `replayability` has the same shape (`docs/architecture_design.md` §4.14): `{ replayability_id: none }` pins nothing and opens no second CDP connection; `pinned` (knobs `base_time`, `clock`, `random_seed`, `timezone`, `locale`, `device_scale_factor`, `user_agent`, `reduced_motion`) makes a recording replayable. Its `clock` is `shared` by default — one clock for the whole run, T0 at its start and then real time, so every tab reads the same time — or `per_document`, each document its own counter, whose values a replay reads back exactly but which tabs disagree on (a site that syncs its tabs by timestamp can hang). It needs a backend that launches its own browser (`local_chrome`), which it launches with `TZ=<timezone>` so a frame that lost the frame owning its process's overrides still reads the pinned zone. Its user agent override carries the browser's own client hints (`navigator.userAgentData`, `Sec-CH-UA`) when `user_agent` is unset or a normal Chrome identifier; such an identifier must name the installed Chrome's major version and platform, or setup refuses it once the browser has started.
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

`browser.navigate` (also what setup opens the task's start page with) waits twice. First for the page to commit, which is Chrome answering the navigation, for at most `timeout_ms` (default 30000). Then for its `wait_until` phase (`load` by default), for at most `lifecycle_timeout_ms` (default: the same as `timeout_ms`). A page that committed but whose `load` never arrives in time (a resource that never finishes loading, while the page is already drawn) gets a WARNING in the log and the navigation succeeds. A server that never answers, a URL Chrome refuses, and a navigation Chrome reports failed (an unreachable host, a 204, a download) fail it.

The list is also an **ablation switch**: only the actions it names are instantiated, advertised to the model and executable. Omit the block entirely (or write `actions: null`) to get everything `BrowserCapability.__wiring__` offers; `actions: []` builds no action at all. Narrow it and an agent that *requires* a dropped action fails action negotiation before the run starts — that is the intended "config errors surface at negotiation, not at the first LLM call" guarantee.

`action_id` uses the fully-qualified registry key (`browser.scroll`, not `scroll`).

## What browser_use sees: two family members

`browser_use` is an agent family (`docs/architecture_design.md` §4.1, "Agent 家族"). Pick the member by `agent_id`; there is no mode field:

```yaml
agent:
  - agent_id: browser_use.dom          # browser_use.dom | browser_use.dom_vision
```

| `agent_id` | in the model's context | click verbs |
|---|---|---|
| `browser_use.dom` | the element tree as text | `browser.click` (index) |
| `browser_use.dom_vision` | that plus a screenshot | `browser.click` **and** `browser.click_coordinate`, independently |

Both members read the DOM. A model that reads only the screenshot is a computer-use agent: use `ui_tars`.

Each member writes out what it observes and which actions it declares, so there is nothing else to keep in sync:

- **what the prompt describes** — one prompt tree, one surface fragment spliced in per member, so a DOM / DOM-plus-screenshot ablation differs by that fragment and nothing else;
- **what the run negotiates** — both require `browser.dom`; only `browser_use.dom_vision` can activate `browser.click_coordinate`;
- **what the model may emit** — the output schema is compiled from the negotiated actions, so a verb the member does not have cannot be written.

The DOM is never optional. Either member against `profile: vision_only` fails at negotiation, before the browser is launched:

```
IncompatibleFragmentError: Required observation fragments not provided by environment.
Missing: ['dom']. Requested by: dom ← browser_use.dom.
Environment provides: ['browser.browser_meta', 'browser.screenshot'].
```

Screenshots are captured and recorded by **both** members: `online_mind2web` and `lexbench_browser` score runs from the screenshots in the trace. The member decides what the model sees, not what the run records.

`step_history_window` (default 10, at least 1) is how many of the most recent steps the agent history in each step's message replays, each with its reply, actions, results and effects. It changes the prompt, so it is an explicit experiment variable.

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

A vision agent (`ui_tars`, or `browser_use.dom_vision` with `browser.click_coordinate`) aims at the screenshot the step showed. Two settings decide whether that is still the page it acts on.

**When the screenshot is taken.** An action returns `post_delay_ms` after its effect was dispatched (default `0`), and the next observation then waits for the page to settle before it captures anything: the document loaded, then `settle_quiet_ms` (default 500) of no DOM change and no resource finishing with something visible in the viewport (text, an image, a video, a canvas, an SVG drawing, a frame or a form control; a white page with only a CSS loading bar does not count), for at most `settle_max_wait_s` (default 5 s; `0` turns the wait off). The wait belongs to the observation, so a DOM-only run waits the same way, and each fragment records how long it waited and whether the cap ended it (`settle`; `docs/architecture_design.md` §4.14). Both are capability knobs:

```yaml
capabilities:
  - capability_id: browser
    settle_quiet_ms: 500        # quiet window before a capture (0: wait for the load and visible content only)
    settle_max_wait_s: 5        # the cap; a page that never goes quiet costs this per step
```

What the wait cannot see is a change that touches neither the DOM nor a resource: a CSS-animated spinner, a canvas challenge, a fade. Such a widget — a reCAPTCHA spinner, an image challenge fading in — can still be captured mid-animation, and the next reply aims at what is no longer there. Give the coordinate actions a settle delay:

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

**What a point is.** `coordinate_space` declares which grid the model writes points on, and `ui_tars` teaches it. The framework does the converting: each point the model writes goes into the capability's units (CSS pixels for the browser) using the size of the screenshot that step showed, and every point fed back to the model (actions, effects, the pointer) is written on the same grid. The device scale factor and viewport size need no setting of their own. Pick the grid the model really answers on — `screenshot_pixels` (the default) or `normalized_1000` (0-1000 on both axes, x first):

```yaml
agent:
  - agent_id: ui_tars
    coordinate_space: normalized_1000
```

`browser_use.dom_vision` takes the same field, with the same two values, and uses it the same way — it is a property of the model, not of the agent class. `browser_use.dom` writes no point and has no such field.

Which grid a given model uses is not something to guess from its name: check a few of its answers against known element positions before a run. A point off the grid is clamped onto the edge of the viewport.

## New tabs

When the page an agent acts on opens a new tab — a `target=_blank` link or form, a button calling `window.open` — the next observation shows the new tab, as a real browser brings it to the front. This holds for every agent, including `ui_tars`, which has no `browser.switch_tab`. It is a capability knob:

```yaml
capabilities:
  - capability_id: browser
    new_tabs: follow       # the default; stay: the agent stays on its tab (the behaviour before)
```

- **Which tab is followed.** A tab the agent's tab opened during the agent's actions, from the first action after an observation until the next observation captures; of several, the last one opened. A tab a page opens on its own without the user's activation — a popup it opens as it loads or from a timer, the second window of one click, the ones a real browser's popup blocker stops — is listed in `browser_meta`'s tabs and not followed. So is a tab a page opens between an observation and the agent's next action. After a `browser.switch_tab` the agent's own choice stands: only tabs the newly focused tab opens from then on are followed.
- **A tab that closes itself.** When the tab the agent is on closes itself (`window.close()`), the agent goes back to the tab that opened it, or to the most recently opened tab still open. This holds under `stay` too: there is nothing left to stay on.
- **What the observation waits for.** The observation's navigation and settle waits run on the followed tab; a tab that does not finish its first navigation within 10 s is not followed (a warning says so).
- **What the trace says.** Every fragment of the capture that followed a tab carries `focus_moved: {reason: new_tab | tab_closed, from_tab, to_tab}` (the `tab_id`s `browser_meta` lists); a capture that moved nothing writes no such key. No agent renders it; a prompt differs only because the page it shows is another one.
- **Tab order.** `browser_meta.tabs`, and the "most recently opened tab" of `browser.switch_tab` with `target_id: null`, follow the order the tabs opened, not Chrome's own tab list.
- **Recording, replay and branching** apply the same rule, so all three configs use one value; the shipped ones use the default. The value is recorded raw in `config.json`. A branch whose `new_tabs` differs from its recording's diverges at the first tab the recording followed (`url_mismatch`).

## Tabs the agent left

What happens to a tab once the agent's focus leaves it — the tab a followed new tab came from, the tab a `browser.switch_tab` left — is a second capability knob, and how hard Chrome slows tabs that are not in front is a `local_chrome` launch knob:

```yaml
capabilities:
  - capability_id: browser
    background_tabs: run          # the default; freeze: a tab the focus left runs nothing until it comes back
    backend:
      provider_id: local_chrome
      background_throttling: false  # the default; true: Chrome throttles background tabs as it does for a user
```

- **Why.** iqiyi.com keeps its tabs in step through `localStorage`; with five of its tabs open the tabs rewrote each other without end, every renderer pegged its CPU and the run ended because the observation's screenshot got no reply. A frozen tab answers nobody, so `background_tabs: freeze` stops it. Throttling slows a background tab's timers, not the `storage` events such tabs trade, so `background_throttling: true` alone does not.
- **What `freeze` does.** The tab the focus leaves is frozen (Chrome's page lifecycle freeze): its timers, scripts, storage events and network callbacks stop until the focus comes back, and it is set running again before it is shown. Only tabs the focus was on are frozen: a popup that was not followed, or a new tab under `new_tabs: stay`, keeps running.
- **What it changes for the page.** A video or audio that was playing in the tab is paused when it is frozen, and is still paused when the agent returns; a countdown or a poll on the page stands still while it is frozen. With `run`, the tab plays and ticks on in the background.
- **What `background_throttling: true` changes.** `local_chrome` launches without `--disable-background-timer-throttling`, `--disable-backgrounding-occluded-windows` and `--disable-renderer-backgrounding`, the switches it took over from Puppeteer's automation defaults. The tab the agent is on is in front either way. Measured in headless Chrome 155: a tab the focus left wakes its timers about 10 times a second with the switches, about once a second without them, and once a minute after its first minute in the background. In a headful run, a window covered by other windows is throttled too.
- **Recording, replay and branching** must use one value of each. `background_tabs` is recorded raw in `config.json` and in the environment fingerprint; the three switches are part of the launch flags. An offline replay refuses a recording made with another value of either, and a recording made before `background_tabs` existed.

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
    archive:                          # the one switch: omitted, null or [] = record nothing
      - archiver_id: browser.network  # WARC/1.1 + index.jsonl per step
      - archiver_id: browser.storage  # cookies + local/session storage, values as they are
      - archiver_id: browser.dom_raw  # raw DOM of each committed cycle
```

- Evidence lands in `traces/<run>/env/` and is read through `RunTrace.archive_files` /
  `blob` / `archive_run_file`.
- `trace_writer.archive` is the one switch for archiving: omitted, `null` or `[]`, nothing
  is recorded. Archivers are listed only there; a top-level `archiver:` section is refused
  at compose time with a message that says to list them under `trace_writer.archive`.
- Each archiver has knobs (`max_inline_body_bytes`, `strip_request_headers`,
  `drain_timeout_s`, `allow_session_state`, …), validated against the generated schema;
  a mistyped `archiver_id` is refused.
- `browser.network` refuses `replayability: none` unless it says
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
- Every site rule declares both of its policies, with no framework default:
  `exhaustion: repeat_last | miss` (after its response sequence is used up, repeat the
  last available response or miss) and `unavailable: miss | serve_identical` (a recorded
  request kept without a body — cancelled by the page, still in flight at the end, served
  from the memory cache, or a body read that timed out — is answered by the miss policy,
  or by an available record of the same sequence when every available record there has
  the same status and body, the consumed record's own recorded status, if any, is that
  status, and the source request did not fail on the network). The shipped `baseline.yaml`
  chooses `repeat_last` and `serve_identical`; `configs/replay_rules/README.md` has the
  schema.
- A recorded JSONP answer served to a request that asks for another callback name
  calls the name this request asks for: the replay rewrites the leading function name
  when the wrapper is provable, and the hit's replay-log entry says so
  (`callback_rewrite`). No knob: `docs/architecture_design.md` §4.14 "JSONP 回调名".
- `strict` fails a document it cannot serve (a main-frame one marks the step diverged),
  answers beacons, pings, reports and analytics hosts (`extra_benign_hosts` adds more)
  with 204, and every other subresource with 404 + `x-replay-miss: 1`.
- `isolation: required` needs a backend that launches its own browser and declares
  `NETWORK_ISOLATION` (`local_chrome`).
- **The archive list**: a replay binds `browser.replay_log` on the trace hook where a
  recording binds `browser.network` (the other archivers stay). `browser.replay_log`
  records the replay log (`replay.jsonl.gz`, one per step); a replay without it is refused.
  `browser.network` always records live traffic, and a replay has none, so it is refused
  under `network_source: archive`, naming `browser.replay_log`; a `live` source refuses
  `browser.replay_log` in turn. All three are refused at `prepare()`.

  ```yaml
  hook:
    - hook_id: trace_writer
      archive:
        - archiver_id: browser.replay_log   # the replay's decisions
        - archiver_id: browser.storage
        - archiver_id: browser.dom_raw
  ```

`flywheel/replay_ui_tars_archive.yaml` replays a run recorded with
`flywheel/record_ui_tars_pinned.yaml`:

```bash
uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml \
    --start-url URL --task "the recorded task" --replay-archive traces/<recorded-run>
```

A relative `--replay-archive` is relative to the launch directory: `AgentRuntime.execute`
resolves it before `prepare()` checks it, the context is recorded (`task_setup`) and it is
handed to setup, for the CLI and a Python caller building `env_context` alike, so the
replay's trace records the source run by absolute path.

- **At `prepare()`** — before the trace is opened, on a `--dry-run` too — the browser's
  setup check opens the source run (only what its manifest committed; the environment
  fingerprint alone, no index) and compares that fingerprint with this configuration; setup
  compares again before Chrome starts, then loads the whole archive to serve it: the tier and its
  parameters (`base_time` aside), the shim, the viewport, the launch flags (without the
  network routing switches on either side; `TZ` included), and the browser's default time
  zone and locale as far as this machine's system settings tell them (the locale on macOS
  only). Any mismatch refuses the run, naming both sides, together with the configuration's
  own cooperation problems. After Chrome starts, before any tab is prepared, its own default
  time zone and locale are compared once more, which decides what the settings could not. A different Chrome
  version, an unpinned user agent or a changed redaction policy only warns — the warnings
  land in the replay's own `env/run.browser.json` (`network_source.warnings`).
- **`base_time: from_archive`** starts the page clock at the recording's T0; it is refused
  with `network_source: live`. `shift_dates: none` (default) serves recorded dates as they
  were and pairs with it; `all` shifts `Date` / `Expires` / `Last-Modified` and cookie
  expiry together and pairs with `base_time: run_start`.
- **The replay log**: every decision (`hit`, `exhausted` repeat, `substituted` answer for a
  record without a body, synthesised CORS preflight, `miss`) goes into the step's
  `env/steps/NNN/browser.replay_log/replay.jsonl.gz`; a step whose log holds a main-frame
  document miss has diverged from the recording. A `substituted` entry names the consumed
  record (`record_id`), the one served (`served_record_id`) and how the source request ended
  (`source_outcome`); a `body_unavailable` miss carries `source_outcome` too, `repeated: true`
  when it repeats an exhausted sequence's body-less last record, and a `range_uncovered`
  miss the page's `Range` header (`requested_range`). Read it with
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
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --list   # the task ids only
```

```yaml
agent_loop:
  loop_id: branch
  max_steps: 30
  max_actions_per_step: 4           # must be the source run's (checked at assembly)
  continue_steps: 0                 # 0: decide step t only; N: N more ReAct steps after it
  terminal_observation: true        # default: one more observation after the run ends
  history_source: source            # exact source history, or restored execution history
  restorer:
    restorer_id: prefix_replay
    verify: strict                  # warn: record a failed check and go on
    screenshot_max_distance: 5      # largest Hamming distance of the 64-bit screenshot dHashes
    pace: none                      # recorded_llm_latency: wait as long as the source waited;
                                    # recorded_timeline: keep to the source's timeline (below)
    media: align                    # none: leave media where the replay put it (control arm)
    media_time_tolerance_s: 1.0     # seconds a media position may differ and still pass
    media_seek_timeout_s: 2.0       # seconds an alignment seek (and play) may take
    media_end_lead_s: 0.5           # a video recorded at its end is played into it from this far before

task_loader:
  - benchmark_name: trace_branches
    data_dir: traces/recorded       # the sources root (default: traces)
    # sources: ["*"]                # globs relative to data_dir, sorted
    step_selector: { selector_id: all }   # every_k {k, offset} | explicit {steps: [2, 5]} | last
    branches_per_step: 3            # b0, b1, b2 per selected step (the shipped value)
    agent_outcome: any              # done | max_steps | error | timeout | vetoed (reads result.json)
    # require_eval: { evaluator_id: online_mind2web, label: 1, slug: null }
    # require_archive: [browser.network]   (default; [] requires none)
    on_unbranchable: raise          # skip: leave such a source out, with its reason
    exclude_unloaded_checkpoints: true   # false: branch from pages captured before they loaded too
    # task_variants: variants.json  # another task per checkpoint (needs history_source: restored)
```

- **The tasks.** Every committed policy step of every selected source run whose prefix is
  committed, whose observation was recorded and that the source's trace is complete through
  is a candidate; the `step_selector` picks among them. The trace's own ledger is a hard
  condition: a trace never closed (`writing`) gives no candidate, an `incomplete` one every
  step before its hole. A task id is `<source_run_id>/s<t>/b<i>`; its context carries the source's
  setup context (`start_url` …), `replay_archive` (the source run directory, absolute) and
  the `checkpoint` — nothing on the command line. `agent_outcome` (how the agent ended,
  `any` by default) / `require_eval` filter sources; a source that cannot be branched (an
  unreadable trace — an older trace format included — itself a branch or a replay run, no
  `browser.network` archive or environment fingerprint, no unredacted setup context and no
  source benchmark to re-resolve it from, a run id another source shares) stops the sweep
  before anything runs, or is left out with `on_unbranchable: skip`. The sources are read
  through the trace reader only — what their manifests committed — and `result.json` only
  when `agent_outcome` asks how the agent ended.
- **Pages captured before they loaded.** With `exclude_unloaded_checkpoints: true` (the
  default; the shipped branch and same-path configs write it), a chosen step whose recorded
  `capture_state` says the main document had not reached its `load` event when the step
  was captured (`settle.loaded: false`, however the settle wait ended) is not a checkpoint: it is listed in the loader's skipped steps
  (`<source>#s<t>`) with the reason. A page whose DOM never went quiet does not count, a
  step with no `capture_state` recorded is kept, and it applies after the `step_selector`
  (`last` on such a step gives that source no task, not an earlier step).
- **Before any setup**, the branch loop opens the source and refuses a mismatch naming both
  sides: a source trace never closed or not complete through the checkpoint, another agent,
  a different closed-set or numeric agent knob (`prompt_template`,
  `coordinate_space` …), a different memory strategy or memory knob (`max_messages` …), a
  different `max_actions_per_step`, a different observation or action negotiation, a source
  loop other than `react`, a source that is itself a branch, and, under
  `pace: recorded_timeline`, source steps that execute actions but measured no action
  start times (`action_spans`, named step by step). The teacher's model and
  sampling may differ from the source's, and so may the loop's `max_steps`.
- **The run.** The context up to step *t* is rebuilt from the source trace — the teacher
  sees exactly what the source agent saw — and every recorded model call is replayed through
  the agent's parser and checked against the recording. The browser replays the source's
  archive; `prefix_replay` re-executes the recorded actions (element indexes re-found by
  position and signature within `retarget_tolerance_px`, tab ids by their position in the
  order the tabs opened and their URL) and verifies every step: URL, interactive-element
  signatures, media playback state, screenshot hash distance, main-frame document misses,
  action outcomes. With `media: align` (the default) each restored observation, the
  checkpoint's included, first moves every matched video or audio element to the position
  and play state recorded with that step's screenshot — inside the capture, after the page
  settles and right before the screenshot — and each action is kept with the step's check;
  a seek that does not complete in `media_seek_timeout_s` (or lands elsewhere) is undone
  and the step fails `media_mismatch`. A video the recording showed at its end is sought
  `media_end_lead_s` before it and played through to its natural end, so the player shows
  its end screen; the lead must be shorter than `media_seek_timeout_s`, or the restorer is
  refused at construction, naming both. Nothing is aligned after the checkpoint, and with
  `media: none` the media state is still compared. A tab
  the recording's actions opened is followed again by the same rule (`new_tabs`, above), so
  the restored observation is of the same tab. The restored steps are recorded as
  `origin: restored` with their check; step *t* and the `continue_steps` after it are the
  teacher's (`origin: policy`); the terminal observation is `origin: terminal`. A branch
  run is a replay, so its trace hook binds `browser.replay_log` in `browser.network`'s
  place, as in the replay config.
- **Pace.** The source run waited for its model between steps (seconds to tens of
  seconds) while the page's own timers (carousels, autoplay, countdowns, lazy loading,
  delayed popups) kept running; restored steps run back to back, so a restored page is
  behind the recording. `pace: none` does not wait. `recorded_llm_latency` waits, before a
  restored step's actions, as long as the source waited for its model at that step;
  observation, action and recording time are not made up, so the restoration drifts.
  `recorded_timeline` keeps to the source's timeline from its step-0 observation on. An
  observation's capture time is the earliest `captured_at` of its environment fragments,
  on both sides, and the two step-0 captures are the anchors. Each later restored
  observation, the checkpoint's included, starts early by as long as this restoration's
  previous observation took, so that it captures when the source's did. Each restored
  step's first action starts when the source step's first action did
  (`StepRecord.action_spans`), and the rest of the step follows at once. A restoration
  already behind goes on at once and never hurries to catch up. How long the start page
  took to load is network speed and is not reproduced. The waits write nothing to the
  trace.
- **Teacher sampling.** The branches of one checkpoint get identical inputs, so only the
  teacher's sampling makes them differ: the shipped file sets the teacher's
  `temperature: 0.8` (the recording ran at the openai default, 0.2) and
  `branches_per_step: 3`. An explicit `prompt_contribution` hook can generate an
  additional instruction after restoration. Its stable Python function receives
  `PromptRequest` and may return text or structured `PromptContribution`; the Agent
  decides where it belongs. Default scope generates once at the first branch policy
  step; each-policy-step scope still reuses contributions for parse retries. Source
  reconstruction reads recorded contributions without regenerating them. Helper calls
  are recorded separately from Agent decisions (`docs/data_flywheel_runbook.md`).
- **Task variants** ("same page, different need"): `task_variants` names a JSON file
  (`{"format": "task-variants-1", "variants": [{source_run_id, step, variant_id, task_text,
  note}]}`) giving another task for a checkpoint. The file names the checkpoints (among the
  candidates `step_selector` chose); each variant becomes the tasks
  `<source_run_id>/s<t>/v<variant_id>/b<i>`, which run under the variant's task text with
  everything else the source's, and record `branch.task_variant` (both task texts and the
  note) in their trace. They need `history_source: restored`; `source` is refused before the
  sweep starts. A malformed file fails the listing; an entry that cannot be used is left out
  with its reason (`docs/data_flywheel_runbook.md`, "任务变体").
- **No scoring yet**: `trace_branches` has no eval loader, so a configured evaluator is
  refused before the sweep starts — pass `--no-eval`.

## Exporting training data

`spectragent export` turns recorded runs into a dataset a student model is trained on
(`docs/architecture_design.md` §4.15). The student is an ordinary agent: its run YAML
(`student_run_config`) is composed and assembled — never run, no browser, no model call —
and every sample is rendered by its own `prepare` / `build_react_prompt` / memory and
written by its `format_react_output`, so the training format is the format it is served
in. Point its `llm` at the trained model and run the same YAML to evaluate it.

```bash
uv run spectragent export --config configs/flywheel/export_sft.yaml
```

```yaml
exporter:
  - exporter_id: openai_messages     # {"id", "messages"} records; assistant turns carry weight
    sources_root: traces             # null: traces in the working directory
    sources: ["recorded/*"]          # globs, sorted by path; branch runs included
    filters:
      agent_outcome: any             # done | max_steps | error | timeout | vetoed
      require_eval: null             # optional: { evaluator_id, label: 1, slug: null }
      exclude_restoration_diverged: true
      max_document_misses: 0         # replay-log main-frame document misses; unlimited: no limit
      exclude_sites: []              # host names, suffix-matched
      exclude_unloaded_steps: true   # a step captured before its page loaded is no sample
    split: { by: site, validation_fraction: 0.1, salt: af-split-1 }   # site | task, never step
    decontamination: { against: [], match: either }   # the benchmarks you evaluate on; []: none
    translator:
      translator_id: index_to_coordinate   # identity: the teacher's actions as recorded
      occlusion: check               # none skips the check
      occlusion_unknown: raise       # untranslatable: drop a step with no occlusion evidence
      untranslatable: drop_step      # truncate | keep_as_text | fail
    student_run_config: configs/flywheel/ui_tars_student.yaml
    thought_source: teacher          # none
    thought_index_refs: drop         # keep
    wait_actions: keep               # drop_when_capped: see "Waits on capped captures" below
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
  filters (left-out sources are listed with their reasons in the manifest). No verdict is
  needed: `require_eval` is an optional filter, `null` by default, so a run the teacher
  failed is exported too and labelled `labels.step_status`. A branch run contributes its
  policy steps, with its source run's prefix as context and its source run's verdicts (if
  any) as labels; branches of one checkpoint whose targets are identical are
  flagged `duplicate_of`, not removed. With `exclude_unloaded_steps: true` (the default) a
  step whose recorded `capture_state` says it was captured before the main document loaded
  (`settle.loaded: false`) is no sample but stays in later samples' context as it
  happened; each one is listed in the manifest's `steps.filtered` with the reason.
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
  export refuses a mismatch). Recording with a vision agent and `max_actions_per_step: 4`
  (`flywheel/record_ui_tars_pinned.yaml`) loses nothing; a DOM teacher's index actions need
  occlusion evidence (above), and a step with more actions than the student's
  `max_actions_per_step` is `exceeds_action_budget`.
- **Decontamination** checks only the benchmarks `against` lists; nothing is inferred from
  verdicts. The default `[]` checks nothing, and the manifest says `decontaminated: false`.
  Listing the benchmark the runs were recorded on excludes them all (`ExportEmptyError`
  saying so): list a disjoint evaluation set. The manifest's leak-scan counts say how many
  samples were actually compared (`compared`) and why the rest were not
  (`no_eval_loader`, `no_reference_answer`).
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
- **Waits on capped captures.** On a slow network the settle wait before a capture is
  often ended by its 5 s cap, and the teacher, shown a page still loading, waits.
  `wait_actions: drop_when_capped` removes those waits from the exported data — never from
  the recorded traces (`docs/architecture_design.md` §4.15, "等待动作"). The shipped
  `export_sft.yaml` keeps `keep`, which changes nothing and calls no model:

  ```yaml
  #   thought_source: teacher
  #   wait_actions: drop_when_capped
  #   # The helper chat model that rewrites the affected thoughts (exactly one), and the
  #   # directory its rewrites are cached in (relative to the working directory). Both are
  #   # required with drop_when_capped and thought_source: teacher, and refused otherwise.
  #   thought_rewrite_llms:
  #     - { model_type: openai, model: gpt-4.1-mini, provider: { name: direct } }
  #   thought_rewrite_cache: datasets/thought_rewrites
  ```

  On a policy step whose `capture_state` says the settle wait was `capped`, every wait
  that ran and succeeded is removed (an action is a wait when its class declares
  `only_waits`, as `browser.wait` does); a failed wait stays, it is why the actions after it
  were skipped, and so does a wait in the skipped tail. A step without `capture_state` is
  left as it is. A step that only waited leaves the trajectory: no sample, no turn in later
  samples' context, no entry in their `history_steps`, and the step numbers the student is
  shown move up. The thought of a step that lost waits and kept other actions, and of the
  first step kept after removed ones, is rewritten by the model to drop only what speaks of
  waiting or loading; a changed thought is marked `synthetic`, one returned as it was is
  `reviewed_unchanged`. Each affected sample's `wait_removal` in `*.samples.jsonl` records
  the removed actions (run, step, action index), the original and rewritten thought, the
  model, the prompt version and digest; the manifest's `wait_actions` sums it up. Rewrites
  are cached by content, so a re-export reads them back, calls no model and writes the
  same bytes; the CLI prints how many came from the cache and how many from a call (not
  the manifest: the dataset must not depend on it). The student's observation turn still
  shows the recorded `capture_state` (`Settle: capped ...`) unless its agent sets
  `show_capture_state: false`.

## Measuring the flywheel

`spectragent flywheel` runs the data flywheel end to end and reports on it
(`docs/architecture_design.md` §4.16): record → same-path replay → branch → export (twice) →
a report of every metric and section, each with a rule-based, evidence-backed recommendation
(`supported` only on live evidence from at least 10 sites; fewer is `partial`, an
"insufficient sample"). The report reads only committed traces and published datasets, so it
can be recomputed anywhere.

The chain is a plain YAML spec — not a run root, so it does not live in `configs/flywheel/`
(every file there must compose and prepare). Its stages name ordinary run YAMLs; the chain
only writes their path fields (the trace writer's `trace_dir`, the `trace_branches` loader's
`data_dir`, the exporter's `sources_root` / `sources` / `output_dir`) and runs the existing CLIs
in this process. Relative paths are relative to the working directory.

```yaml
name: first-live
root: traces/chains/first-live         # everything lands under it (traces/ is git-ignored)
evidence: live                         # live | rehearsal: the basis of every recommendation
tasks: {benchmark: online_mind2web, subset: All, task_ids: [...], limit: null}   # record / control flags
token_limit: 2000000                   # every run stage together; unlimited (the default): no limit
record:    {config: <recording YAML>, timeout: 600, evaluate: false}   # evaluate: false → --no-eval
control:   {config: <the recording with archive: []>}                  # optional: the overhead control group
same_path: {config: configs/flywheel/same_path_ui_tars_archive.yaml}
branch:    {config: <branch YAML>}
export:    {config: <export YAML>, repeat: 2}                          # 2: export determinism
report:    {planted: [{label: teacher api key, env: OPENAI_API_KEY}]}  # searched for, never written
```

```bash
set -a; . ./.env; set +a     # spectragent bench and the chain never read .env
uv run spectragent flywheel run --spec chain.yaml                      # every configured stage
uv run spectragent flywheel run --spec chain.yaml --stages record      # one stage at a time
uv run spectragent flywheel report --spec chain.yaml                   # recompute the report
uv run spectragent flywheel report --root traces/chains/first-live --evidence live --out /tmp/report
```

- **Layout**: `<root>/traces/{record,control,same_path,branch}/`, `<root>/datasets/{first,second}/`,
  `<root>/report/{report.json,report.md}`, `<root>/configs/<stage>.yaml` (what each stage ran),
  `<root>/evals/<stage>/` (a stage with `evaluate: true`), `<root>/chain.json` (argv, exit codes,
  runs and times — provenance only).
- **Outcomes**: a run stage that exits 0 or 1 and wrote a run directory ran (a failed task is
  data); anything else stops the chain naming the stage, except 4 below. An export never
  overwrites `datasets/first|second`: with the export selected and either one present, the
  chain refuses before any stage runs (exit 2) — remove them to export again.
- **Token limit**: `token_limit` caps the tokens every run stage uses together, counted from
  the model calls each run's trace recorded (the provider's `total_tokens`, or prompt plus
  completion; a call whose provider reported no usage is counted and named, never read as 0).
  The chain counts the runs already under the root first, hands each run stage what is left
  (`spectragent bench --token-limit`), and the sweep adds each finished task. Once the total
  reaches the limit, the task that reached it finishes, no later task or stage (the export
  included) starts, the report is still computed from what exists and says why the chain
  stopped, `chain.json` records `stopped_by_token_limit`, and `spectragent flywheel run` exits 4.
  `unlimited` (the default) counts nothing and changes nothing.
- **The export YAML** of a chain uses `filters.require_eval: null` (the chain does not evaluate)
  and `decontamination: {against: []}` (the sources are the benchmark's own tasks) — the
  defaults, as in the shipped `export_sft.yaml`.
- **After the report**: `sections.sites.yaml` is the exclusion list in `filters.exclude_sites`
  shape; put it into the export YAML and export again by hand.
- **A rehearsal** on the fixture site with scripted models (no spend) runs the whole chain:
  `uv run python -m tests._helpers.flywheel_rehearsal --out <new dir>`.

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

A browser task's start page must be one complete `http://` or `https://` URL.
LexBench's data writes its start pages as bare hosts (`www.bilibili.com`,
sometimes with a path), so the `lexbench_browser` loader turns each into
`https://…` before the run records it; the flywheel report and the export
recognise a site by that URL's host. A start page written as text or as
several sites (`www.xiachufang.com或其他美食网站`) is left as written, and
`prepare()` refuses that task, naming its start page, before a trace is
opened. The other benchmarks already write complete URLs and are not changed.

## Regenerating the schema

After registering a new action / capability / provider:

```bash
uv run spectragent config generate
```

This rewrites `spectragent/generated/_composed.py`, `_composed.schema.json` and
`.fingerprint.json`, and prunes obsolete per-class `.pyi` stubs.


## Independent replay review

The three files under `configs/replay_review/` compose an independent `replay_reviewer` job
(`docs/architecture_design.md` §4.18). Its `decision_model` receptor accepts TypeSafe native
and OpenRouter Decisions providers; Chat `llm` receptors remain separate. The three files
differ only in the route; their review blocks are identical.

```yaml
replay_reviewer:
  - reviewer_id: jev
    decision_model: {...}              # the route
    evidence:                          # what a request carries
      done_answer: withhold            # keep | withhold: the source done answer's text
      max_text_chars: 6000             # per observation side; the budget the thresholds were fitted on
      max_links: 20
      include_screenshots: true
    questions:
      version: replay-relevance-2      # a label; the content digest is what counts
      items: {targets_equivalent: ..., information_equivalent: ..., evidence_sufficient: ...}
    policy:
      version: replay-triage-2         # fixes the gates below
      information_question: information_equivalent
      thresholds:
        accept_information_min: 0.45
        investigate_information_max: 0.25
        fit: {dataset, dataset_sha256, fitted_on, policy_version, questions_sha256, evidence, evidence_sha256}
      overrides: []                    # required, may be empty
```

- **All three blocks are required**; the code has no defaults and keeps no older version.
  Every job snapshot records each block's text and content digest, and every assessment
  records the question-set version and digest, the whole policy and its digest, the evidence
  options and their digest, the thresholds it used and the machine facts, so
  `route_result(result, settings, facts)` re-routes offline.
- **Questions.** Three independent native Noul questions. Only `information_question` routes;
  the other two are recorded. There is no overall Choice, confidence or verdict.
- **Routes.** `investigate_difference` when the replay left an in-viewport image unserved or
  the information value is at or below `investigate_information_max` (the image gate holds
  even when the model call failed); `candidate_acceptable` when it is at or above
  `accept_information_min`, the restoration check passed and no such image was measured;
  `further_review` otherwise. `reasons` names the deciding conditions.
  `candidate_acceptable` only orders human review; it never skips it.
- **Thresholds are fitted.** Each threshold set carries its `fit` provenance; composition
  refuses a set whose `questions_sha256` or `evidence_sha256` differs from the configured
  blocks' digests — re-fit, or write a new fit entry. An override is
  `{scope: site, domain: <registrable domain>, ...}` (the domain and its subdomains; the
  longest matching domain wins) or `{scope: task, benchmark, subset, task_id, ...}`, with the
  same threshold fields; a task match wins over a site match, both over the default.

See `docs/data_flywheel_runbook.md` for the paired-input JSON, command, route roots, the
missing-image fact's two sources and result interpretation. All shipped run YAML explicitly
sets `max_actions_per_step: 4`, including record/replay/branch/student.
