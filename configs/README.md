# Configs

End-to-end YAML samples that drive `ConfigComposer` → `AgentRuntime`.

## Layout

| File | Purpose |
|---|---|
| `browser_use_local.yaml` | BrowserUseAgent + ReactLoop + LocalChrome backend |
| `browser_use_skyvern.yaml` | Same agent/loop, switched to the hosted Skyvern backend |

## Quick start

```bash
# Validate compose pipeline only (no real LLM / Chrome required)
uv run python demo_yaml_compose.py --config configs/browser_use_local.yaml --dry-run

# Run end-to-end (needs OPENAI_API_KEY + a Chrome binary)
uv run python demo_yaml_compose.py --config configs/browser_use_local.yaml \
    --task "navigate to https://example.com and report the page title"
```

`--dry-run` exercises:

1. YAML schema validation against `ConfigComposer.top_level_type`.
2. `ConfigComposer.compose()` — instantiates every layer, setattr-injects all wired receptors (including `capability.backend`, `agent.llm`, `agent.memory`).
3. Surface report (`agent`, `llm`, `capability`, `backend`, `env`).

It does **not** call `agent.run(task)` — useful for CI smoke tests.

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
- Single-instance receptors (`llm`, `agent_loop`, `capability.backend`, `capability.mouse_trajectory`) are **required**; list-shaped ones (`environment.capabilities`, `capability.actions`) are optional and fall back to a sensible default when omitted.

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

## Switching backend

Edit only `environment[0].capabilities[0].backend`:

```yaml
backend:
  provider_id: skyvern_lite       # was: local_chrome
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
