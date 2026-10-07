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
- Single-instance receptors (`llm`, `agent_loop`, `capability.backend`, `capability.mouse_trajectory`, `capability.typing_rhythm`, `capability.dom_cleaner`, `capability.dom_serializer`) are **required**; list-shaped ones (`environment.capabilities`, `capability.actions`) are optional and fall back to a sensible default when omitted.
- A strategy is always a receptor block, never a name on `profile:`. `profile:` says which watchdogs run and which CDP round trips they may spend — nothing else. Naming a strategy there is what made `mouse_trajectory`, then `dom_cleaner`, then `dom_serializer` arrive default-constructed with every configured knob discarded.
- `browser.dom` has a cost surface of its own, off by default: `dom_max_elements`, `dom_max_serialized_chars`, `dom_interactive_only`, `dom_viewport_only`. When any of them cuts, the observation fragment records the cut in `budget`, so a trace never shows a truncated page as a sparse one.
- The two input-humanization receptors are independent axes and each is its own off-switch: `mouse_trajectory: { trajectory_type: instant }` and `typing_rhythm: { rhythm_type: instant }` are the legacy instant jump and the legacy single bulk `Input.insertText`. There is no boolean beside either — the strategy *is* the switch.

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
