# Spectragent

An evaluation and experimentation framework for agent researchers.

[中文](README.zh.md)

Spectragent splits an agent run into parts you can change one at a time: the agent, its model, the execution loop, observations, actions, memory and the environment. One YAML file picks an implementation for each part. A trace hook records the run, and everything after it reads that recording instead of rerunning the agent: inspection, scoring, offline replay, branching from a recorded step, and training export.

Version 0.3.0 · Python 3.11+ · Apache-2.0 · trace format 7 · `AgentResult` schema 5.0

- [How it works](#how-it-works)
- [What is implemented](#what-is-implemented)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Data flywheel](#data-flywheel)
- [Replay review](#replay-review)
- [RunTrace](#runtrace)
- [Inspecting runs](#inspecting-runs)
- [Command line](#command-line)
- [Extending](#extending)
- [Development](#development)
- [Documentation](#documentation)
- [Status and limits](#status-and-limits)

## How it works

```
YAML ─▶ ConfigComposer ─▶ AgentRuntime.prepare() ─▶ execute ─▶ RunTrace
                               ├─ agent × loop slot check
                               ├─ observation / action / archive negotiation
                               └─ setup check (environment, task context)
```

- **One registry per axis.** Agents, loops, models, memory, capabilities, actions, observation fragments, archivers, hooks, browser backends, DOM cleaners and the rest are separate registries built with `conscribe`. An ablation is an ordinary run with one registry key or one field changed.
- **Agents and loops are separate.** An agent holds domain knowledge: prompt building, output parsing, history formatting. A loop drives observe, think, act and declares the agent methods it needs. `prepare()` checks those methods and names any that are missing.
- **Three-way negotiation.** A capability declares the observation fragments, actions and archivers it provides; the agent and the loop declare what they require and what is optional. The prompt advertises exactly the actions the environment can execute, and an impossible combination is refused before a browser starts or a model is called.
- **Generated, typed configuration.** Each class's `__wiring__` and annotated constructor fields produce a discriminated-union schema. Every strategy named in YAML arrives as a constructed instance carrying its own fields.
- **Recording is woven in.** Hooks wrap the public methods of the agent, environment, model and loop; loop code never calls a recorder. `trace_writer` is the one component that writes runs to disk.
- **Write-ahead trace.** Each step commits its content files, then its step record, then atomically replaces the manifest. A recording failure is reported as recording health and never changes the agent's outcome.

## What is implemented

The browser is the only implemented environment. Output of `spectragent components list` on 2026-10-25:

| Axis | Registered |
|---|---|
| Agents | `browser_use.dom`, `browser_use.dom_vision`, `skyvern.dom`, `skyvern.dom_vision`, `ui_tars` |
| Loops | `react`; `branch` (resume a recorded run at a checkpoint) with restorer `prefix_replay` |
| Environment and capability | `browser` |
| Browser backends | `local_chrome`; cloud: `lexmount`, `browser_use`, `hyperbrowser`, `agentbay`, `airtop`, `skyvern` |
| Browser actions (16) | `navigate`, `click`, `type`, `scroll`, `wait`, `send_keys`, `select_dropdown`, `drag`, `screenshot`, `extract_content`, `switch_tab`, `go_back`, `go_forward`, `click_coordinate`, `type_coordinate`, `done` |
| Observation fragments | `browser.dom`, `browser.screenshot`, `browser.browser_meta`, `browser.platform`, `browser.capture_state`; loop side: `loop.task`, `loop.step_info`, `loop.history`, `loop.action_result` |
| DOM pipeline | cleaners `browser_use`, `skyvern`, `skyvern_hit_test`; serializers `text`, `html` |
| Input | mouse `instant`, `windmouse`, `bezier`, `learned` (needs the `learned-mouse` extra); typing `instant`, `human` |
| Chat models | `openai.{direct, azure, deepseek, openrouter, groq, cerebras, vercel}`, `anthropic.{direct, aws}`, `google.default`, `bedrock.default`, `oci.default`, `mistral.default`, `ollama.default`, `browser_use.default` |
| Decision models | `typesafe.direct`, `typesafe.openrouter` (used by replay review) |
| Memory | `simple`, `sliding_window`, `token_budget` |
| Hooks | `trace_writer`, `live_monitor`, `prompt_contribution`, `omit_source_replies`, `ui_navigation_guard` |
| Archivers | `browser.network` (WARC), `browser.storage`, `browser.dom_raw`, `browser.frames`, `browser.replay_log` |
| Replay | replayability `none`, `pinned`; network source `live`, `archive`; miss policy `strict` |
| Task loaders | `online_mind2web`, `browsecomp`, `lexbench_browser`, `trace_branches` (tasks from recorded runs); step selectors `explicit`, `every_k`, `all`, `last` |
| Evaluators | `exact_match`, `online_mind2web`, `browsecomp`, `lexbench_browser` |
| Export | exporter `openai_messages`; translators `identity`, `index_to_coordinate` |
| Replay reviewer | `jev` |

Registered means available to select; a run enables only what its YAML names. Keys are scoped by layer, so `browser_use` can name an agent family, a cloud backend and a DOM cleaner at once.

Browser behaviour worth knowing:

- **Action effects.** Actions measure what they did (where a click landed and what it hit, whether typing changed a field, scroll displacement, URL before and after) and return it in `ActionResult.effect`. Agents show it to the model as one line; `show_action_effects: false` hides the line while the trace still records it.
- **`capture_state`.** An optional fragment with the page-settle result, focus moves the agent did not cause, the last known pointer position, and visible images that failed to load. `show_capture_state: false` on the agent drops it.
- **Browser shortcuts.** `send_keys` runs the browser's own Back, Forward, Reload, Close tab and tab-switch shortcuts for the measured platform (for example `Meta+[` on macOS, `Alt+ArrowLeft` on Linux) instead of sending the keys to the page.
- **Failed actions.** In `react`, the first failed action ends its step. The remaining actions are recorded as skipped, and the next prompt tells the model they were not executed. `type` and `type_coordinate` fail without typing when no text field has focus.

Not implemented: Docker/SWE, tool sandbox, desktop, Android and GPU profiling capabilities. The registrar mechanism is in place, and the `docker.terminal` and `tool_sandbox.tool_result` fragment schemas exist, but no capability provides them.

## Getting started

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), and Chrome or Chromium for the `local_chrome` backend (set `CHROME_EXECUTABLE_PATH` if discovery fails). Cloud backends need the vendor's credentials.

```bash
uv sync --extra dev
export OPENAI_API_KEY=sk-...

# Compose and prepare only: no browser, no model call
uv run spectragent config check configs/browser_use_local.yaml

# One task (this config opens a visible Chrome window)
uv run spectragent run --config configs/browser_use_local.yaml \
  --start-url https://example.com --task "Report the page title"

# Five benchmark tasks, recorded without scoring
uv run spectragent bench --config configs/browser_use_local.yaml \
  --benchmark online_mind2web --subset All --limit 5 --no-eval

# Look at what was recorded
uv run spectragent runs list
uv run spectragent view --open
```

`run`, `eval` and `replay-review` also read `.env` from the working directory; `bench` and `flywheel` read only the shell environment (`set -a; . ./.env; set +a`). Benchmark data is not shipped: set `data_dir:` on a task loader, or point `SPECTRAGENT_BENCHMARK_DATA` at a root laid out as `<env_type>/<benchmark>`.

Shipped entry points: `configs/browser_use_local.yaml`, `configs/skyvern_agent_local.yaml`, `configs/ui_tars_local.yaml`, `configs/browser_use_skyvern.yaml` (Skyvern cloud browser) and `configs/browser_use_shared.yaml` (one shared Chrome, a fresh context per task). Switching backends changes only `environment[0].capabilities[0].backend`. The [browser agent runbook](docs/browser_agent_runbook.md) (Chinese) covers logins, shared Chrome and troubleshooting.

## Configuration

A minimal configuration that passes `config check`:

```yaml
agent:
  - agent_id: browser_use.dom      # or browser_use.dom_vision, skyvern.dom, skyvern.dom_vision, ui_tars
    id: main
    agent_loop: { loop_id: react, max_steps: 30, max_actions_per_step: 4 }
    llm:
      model_type: openai           # the model vendor's own protocol
      model: gpt-4o
      provider: { name: direct }   # add base_url: for a gateway
    memory: { memory_id: simple, max_messages: 200 }

environment:
  - env_type: browser
    capabilities:
      - capability_id: browser
        backend: { provider_id: local_chrome, headless: true }
        mouse_trajectory: { trajectory_type: instant }
        typing_rhythm: { rhythm_type: instant }
        replayability: { replayability_id: none }
        network_source: { source_id: live }
        dom_cleaner: { cleaner_type: browser_use }
        dom_serializer: { serializer_type: text }

hook:
  - hook_id: trace_writer
    trace_dir: traces
```

- YAML selects components. Task text and start URL come from the CLI or a task loader, so one file serves many tasks.
- Single-instance receptors are written out. `replayability` and `network_source` are required in every capability block, even when nothing is recorded; "off" is spelled `none`.
- `actions:` is omitted above, which builds all 16. A list builds, advertises and allows only those actions, and `[]` builds none. An agent that requires a dropped action fails negotiation before the run.
- Keys in YAML and in the trace are fully qualified (`browser.click`). Agents declare relative names (`click`), and negotiation binds each to the one capability that offers it.
- After changing a `__wiring__` entry or an `Annotated[...]` constructor field, run `uv run spectragent config generate`. `spectragent/generated/` is build output.

`trace_writer` records metrics, errors, trajectory and observations by default. `recording` narrows that, and `archive` is the one switch for environment evidence (empty by default):

```yaml
hook:
  - hook_id: trace_writer
    trace_dir: traces
    recording: { metrics: true, errors: true, trajectory: false, observations: false }
    archive:
      - archiver_id: browser.frames     # screenshots before each action and after each observation; the agent never sees them
      - archiver_id: browser.storage    # cookies and Web Storage
```

`browser.network` additionally needs `replayability: pinned` (or `require_replayable: false`), and every archiver needs `local_chrome`; `prepare()` refuses the other combinations.

```bash
uv run spectragent config explain configs/browser_use_local.yaml   # components, provenance, injection, negotiation, redacted config
uv run spectragent config template --model gpt-4o                   # fully expanded, commented YAML on stdout
uv run spectragent components list --layer agent
```

`config check` and `run --dry-run` both compose and run `prepare()`, then stop before environment setup. `bench` runs the same check on its first task and exits with code 2 if it is refused.

## Data flywheel

Record a browser run with its environment, replay it offline, branch it at a step, export training data, and measure the chain. Only `local_chrome` can record or replay.

| Stage | Entry | Shipped config |
|---|---|---|
| Record | `run` or `bench` | `configs/flywheel/record_ui_tars_pinned.yaml`: `pinned` fixes seed, clock, time zone, locale, DPR and colour scheme; archives network (WARC), storage and raw DOM |
| Replay | `run --replay-archive DIR` | `configs/flywheel/replay_ui_tars_archive.yaml`: every request is answered from the archive; misses follow `strict` (main-frame document fails, beacons 204, other subresources 404) |
| Branch | `bench` with `trace_branches` | `configs/flywheel/branch_ui_tars_archive.yaml`: restore to before step *t*, verify each restored step, let the teacher decide again |
| Same-path replay | `bench` with `trace_branches` | `configs/flywheel/same_path_ui_tars_archive.yaml`: restore each recording to its last step, twice, to measure fidelity and determinism |
| Export | `export` | `configs/flywheel/export_sft.yaml` with student `configs/flywheel/ui_tars_student.yaml`: `openai_messages` rendered in the student's own format |
| Measure | `flywheel run`, `flywheel report` | a chain spec you write; produces `report.json` and `report.md` |
| Diagnose | `flywheel diagnose` | aligns recorded and replayed screenshots and marks changed grid cells, for human review |

```bash
# Rehearse the whole chain on a local fixture site with scripted models (Chrome, no API key, about 40 s)
uv run python -m tests._helpers.flywheel_rehearsal --out /tmp/spectragent-rehearsal

# Record into traces/recorded (copy the config and set trace_dir), then replay one run
uv run spectragent run --config my/record.yaml --start-url https://example.com --task "..."
uv run spectragent run --config configs/flywheel/replay_ui_tars_archive.yaml \
  --task "<task from the source config.json>" --start-url <its start_url> \
  --replay-archive traces/recorded/<run>

# Branch every recorded run, export, then run a measured chain
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --list
uv run spectragent bench --config configs/flywheel/branch_ui_tars_archive.yaml --no-eval
uv run spectragent export --config configs/flywheel/export_sft.yaml
uv run spectragent flywheel run --spec chain.yaml
```

A replay requires `browser.replay_log` in place of `browser.network` in `archive`. Per-site replay rules in `configs/replay_rules/` may only narrow matching; each replay saves the rule text and digest it used ([rule syntax](configs/replay_rules/README.md)). Branching can also swap in a different task at the checkpoint (`task_variants` on `trace_branches`). The export stage is frozen at its first version, and the student side is provisional. The [data flywheel runbook](docs/data_flywheel_runbook.md) (Chinese) and [configs/README.md](configs/README.md#recording-environment-evidence) cover chain specs, fidelity metrics and privacy.

## Replay review

`spectragent replay-review` compares recorded and replayed steps after the fact with the `jev` reviewer, which asks a decision model three separate questions (same targets, same information, enough evidence) and routes each pair to `candidate_acceptable`, `further_review` or `investigate_difference`. It only orders human review; it never changes restoration verdicts.

```bash
uv run spectragent replay-review --config configs/replay_review/jev_direct.yaml \
  --pairs pairs.json --out reviews/job-1 --dry-run
uv run spectragent view --root reviews
```

Configs for the official API, OpenRouter and a gateway are in `configs/replay_review/`. The pairs format is in [configs/README.md](configs/README.md#independent-replay-review).

## RunTrace

One directory per run, committed step by step:

```
traces/<run_id>/
  manifest.json                    # commit point: status writing | complete | incomplete, complete_through
  config.json                      # effective configuration (redacted), source YAML path and hash
  result.json                      # agent outcome (agent_done), usage, execution_ms
  steps/000.json                   # StepRecord: origin policy | restored | terminal, actions, results, frame
  observations/000_browser.dom.json
  observations/000_browser.screenshot.png
  messages/000_call_00.json        # one file per model call; images/ and output_formats/ stored once per run
  env/                             # environment evidence, only when archive is set
  evals/                           # post-run verdicts, outside the manifest
```

A trace is complete within its declared recording scope; an omitted channel is not corrupt evidence. Token usage is stored as the provider reported it, and unreported usage stays unknown. No prices are computed. Readers refuse any trace format other than 7.

Read it in Python through `spectragent.core.trace.RunTrace`, which only serves committed files and checks their sizes and hashes.

## Inspecting runs

```bash
uv run spectragent runs list --search mind2web
uv run spectragent runs show <run> --step 0
uv run spectragent runs compare <run-a> <run-b>
uv run spectragent runs compare-groups --left <hash> --right <hash> --evaluator <slug>
uv run spectragent tui                                   # terminal browser for existing runs
uv run spectragent run --config ... --task ... --tui     # live monitor (also on bench)
uv run spectragent view --open                           # local web viewer on 127.0.0.1:8765
uv run spectragent view --annotations labels/ --annotator alice
```

All of these share one read side and never start an agent or a model. The viewer shows steps, screenshots with action marks, every fragment and model call, restoration evidence, branch sources and reports. It is read-only unless `--annotations DIR` is given; then it appends labels for review queues (`DIR/queues/*.json`, written by your tools) and ad hoc step labels to `DIR/labels/`, and never writes a trace. Annotation requires a loopback `--host`. `--config` and `--config-hash` filter runs by source YAML or effective configuration.

## Command line

| Command | Purpose |
|---|---|
| `run` | One task; `--dry-run`, `--replay-archive`, `--tui` |
| `bench` | Benchmark or branch tasks; `--list`, `--task-id`, `--limit`, `--no-eval`, `--token-limit`, `--tui` |
| `eval` | Score saved traces with one evaluator and eval loader |
| `export` | Training data from saved traces |
| `flywheel` | `run`, `report`, `diagnose` |
| `replay-review` | Independent review of replay pairs |
| `runs` | `list`, `show`, `compare`, `compare-groups` |
| `tui`, `view` | Terminal and web interfaces |
| `config` | `check`, `explain`, `template`, `generate` |
| `components` | `list` registered implementations |

The global `--json` writes one JSON result to stdout and keeps logs on stderr. Every command has `--help`.

## Extending

Each axis has one registration point and a written procedure under `.agents/skills/`, usable by people and code agents:

| Task | Skill |
|---|---|
| Capability or backend | `af-add-a-capability` |
| Loop | `af-add-a-loop` |
| Action | `af-add-an-action` |
| Observation fragment | `af-add-an-observation-fragment` |
| Configurable receptor | `af-add-a-receptor` |
| Model provider or gateway route | `af-add-an-llm-provider` |
| Regenerate and verify configuration | `af-regenerate-and-verify` |
| Flywheel stages and strategies | `af-data-flywheel` |
| Per-site replay rules | `af-improve-replay-rules` |
| Hooks and trace semantics | `af-trace-and-hooks` |

A new registrar must also be listed in [AGENTS.md](AGENTS.md) §2.2 and `docs/architecture_design.md` §6; `tests/test_docs/test_registrar_coverage.py` fails otherwise.

## Development

```bash
uv sync --extra dev
uv run pytest -m "not requires_chrome"     # skip the tests that launch a real Chrome
uv run pytest                              # everything, on a quiet machine
uv run ruff check . && uv run ruff format .
uv run pyright spectragent tests
```

- `docs/architecture_design.md` is the design authority. When code disagrees with it, the code changes.
- Tests mirror `spectragent/`. Async tests need no decorator (`asyncio_mode = "auto"`).
- `tests/test_generated/test_in_sync_with_registries.py` checks the generated configuration against the live registries.
- Superseded code is deleted outright: no compatibility adapters or migration shims.

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture_design.md](docs/architecture_design.md) | The design (Chinese): abstractions, negotiation, runtime, traces, registries |
| [docs/execution_dag.md](docs/execution_dag.md) | Module breakdown, dependencies and order (Chinese) |
| [docs/browser_agent_runbook.md](docs/browser_agent_runbook.md) | Running browser agents (Chinese) |
| [docs/data_flywheel_runbook.md](docs/data_flywheel_runbook.md) | Record, replay, branch, export, measure, replay review (Chinese) |
| [configs/README.md](configs/README.md) | Configuration layout and field semantics |
| [configs/replay_rules/README.md](configs/replay_rules/README.md) | Replay rule syntax |
| [AGENTS.md](AGENTS.md) | Working rules for code agents in this repository |

## Status and limits

- The browser is the only environment. Integration was accepted on sampled end-to-end runs; no full agent-by-benchmark matrix was run. Workflows with several real agents each driving multiple tabs have not been verified.
- Evaluators run as a separate stage (`spectragent eval`) that reads committed traces and keeps task correctness apart from replay fidelity. No flywheel stage needs a verdict.
- A complete recording does not mean the task succeeded. Trace completeness, restoration checks and matching screenshots are separate evidence; none of them proves a task or a login succeeded.
- Cloud backends cannot record or replay. WebSocket replay is not supported.
- `pinned` fixes the page colour scheme, but the host's own light or dark appearance is not recorded, and headless Chrome's native scrollbar seems to follow it. Keep it fixed for production runs and compare restoration results only between runs made under the same appearance.
- Version 0.3.x is still changing, and this README makes no API stability promise.

## License

Apache-2.0. Third-party code and dependencies are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
