# Add a Loop

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

`ReactLoop` is the only Loop that exists. `CodeActLoop` (M20, P2), `FunctionCallingLoop`
(M22, P3) and `PlanExecuteLoop` (M24, P3) are designed and unbuilt — target registry keys
and slots are in `docs/architecture_design.md:228-247`. You are loop author #2. Several
things in `react.py` are shaped the way they are *because* there is only one sample; this
skill says which, so you know which lifts are yours.

## 1. The split: the Agent is passive, the Loop drives

Two independent registries. The Agent holds no Loop reference and never calls the Loop;
the Loop holds no agent state and never stores deps on itself.

```python
# spectragent/core/loops/react.py:153-159
__registry_key__ = "react"
__required_slots__ = {"build_react_prompt", "parse_react_output"}
__optional_slots__ = {"get_react_output_model", "prepare"}
__required_message_protocols__ = frozenset({"user_assistant"})
__requires_env__ = True
```

`weave()` (`core/loops/_registrar.py:117-135`) validates the slots, captures deps in a
closure and monkey-patches `agent.run` onto the *instance*:

```python
ctx = dict(deps)
async def run(task, **kw):
    return await loop._execute(agent, task, ctx=ctx, **kw)
agent.run = run
```

**Stateless on purpose.** Deps live only in that closure, so one Loop instance drives N
agents concurrently and the Nth weave cannot rewrite the 1st agent's env. Everything your
`_execute` needs arrives in `ctx`: `env`, `llm`, `memory`, `act_fn`, `negotiated_obs`,
`active_descriptors`, `step_recorder`, `task_id` (`core/runtime.py:349-360`). Keep it that
way: do not store per-run dependencies or mutable run state on `self`. Reading immutable
config fields and calling stateless helper methods on `self` is fine.

### Compatibility is slots, not Protocols

Current `AGENTS.md` §2.3 and `docs/architecture_design.md` §4.1 both define compatibility
through method slots. `rg "class .*Compatible" spectragent/` returns nothing, and it
should. The design reason is that a Loop that is ReAct plus one method is
`class ReactWithThoughtLoop(ReactLoop):
__required_slots__ = ReactLoop.__required_slots__ | {"parse_thought"}` — zero new Protocol.
The Protocol route needs a new `*Compatible` per variant, an N×M explosion. The cost is no
static check; the compensation is that a missing slot raises `IncompatibleError` naming the
methods, at `prepare()` time, before an LLM is ever called
(`core/loops/validation.py:16-31`, `_registrar.py:97-113`).

So: "an Agent supports many Loops" means it defines both method sets, and names both loops
in `__wiring__` — `agents/browser_use.py:282` pins one (`"agent_loop": ("agent_loop",
["react"])`); `agents/_registrar.py:90` is the Mode 1 default that allows all. conscribe
turns that into `Literal[...]` on the generated config, so an illegal pairing dies in
Pydantic validation, never at runtime.

### Where your Loop is picked up

`AgentRuntime.prepare` (`core/runtime.py:276-374`), in order: slot validation (`:295`) →
observation negotiation (`:298`) → action negotiation (`:306`) → memory-protocol
negotiation (`:310`, reads your `__required_message_protocols__`) → AOP weaving of
agent/env/llm/recorder (`:314-323`) → `build_guarded_act` (`:341`) → `loop.weave` (`:349`)
→ intercept audit (`:367`). Your Loop's observation demand goes in **unnarrowed**: an agent
may narrow its own half per mode, but "cannot excuse itself from a fragment the Loop needs"
(`core/negotiation/observation.py:245-272`).

> **Known document drift:** `docs/architecture_design.md` §4.8 still describes memory
> protocol negotiation as future work, while `AgentRuntime.prepare()` currently calls
> `negotiate_memory()` and the tests exercise it. Per `AGENTS.md` §3.1, surface that
> conflict before changing the memory/Loop contract; do not use this guide to silently
> choose one side.

Minimum new Loop: subclass `AgentLoopBase`, set `__registry_key__`, `__required_slots__`,
`__requires_env__`, implement `async def _execute(self, agent, task, *, ctx, **kwargs)`
returning an `AgentResult`. Registration is automatic via the conscribe metaclass
(`tests/test_core/test_loops/test_loop_registration.py`).

## 2. Where a knob belongs — the worked example

Three knobs sit together on `ReactLoop` (`react.py:162,166,173`):

| field | meaning |
|---|---|
| `max_steps` | steps before termination |
| `max_consecutive_parse_errors` | consecutive unparseable **steps** before termination |
| `parse_retries_per_step` | extra re-asks **inside** one step, on the observation it already took |

`parse_retries_per_step` is the interesting one, because *parsing* is agent domain
knowledge — `parse_react_output` is an agent slot, and only the agent knows this run's
verbs — while **what to do when parsing fails is execution strategy**. Putting the retry
budget on the Agent would split the two halves of one decision across two classes, and the
next person asking "how many times does a bad reply get re-asked?" would have two places to
look and no rule for which wins. One fact, one source: the knob goes on the Loop.

### The seam that makes it work

The Loop does not invent the feedback text. The agent raises `ParseError(detail,
raw_output=...)` (`core/loops/errors.py:21-38`); the Loop renders it:

```python
# react.py:83-95
def _parse_feedback(exc: ParseError) -> str:
    return (
        f"[SYSTEM] Your previous output could not be parsed into actions. "
        f"Error: {exc.detail}\n"
        f"Please try again with valid output format."
    )
```

**Agent supplies content, Loop supplies policy.** Copy that shape for any new phase: the
agent's exception carries the thing only the agent knows, the Loop decides what it costs.

### The invariant worth preserving

`react.py:457-458` writes the rejected reply and the feedback into memory *before* deciding
whether to retry. So the in-step retry and the next step read **exactly the same two
messages** — the only difference is whether a new observation follows. If they differed the
model could tell which situation it was in, and the in-step path would be training on a
prompt shape the cross-step path never produces. Do not "improve" one of them in isolation.

One more convention, and it is a convention rather than something the design doc settles:
in-step retries are invisible to the cross-step streak (`react.py:476-481`) — a step either
parses or it does not. `test_react_parse_retry.py:380
::test_streak_counts_steps_not_attempts` documents the decision, not just the behaviour.
Read it first if you ever unify the two budgets.

## 3. `RunAbort`, and the layering question

### 3.1 What it fixes

A blanket `except Exception` around act/parse used to swallow a guard's veto. Observed
live: an agent's own safety guard fired, the run came back as an ordinary
`agent_done="error"`, and the benchmark driver went on to the next task. A Loop turns
*failures* into results — that is right for a dead browser and wrong for a refusal.

### 3.2 The rule is global and mechanically enforced

**No Loop may swallow `RunAbort`.**
`tests/test_core/test_loops/test_abort_handler_hygiene.py` AST-scans every module in
`spectragent/core/loops/` and fails on any `try` with a blanket `except Exception` that
does not carry one of the two accepted shapes. It constrains the *source*, not call sites,
so your loop inherits the rule on the day you create the file — and the test survives being
refactored into a base-class helper later. `:248` guards against the rule going vacuous.

Third answer, allowed and deliberately ugly: `# RunAbort-exempt: <reason>` on the `except`
line, for a handler that genuinely cannot be vetoing anything (teardown, logging).

### 3.3 The two shapes

**(a) Handle inline** — observe (`react.py:263`), prompt (`:308`), llm (`:377`), parse
(`:431`). `except RunAbort as exc:` **above** the blanket handler (Python matches top-down,
so position is load-bearing, not style), call `await self._note_abort(phase=..., exc=exc,
recorder=..., observation=..., step_idx=..., step_records=...)`, then bare `raise`. Pass
`llm_output=` / `token_usage=` if the phase has them — for a parser veto the refused reply
*is* the evidence (`react.py:439-449`).

**(b) Capture and re-raise** — the per-action loop only (`react.py:566-571`, resolved at
`:597`). The blanket handler assigns to `act_error` and breaks, because the failure must be
attributed to one action among several; the ordinary (richer) `StepRecord` is built,
`_surrender`ed, and only then `if isinstance(act_error, RunAbort): raise act_error`. **Do
not call `_note_abort` here** — the record is already written and you would double it.

The prompt phase is the one asymmetry: it catches `RunAbort` and *nothing else*
(`react.py:297-318`). An exception out of the agent's own `build_react_prompt` is a bug in
the agent, and converting it to `agent_done="error"` would hide it.

Guard side, for completeness: a hook raising `RunAbort` must be `blocking=True`
(`core/aop/proxy.py:337`) or its exception is logged and dropped by `_safe_call` (`:552`) —
the guard looks like it is running and the run continues. Subclasses work; the Loop only
does `isinstance`.

### 3.4 Three layers — and "stop the sweep" is not one of them

The Loop's only job is to **raise a typed signal and not eat it**. It does not know a sweep
exists. Each layer above decides what the signal means:

| layer | decision | code |
|---|---|---|
| `ReactLoop._execute` | record the interrupted step, then let it fly | `react.py:263/308/377/431/597` |
| `AgentRuntime.execute` | does it travel past `raise_on_error=False`? | `runtime.py:527-550` |
| `BenchmarkRunner.run` | does the sweep stop? | `benchmark_runner.py:301-328` |

At the runtime layer the order is the point: build the terminal result, `await
self._finish(result, error=exc)` — `on_run_end`, then `on_run_error` with `partial_steps`,
then drain (`runtime.py:567-583`) — and *only then*:

```python
# runtime.py:548
if raise_on_error or isinstance(exc, RunAbort):
    raise
```

`raise_on_error=False` means "a failed task must not stop the sweep". A veto is the
opposite instruction, so it propagates either way — but after the trace is closed out.

At the sweep layer, `run_task` re-raises `RunAbort` first (`benchmark_runner.py:413-420`)
while still absorbing ordinary failures, and `run` breaks out, records
`BenchmarkRunSummary.aborted` naming the task (`:132-155`, `:304-317`) — **and then still
evaluates the outcomes that finished** (`:318-320`). The guard said stop, not destroy the
evidence; under Record Once, Evaluate Many those traces are complete and worth scoring.
Without the `aborted` field a short sweep is indistinguishable from a small one.

This is also why `RunAbort` derives from `Exception` and not `BaseException`
(`core/loops/errors.py:59-65`): a `BaseException` would skip `_finish` and destroy exactly
the WAL commit the abort exists to preserve.

Known gap, so you do not rediscover it: `AgentDoneStatus` (`schemas/_types.py:15-20`) has
only done/max_steps/timeout/error. A finished veto reports `agent_done="error"` with
`RunAbort` in the error string, so an evaluator selecting vetoed runs is string-matching
(`tests/test_core/test_benchmark_runner.py:465`). The evidence is in the trace; the label
is not in the artifact.

### 3.5 What is deliberately still ReactLoop-local

`_note_abort` (`react.py:703-754`) and its per-phase bookkeeping live on `ReactLoop`, not
on `AgentLoopBase`. That is intentional: React's phases are observe/prompt/llm/parse/act,
CodeAct's would be generate/execute/verify, PlanExec's plan/execute. Phase vocabulary does
not generalise from one sample, and inventing the abstraction now would be guessing at its
shape.

**Generalise at the second instance, not the first — and that instance is yours.** When you
write loop #2, the right move is to lift the `RunAbort`/blanket pairing into a helper on
`AgentLoopBase` that phases run through, so it cannot be forgotten. Until then, copying the
pairing per phase is correct, and the hygiene test is the guard rail that makes copying
safe. The duplication is accidental shape; do not defend it as design.

## 4. One `Environment.observe` is one cycle

`EnvironmentBase.observe` (`core/environments/_registrar.py:344-384`) holds a monotonic
counter, increments it on every call, and announces `begin_observation(cycle)` to each
participating capability (`core/capabilities/_registrar.py:186-206`) before resolving
anything. Capabilities run concurrently; one capability's own fragments run sequentially
because they share a capture.

**`observe()` is not a getter and is not idempotent.** Defect F11 was a step being served
the previous step's screenshot byte for byte, because the browser held one capture until
the next `execute_action` and a step that ran no action (a reply that failed to parse)
reused it. The fix made the caller's `observe()` *be* the boundary.

The consequence for any Loop that re-asks the model inside a step is blunt: **an in-step
retry must not open a new cycle.** If it did, the model would be answering a follow-up
question about a screenshot it was never shown — F11 from the other side. ReactLoop's retry
therefore re-reads `memory.get_context_messages()` and nothing else (`react.py:470`; the
reasoning is in the comment at `:322-340`).

Pinned by `tests/test_core/test_loops/test_react_parse_retry.py:193`
(`TestRetryDoesNotOpenANewObservationCycle`). Note *how*: `_CycleCap` at `:56` is a fake
capability that implements the cycle rule and advances a counter when a fragment is asked
for twice, and the assertions are `cap.cycles == 1` / `agent.cycles_seen == [1]`. Write
your version the same way — an `observe_calls == 1` assertion passes for the wrong reasons
and dies the first time the call site moves. The environment-side half is
`tests/test_core/test_environments/test_observe.py:103`.

Before adding any code path that re-asks the model or re-reads the world, ask whether it
calls `env.observe`. If it does and it did not before, you have changed which screenshot
the model is looking at.

## 5. Config surface

Loop config fields are `Annotated` conscribe fields on the dataclass. `AgentLoopBase` sets
`__config_annotated_only__ = True` (`_registrar.py:70`), so only annotated attributes reach
the generated config.

```python
# react.py:173-185 — the default appears twice, and that is this file's convention
parse_retries_per_step: Annotated[
    int, Field(default=0, description="...")
] = 0
```

- Pick a default that is a **no-op**, so no shipped YAML changes behaviour on upgrade.
- Add the field to the Loop class docstring's config list (`react.py:143-151`) — the only
  prose inventory of that surface.
- Never hand-edit `spectragent/generated/_composed.py`. Regenerate: `uv run spectragent-gen`.
- The generated class is named after the *wiring*, not the loop: `ReactAgent_LoopConfig`
  (`_composed.py:254`). Search with `rg` for your field name, not for a class name you guessed.
- Until regeneration runs, the knob is reachable in Python and rejected by YAML with
  `extra_forbidden` — loud, not silent. Details and the staleness traps: the
  **af-regenerate-and-verify** skill.

## 6. Test seams a new Loop owes

- `_validate_slots` rejects an agent missing a slot — model on `test_weave.py`.
- Weave is stateless: two agents, one loop instance, no cross-talk (`test_weave.py`).
- Every phase, abort: the exception escapes **and** a record reached the sink. The hygiene
  test catches a missing pairing; only a behaviour test catches a pairing that forgets to
  record. Model on `test_react_run_abort.py::TestAbortOnEveryPhase` (`:286-443`).
- Ordinary failures still return a terminal `AgentResult` with an honest `env_status` —
  `"failed"` only when the environment is what broke; a 429 is `"success"`
  (`react.py:388-408`).
- If your loop re-asks inside a step: the cycle test from §4.
- The Action seam AGENTS.md §2.5 names: negotiation matches *names*, not parameter
  contracts. A loop that emits actions must produce the params `descriptor().params`
  declares — put a test on it.
