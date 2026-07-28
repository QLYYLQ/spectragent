# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 0. Read this first — this repo is mid-refactor

This project is a **large-scale refactor in progress**. Its current shape is transitional; do not treat it as a finished design.

1. **Source of truth is `docs/architecture_design.md`, not the code.** All code under `spectragent/` is disposable — deletable, renameable, rewritable. When code disagrees with `architecture_design.md`, the code is wrong and the task is to change it. Never reshape a new design to accommodate existing files, class names, or import paths.

2. **`architecture_design.md` itself evolves.** Re-read the relevant section at the start of every non-trivial task (use `Grep`/`Read` with offset/limit — the file is >1700 lines). Do not rely on memories of prior sessions. If the doc is ambiguous or stale, flag it and ask the user before coding — **do not** invent a compromise.

3. **Supplementary design docs (read on demand):**
   - `docs/execution_dag.md` — M01–M26 module breakdown with priorities (P0–P3), dependencies, and status. This is the authoritative implementation order.
   - `docs/design_discussion.md` — rationales and rejected alternatives for key decisions (Agent-Loop split, Observation fragments, Action wiring, Environment composition).
   - `docs/a2a_protocol_design.md`, `docs/self_evolution_design.md`, `docs/benchmark_design_fixes.md` — orthogonal subsystems; skim only when the task touches them.
   - `docs/flat_capability_architecture.svg` — Capability layer visual.

4. **Do not preserve the legacy code path.** Specifically: no `# deprecated` re-exports, no parallel old/new class pairs, no "bridge" adapters that keep an obsolete shape alive, no migration shims. If `architecture_design.md` calls for a new registry or protocol, delete the old one outright. Backwards-compat concerns do not apply — there are no external consumers.

## 1. Commands

Package management is `uv`. A `.venv/` exists at repo root; `pyproject.toml` declares deps.

```bash
# Install / sync deps (including dev tools)
uv sync --extra dev

# Run full test suite (pytest-asyncio is in auto mode)
uv run pytest

# Run a single test file / node
uv run pytest tests/test_core/test_loops/test_validation.py -xvs
uv run pytest tests/test_core/test_runtime/test_agent_runtime.py::test_prepare_weaves_hooks -xvs

# Lint / format
uv run ruff check .
uv run ruff format .

# Type-check (pyright, basic mode, configured in pyrightconfig.json)
uv run pyright spectragent tests
```

`asyncio_mode = "auto"` means async test functions do not need `@pytest.mark.asyncio`.

## 2. Architecture — the big picture

The framework is an **agent evaluation platform** targeting researchers. Design name: "All4One" (package rename `spectragent` → `all4one` is P3, not yet done). Target scope goes well beyond browser agents: SWE agents (SWE-bench), tool-use (τ-bench, ToolBench), multi-tool reasoning (GAIA), desktop (OSWorld), mobile, GPU profiling (KernelBench).

### 2.1 Seven non-negotiable principles

| Principle | Concrete consequence |
|---|---|
| **Protocol-First** | All public contracts are `typing.Protocol` (structural). Consumers never import concrete classes. |
| **Config-Driven** | YAML + `conscribe` auto-register → typed discriminated-union configs in `spectragent/generated/`. No manual wiring. |
| **Strategy-Pluggable** | Every varying axis is its own registrar; swap via registry key in YAML. |
| **Zero Global State** | No singletons, no module-level mutables. Every run constructs fresh instances. |
| **Composition over Inheritance** | Dependencies injected through constructor / `__wiring__`; profiles compose stacks. |
| **Record Once, Evaluate Many** | Run-time WAL-style trace is a first-class citizen (`spectragent/core/trace/`). Evaluators read `RunTrace`, not in-memory state — evaluation is fully decoupled from running. |
| **AOP Cross-cutting** | Tracing, metrics, monitoring are woven into agent/env/llm method calls by `core/aop/`. Loop code never sees hooks, never calls `emit()`. |

If a proposed change violates one of these, stop and re-examine — do not silently ship the compromise.

### 2.2 Registrar map (target state)

All registrars use `conscribe.create_registrar(...)`. The ones marked ✅ already exist at least as stubs; the rest still need to be built or migrated.

| Registry | Discriminator | Where | Status vs target |
|---|---|---|---|
| `AgentLoopRegistrar` | `loop_id` (flat) | `core/loops/_registrar.py` | ✅ skeleton |
| `AgentRegistrar` | `agent_id` (flat) | `agents/_registrar.py` | ✅ but being reshaped for Loop separation |
| `LLMRegistrar` | `model_type.provider` (nested) | `core/llm/_registrar.py` | ✅ stable |
| `EnvironmentRegistrar` | `env_type` (flat) | `core/environments/_registrar.py` | ✅ skeleton + prefix routing |
| `CapabilityRegistrar` | `capability_id` (flat) | `core/capabilities/_registrar.py` | ✅ browser; docker/tool_sandbox/desktop_vm/android/gpu_profiling pending |
| `ActionRegistrar` | `action_id` (flat) | `core/actions/_registrar.py` | ✅ + negotiation |
| `ObservationRegistrar` | `fragment_id` (flat) | `schemas/observation/` | ✅ with `__source__` |
| `HookRegistrar` | `hook_id` (flat) | `core/hooks/_registrar.py` | ✅ + AOP weaver |
| `MemoryRegistrar` | `memory_id` (flat) | `agents/memory/_registrar.py` | ✅ stable |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | `evaluator/` | 🚧 partial |
| `TaskLoaderRegistrar` / `EvalLoaderRegistrar` | `benchmark_name` (flat) | `core/data_loader/` | ✅; needs generalisation past browser |
| `DOMCleanerRegistrar` / `DOMSerializerRegistrar` | flat | `core/capabilities/browser/dom/` | browser-internal |

Generated configs land in `spectragent/generated/*.py` — **do not edit these by hand**; they are derived from `__wiring__` + `Annotated[...]` fields at build time.

### 2.3 Agent-Loop separation (the most load-bearing idea)

Two independent registries connected by `__wiring__` plus Protocols:

- **Agent** = domain knowledge (prompt building, output parsing). One `Agent` class supports many Loops by implementing the matching `*Compatible` Protocol (`ReactCompatible`, `CodeActCompatible`, `FuncCallCompatible`, `PlanExecCompatible`). Agents are **passive** — they do not hold a Loop reference.
- **Loop** = execution strategy (drives `observe → think → act`). Each Loop defines its required agent method slots (`__required_slots__` / `__optional_slots__`) and patches them onto `agent.run` via `weave()`.
- **`__wiring__`** on the Agent class lists allowed loops, capabilities, and observation fragments (required + optional). `conscribe` turns these into `Literal[...]` constraints at config-generation time, so illegal combinations fail in Pydantic validation, never at runtime.

`AgentRuntime` (`core/runtime.py`) is the loop-agnostic orchestrator: it validates compatibility, runs three-party observation negotiation (Capability provides / Agent requires / Loop requires), runs action negotiation, builds `guarded_act`, weaves AOP hooks via `core/aop/weaver.py`, weaves the Loop onto the Agent, then drives with timeout + lifecycle callbacks.

### 2.4 Observation fragments + 3-way negotiation

`Observation` is **not** a god-object. Each fragment (DOM, Screenshot, Terminal, ToolResult, ActionResult, …) is its own registered class with `__source__ = "env" | "loop"`. Capabilities declare `provides`, Agents and Loops declare `requires (required, optional)`, and `AgentRuntime` runs Avro-style negotiation before the run. Active fragments assemble into a dynamically generated `NegotiatedObservation` Pydantic model (nested: `obs.dom.element_tree`, `obs.terminal.stdout`).

Within a Capability, fragments are produced by **Resolvers** (the Browser Capability uses Watchdogs as Resolvers — adding a new fragment = adding one class, the capability does not change).

### 2.5 Action system — symmetric to Observation

Actions are individually registered (`ActionRegistrar`). Each is a dataclass with conscribe metaclass; side-effect parameters live on the action instance (e.g. `ScrollAction.default_amount`). A Capability holds a `list[ActionBase]` — populated from YAML or defaulted from `__wiring__["action"]` — and routes by `action_id`. Agents declare `(required, optional)` action keys; negotiation yields an `active_actions` set; `build_guarded_act` returns a closure that rejects anything outside the set. **Result: prompt advertises exactly what the env can execute, and config errors surface at negotiation, not at first LLM call.**

Composite environments add a prefix (`browser.click`, `tool_sandbox.call_tool`); single-capability environments stay un-prefixed.

### 2.6 AOP hooks — no emitter, no opt-in

Hooks declare `__intercepts__ = [Intercept("observe", role="env", advice="after"), …]`. The Weaver builds an `AOPProxy` around agent/env/llm; the Loop calls plain methods and the proxy triggers the right handler. `TraceWriter` (WAL persistence, commits per step at `after_ainvoke`), `MetricsCollector`, `LiveMonitor` are all implemented this way. If a semantic event does not correspond to an existing method, model it as an Agent/Loop Protocol method instead of adding an emitter sidechannel.

Two layers of hook:
- **Lifecycle callbacks** (`on_run_start/end/error`) — `AgentRuntime` calls these directly.
- **Method interception** (`__intercepts__`) — the Weaver does this transparently.

### 2.7 RunTrace persistence (WAL)

Writes per step: content files first (observations, screenshots, messages), then step record, then an atomic `manifest.json` rename — the manifest is the commit point. Crash recovery reads `manifest.json` and ignores orphan files. No SQLite — trace schemas are heterogeneous and evolve frequently; querying is via lazy-loaded accessors on `RunTrace`. See `core/trace/wal.py`.

## 3. Working style in this repo

### 3.1 When design intent is unclear

If `architecture_design.md` does not obviously answer the question you are about to code around, the correct next move is **ask**, not guess. The user explicitly prefers a clarifying question over a compromise design that half-matches old code. Signs you should stop and ask:

- Two docs disagree (e.g. `architecture_design.md` vs `design_discussion.md` vs `execution_dag.md`).
- Existing code implements something that `architecture_design.md` does not mention.
- A file pattern you are about to copy looks load-bearing but you cannot find it referenced in the target design.
- You are tempted to write "for backward compatibility" anywhere.

### 3.2 Prefer deletion over accommodation

When the target design supersedes an existing file: delete the file, remove its imports, update tests. Do not leave a shell class that re-exports the new API under the old name. Do not keep an old test that exercises a shape the new design abandons — rewrite the test against the new shape or delete it.

### 3.3 Tests mirror the refactor

Test layout mirrors `spectragent/` (`tests/test_core/test_loops/`, `tests/test_agents/`, etc.). If you rename or remove a module, update the mirror. Async tests do not need a decorator — `asyncio_mode = auto`.

### 3.4 Generated config files

`spectragent/generated/*.py` is output of `conscribe` config generation driven by `__wiring__` + `Annotated[...]` fields. Do not hand-edit them; change the source class and regenerate. The generator entry points are the `generate_config.py` files scattered under each registrar directory (`core/loops/`, `core/capabilities/`, `core/llm/`, `agents/`, `agents/memory/`, `core/data_loader/`, `core/environments/`, `core/actions/`).

### 3.5 Priority order when choosing what to do next

Follow `docs/execution_dag.md` priorities (P0 foundation → P0 abstractions → P0 framework → P1 persistence/migration/generalize → P1 E2E → P2 Docker/SWE → P3 advanced). Do not start a P2/P3 module if its P0/P1 dependencies in the DAG are not in place — the DAG is dependency-ordered, not wish-list-ordered.

## 4. External dependencies worth knowing

- `conscribe` (v≥0.6.1, in-house) — the registrar + `__wiring__` + config-generation framework. Its three-element `__wiring__` tuple (`(registry, required, optional)`) is the keystone of the whole design. M01 in the DAG = already shipped.
- `agent-cdp` (v≥0.4.0, in-house) — CDP client + Watchdog + Event scope; **only the Browser Capability consumes it**, it is not a framework-level concern.
- `bubus` — event bus used inside `agent-cdp` watchdogs (optional extra).
- `pydantic` v2 — all data contracts.
- Cloud browser "lite" SDKs (`lexmount`, `browser-use-lite`, `hyperbrowser-lite`, `agentbay-lite`, `airtop-lite`, `skyvern-lite`) — session lifecycle + CDP URL only, wrapped behind the Browser Capability's backend abstraction.
