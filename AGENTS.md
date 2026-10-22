# AGENTS.md

This file provides guidance to OpenAI Codex when working with code in this repository.

## 0. Read this first — this repo is mid-refactor

This project is a **large-scale refactor in progress**. Its current shape is transitional; do not treat it as a finished design.

1. **Source of truth is `docs/architecture_design.md`, not the code.** All code under `spectragent/` is disposable — deletable, renameable, rewritable. When code disagrees with `architecture_design.md`, the code is wrong and the task is to change it. Never reshape a new design to accommodate existing files, class names, or import paths.

2. **`architecture_design.md` itself evolves.** Re-read the relevant section at the start of every non-trivial task (locate it with `rg`, then use a bounded read — the file is >1700 lines). Do not rely on memories of prior sessions. If the doc is ambiguous or stale, flag it and ask the user before coding — **do not** invent a compromise.

3. **Versioned docs and local references.** The four docs kept in Git are `docs/architecture_design.md`, `docs/execution_dag.md`, `docs/browser_agent_runbook.md`, and `docs/data_flywheel_runbook.md`. The other supplementary docs and continuation records below are local-only; they may be absent in a fresh clone. Read them on demand when present, and use the versioned design, code, and tests when they are absent.
   - `docs/execution_dag.md` — M01–M33 module breakdown with priorities (P0–P3), dependencies, and status. This is the authoritative implementation order; revalidate status against current code and tests.
   - `docs/data_flywheel_runbook.md` — operator guide for the data flywheel: recording browser runs with their environment archive, offline replay, branching, SFT export and the metrics report, driven from the shipped `configs/flywheel/` YAML and the CLIs.
   - `docs/design_discussion.md` — rationales and rejected alternatives for key decisions (Agent-Loop split, Observation fragments, Action wiring, Environment composition).
   - `docs/a2a_protocol_design.md`, `docs/self_evolution_design.md`, `docs/benchmark_design_fixes.md` — orthogonal subsystems; skim only when the task touches them.
   - `docs/flat_capability_architecture.svg` — Capability layer visual.
   - `docs/llm/dmxapi.cn.md` — measured routing rules for the dmxapi.cn gateway (which `model_type` + `base_url` per model family) and the gateway failure modes behind them. Read it **before** debugging structured output against any gateway: the headline is that HTTP 200 does not mean the schema was honoured, and that DMX round-robins upstream channels, which makes its Claude routes unfit for reproducible evaluation.

   **Current handoff and historical continuation records (not design authority):**
   - `tmp/framework_expansion/delivery.json` — current handoff for the five framework extensions plus cursor presentation accepted at `626612a` on 2026-10-16. `review/final.json`, `review/cursor.json` and `final/626612a/summary.json` preserve review scope and frozen acceptance: 8,127 passed, zero failures/errors/skips, 22 existing warnings and zero type errors/warnings. Published Conscribe 2.1.0 is adopted. Cursor-on real-site acceptance is tracked separately in `live/acceptance.json`; complete recording does not imply task/login success. If local artifacts are absent, use `.agents/skills/af-project-history/references/unfinished-work.md` and current versioned design/code/tests.
   - `tmp/flywheel/followup/implementation/README.md` — prior six-package handoff accepted on 2026-10-15 at `a6a36d0`; its `delivery.json` and `final/summary.json` retain 7,882 passing tests and zero type errors. The old `review_changes/README.md` is the completed requirements index, not an active backlog.
   - `HANDOFF_260918_port_residue.md` + `HANDOFF_260918_skyvern_cleaner.md` — preserved historical evidence with mixed status, not the default current handoff. The broad residue report requires item-by-item revalidation. SkyvernCleaner's JS capture path is implemented; read the cleaner handoff §9 for the 2026-10-01 implementation and `tests/test_core/test_capabilities/test_browser_skyvern_live.py` for real Chrome verification. Its earlier sections and `docs/ref_gap_analysis.md` C1 preserve historical findings and the delete-vs-complete decision. User ruling 2026-10-19: M18 is complete on sampled end-to-end evidence; a full Agent × benchmark matrix is no longer an acceptance gate. Separate browser experiments retain their own evidence requirements (see the M18 entry in `docs/execution_dag.md`).
   - `docs/jev_system_one_model.md` — user-owned, unfinished Jev/System One research and decision memo. Preserve it. Every proposed integration remains pending; read it when work touches decision models, cascade loops, dense probes, evaluator triage, or non-agent LLM attribution. It is not permission to implement a proposal or a substitute for `architecture_design.md`.
   - `HANDOFF_260916_findings.md` + `HANDOFF_260916_next.md` — the anti-bot/input-research evidence pair. Preserve as dated evidence; many backlog items were later completed, rejected, or superseded.

   All root `HANDOFF_*.md` files are dated evidence, not current task lists. Follow their explicit supersession notices and revalidate every apparently open item against current design, code, tests, and later commits.

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
| `AgentLoopRegistrar` | `loop_id` (flat) | `core/loops/_registrar.py` | ✅ `react`; `branch` (`BranchLoop`, M31: resumes a recorded run at a checkpoint, §4.1 of the design doc) |
| `AgentRegistrar` | `agent_id` (flat) | `agents/_registrar.py` | ✅ but being reshaped for Loop separation |
| `LLMRegistrar` | `model_type.provider` (nested) | `core/llm/_registrar.py` | ✅ stable |
| `DecisionModelRegistrar` | `model_type.provider` (nested) | `core/llm/decision_registrar.py` | Decision-only `decision_model` axis; `typesafe.direct` / `typesafe.openrouter`, shared model identity and lifecycle with Chat (§4.7) |
| `ReplayReviewerRegistrar` | `reviewer_id` (flat) | `replay_review/_registrar.py` | Independent `replay_reviewer` jobs; `jev` owns a Decision receptor and separate results (§4.18) |
| `EnvironmentRegistrar` | `env_type` (flat) | `core/environments/_registrar.py` | ✅ skeleton + prefix routing |
| `CapabilityRegistrar` | `capability_id` (flat) | `core/capabilities/_registrar.py` | ✅ browser; docker/tool_sandbox/desktop_vm/android/gpu_profiling pending |
| `ActionRegistrar` | `action_id` (flat) | `core/actions/_registrar.py` | ✅ + negotiation |
| `ObservationRegistrar` | `fragment_id` (flat) | `schemas/observation/` | ✅ with `__source__` |
| `ArchiverRegistrar` | `archiver_id` (flat) | `core/archive/_registrar.py` | ✅ framework axis (M28); browser archivers `browser.network` / `browser.storage` / `browser.dom_raw` (M29, `core/capabilities/browser/archive/`), `browser.frames` (label-only screenshots before each action and after each observation, on its own `action_boundary` channel) and `browser.replay_log` (a replay's serving decisions; a replay binds it in `browser.network`'s place, which `network_source: archive` refuses), collected in `collect_registrars()`, selected in YAML only through `trace_writer.archive` (the one archiving switch: omitted, `null` or `[]` records nothing, and the Composer refuses a top-level `archiver:` section) |
| `ReplayabilityRegistrar` | `replayability_id` (flat) | `core/capabilities/browser/replay/` | browser-internal; `none` (default) / `pinned`; a required single-instance receptor, so every hand-written capability block writes `replayability: {replayability_id: none}` ("not enabled" is spelled `none`, never a word YAML 1.1 reads as a boolean — `tests/test_core/test_composer/test_yaml_safe_values.py`) |
| `NetworkSourceRegistrar` | `source_id` (flat) | `core/capabilities/browser/replay/network_source.py` | ✅ browser-internal (M30); `live` (default) / `archive` (offline replay of a recorded run, §4.14 of the design doc); a required single-instance receptor, so every hand-written capability block writes `network_source: {source_id: live}`; isolation is derived from the source, never a provider boolean |
| `MissPolicyRegistrar` | `miss_policy_id` (flat) | `core/capabilities/browser/replay/miss_policy.py` | ✅ browser-internal (M30); `strict` only; a receptor of `ArchiveNetworkSource` (nested in `network_source`), required in every `archive` block |
| `RestorerRegistrar` | `restorer_id` (flat) | `core/loops/restore/_registrar.py` | M31; `prefix_replay` (re-executes the recorded prefix with retargeting and per-step verification); a receptor of `BranchLoop`, collected in `collect_registrars()` |
| `HookRegistrar` | `hook_id` (flat) | `core/hooks/_registrar.py` | ✅ + AOP weaver |
| `MemoryRegistrar` | `memory_id` (flat) | `agents/memory/_registrar.py` | ✅ stable |
| `EvaluatorRegistrar` | `evaluator_id` (flat) | `evaluator/` | 🚧 partial |
| `ExporterRegistrar` | `exporter_id` (flat) | `export/_registrar.py` | M32; `openai_messages`; `ExporterBase` holds the training-export job fields and the `translator` receptor; an optional top-level layer of the one run root (the `evaluator` precedent), collected in `collect_registrars()` |
| `ActionTranslatorRegistrar` | `translator_id` (flat) | `export/translate/_registrar.py` | M32; `identity` / `index_to_coordinate` (element index → CSS-px point, with occlusion evidence); the exporter's `translator` receptor, collected in `collect_registrars()` |
| `TaskLoaderRegistrar` / `EvalLoaderRegistrar` | `benchmark_name` (flat) | `core/data_loader/` | ✅; needs generalisation past browser |
| `StepSelectorRegistrar` | `selector_id` (flat) | `core/data_loader/task/step_selector/` | M31; `explicit` / `every_k` / `all`, plus `last` (M33: each source run's last branchable step — the metrics chain's same-path replay); a receptor of the `trace_branches` task loader, collected in `collect_registrars()` |
| `NegotiationResultRegistrar` | `result_type` (flat) | `core/negotiation/_registrar.py` | ✅; built-in `observation` / `action` / `archive` results |
| `BrowserProviderRegistrar` | `provider_id` (flat) | `core/capabilities/browser/backends/` | ✅ local_chrome + 6 cloud lite SDKs |
| `DOMCleanerRegistrar` / `DOMSerializerRegistrar` | `cleaner_type` / `serializer_type` (flat) | `core/capabilities/browser/dom/` | browser-internal; cleaners `browser_use` / `skyvern` / `skyvern_hit_test` (M32: Skyvern plus an in-page hit test per indexed element) |
| `MouseTrajectoryRegistrar` | `trajectory_type` (flat) | `core/capabilities/browser/input/trajectory.py` | browser-internal; `instant` / `windmouse` / `bezier` / `learned` |
| `TypingRhythmRegistrar` | `rhythm_type` (flat) | `core/capabilities/browser/input/rhythm.py` | browser-internal; `instant` / `human` |

**This table is enforced, not decorative.** `tests/test_docs/test_registrar_coverage.py` AST-scans `spectragent/` for every `create_registrar(...)` and fails if one is missing here or from `docs/architecture_design.md` §6, or if the discriminator named in either table disagrees with the code. A registrar is this framework's top-level unit of extension; an undocumented one is deletable-on-sight under §0.1, which is exactly how the input humanization layer (§4.13 of the design doc) nearly got lost. When you add a registrar, add the row in the same change.

Generated configs land in `spectragent/generated/*.py` — **do not edit these by hand**; they are derived from `__wiring__` + `Annotated[...]` fields at build time.

### 2.3 Agent-Loop separation (the most load-bearing idea)

Two independent registries connected by `__wiring__` plus slot validation:

- **Agent** = domain knowledge (prompt building, output parsing). One `Agent` class supports many Loops by **providing the method slots** each Loop declares. Agents are **passive** — they do not hold a Loop reference. Compatibility is checked by slot validation (`callable(getattr(agent, slot))` in `weave()`), failing at `prepare()` with `IncompatibleError(missing_slots=[...])` — **not** by `*Compatible` Protocols. `architecture_design.md` §4.1 rejects the Protocol approach outright (a Loop subclass that adds one method would need a whole new Protocol → N×M explosion), and `rg "class .*Compatible" spectragent/` returns zero.
- **Loop** = execution strategy (drives `observe → think → act`). Each Loop defines its required agent method slots (`__required_slots__` / `__optional_slots__`) and patches them onto `agent.run` via `weave()`. Whoever calls an optional slot validates it, right before the call. The runtime checks `prepare`; the training exporter (M32) checks the two ReAct slots only it uses, `format_react_output` (the student side, the inverse of `parse_react_output`) and `parse_react_thought` (the teacher side), and raises the same `IncompatibleError`, worded "needed for training export". Its third slot, `format_action_fact` (how the student writes an action it shows as a fact, used for `keep_as_text` turns), has a default — the action's own JSON — so it is not checked. `prepare()` never checks an optional slot.
- **`__wiring__`** on the Agent class lists configurable Loop choices and observation/action fragment demands (required + optional). `conscribe` uses it to constrain registry keys and nested configs during configuration validation. Whether the selected implementations can actually cooperate is checked by `AgentRuntime.prepare()` and reported with an error naming the unmet contract.
- **Loop inputs** (M31) are the one per-task channel into a Loop: a Loop declares them in `__loop_inputs__` (`LoopInput(type, required)`), a task loader supplies them as `TaskContext.loop`, and `prepare()` refuses unknown, missing or mistyped keys with `IncompatibleLoopInputError`. They reach `_execute` as `ctx["loop_inputs"]`; nothing is stored on the Loop (there is no `loop.ctx`). `TaskContext{env, loop, branch}` keeps the three consumers apart: `env` is routed to capability `setup()`, `loop` to the Loop, `branch` (provenance) only to the trace.
- **What setup would refuse, `prepare()` refuses.** A combination decided by configuration and task data is checked before the trace is opened: `prepare(env_context=...)` calls `Environment.check_setup` (each capability implementing the optional `SetupCheck` gets its routed context, the negotiation and the archivers about to be bound; a context key no `setup()` declares is refused), a Loop declares `__requires_env__` (refused by name without an environment) and `__loop_fragments__` (the loop-source fragments negotiation may offer on its side), and `spectragent bench` runs `prepare()` once for the first task before the sweep (exit 2 when refused). Setup may re-check by calling the same function.

`AgentRuntime` (`core/runtime.py`) is the loop-agnostic orchestrator: it validates compatibility, runs three-party observation negotiation (Capability provides / Agent requires / Loop requires), runs action negotiation, builds `guarded_act`, weaves AOP hooks via `core/aop/weaver.py`, weaves the Loop onto the Agent, then drives with timeout + lifecycle callbacks.

Runtime resolves `RunIdentity` once, passes the Loop's typed `ExecutionLimits` through the Agent's existing `prepare` entry point, and checks explicit consumption declarations before setup. ReAct prompt builders are asynchronous and accept a keyword-only `ReactPromptContext`; the Agent owns prompt text, placement and history formatting. Public Loop step operations and `StepSink` are the interception boundaries. Generic result/step handoff belongs to the base Loop; parse retries, action truncation and round memory belong to ReAct. Shared trace reconstruction lives in `core/trace/reconstruction.py`, never under a concrete Loop's private helpers. Loop state contains execution facts, not token accounting.

`execution_ms` has one Runtime clock, from immediately before environment setup through the end of execution, before ending lifecycle callbacks. It includes restoration and terminal observation; the timeout budget still covers only `agent.run`. `TaskOutcome.elapsed_ms` includes the full task, cleanup and result read-back. Providers lazily reuse clients within one execution. Runtime closes the main model and explicitly declared helper models in its final cleanup; independent evaluation owns its model cleanup. Borrowed transports remain borrowed, cleanup is bounded and diagnostic, and it never replaces a reply, original error or cancellation. Provider authors use the public `client_resources` extension surface; callers use the model lifecycle Protocol.

Branching remains ReAct-specific. `history_source: source | restored` is independent of environment verification's `strict | warn`: source history retains exact replay checks, while restored history uses actual observations, retargeted actions and returned results with explicit source references. Both consume the same live checkpoint observation. Prompt contributions use a configured Hook/AOP around the Agent's prompt method, stable Python function references and optional wired helper models. They are generated once per selected policy step, reused for parse retries, and read from recorded evidence during history reconstruction. Helper calls carry their own caller/purpose and never become Agent thoughts or policy decisions.


### 2.4 Observation fragments + 3-way negotiation

`Observation` is **not** a god-object. Each fragment (DOM, Screenshot, Terminal, ToolResult, ActionResult, …) is its own registered class with `__source__ = "env" | "loop"`. Capabilities declare `provides`, Agents and Loops declare `requires (required, optional)`, and `AgentRuntime` runs Avro-style negotiation before the run. Active fragments assemble into a dynamically generated `NegotiatedObservation` Pydantic model (nested: `obs.dom.element_tree`, `obs.terminal.stdout`).

Within a Capability, fragments are produced by **Resolvers** (the Browser Capability uses Watchdogs as Resolvers — adding a new fragment = adding one class, the capability does not change).

`fragment_id` is a real serialized field defaulted from `__registry_key__`, and the WAL names observation files by the fully-qualified key (`000_browser.dom.json`). Both exist so a `RunTrace` says *which capability* produced a fragment — an evaluator has the file, not the config.

### 2.5 Action system — symmetric to Observation

The symmetry is literal, on both axes: same registry-key shape (`source.local_id`), same relative declaration surface, same delivery. Do not restore an asymmetry between them.

Actions are individually registered (`ActionRegistrar`). Each is a dataclass with conscribe metaclass; side-effect parameters live on the action instance (e.g. `ScrollAction.default_amount`) — an agent declares that it can scroll, never how far. A Capability holds a `list[ActionBase]` — populated from YAML or defaulted from `__wiring__["action"]` — and routes by `action_id`. Agents declare `(required, optional)` action keys; negotiation yields an `active_actions` set; `build_guarded_act` returns a closure that rejects anything outside the set. **Result: prompt advertises exactly what the env can execute, and config errors surface at negotiation, not at first LLM call.**

**Registry keys are always fully qualified** (`browser.click`, `tool_sandbox.call_tool`) — including in a single-capability environment; `EnvironmentBase.act` routes on the prefix and refuses a bare name. **`__wiring__` declarations are not.** An agent writes `click`, and negotiation binds it to the one capability in *this* environment that offers it (`action_bindings` / `fragment_bindings` on the negotiation result). Two providers of one relative key raises, naming both candidates; writing the fully-qualified key in `__wiring__` pins one. `build_guarded_act` is the only place a relative name becomes qualified — everything downstream (routing, descriptors, the trace) sees the full key. Same rule, same code path, for observation fragments. The action rule itself has one copy, `resolve_action_name` in `core/action_guard.py`; code that must know what the guard did to an action (`BranchLoop`'s replay check) calls it instead of re-deriving it.

`build_guarded_act` returns a typed `ActionSpace` and its executor. The original `action_filter` (`None`, `[]`, or a subset), final prompt descriptors, allowed full action names and bindings form one experiment identity. Public snapshots cannot mutate the guard; configuration digests normalize sets deterministically. Branch compatibility reads the effective action space, not merely the pre-filter negotiation.

Corollary the action layer paid for once already: negotiation matches *names*, not parameter contracts. An agent that emits an action must produce the params `descriptor().params` declares — put a test on that seam.

`Effect` is the output counterpart of `Params`: an action may declare a nested `Effect` model for what it measurably did, returned in `ActionResult.effect`, read back from a trace through the existing action registry by `effect_id` (no effect registry), and shown to models only as its `describe()` line (`docs/architecture_design.md` §4.5).

**Environment evidence is the third provides axis**, with the same shape: archivers are individually registered (`ArchiverRegistrar`, keys `source.local_id` such as `browser.network`), a Capability declares provides in `__wiring__["archiver"]` (fully qualified), the Environment aggregates `provided_archivers`, and `negotiate_archive` runs in `prepare()` and yields `archive_bindings` (archiver key → `capability_id`). It has exactly one deliberate asymmetry: **requires come from the trace hook instance's `archive` list, always fully qualified** — never from a class `__wiring__`, and never relative (user ruling D3: what a run records is a per-run choice, not an agent's domain knowledge). Do not "fix" that asymmetry by adding an archive entry to an agent's `__wiring__`.

### 2.6 AOP hooks — no emitter, no opt-in

Hooks declare `__intercepts__ = [Intercept("observe", role="env", advice="after"), …]`. The Weaver builds an `AOPProxy` around agent/env/llm/loop roles; the Loop calls ordinary public methods and knows no hooks. `TraceWriter` is the one recording component: it measures calls and steps and persists only the channels selected by `RecordingPlan` before expensive capture or encoding. The old separate metrics hook does not exist. With no statistics enabled, usage and call counts are `None`; unknown provider usage, known subtotals and measured zero remain distinguishable.

`around_ainvoke` assigns stable call identity before `proceed()` and settles success, failure or cancellation once. Attribution uses a run-owned `RunCallContext`, including helper purpose and model identity. Step handoff seals each step's own calls, execution facts and archive batch before the next segment starts. Persistence and bounded retries are separate from execution; a failed sealed step never becomes the next step or trailing calls. Only calls from an execution step that was never surrendered belong in trailing records. Public execution/handoff boundaries measure execution and recording overhead separately, including restoration and terminal observation.

The archive consumer pulls evidence through `Environment.collect_archive()` on the raw environment passed at run start. It never drives actions or observations, and Runtime recognizes the `ArchiveConsumer` Protocol rather than a concrete hook type. Recorder initialization, capture, encoding, persistence and close-out faults remain recording-health diagnostics and never decide the Agent's outcome. `LiveMonitor` captures transient phases in run-owned memory through blocking around advice; rendering performs no I/O inside the hook, and persisted progress comes from RunTrace. If a semantic boundary has no method, model the actual operation as a public Protocol method; do not introduce an emitter side channel.

Two layers of hook:
- **Lifecycle callbacks** (`on_run_start/end/error`) — `AgentRuntime` calls these directly, on every termination path including external cancellation.
- **Method interception** (`__intercepts__`) — the Weaver does this transparently.

### 2.7 RunTrace persistence (WAL)

A sealed step is committed through `WALWriter.commit_step`: selected content files first, each with data `fsync`; then the step record and extra-file index; then one `fsync` per distinct directory; finally an atomic manifest replacement. The manifest remains the authority for committed files. Orphans are invisible, paths cannot escape the run directory, and unreadable committed data raises `TraceSchemaError`. An uncertain manifest replacement must be reconciled before the next write so in-memory state cannot overwrite a commit that already became visible.

Trace format version 6 declares recording scope, indexes actual committed steps and lightweight call metrics, and preserves content-addressed prompt images. Full call readers restore prompts byte for byte and validate image hashes. Lightweight records are complete within their declared scope; omitted trajectories, observations or archive channels are not corrupt empty evidence. Consumers explicitly require the channels they need. `AgentResult` uses schema version 3.0 and `execution_ms`; old data shapes are rejected, not adapted.

Execution and recording have separate outcomes: `result.json` states `agent_done`, while recording health and the manifest state `writing | complete | incomplete`, diagnostics, gaps and `complete_through`, the continuous complete prefix. Recording failures never stop or veto the Agent. Later steps may commit beyond a hole, and step count is the number actually committed, not the largest index. Successful repair can restore current completeness while retaining historical fault diagnostics. Background work and close-out have explicit finite budgets; outstanding system I/O is not reported as successfully stopped. Branching and export require sufficient complete history and declared evidence, while inspection and metrics may read saved content after gaps with its coverage visible.

Environment evidence uses the same WAL under `env/`, indexed with its owning step or trailing batch. Run records are immutable versions; content-addressed blobs are deduplicated only against committed content. Browser WARC revisit references stay inside a collection batch, and nonempty batches carry their required fingerprint, so losing an earlier batch cannot create dangling evidence. Evidence keeps its own capture time and identities; its collection step is not a claim of action causation. Replay rule snapshots save the actual loaded text, digest and provenance through these same archive contracts. Full or summarized replay logs declare their detail and retain counts and time bounds.

`RunTrace.archive_files` / `iter_archive` / `blob` / `archive_run_file` / `archive_lines` are the archive read boundary. Browser consumers receive the `ArchiveReader` Protocol through `core/archive/source.py::open_archive`, never walk `env/` themselves. An empty archive creates no `env/`. Each step declares `policy`, `restored` or `terminal`; only policy steps count as the Agent's decisions. Restored attempts and exceptions retain explicit correspondence even where no action result returned. Branch provenance remains separate from experiment configuration identity. Offline evaluators, exporters, inspection and flywheel metrics use committed readers and declared recording capabilities, never live runtime state. `RunTrace.evals()` is the only accessor that lists a directory because post-run verdicts are outside the manifest. No SQLite or old-format compatibility paths are introduced.


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

`spectragent/generated/*.py` is output of `conscribe` config generation driven by `__wiring__` + `Annotated[...]` fields. Do not hand-edit them; change the source class and regenerate.

There is **one** generator entry point for every registrar:

```bash
uv run spectragent config generate                             # or: .venv/bin/python -m spectragent.scripts.generate_all
```

It collects every configurable registrar through the public `core/registries.py::collect_registrars()` used by all composition entry points, and writes `_composed.py`, `_composed.schema.json`, `.fingerprint.json` and prunes obsolete conscribe-generated `.pyi` stubs in one pass. Discovery is strict and excludes `spectragent.generated` and `spectragent.scripts`; old generated config files remain intact until replacement content renders successfully. All collected layers must have implementations. Regenerate after **any** change to a `__wiring__` entry or an `Annotated[...]` constructor field.

A new strategy axis must be added to `collect_registrars()` as well as registered. A registrar the Composer does not collect degrades to `Literal[<keys>]` instead of the discriminated union, **so every knob the strategy declares stays unreachable from YAML** — measured, not assumed: regenerating with and without `dom_serializer` collected gives `Union[HtmlDom_SerializerConfig, TextDom_SerializerConfig]` versus `Literal['text', 'html']`. The `Literal` still rejects a typo, so the loss is silent only for a *brand-new* axis that no YAML names yet; once a config block exists, the degraded field rejects it loudly. (An earlier note here claimed a bare `str` and a silently-defaulted typo — that was the pre-`__wiring__` shape `mouse_trajectory` had, not what an uncollected registrar does today.)

`spectragent/generated/` is untracked build output (only `__init__.py` and `.fingerprint.json` are in git), so regeneration mostly produces no diff. `.fingerprint.json` records each registrar's content fingerprint precisely so staleness is detectable. A stale `_composed.py` remains internally consistent and can pass tests that merely consume it; `tests/test_generated/test_in_sync_with_registries.py` is the dedicated guard that compares the tracked fingerprint with the live registries.

### 3.5 Priority order when choosing what to do next

Follow `docs/execution_dag.md` priorities (P0 foundation → P0 abstractions → P0 framework → P1 persistence/migration/generalize → P1 E2E → P2 Docker/SWE → P3 advanced). Do not start a P2/P3 module if its P0/P1 dependencies in the DAG are not in place — the DAG is dependency-ordered, not wish-list-ordered.

### 3.6 Commits, branches and internal codes

Names must explain the resulting code change without requiring a planning document. Do not put module IDs such as `M05` in commit messages, do not use process words such as `handoff` as the substance of a branch name, and do not add `Co-Authored-By` trailers. Leave pre-existing opaque branches unchanged.

Branches are fine when their names are readable. Name a branch `<type>/<what-changes>` in lowercase kebab case, where the type is `feature/` (adds a capability), `fix/` (corrects a defect), `enhance/` (improves existing behaviour), `refactor/` (changes structure, not behaviour) or `docs/` (documentation only). Examples: `feature/add-jev`, `fix/observation-returns-other-tab`, `enhance/add-action-result-summary`. The earlier linear-`main` rule existed because agent branches carried opaque labels such as `c1`, `c2`, `b1` or `b2`, which say nothing about the change. Opaque labels and planning codes are never branch names. Each commit on a branch still describes one change, so the history reads without the branch name.

The same rule covers everything people read, not only commit messages. Identifiers, config keys and values, field descriptions, report fields and headings, CLI and error messages, and test names are written in plain words and never use a planning code: PRD question and revision numbers (`Q1`, `C31`, `E20`), module IDs (`M33`), decision and acceptance file labels (`dec29`, `acc30`), handoff and live-plan labels (`HO`, `LP`), defect labels (`H28`) or spike labels (`S1`, `S2a`). A comment may cite a versioned doc by file and section (`docs/architecture_design.md` §4.14), never the local-only PRD. Module IDs stay in `docs/execution_dag.md`. `tests/test_docs/test_no_planning_codes.py` checks the identifiers, field descriptions and CLI help it can read from the source, and `tests/test_flywheel/test_report_schema.py` checks the flywheel report's keys, headings and rule texts.

### 3.7 Protected local visual assets

`/assets/` is intentionally gitignored, locally retained source artwork accepted by the user — not disposable residue. Never delete, clean, or overwrite it unless explicitly requested. Read `assets/README.md` and run its verifier before visual changes.

### 3.8 Migrated project history

The repo-scoped `af-project-history` skill under `.agents/skills/` contains curated historical decisions, experiment context, and an unfinished-work ledger migrated from Claude project memory and handoffs. Use it when the user asks to resume earlier work, explain why a boundary exists, or revisit prior anti-bot, input, LLM-routing, JEV, roadmap, handoff, or asset work. Its references are historical evidence and continuation state, not a source of current implementation truth: re-check the relevant architecture section, code, and tests before acting.

### 3.9 Waiting for long-running work

Do not wait for a run, a test sweep or a server with a fixed `sleep N` followed by a look at its log. A guessed pause is either too short (another round) or too long (idle wall-clock time), and one longer than the tool's command timeout is killed outright. Session history up to 2026-10-22 held about 500 such pauses, about 22 hours of idle time, almost all `sleep N; tail log`.

- If your harness notifies you when a background command exits, start the job in the background and rely on that notification. Do not poll it.
- If you must block on the outcome (a subagent that needs a run's result to continue), wait on the event itself, with an upper bound. Run it in the foreground with an explicit tool timeout when it fits, or use `wait <pid>`, `tail -f <log> | grep -m1 '<finished line>'`, or a deadline loop such as `for _ in $(seq 300); do <condition> && break; sleep 1; done`. macOS ships no `timeout` command, so the bound is the tool's command timeout or the loop's count. Keep each blocking command under the tool's command timeout.
- To check that a server or job started, poll for readiness (the port answers, the ready line is in the log) at a short interval with an upper bound, e.g. `for _ in $(seq 30); do curl -s localhost:<port> >/dev/null && break; sleep 1; done`. Do not use a fixed pause.
- A fixed pause is right only where the delay itself is the point: a test that needs time to pass, or rate limiting.

## 4. External dependencies worth knowing

- `conscribe` (v≥2.1.0,<3, in-house) — the registrar + `__wiring__` + config-generation framework. Its three-element `__wiring__` tuple (`(registry, required, optional)`) is the keystone of the whole design. M01 in the DAG = already shipped.
- `agent-cdp` (v0.5.0, in-house) — CDP client + Watchdog + Event scope; **only the Browser Capability consumes it**, it is not a framework-level concern.
- `bubus` — event bus used inside `agent-cdp` watchdogs (optional extra).
- `pydantic` v2 — all data contracts.
- Cloud browser "lite" SDKs (`lexmount`, `browser-use-lite`, `hyperbrowser-lite`, `agentbay-lite`, `airtop-lite`, `skyvern-lite`) — session lifecycle + CDP URL only, wrapped behind the Browser Capability's backend abstraction. None of them declares `NETWORK_CAPTURE`, so browser archiving and `replayability: pinned` refuse them (`docs/architecture_design.md` §4.9.x, §4.14).
- `warcio` (Apache-2.0) — writes `browser.network`'s WARC files. **Never** add `py-wacz`, `cdxj-indexer` or `surt` as dependencies: `surt` is AGPL and the other two pull it in. A lockfile test enforces this; port permissive code instead and record it in `THIRD_PARTY_NOTICES.md`.

## conscribe 2.0 integration

- Mark observation/action/archive negotiation declarations with dict-form `declare_only: True`; they create neither injected attributes nor stubs. Runtime receptors keep their existing declarations.
- `ArchiverRegistrar` declares `inline_only=True`. conscribe refuses every top-level `archiver:` value, including `[]` and `null`, with Pydantic error type `inline_only_layer`; write instances under `hook[trace_writer].archive`.
- Composer uses `conscribe.introspect` for constructor forwarding, runtime slots and container shapes, and `ComposedConfigResult.top_level_layers` for writable root sections. Conscribe 2.1 adds public `registrar.get_config_provenance(key)` for the declaration/MRO/wiring evidence consumed by `config explain`. Do not reimplement these rules or inspect private caches.
- User-defined Pydantic models, nested constraints, tuple defaults and dataclass receptors are supported. Do not add 1.x workarounds for them. Keep ordinary `BaseModel` config values typed when passing them to constructors.
- Tests treat `conscribe.ConscribeWarning` as errors. Use `conscribe_isolated` / `isolated_registries()` for test registrations; capture module-level fakes and activate them only in tests that need them.
