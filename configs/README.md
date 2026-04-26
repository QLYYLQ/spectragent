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

conscribe 1.0 + `inline_wiring=True` lets every wired child be inlined inside its owning entry. The full agent block is self-contained:

```yaml
agent:
  - agent_id: browser_use
    # ...agent fields...
    agent_loop: { loop_id: react }
    llm:
      model_type: openai
      model: gpt-4o
      provider: { name: direct }
    memory: { memory_id: simple, max_messages: 200 }
    capability:
      capability_id: browser
      observation: browser.dom
      action: { action_id: browser.click }
      backend:                    # ← execution-layer config (IoC)
        provider_id: local_chrome
        headless: true
    observation: browser.dom
    action: { action_id: browser.click }

environment:
  - env_type: browser
capability: []                    # explicit (agent owns its capability)
```

Notes:

- `observation` and `action` on the agent / capability are **declarative-only** Pydantic constraints — Composer skips them at instantiation time but Pydantic still requires a value.
- Mode 1 wirings (`agent_loop`, `llm`, `memory`) become full discriminated unions of every registered config — you write the **complete child config** inline, no top-level reference list needed.
- `environment` is declared at top level so `ComposedRun.env` returns a real `BrowserEnvironment` instance.

## Switching backend

Edit only `agent[0].capability.backend`:

```yaml
backend:
  provider_id: skyvern_lite       # was: local_chrome
  api_key: sk-...                 # provider-specific fields
  timeout_minutes: 30
```

Every other section stays identical. Trace output (`./traces/<run_id>/config.yaml`) records which `provider_id` was used so cross-backend runs are easy to diff.

## Regenerating the schema

After registering a new action / capability / provider:

```bash
uv run python spectragent/scripts/generate_all.py
```

This rewrites `spectragent/generated/_composed.py` and per-class `.pyi` stubs.
