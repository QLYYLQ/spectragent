# Add or change a Loop

Read the relevant section of `docs/architecture_design.md` first. Find the symbols below
with `rg`; this guide names contracts, not historical line numbers. The architecture is
being refactored, and neither an old test nor a dated handoff justifies retaining a
superseded implementation.

## 1. The split: the Agent is passive, the Loop drives

An Agent owns domain prompts and output parsing. A Loop owns execution policy: when to
observe, ask, retry, act and finish. `BranchLoop` is a ReAct specialization, not a second
generic strategy. Do not generalize all branching protocols from that one implementation.

A new strategy subclasses `AgentLoopBase`, declares `__registry_key__` and its required
Agent method slots, and implements `async def _execute(self, agent, task, *, ctx, **kwargs)`.
Conscribe registers it. The public structural contract is `core/loops/base.py::AgentLoop`;
keep that Protocol consistent with the methods Runtime actually invokes.

`AgentLoopBase.weave` validates slots and task inputs, captures dependencies in a closure,
and installs `agent.run`. Every invocation constructs fresh run state. Dependencies and
mutable state never live on the Loop instance: no `loop.ctx`, per-Loop retry counter or
cached task history. Repeated weaving must not redirect an earlier Agent to a later run.

### Compatibility and assembly

Compatibility uses `__required_slots__` and `validate_slots(agent)`, raising
`IncompatibleError` for missing callables. Do not add `*Compatible` Protocols or a
compatibility wrapper around an unused API. `__optional_slots__` documents methods a
consumer may use; the consumer validates an optional slot just before calling it.

Runtime validates slots, typed Loop inputs, execution limits, environment requirements,
fragment/action/memory contracts and setup compatibility before it starts the environment
or model. It then weaves AOP and the Loop. A config that setup would refuse must be refused
by `prepare()` too; do not defer an already knowable error to the first step.

- `__requires_env__` defaults to `True`. An environment-free Loop explicitly declares
  `False`; the Agent's fragment and action requirements still undergo negotiation.
- `__loop_fragments__` declares exactly the loop-source fragments this strategy can
  construct. The default is `LOOP_FRAGMENT_KEYS` from `core/loops/_loop_fragments.py`.
  Negotiation refuses a required fragment the Loop cannot build.
- `__required_message_protocols__` participates in memory negotiation. Extending the
  protocol names also requires a concrete message and memory behavior contract.
- An Agent can narrow its own fragment demands; it cannot remove the Loop's requirements.

### Typed execution limits and prompt ownership

`execution_limits() -> ExecutionLimits` is the Loop's sole source of execution limits.
`ReactLoop` supplies `max_actions_per_step`. The immutable type and validation live in
`core/loops/limits.py`.

An Agent declares consumed fields in `__execution_limit_fields__`. A deliberate empty set
means these limits do not apply to its prompts; an absent declaration cannot silently
ignore a supplied limit. Consumed fields must be known and provided. A consuming Agent's
existing `prepare` slot explicitly accepts `execution_limits` by keyword; `**kwargs` is
not a consumption declaration. Runtime checks this in `prepare()` and hands the same typed
value to `AgentRuntime.seed_messages()`.

The Agent uses its existing `prepare`, `_build_system_prompt` and `build_react_prompt`
methods to decide wording and placement. Runtime never constructs domain prompt text.
Do not restore the convention that probes `getattr(loop, "max_actions_per_step")` and
passes a matching keyword only when a signature happens to accept it.

### Run state, identity and measurements

`core/loops/run.py::LoopRun` owns one execution's dependencies, resolved `RunIdentity`,
step records, action history, last answer and previous step. `ReactRun` in `react.py`
extends it with the ReAct-specific parse-error streak. `BranchRun` extends the ReAct state
for checkpoint execution. Use `from_ctx(..., step_executor=...)` to construct a new state
for each invocation; the step executor is the declared execution seam, including its AOP
wrapper when supplied by Runtime.

Runtime resolves run and model identity once. Other layers receive that identity instead
of independently inspecting model implementations. Token accounting and model-call counts
belong to the recording component, including failed replies with reported usage and calls
before an in-step retry fails. Neither `LoopRun` nor `ReactRun` has a usage tally or a
second run clock. With no recording component, usage and call count remain `None`.

`AgentLoopBase` provides the common public operations `surrender`, `surrender_vetoed`,
`end_step`, `build_result`, `observe_terminal` and `observe_after_result`. Both ordinary and
failing paths use the same overridable hand-over method; never hard-code a call to a
concrete `ReactLoop` implementation from a generic failure path.

## 2. ReAct policy, parsing and its public round operations

ReAct keeps its own phase vocabulary and knobs:

| Field | Meaning |
|---|---|
| `max_steps` | Budget for this run's policy decisions |
| `max_actions_per_step` | Maximum actions executed from one accepted reply |
| `max_consecutive_parse_errors` | Consecutive unparseable steps before termination |
| `parse_retries_per_step` | Additional asks inside the same observation cycle |
| `today_override` | Date shown by ReAct's step-info fragment |
| `terminal_observation` | Whether to observe after a normal terminal result |

Parsing is Agent knowledge; deciding what to do with a parse failure is Loop policy. The
Agent raises `ParseError(detail, raw_output=...)`; ReAct's public `parse_feedback` renders
that diagnostic for another ask. Before any parsed action runs, ReAct's public
`check_action_params(run, actions)` validates the ones that would run against their
`Params` (`core/action_guard.py::params_refusal` over `run.action_space`, with
`name_refusal` for a name the run does not execute) and raises the same `ParseError` on a
misfit, so a malformed action is rewritten by the model, never executed and never a failed
`ActionResult` (user ruling 2026-10-23). A truncated provider response follows the existing
truncation policy; do not reinterpret it as a successful parse or manufacture a partial
action.

A structured reply that does not fit the Agent's output model is refused by the
framework, not the Agent. The provider raises `StructuredOutputValidationError` with the
text its validator saw (`raw_output`); ReAct words the refusal from that text, the output
model and the run's action space (`core/action_guard.py::reply_refusal`), in the voice of
the params check: invalid JSON as one line with the decoder's position, an action item as
`action N, <verb>: <param>: <msg> (<verb> takes ...)`, an unknown verb as the name check
words it, other fields by their path. It reads the action union's typed errors
(`action_shape`, `action_unknown`, `action_params` in `core/actions/action_union.py`),
never a message string. The Agent's parser never sees that reply: an Agent with an output
model parses only a validated instance (`core/loops/react.py::validated_reply` validates
any text first). There is no Agent slot for custom feedback.

`read_reply(run, reply, *, frame, output_model)` is ReAct's one reading of a reply:
framework validation of a structured run's text, the Agent's parser, the points moved off
the step's frame (`actions_in_capability_units`), then `check_action_params`. A new Loop
that executes parsed actions reads replies through it, or calls the same checks in the
same order.

A step stops at its first failed action (user ruling 2026-10-24): a result with `success`
False, the guard's refusal of a name included, ends the step's execution, and the actions
after it are not executed. The step is handed over normally (no `error_phase`; the run goes
on). The record keeps the accepted reply whole in `actions`, real results only in
`action_results`, and the count of unexecuted trailing actions in
`StepRecord.skipped_actions` (named fully qualified by `executed_name`, never given a fake
result). Agents tell the model next step through `agents/rendering/skipped_actions.py`. A
raise still ends the run and `done` still ends it; neither is a skip.

`ReactLoop.execute_step(run, step_idx, *, observation=None, restoration=None)` is the
public reusable round. It returns an `AgentResult` when finished, otherwise `None`.
Supplying an observation means the world has already been captured for that step and must
not be observed again. A supplied restoration check belongs on every record that round
hands over.

The shared ReAct operations are public:

- `read_reply(run, reply, *, frame, output_model)` reads a reply into checked actions or
  raises the `ParseError` that sends it back (§2 above).
- `cap_actions(actions, step_idx)` applies the action budget and names discarded actions.
- `remember_rejected(memory, output_text, exc)` records the rejected assistant reply and
  feedback turn.
- `remember_reply(memory, output_text, dropped)` records an accepted reply and any action
  truncation notice.
- `today_iso()` and `loop_fragment_builders(run, step_idx)` supply ReAct's fragment values.
- `serialize_llm_output(raw_output)` provides the common reply text representation.

Branch history and frozen student rendering call these declared interfaces. Do not expose
old private names as aliases, import another class's private helpers, or duplicate its
memory protocol in a consumer.

A step that exhausts parsing retries records `error_phase="parse"` and the actual parse
error. A successfully parsed empty action list remains a different execution fact.
Whether the run continues follows the configured parse-error streak; recording the error
does not change that strategy. Both steps remain excluded from training samples under the
existing export filter.

### The asynchronous prompt slot

The current contract is
`await agent.build_react_prompt(observation, context=ReactPromptContext(...))`.
`ReactPromptContext` is in `schemas/prompt.py`; it carries the explicit step, phase,
policy-step index, branch identity, source facts and prompt contributions. Runtime and
Loop pass facts; the Agent decides how to express them.

Policy steps use `phase="policy"`. Historical reconstruction uses `phase="history"` and
recorded contributions, so it never calls a current contribution function to guess an old
prompt. Contributions produced for a policy step are retained on that step's record.
In-step parse retries reuse the built prompt and contributions. Do not infer phase from a
hook call counter, start background generation, or nest an event loop inside a builder.
Prompt/helper failures are execution failures and cancellation propagates; they are not
recording failures to suppress.

UI-TARS declares its task once in the prepare seed and the measured platform once on
the first observation after prepare, which need not be source step zero in an export
segment. Its user turn renders the preceding batch from `loop.history`, including each
returned result, rather than the singular `loop.action_result`. This layout is Agent-owned;
Memory retains/trims the returned messages and does not infer missing execution facts.
Check fresh executions, source/restored history and segment reseeding when changing it.

## 3. Execution outcomes, cancellation and recording isolation

### Execution failures and vetoes

`RunAbort` is a typed execution veto. Any broad `except Exception` in a Loop must first
handle `RunAbort` or explicitly re-raise it after handing over the interrupted step.
`tests/test_core/test_loops/test_abort_handler_hygiene.py` checks these exception shapes.
The step preserves the phase, observation, reply and completed actions that actually
existed. ReAct's `_note_abort` stays private to ReAct because its phase vocabulary is
strategy-specific.

The action phase may capture an exception long enough to assemble partial results. It
must log a veto before awaiting hand-over and then re-raise that veto. An ordinary model
failure does not become an environment failure. An Agent prompt bug propagates as an
execution error; a diagnostic recording failure must never impersonate it.

Runtime closes out a vetoed run and re-raises `RunAbort` even with `raise_on_error=False`.
The benchmark layer decides that a veto stops a sweep and still evaluates completed
outcomes. A Loop does not know whether a sweep exists.

### Recording failures belong to the recorder

The Loop hands execution facts to `record_step`; the recording component owns collection,
encoding, persistence health, gaps and retry/recovery policy. A recording/WAL failure does
not stop the Agent, change `agent_done` or `answer`, append an artificial execution error,
or masquerade as a model/action failure. Later execution may continue while the trace
truthfully records its gaps.

Keep this isolation at the recording boundary. Do not add `except Exception: pass` around
`surrender`, inspect recorder health from the Loop, or reinstate a generic
`RecordingFailedError` stop path. Such catches would also swallow real execution vetoes
and bugs from arbitrary blocking advice. AOP guards and prompt injectors remain execution
components; the recorder's fault-isolation policy does not apply to them.

The result ledger and trace completeness ledger are separate. Offline consumers use
manifest evidence and completeness limits; they do not conclude that an Agent failed
merely because some evidence could not be persisted.

### Cancellation preserves already decided outcomes

An outside `asyncio.CancelledError` is re-raised unchanged after lifecycle cleanup. Never
swallow or replace it with a recording result.

When cancellation interrupts an await after the Loop has decided an outcome,
`surrender_vetoed`, `end_step` and `observe_after_result` use `carry_outcome` to retain that
veto or terminal result on the same cancellation object. They call the public hand-over
operation, including overrides. Runtime examines a carried outcome only for its own
`wait_for` timeout: a decided veto remains a veto, a decided result remains that result,
and a run still executing becomes a timeout. Do not use this to absorb outside cancellation.

### Two clocks, with one owner each

Runtime alone measures `AgentMetrics.execution_ms`: immediately before environment setup
until the execution path ends, before ending lifecycle callbacks. This includes setup,
restoration, policy steps, terminal observation and actual step-commit waits. Timeout and
error paths report measured elapsed time, not `timeout * 1000`.

The `timeout` budget still wraps `agent.run`; it does not expand to setup, end hooks or
teardown. `TaskOutcome.elapsed_ms` measures the outer task through assembly, Runtime
cleanup, resource release and result readback. Do not write that not-yet-finished task
clock into `result.json`. Step durations have their own recording boundaries; their sum
is not an asserted identity for execution time.

## 4. One `Environment.observe` is one cycle

`EnvironmentBase.observe` begins a fresh observation cycle and each capability's resolvers
share that cycle's capture. `observe()` is not an idempotent getter.

A parse retry asks about the observation already shown. Re-read memory for the retry,
without observing again or rebuilding the prompt. Conversely, a new policy step observes
again even when the preceding reply failed to parse and executed no action. Otherwise a
no-action step would indefinitely reuse stale browser evidence.

A restored checkpoint observation is already the policy step's capture. Pass it to
`execute_step(..., observation=...)`; opening another cycle would discard the verified
state the branch was supposed to decide on. Check behavior with cycle-aware fakes, not
only a total method-call counter (`test_react_parse_retry.py`, `test_restore_prefix_replay.py`).

## 5. Config surface

Loop knobs are `Annotated` conscribe fields on the strategy dataclass.
`AgentLoopBase.__config_annotated_only__` prevents ordinary state from becoming YAML.
Document each new knob on the owning class, choose defaults from the agreed behavior, and
keep ReAct-only date/retry/budget knobs out of unrelated Loop bases.

After changing a field or `__wiring__`, regenerate through the single entry point:

```bash
uv run spectragent config generate
```

Do not edit `spectragent/generated` by hand. Verify the live-registry fingerprint and a
real YAML that sets the knob; a stale generated class can pass its own tests. Use
`af-regenerate-and-verify` for that seam and `af-add-a-receptor` when the new axis is a
configurable strategy. Every new registrar also belongs in the architecture and AGENTS
registrar tables and `collect_registrars()`.

## 6. Test seams a Loop change owes

Use existing behavior tests for the path being changed; extend only the meaningful seams:

- Slot, typed input, limit and environment incompatibility fail at `prepare()` before
  setup or model calls. `test_execution_limits.py` covers missing declarations, absent
  fields and keyword sinks that do not satisfy explicit consumption.
- Repeated weave/execution keeps run state separate (`test_weave.py`). A common hand-over
  override is reached on normal, failed and vetoed steps.
- Every execution phase preserves a veto and its partial evidence. Recording-component
  failures leave execution outcomes intact; test actual recorder isolation, not a Loop
  swallowing an arbitrary fake sink exception.
- Cancellation preserves its object and any already decided outcome. Runtime close-out
  runs once, outside cancellation propagates, and its own timeout recognizes carried
  outcomes (`test_run_lifecycle.py`).
- Controllable clocks distinguish setup/restore/terminal observation from end hooks and
  teardown; timeout duration is measured. Outer task timing includes cleanup and readback.
- Parse retry tests keep one observation cycle, preserve all model calls for recorder
  attribution, and distinguish exhausted parsing from a successful empty action list.
- A shared async prompt builder receives explicit phase and recorded contributions;
  helper work is not repeated on history replay or an in-step parse retry.
- Emitted action parameters satisfy the advertised descriptors. Negotiating a matching
  action name alone does not establish a parameter contract.
- Branch context identity and the frozen export remain stable after public API changes
  (`test_branch_context_identity.py`, `tests/test_export/`). Trace reconstruction respects
  committed evidence and imports no concrete Loop (`test_reconstruction.py`).

Do not preserve tests for superseded private methods, duplicate usage ledgers or the old
recording-failure stop rule. Replace them with the current behavior contract.

## 7. Loop inputs, terminal observations and ReAct branching

`__loop_inputs__: Mapping[str, LoopInput]` declares per-task Loop inputs. Runtime and
`weave` both refuse unknown, missing or mistyped values with
`IncompatibleLoopInputError`. Validated values reach the closure as `ctx["loop_inputs"]`.
`TaskContext.env`, `.loop` and `.branch` remain separate channels: setup, execution inputs
and trace provenance respectively.

`terminal_observation` is a ReAct policy switch. Common capture is
`AgentLoopBase.observe_terminal`, and common outcome/cancellation handling is
`observe_after_result`; ReAct decides when to call them through `with_terminal`.
Done, budget exhaustion and the configured parse-error limit may produce the terminal
observation. Abort, action failure and restoration divergence do not. It contains only
environment fragments, has `origin="terminal"`, and makes no model call. An ordinary
capture failure creates an observe-error record without changing the decided result;
a guard's veto still propagates. Runtime's execution clock includes the observation.

`BranchLoop` remains a ReAct branch. Its restored prefix and terminal observation do not
consume the policy-step budget, enter the run's policy action history, or inherit source
model usage. Source history reconstruction replays only Agent decision/retry calls, not
auxiliary model replies. `history_source: source` reconstructs and exactly checks recorded prompts, replaying only
main decision calls and recorded contributions. It reads each recorded reply through
`read_reply`, a refused structured reply from its recorded `raw_output`, so a refusal reads
back in the words the live step used; a source recorded before structured refusals were the
framework's (2026-10-26) carries the old pydantic wording and no longer matches.
`restored` asks the Agent's public
`build_restored_history` slot to express live restoration observations, actual retargeted
actions and results, and separately labelled source material. It does not rewrite source
thoughts or touch Agent-private history. Both use the same live checkpoint observation;
environment `strict`/`warn` never waives history-contract errors.

`BranchLoop.execute_step` drives each restoration iteration while a run-owned iterator
is active, then delegates policy steps to ReAct. The checkpoint capture and first policy
step have one step identity and one handoff; recording keeps that step's existing start
time. Restored failures and cancellation retain their actual step timing. Prompt
contributions use a Hook around the asynchronous builder with explicit
`ReactPromptContext`: generate once per selected policy step, reuse on retries, read
recorded contributions without calling helpers during history reconstruction.

A restorer receives a narrow `RestorationContext` with environment, action function,
negotiated observation, source accessor and checkpoint. It has no model, memory or
recorder. It yields restored steps and the checked checkpoint observation. Under strict
verification, the first divergent observation, retarget or action outcome stops restoration
before later actions; each restored observation gets one `verify_restoration` call.

`SourceRun` stays in `core/loops/restore/source.py` as the checkpoint-specific wrapper.
Shared `recorded_observation`, `recorded_step_info`, `recorded_form` and `first_difference`
live in `core/trace/reconstruction.py`; `RunTrace.step` reads the typed step. The trace
layer never imports a concrete Loop. Do not leave a forwarding alias in the old module.

## 8. Optional export slots and shared fragment construction

`format_react_output` and `parse_react_thought` are optional ReAct slots consumed by
training export. The exporter checks them immediately before use and raises the ordinary
`IncompatibleError` with the training-export need. Runtime does not make optional export
capabilities mandatory for a live run.

`core/loops/_loop_fragments.py` exposes `loop_fragment_builders` with explicit task, step,
budget, date, history and previous-step inputs. `populate_loop_fragments` constructs only
negotiated loop-source fragments. ReAct supplies current values; a branch or student uses
`pinned_step_info` for a source-recorded budget/date, with `today_override` taking priority.
Keep one construction rule instead of copying it into each consumer.

`StudentRenderer.render` is async and awaits the Agent's prompt slot with historical
context and recorded contributions. It writes accepted assistant history through the
public `ReactLoop.remember_reply`, and seeds segments through `AgentRuntime.seed_messages`.
The offline `ExportRunner.run` remains synchronous and drives its whole async rendering
phase with one `asyncio.run`. Consumers already inside an event loop await the renderer;
there is no nested event loop in an Agent builder. Rendering never starts an environment
or calls a model, and this API adaptation adds no student-training strategy.
