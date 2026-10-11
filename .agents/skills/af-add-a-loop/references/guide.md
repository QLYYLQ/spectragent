# Add a Loop

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

`ReactLoop` is the only execution strategy that exists. `BranchLoop` (`branch`, M31) is a
`ReactLoop` *subclass* — a branch run's steps are ReAct steps, so it adds a restore phase,
not a new phase vocabulary (§7). `CodeActLoop` (M20, P2), `FunctionCallingLoop` (M22, P3)
and `PlanExecuteLoop` (M24, P3) are designed and unbuilt — target registry keys and slots
are in `docs/architecture_design.md` §4.1. You are the author of the second *strategy*.
Several things in `react.py` are shaped the way they are *because* there is only one
sample; this skill says which, so you know which lifts are yours.

## 1. The split: the Agent is passive, the Loop drives

Two independent registries. The Agent holds no Loop reference and never calls the Loop;
the Loop holds no agent state and never stores deps on itself.

```python
# spectragent/core/loops/react.py:233-239
__registry_key__ = "react"
__required_slots__ = {"build_react_prompt", "parse_react_output"}
__optional_slots__ = {"get_react_output_model", "prepare",
                      "format_react_output", "parse_react_thought"}  # the last two: §8
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
`active_descriptors`, `step_recorder`, `task_id` (`core/runtime.py:419-430`). Keep it that
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

`AgentRuntime.prepare` (`core/runtime.py:315-455`), in order: slot validation (`:346`) →
observation negotiation (`:349`) → action negotiation (`:357`) → memory-protocol
negotiation (`:361`, reads your `__required_message_protocols__`) → archive negotiation
(`:363-380`; validation only, and only when a hook declares a non-empty `archive`) → AOP
weaving of agent/env/llm/recorder (`:384-393`) → `build_guarded_act` (`:411`) →
`loop.weave` (`:419`) → intercept audit (`:437`) → `env.bind_archivers` (`:453`, the last
statement, on the raw environment; again only when a hook archives). Your Loop's
observation demand goes in **unnarrowed**: an agent may narrow its own half per mode, but
"cannot excuse itself from a fragment the Loop needs"
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

Three knobs sit together on `ReactLoop` (`react.py:242,246,253`):

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
raw_output=...)` (`core/loops/errors.py:31-48`); the Loop renders it:

```python
# react.py:87-99
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

`react.py:570-571` writes the rejected reply and the feedback into memory *before* deciding
whether to retry. So the in-step retry and the next step read **exactly the same two
messages** — the only difference is whether a new observation follows. If they differed the
model could tell which situation it was in, and the in-step path would be training on a
prompt shape the cross-step path never produces. Do not "improve" one of them in isolation.

One more convention, and it is a convention rather than something the design doc settles:
in-step retries are invisible to the cross-step streak (`react.py:589-593`) — a step either
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
`tests/test_core/test_loops/test_abort_handler_hygiene.py` AST-scans every module under
`spectragent/core/loops/` — recursively since M31, so the restorer package
(`core/loops/restore/`) is covered, and a meta test pins that `restore/prefix_replay.py` and
`branch.py` are in the scanned set — and fails on any `try` with a blanket
`except Exception` that does not carry one of the two accepted shapes. It constrains the *source*, not call sites,
so your loop inherits the rule on the day you create the file — and the test survives being
refactored into a base-class helper later. `:248` guards against the rule going vacuous.

Third answer, allowed and deliberately ugly: `# RunAbort-exempt: <reason>` on the `except`
line, for a handler that genuinely cannot be vetoing anything (teardown, logging).

### 3.3 The two shapes

**(a) Handle inline** — observe (`react.py:342`), prompt (`:387`), llm (`:454`), parse
(`:544`). `except RunAbort as exc:` **above** the blanket handler (Python matches top-down,
so position is load-bearing, not style), call `await self._note_abort(phase=..., exc=exc,
recorder=..., observation=..., step_idx=..., step_records=...)`, then bare `raise`. Pass
`llm_output=` / `token_usage=` if the phase has them — for a parser veto the refused reply
*is* the evidence (`react.py:544-563`).

**(b) Capture and re-raise** — the per-action loop only (`react.py:682-687`, resolved at
`:713`). The blanket handler assigns to `act_error` and breaks, because the failure must be
attributed to one action among several; the ordinary (richer) `StepRecord` is built,
`_surrender`ed, and only then `if isinstance(act_error, RunAbort): raise act_error`. **Do
not call `_note_abort` here** — the record is already written and you would double it.

**The step a failing run ends on can fail to record, and that must not replace the
outcome.** The sink is woven: `TraceWriter`'s blocking `after` advice on `record_step`
raises when the step's archive collection fails or the disk is full — and a page broken
enough for a guard to veto is exactly when capture breaks too. If that error leaves the
Loop instead of the veto, `AgentRuntime.execute`'s `isinstance(exc, RunAbort)` sees an
ordinary exception, `raise_on_error=False` swallows it, and the sweep starts its next
task; an `_abort` result likewise names the recorder's error instead of the root cause.
So every surrender on a step after which the run is over *because it failed* — both
abort shapes, `_abort`, a failed action, the last tolerated parse error — goes through
`ReactLoop._surrender_failing`, which logs the recording failure and returns it: the
veto is re-raised with it attached as a note (`_note_unrecorded`), the error result
appends "recording this step failed too: …" (`_unrecorded_suffix`). A `RunAbort` the sink
raises itself still propagates. On every other step — the run would go on, or succeed —
a recording failure still ends the run (fail closed). Nothing is lost by not raising it:
the recorder already knows (TraceWriter keeps the step's calls owed, commits them as
trailing at close-out and closes the trace out as `error`, as far as the disk lets it).
When the disk is still full at the close-out, that second failure is the runtime's to
rank, not the Loop's: `AgentRuntime.execute` logs it and adds it to the veto as one more
note (§3.4). A new Loop owes the same split.

**A cancellation that cuts that hand-over off must carry the outcome.** The hand-over
can take a while — `TraceWriter` pulls the environment's evidence there, of the very
page the guard refused — and the run's own timeout can land in it. The Loop must not
swallow the cancellation (it may be Ctrl-C, or a caller's own `asyncio.timeout`), but
re-raising it bare loses the outcome: `asyncio.wait_for` turns it into `TimeoutError`,
and `AgentRuntime.execute` returns an ordinary timeout result whatever `raise_on_error`
says — the sweep starts its next task against the site the guard refused, and nothing
says a guard said stop. So `_surrender_vetoed` and `_end_failing` catch
`asyncio.CancelledError`, call `carry_outcome(cancelled, outcome)`
(`core/loops/errors.py`: a `RunEnded` holding the veto or the terminal result goes to the
head of the cancellation's `__context__` chain) and re-raise the same object; the
runtime reads it back only for its own timeout (§3.4). The inline shape gets the veto
for free — awaiting inside `except RunAbort` makes it the cancellation's context — but
the capture-and-re-raise shape does not: there the veto is only a local, so carry it
explicitly. A terminal result has to be carried explicitly in every shape: `_end_failing`
builds it in the handler, without the "recording failed" suffix, and the runtime appends
what cut it off. And log the veto *before* the hand-over (the act phase does), so it is
on record however the hand-over ends. A new Loop owes this too: every await between
deciding a failing outcome and leaving the Loop either carries that outcome on a
cancellation or cannot be cancelled.

The prompt phase is the one asymmetry: it catches `RunAbort` and *nothing else*
(`react.py:372-397`). An exception out of the agent's own `build_react_prompt` is a bug in
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
| `ReactLoop._execute` | record the interrupted step, then let it fly | `react.py:342/387/454/544/713` |
| `AgentRuntime.execute` | does it travel past `raise_on_error=False`? | `runtime.py:636-729` |
| `BenchmarkRunner.run` | does the sweep stop? | `benchmark_runner.py:301-328` |

At the runtime layer the order is the point: build the terminal result, close the run out
with `_finish` — `on_run_end`, then `on_run_error` with `partial_steps`, then drain
(`runtime.py:788-805`) — and *only then* raise. The veto arm calls `_close_out_vetoed`,
then re-raises:

```python
# runtime.py:820-826 — AgentRuntime._close_out_vetoed
try:
    await self._finish(result, error=veto)
except Exception as failure:
    logger.exception("AgentRuntime: closing out a vetoed run failed")
    veto.add_note(f"Closing the vetoed run out failed too: {type(failure).__name__}: {failure}")
```

`raise_on_error=False` means "a failed task must not stop the sweep". A veto is the
opposite instruction, so it propagates either way — after the trace is closed out, and
even when that close-out fails (a disk that stays full makes `TraceWriter.on_run_end`
raise): like a cancellation, a veto outranks a close-out that fails under it, and the
failure becomes one more note on it. Every other failure keeps `if raise_on_error: raise`,
and a close-out failure there propagates as it is.

The run's own timeout is the other way a veto used to get lost, and it is not an outside
cancellation. `except TimeoutError` first asks `_outcome_under(timed_out)`: `wait_for`
raises its `TimeoutError` *from* the cancellation it delivered into the Loop, and when
that cancellation carries an outcome (`carried_outcome`, §3.3) the run had already
ended. A carried veto is closed out as vetoed — `agent_done="error"`, an `error` that
names the veto and says the timeout expired while the vetoed step was being recorded,
the timeout as a note on the `RunAbort` — and re-raised whatever `raise_on_error` says;
a carried terminal result is closed out and returned with the same remark appended to
its `error`. Only a run cut off while still under way gets `agent_done="timeout"`. An
outside cancellation never reads the chain: it is re-raised unchanged, as before.

At the sweep layer, `run_task` re-raises `RunAbort` first (`benchmark_runner.py:409-419`)
while still absorbing ordinary failures, and `run` breaks out, records
`BenchmarkRunSummary.aborted` naming the task (`:132-155`, `:304-317`) — **and then still
evaluates the outcomes that finished** (`:318-320`). The guard said stop, not destroy the
evidence; under Record Once, Evaluate Many those traces are complete and worth scoring.
Without the `aborted` field a short sweep is indistinguishable from a small one.

This is also why `RunAbort` derives from `Exception` and not `BaseException`
(`core/loops/errors.py:69-79`): a `BaseException` would skip `_finish` and destroy exactly
the WAL commit the abort exists to preserve.

Known gap, so you do not rediscover it: `AgentDoneStatus` (`schemas/_types.py:15-20`) has
only done/max_steps/timeout/error. A finished veto reports `agent_done="error"` with
`RunAbort` in the error string, so an evaluator selecting vetoed runs is string-matching
(`tests/test_core/test_benchmark_runner.py:453`). The evidence is in the trace; the label
is not in the artifact.

### 3.5 What is deliberately still ReactLoop-local

`_note_abort` (`react.py:932-988`) and its per-phase bookkeeping live on `ReactLoop`, not
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

`EnvironmentBase.observe` (`core/environments/_registrar.py:517-577`) holds a monotonic
counter, increments it on every call, and announces `begin_observation(cycle)` to each
participating capability (`core/capabilities/_registrar.py:236-259`) before resolving
anything. Capabilities run concurrently; one capability's own fragments run sequentially
because they share a capture.

**`observe()` is not a getter and is not idempotent.** Defect F11 was a step being served
the previous step's screenshot byte for byte, because the browser held one capture until
the next `execute_action` and a step that ran no action (a reply that failed to parse)
reused it. The fix made the caller's `observe()` *be* the boundary.

The consequence for any Loop that re-asks the model inside a step is blunt: **an in-step
retry must not open a new cycle.** If it did, the model would be answering a follow-up
question about a screenshot it was never shown — F11 from the other side. ReactLoop's retry
therefore re-reads `memory.get_context_messages()` and nothing else (`react.py:582`; the
reasoning is in the comment at `:408-419`).

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
# react.py:253-265 — the default appears twice, and that is this file's convention
parse_retries_per_step: Annotated[
    int, Field(default=0, description="...")
] = 0
```

- Pick a default that is a **no-op**, so no shipped YAML changes behaviour on upgrade.
- Add the field to the Loop class docstring's config list (`react.py:223-230`) — the only
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
  record. Model on `test_react_run_abort.py::TestAbortOnEveryPhase` (`:293-449`).
- Every phase again, with a sink that raises on the step the run ends on: the veto still
  escapes (the recording failure as a note), an error result still names its root cause,
  and on an ordinary step the recording failure still propagates. Model on
  `test_react_run_abort.py::TestARecordingFailureNeverReplacesTheOutcome`; the
  end-to-end version with a real archive collection is
  `test_trace_writer_archive.py::TestThroughTheRuntime::test_a_veto_at_a_step_whose_collection_fails_is_still_a_veto`,
  and the one where the close-out fails too (a disk that stays full) is
  `::test_a_veto_survives_a_disk_that_stays_full_through_the_close_out` — that half is the
  runtime's (`test_run_lifecycle.py::TestTheCloseOutRunsOnce::test_a_vetoed_run`), so a new
  Loop inherits it by raising `RunAbort` and nothing else.
- Every phase a third time, with a sink a cancellation cuts off on the step the run ends
  on: the cancellation escapes unchanged and `carried_outcome` reads back the veto, or the
  terminal result naming its root cause; on a step the run goes on from, or succeeds on,
  nothing is carried. Model on
  `test_react_run_abort.py::TestACancelledRecordingCarriesTheOutcome` (it also pins that
  the act-phase veto is logged before the hand-over). The runtime half is
  `test_run_lifecycle.py::TestAnOutcomeOutranksTheRunsOwnTimeout`; end to end, with the
  run's own timeout landing in a stalled archive collection,
  `test_trace_writer_archive.py::TestThroughTheRuntime::test_a_veto_whose_step_the_runs_timeout_cuts_off_is_still_a_veto`
  and `::test_such_a_veto_stops_the_sweep_when_the_timeout_cuts_its_step_off`.
- Ordinary failures still return a terminal `AgentResult` with an honest `env_status` —
  `"failed"` only when the environment is what broke; a 429 is `"success"`
  (`react.py:503-523`).
- If your loop re-asks inside a step: the cycle test from §4.
- The Action seam AGENTS.md §2.5 names: negotiation matches *names*, not parameter
  contracts. A loop that emits actions must produce the params `descriptor().params`
  declares — put a test on it.

## 7. What M31 added: loop inputs, one run and one step, terminal observation, restore

`docs/architecture_design.md` §4.1 ("Loop 输入", "一个 run 与一步", "终止观测",
"`BranchLoop`") is the design; this is where each piece lives and what a new Loop inherits.

- **Per-task Loop inputs** (`AgentLoopBase.__loop_inputs__: Mapping[str, LoopInput]`,
  `core/loops/_registrar.py`). Declare what a task must hand your Loop
  (`{"checkpoint": LoopInput(Checkpoint)}` on `BranchLoop`); `check_loop_inputs` refuses
  unknown, missing and mistyped keys in one `IncompatibleLoopInputError`, at
  `AgentRuntime.prepare(loop_inputs=…)` (right after slot validation, before negotiation)
  and again in `weave()`. The checked inputs reach `_execute` as the read-only
  `ctx["loop_inputs"]` — never as attributes on `self`. They come from
  `TaskLoader.get_task_context(id).loop` through `BenchmarkRunner` (§4.10). The old
  untyped `**kwargs` channel of `prepare` / `execute` is gone: a misspelled keyword is a
  `TypeError`. A Loop that declares nothing (`react`) refuses every input, so a checkpoint
  handed to the wrong loop fails at `prepare()` instead of running from scratch.
- **One run, one step.** `LoopRun` (`core/loops/run.py`) is the per-run state every Loop
  keeps (deps from `ctx`, records, action history, model calls, last answer, parse-error
  streak, the token ledger); build it with `LoopRun.from_ctx` and subclass it for more
  (`branch.py::BranchRun`). `ReactLoop._run_step(run, step_idx, *, observation=None,
  restoration=None)` is one ReAct step, returning a terminal `AgentResult` or `None`:
  given `observation` it does not observe again (the restorer's verified obs_t is the
  step's cycle, §4); given `restoration` every record the step builds carries it. ReAct's
  memory protocol is three `ReactLoop` methods — `_cap_actions`, `_remember_rejected`,
  `_remember_reply` — the only copies, used by the live step and by `BranchLoop`'s replay
  alike. Do not add module-level private helpers to `react.py` (the layering handoff,
  `HANDOFF_261010_react_loop_layering.md`): Loop-generic code goes on `AgentLoopBase` or
  in a `core/loops/` module, trace/accounting code in the trace layer.
- **Steps are counted by origin.** `StepRecord.origin` is `policy` / `restored` /
  `terminal`; `AgentMetrics.steps` and the `max_steps` budget count `policy` only, and only
  policy actions enter `AgentResult.action_history`.
- **Terminal observation** (`terminal_observation`, default off on `ReactLoop`, on for
  `BranchLoop`): after done / `max_steps` / the parse-error streak — never after an abort,
  a failed action, a divergence or a veto — one more observe recorded as an
  `origin="terminal"` record. `AgentLoopBase._observe_terminal(env, negotiated_obs, *,
  step)` observes and builds the record (any Loop can use it; an ordinary failure becomes
  an `error_phase="observe"` record, a `RunAbort` propagates); surrendering it and ranking
  a recording failure against the outcome stay the Loop's (`ReactLoop._with_terminal`).
  The terminal result is built before, and its `end_to_end_ms` excludes the observation.
- **The restore phase** (`BranchLoop`): rebuild the context from the source trace by
  replaying every recorded model call through the agent's parser and the three memory
  methods, checking each call's context against the recording; then `async for` the
  restorer (`self.restorer`, a receptor — `RestorerRegistrar`, `core/loops/restore/`),
  which yields one `RestoredStep` per restored step and finally the verified obs_t; then
  `_run_step(run, t, observation=obs_t, restoration=check_t)` and `continue_steps` more.
  A restorer gets a narrow `RestorationContext` (env, `act_fn`, the negotiated
  observation, the source accessor, *t*) — no model, no memory, no recorder — so it can
  restore only the environment. Vetoes in replay and restore record an
  `origin="restored"` step with `error_phase="replay"` / `"restore"` and propagate;
  a strict divergence ends the run with `RestorationDivergedError`, without a terminal
  observation. Under `strict` a restorer raises at the first failed check — the
  observation's, a retarget's or an action's outcome — before the step's next action runs,
  and it calls `env.verify_restoration` exactly once per restored observation.
- **A Loop subclass with a receptor** — `BranchLoop` is a `@dataclass` subclass of
  `ReactLoop` whose `restorer` is annotated only on a plain mixin (the `AgentReceptors`
  shape) with an in-Python default; see the af-add-a-receptor skill §6 for why, and for
  the PEP 563 trap an inherited `react.py` string annotation sets for `branch.py`.
- **Test seams this adds**: loop inputs refused at `prepare()` before negotiation and
  before any trace directory exists (`test_loop_inputs_channel.py`); the terminal
  observation's firing and non-firing paths (`test_react_terminal_observation.py`); a
  restorer's call order, one cycle per observe and its narrow context
  (`test_restore_prefix_replay.py`); a branch's context identity with its source for every
  browser agent (`test_branch_context_identity.py`); and, in a real Chrome, 100% prefix
  verification, identical terminal screenshots and branch isolation
  (`test_core/test_capabilities/test_browser_branch_live.py`).

## 8. What M32 added: optional slots consumed by others, one builder for loop fragments

`docs/architecture_design.md` §4.1 ("可选 slot", "一个 run 与一步") and §4.15 are the design.

- **An optional slot is validated by whoever calls it, right before calling it.**
  `__optional_slots__` lists agent methods used *if present*, and the user need not be the
  Loop: `prepare` is called by `AgentRuntime.seed_messages(task)`; `ReactLoop` also lists
  `format_react_output` (the inverse of `parse_react_output`, the student side) and
  `parse_react_thought` (the teacher side), which only the training export calls.
  `prepare()` never checks an optional slot; the export checks after assembling the
  student and raises the existing `IncompatibleError` with `needed_for=` (the phrase is
  `export/errors.py::needed_for_export`), so a missing optional slot never reads like a
  Loop's required one. Do not invent an `__inverse_slots__` mechanism; a new ReAct-family
  Loop inherits the set.
- **Loop-source fragments have one builder.** `core/loops/_loop_fragments.py::
  loop_fragment_builders(*, task, step, max_steps, today, history, prev_step)` returns the
  four `loop.*` builders `populate_loop_fragments` calls; `ReactLoop._loop_fragment_builders`
  hands it the live run's values, `BranchLoop` the budget its source recorded at the
  checkpoint (`pinned_step_info(recorded, today_override=...)`: the recorded `max_steps` and
  `today`, `today_override` winning), and the export's student renderer the values each
  recorded step was shown. A Loop that needs different values passes different arguments —
  never a second copy of the construction.
- **The memory protocol has a third user.** The export writes each earlier assistant turn of a
  student context through `ReactLoop._remember_reply(memory, text, [])` — the same method the
  live step and `BranchLoop`'s replay use — and seeds each context segment with
  `AgentRuntime.seed_messages`, never by calling the agent's `prepare` itself.
