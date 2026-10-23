# Adding an action

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

An **Action** is one registered unit: its own class, its own parameter contract, its
own `execute`. It is the symmetric twin of an Observation fragment — same registry-key
shape (`source.local_id`), same relative-vs-qualified declaration rule, same
negotiation. `AGENTS.md` §2.5: *do not restore an asymmetry between them.*

An action is held by a **Capability**, never by an Agent. The agent only declares that
it can scroll; the capability decides how far.

Neighbouring skills: `af-add-an-observation-fragment` (the mirror axis),
`af-add-a-capability` (if the owning capability does not exist yet),
`af-add-an-llm-provider` (the far end of §4), `af-regenerate-and-verify` (§1.6).

## 1. The five edits, and the optional `Effect`

Worked reference: `spectragent/core/capabilities/browser/actions/scroll.py` (every
feature in one file, its `Effect` included). Base class and its docstring example:
`spectragent/core/actions/_registrar.py:44-177`.

### 1.1 The class

```python
@dataclass
class ScrollAction(SettleableAction, ActionBase):      # scroll.py:26-30
    __registry_key__ = "browser.scroll"
```

`ActionBase` carries the conscribe metaclass (`ActionRegistrar.Meta`,
`_registrar.py:36-44`). `__registry_key__` is **always fully qualified** —
`{capability}.{local_id}`. The prefix is what makes `browser.click` and
`desktop.click` coexist, and it is what `EnvironmentBase.act` routes on.
`action_id` returns the whole key; `source` returns the prefix (`_registrar.py:115-123`).

A mixin must precede `ActionBase` in the MRO so dataclass field ordering works
(`actions/_mixins.py:8-11`). `SettleableAction` gives you `post_delay_ms` for free.

### 1.2 Side-effect parameters live on the **instance**

```python
default_amount: Annotated[int, PydanticField(description="Default scroll distance in pixels")] = 300
max_amount:     Annotated[int, PydanticField(description="Per-call cap on scroll distance")] = 2000
```
`scroll.py:32-44`. These are `Annotated[...]` **class** fields, so conscribe emits them
into `spectragent/generated/_composed.py` (`BrowserScrollActionConfig`, `:172-179`) and
a run configures them in YAML:

```yaml
capabilities:
  - capability_id: browser
    actions:
      - action_id: browser.scroll
        default_amount: 600
```
(`configs/browser_use_local.yaml:197-205`.) The agent says "scroll down"; the *run*
says how far. That split is the whole point — an ablation changes scroll distance
without touching the agent or the prompt.

> `_mixins.py:13-24` documents a trap that already cost six actions their
> `post_delay_ms`: a module holding an `Annotated[..., PydanticField(...)]` class field
> must **not** use `from __future__ import annotations`. Under PEP 563 the annotation
> survives as a string, type-hint resolution fails in the subclass module, and
> `__config_annotated_only__` silently drops the knob from the generated config. Action
> modules that declare knobs only in `Params` are fine — `scroll.py` has the future
> import because its knobs come from the mixin's module.

### 1.3 `Params` — the typed, flat, `extra="forbid"` model

```python
class Params(BaseModel):                                # scroll.py:46-64
    model_config = ConfigDict(extra="forbid")
    direction: Literal["up", "down", "left", "right"]
    amount: int | None = None
    index: Annotated[int | None, AddressesElement("browser.dom"),
                     PydanticField(description="scroll inside specific element")] = None
```

This is the **single** source of four things (`ActionBase` docstring in `_registrar.py`):
the descriptor's `params_schema` (what the provider constrains on), the descriptor's
`params` prose (what the prompt shows), the action's fragment dependencies, and what
some params mean. Nothing hand-writes any of them.

- **What a param means is a marker, never a name.** Mark the two params of a point with
  `PointAxis(point, axis)` (`coordinate_x: Annotated[int, PointAxis("coordinate", "x")]`)
  and a param whose value names an element with `AddressesElement(fragment)` (a
  `RequiresFragment` subclass, so the dependency below follows from it). `describe()`
  renders them into `ActionDescriptor.point_params` / `element_params`; Agents read them
  there (through their `ActionSpace`, `core/action_guard.py::executed_descriptor`),
  restoration, the export, the viewer's marks and lookahead read the same markers. An
  `_x` / `_y` pair without `PointAxis` is not a point, and no reader keeps a list of
  coordinate or index actions (`docs/architecture_design.md` §4.5 "参数的含义").

- `RequiresFragment` (`core/actions/params.py:59`) is a value, not a string, so a typo
  is a name error (pinned: `test_action_params_schema.py:339`). Fully qualified — only
  the providing capability knows which `dom` it means (`:239`).
- **You do not choose the dependency level; optionality does.** A *required* param with
  `RequiresFragment` makes the whole action depend on that fragment
  (`requires_fragments()`, `_registrar.py:137-145`). An *optional* one is dropped from
  prompt and schema when the fragment is absent (`describe(fragments=...)`,
  `_registrar.py:164-167`). Vision mode keeps `scroll` and loses its `index`
  (`test_action_params_schema.py:252`); `extract_content` loses its only param and ships
  with an empty list (`:270`).
- `params_view` strips `default` from the schema (`params.py:250`): the default belongs
  to the capability and is applied in `execute`. Publishing it would state a value that
  omitting the param does not give you.

### 1.4 `descriptor()` and `execute()`

```python
def descriptor(self, **context):                        # scroll.py:66-73
    return self.describe(f"Scroll the page or an element "
                         f"(default {self.default_amount}px {self.default_direction}).",
                         **context)
```
Delegate to `describe()` (`_registrar.py:147-177`) — it renders prose and schema from
`Params` together. Pass `hints={"clear": "default=true"}` to surface a YAML knob's
current value in the param text (`test_action_params_schema.py:211,220`). Always
forward `**context`: that is how `fragments` reaches the drop logic, and the runtime
supplies it (`core/runtime.py:337-341`).

```python
async def execute(self, params: dict[str, Any], *, capability) -> ActionResult:  # scroll.py:75
```
One call, one `ActionResult`. The capability is injected whole — an action belongs to
exactly one capability and they ship together (`core/actions/base.py:59-85`). There is
no routing handle: which tab/container/window you land in is capability *state*,
selected by `switch_tab` (or, for the browser, moved by the capability itself at the next
observation to a tab the agent's actions opened, architecture §4.14 "页面新开的标签页") and
remembered. An action never moves the focus as a side effect: every action of one step acts
on the tab the step observed. Return `ActionResult(success=False, error=...)` for a failure;
do not raise.

### 1.5 `Effect` — the output twin of `Params` (optional)

`Params` is what the action takes; a nested `Effect` is what it measurably did
(`docs/architecture_design.md` §4.5 "动作效果"). `ActionBase.Effect`
(`core/actions/_registrar.py`) defaults to `None`: the action reports no effect
(`done`, `screenshot`, `extract_content`). The base class `ActionEffect` and the gap
type `Unmeasured` are in `schemas/action.py`.

```python
@dataclass
class ScrollAction(SettleableAction, ActionBase):
    __registry_key__ = "browser.scroll"
    Effect = ScrollEffect          # core/capabilities/browser/effects.py
```

- **The model.** Subclass `ActionEffect` (frozen, `extra="forbid"`). Its `effect_id` is the
  producing action's own fully-qualified key. Several actions may share one model
  (`PointerEffect` serves `click`, `click_coordinate` and `drag`). Capability-specific
  models live with the capability — the browser's in `browser/effects.py`, outside
  `actions/`, because the executor that measures them builds them too; `schemas/` never
  names a capability field.
- **Gaps are typed, never zero.** Every fact is a value or `Unmeasured`: `"unknown"` (the
  probe failed or ran out of its budget) or `"unavailable"` (it cannot be measured there,
  such as inside another site's frame). A measured zero is a value; keep the three apart.
  Wrap string facts in small models (`Location`, `TabFacts`, `OptionFacts`) so a URL or
  option text that reads `unknown` is never taken for a gap. A fact you did not see is
  `unknown`, not a plausible default: the scroll effect is a measured zero only when every
  container the wheel could reach was already at its edge; when one still had room and no
  move was seen within the budget, `after` and `at_boundary` are `unknown`.
- **`describe()`.** One deterministic English line derived only from the object's typed
  fields: no task judgement, `unknown` / `unavailable` said as such, a sensitive field's
  content and length never said (no fact echoes typed text, and an editable region's text
  never names it). Agents render that line and never the fields
  (`agents/rendering/action_effects.py::effect_line` / `result_facts`), so a cross-platform Agent
  never imports your type. Whether the line reaches the prompt is each Agent's
  `show_action_effects`; the trace records the effect either way.
- **Measure around your own dispatch, within a budget.** Facts are read just before and
  just after this action's own dispatch, so they belong to it alone; the page after the
  whole batch is the next observation's. Probe time is bounded by a capability YAML knob
  (browser: `action_effect_timeout_s`, default 1 s; only probes count, waiting for a wheel
  to land included, the action itself does not), and a probe that runs out records
  `unknown`. Measuring never changes `success` / `error` and never aborts the action. In
  the browser the watchdog handler answers with `effect_facts` and `execute` builds the
  model with `actions/_common.py::effect_from(self, handler_field(event, "effect_facts"))`;
  facts the model refuses are logged as an executor bug and the result goes back without
  an effect.
- **Read-back goes through the action registry.** `ActionResult.effect` serializes whole as
  its concrete type and refuses a bare mapping. `RunTrace` rebuilds it with
  `core/trace/effects.py::restore_effects`: `effect_id` is looked up in `ActionRegistrar`
  and the payload validated with that action's `Effect` — there is no effect registry. An
  unregistered key, an action with no `Effect`, or a payload its `Effect` refuses is a
  `TraceSchemaError` naming the file, never a dict. Renaming an action key or dropping its
  `Effect` therefore makes recorded traces unreadable: regenerate the data, never alias.
- **Tests it owes**: every `describe()` variant (measured, zero, `unknown`, `unavailable`, a
  sensitive field), a trace round trip back to the same type
  (`tests/test_core/test_trace/test_action_effects.py`, the registry scan in
  `tests/test_core/test_actions/test_action_effect_contract.py`), and a live probe case
  where the fact comes from a real page (`test_browser_effects_*_live.py`).

### 1.6 Register it, then wire it

Two edits, both mandatory:

1. `spectragent/core/capabilities/browser/actions/__init__.py` — add the import.
   Importing the package is what triggers metaclass registration; the docstring says so
   at `:1-8`. No `discover()` here.
2. The owning capability's `__wiring__["actions"]` list —
   `spectragent/core/capabilities/browser/capability.py:291-311`. That list *is*
   `provided_actions` (`:623-624`), which is the universe negotiation resolves against.
   A registered action absent from it does not exist to any run.

A YAML `actions:` block **narrows** that list (ablation); it never widens it
(`capability.py:288-290`).

Then regenerate — you touched `Annotated[...]` class fields, so `_composed.py` is now
stale: `uv run spectragent config generate`. See the `af-regenerate-and-verify` skill. (Editing
only `Params` needs no regeneration — `Params` is not a config surface,
`__config_annotated_only__` at `_registrar.py:103`.)

> Older project notes named nine per-layer `generate_config.py` scripts. They no
> longer exist. `find spectragent -name generate_config.py` returns zero; current
> `AGENTS.md` §3.4 documents the single global generator.

## 2. Negotiation: who spells the prefix

Registry keys are always fully qualified. `__wiring__` declarations on an **agent** are
not — an agent writes `click` and negotiation binds it to the one capability in *this*
environment that offers it.

| Step | Where |
|---|---|
| Agent declares `(required, optional)` relative keys | `agent.__wiring__["action"]` |
| `negotiate_action(agent, env)` resolves them against `env.provided_actions` | `core/negotiation/action.py:159-174` |
| Result: `active_actions` (qualified) + `action_bindings` (short → qualified) | `:87-103` |
| `get_action_space(active_actions=..., fragments=...)` narrows descriptors | `core/runtime.py:337-341` |
| `build_guarded_act(...)` returns `(descriptors, guarded_act)` | `core/action_guard.py:30` |

`build_guarded_act` is **the only place a relative name becomes qualified**
(`action_guard.py:39-43`). Its closure (`:81-94`) rejects anything outside
`active_names`, resolves a short name with `resolve_action_name` (the rule's one copy; a
caller that must know what the guard did to an action, such as `BranchLoop`'s replay
check, calls it too and never re-derives it), and rewrites `action.name` **in place** —
not onto a copy, because the caller writes that object into the `StepRecord`, and a copy would leave the trace naming a provider-less verb
(`:87-93`).

Downstream, `EnvironmentBase.act` (`core/environments/_registrar.py:255-289`) partitions
on `.` and **refuses a bare name** outright:

```
Action name must be fully qualified ('source.action_id'), got 'click'
```

Failure modes worth recognising:
- Required action missing → `IncompatibleActionError` at negotiation, naming the YAML
  `actions:` ablation as the likely cause (`negotiation/action.py:33-79`).
- Two capabilities offering the same relative key → `AmbiguousActionError` naming both
  candidates; pin one by writing the qualified key in `__wiring__`. If both are merely
  *active*, both stay routable and only the bare form has no answer — that is a logged
  warning, not an error (`negotiation/action.py:143-152`).

## 3. The seam AGENTS.md §2.5 warns about — check **both** directions

> Negotiation matches *names*, not parameter contracts.

The guard checks the name. `execute` reads a bare dict. Nothing in between compares
them. There are two ways to fall through, and af has a test for one of them.

### (a) Agent → action: does the agent emit what the descriptor declares?

Tested. `tests/test_agents/test_browser_use/test_browser_use_modes.py:483-515` walks every negotiated
descriptor, synthesizes the minimal legal payload from its own `params_schema`
(`tests/_helpers/action_params.py:44`), pushes it through the agent's parser and the
guard, and validates what arrives against the action's real `Params`:

```python
assert set(emitted.params) <= set(descriptor.params)                  # :511-514
ActionRegistrar.get(descriptor.name).Params.model_validate(emitted.params)   # :515
```

Add your action to the run and this covers it — **for required params only**.
`minimal_params` builds `{name: ... for name in required}` (`action_params.py:48-49`),
and the Skyvern probe filters the same way
(`test_skyvern_prompt_action_vocabulary.py:215-219`). That gap is not hypothetical: it
is exactly how defect D3 shipped — `extract_content(goal)` was advertised while
`SkyvernActionItem` had no `goal` field, so Skyvern's extraction was permanently
goal-less, and the prompt-vocabulary probe missed it because `goal` was optional
(`HANDOFF_260730_remaining.md:80-88`). If your new param is optional, the existing
tests will not catch this for you.

### (b) Action → environment: does the implementation honour what it advertises?

**No test exists in this direction.** It has now bitten twice, with the same parameter.

`ExtractContentAction.Params` carried `goal: str | None`, prose "free-form extraction
goal". The value rode the descriptor into the prompt and `ExtractContentEvent.goal` into
the watchdog — where `ActionWatchdog._on_extract_content`
(`watchdogs/action_watchdog.py:1107-1119`) branches on `backend_node_id` and nothing
else. Whole scope, every time. Live: the model asked twice for blogger data and got the
site's legal footer, returning in ~4.7 ms with no model call — that timing is the tell
(`HANDOFF_260916_findings.md:84`, `HANDOFF_260916_next.md:43`). The param is now
deleted, not deprecated; the action's description says out loud that it "does not search
or summarise" (`extract_content.py:1-10,51-56`), and `ExtractContentEvent` carries a
comment saying why there is no `goal` field (`watchdogs/events.py:163-168`).

So for **every param you add**, follow the value all the way through by hand:

1. `Params` field →
2. `execute()` reads it →
3. it enters the capability's transport (for browser: a `BaseEvent` subclass in
   `watchdogs/events.py`) →
4. **the handler actually reads it** (`watchdogs/action_watchdog.py`).

Step 4 is the one that was skipped. A param that dies at step 3 is a promise the prompt
makes and the run breaks, silently and fast.

Also update `_GOLDEN_PROSE` in `tests/test_core/test_actions/test_action_params_schema.py:40`
— a table of the previously-shipped prompt text, so a renderer that formats "correctly
but differently" fails loudly instead of silently rewording Skyvern's and UI-TARS's
prompts. A new action fails `:181-190` until it has an entry. When you legitimately
change one, edit it **with a comment saying why**, or the next reader reads the gap as
drift and puts it back.

## 4. Schema dialect: what your params must survive

The per-run closed action union is compiled into what the model must emit, and it must
survive three routes. This is measured, not theoretical — every rule below came from a
400 that named a protobuf field path (`tests/test_core/test_llm/test_schema_dialects.py:1-23`).

**The union is a plain union of single-key models, where the verb is the field name.**
`spectragent/core/actions/action_union.py:224` builds `{"browser.click": {...}}` per verb and
combines them into `anyOf`. A Pydantic **discriminated** union is unusable on all three
routes, each for a different reason, which is why no single provider fix recovers it
(`test_schema_dialects.py:381-397`):

- **Gemini** rejects `discriminator` and `const`.
- **google-genai** rejects it in its own local pydantic validation, before a request is
  ever made — so it cannot even be probed.
- **Anthropic** rejects `oneOf`, which is what a callable discriminator compiles to.

Pinned by `test_schema_dialects.py:400` (no `discriminator`/`const`/`oneOf`, strict and
non-strict), `:409` (every branch has exactly one property and one `required`), and
`test_browser_use_modes.py:566,652`. Do not "tidy" it into a discriminated union; it
reads better and does not work.

Two more constraints your params can trip:

- **`additionalProperties` inside union branches.** The translated Gemini route 400s on
  a closed `anyOf` branch while accepting a closed root; the native route rejects the
  keyword anywhere, root included. Handled by `SchemaOptimizer.open_union_branches`
  (`core/llm/schema/optimizer.py:423-455`, family-gated at `providers/openai_base.py:344-345`)
  and `strip_additional_properties` (`:400-421`, unconditional at `providers/google.py:324`).
  Tests: `test_schema_dialects.py:116-186` and `:192-212`. OpenAI strict *requires* the
  opposite, so this is family-shaped, not a universal strip.
- **`min_length=1` on a list compiles to `minItems`, which OpenAI's strict dialect
  refuses — defect F12, and the symptom is the first GPT call of every run 400ing on the
  schema.** The Pydantic bound stays (`agents/browser_use/agent.py:682`); the keyword is
  stripped at compile (`optimizer.py:386-398`, implied by `strict=True` at `:112`).
  Tests: `test_schema_dialects.py:215-238`, and `test_browser_use_modes.py:579`, which
  scans the strict payload against a frozenset of every keyword strict refuses
  (`:537-561`) while asserting the bound still raises locally and the *non-strict*
  compile still states `minItems: 1`.

Practically: keep `Params` a **flat object of scalars, string enums, and nullable
scalars**. That is the intersection of the three dialects, and everything outside it
raises `UnsupportedParamsSchema` at `prepare()` — before the environment is even set up
(`action_union.py:73-92`). `test_action_params_schema.py:158` scans the whole registry
for it, so a non-conforming param fails in CI, not on a live run. If you are changing
the compiler rather than the params, that is `af-add-an-llm-provider`'s territory.

## 5. If your action needs an LLM

**Read `docs/actions_that_call_an_llm.md` first.** Short version:

- Go through a `__wiring__` **llm receptor**, never reflection. An action is held by a
  Capability (`capability.actions: list[ActionBase]`), so there is *no agent to reflect
  to* — and reflection would violate Protocol-First, constructor injection and Zero
  Global State at once. `spectragent/agents/_registrar.py:35-55` is the worked receptor
  example; conscribe treats a `__wiring__` entry as a receptor only when the name is
  also an `__init__` param or a class-level annotation. See `af-add-a-receptor` for the
  mechanism in general.
- **Where the receptor is wired *is* the meaning** (doc §4, ratified 2026-09-17). On the
  **Capability** it is environment furniture — identical for every agent under test, a
  constant, not a variable in the comparison (the model Anthropic's server-side
  `web_search` follows). On the **Agent** it is part of the subject under test, and must
  be recorded and compared alongside the agent's main LLM. A leaderboard that hides
  which extraction model an agent shipped is lying.
- Either way, internal calls must be attributed to the action in both trace and usage.
  Unified recording now supports run-owned caller/purpose attribution for explicitly
  supplied models, including prompt helpers. That does not implement general nested
  action planning or automatically discover an action's model: keep this extension's
  receptor and execution contract explicit.

**That general form is deliberately not built.** Do not build it as a side effect of
adding an action. It needs a fifth AOP role, a decision on `ActionBase.execute`'s
signature (16 actions), and a shape for nested calls in the WAL — all framework work,
and doc §5 lists the open questions as explicitly undecided. If you need goal-directed
extraction or the grid-captcha solver, that is the prerequisite, and per AGENTS.md §3.1
the move is to ask, not to ship a compromise.

> Related trap when you do get there: `ActionBase` is a `@dataclass` base
> (`_registrar.py:44`), and conscribe reads `inspect.signature(cls.__init__)` at
> class-creation time — before `@dataclass` synthesises one. Every wiring key therefore
> looks unreceived, conscribe installs a `WiredField` over it, the default is destroyed
> and the generated config field becomes **required**. `BrowserCapability` documents and
> solves this by hand-writing `__init__` (`capability.py:258-269`); an action receptor
> must do the same.

## Browser event execution contracts

A browser action dispatches through `watchdogs.contracts.dispatch_request` and
requires its actual executor's receiver key. Connect executors as `HANDLER` with
an explicit `ResultContract`; connect guards, logging and archive observers as
`OBSERVER`. Waiting only settles deliveries: use `raise_for_status()` and a
business result that proves the operation happened. Allow `None` only for a
handler whose successful return itself establishes completion. Missing receivers,
JS exception details and unmatched dropdown choices are failures. When the action
initiator stops waiting, withdraw queued work with
`cancel_execution(include_running=False)`; never claim running side effects rolled
back. Use the capability's public `action_scope` and focused selector-map query,
not its private dictionaries.

`build_guarded_act` returns an `ActionSpace` and the executor. Preserve the raw
filter (`None` differs from `[]`), final descriptors, allowed names and bindings.
Prompt construction and recording receive independent deep snapshots; mutating a
prompt-side descriptor cannot alter experiment identity or guard execution.

## 6. Before you call it done

```bash
uv run spectragent config generate                              # only if you touched class fields
uv run pytest tests/test_core/test_actions/         # registry-wide scans
uv run pytest tests/test_agents/                    # descriptor prose IS prompt text
uv run pytest tests/test_core/test_llm/test_schema_dialects.py
```

- [ ] `__registry_key__` fully qualified; imported in the capability's
      `actions/__init__.py`; listed in the capability's `__wiring__["actions"]`.
- [ ] `Params` flat, `extra="forbid"`, scalars/enums/nullable-scalars only.
- [ ] If the action reports what it did: an `Effect` model with typed gaps
      (`unknown` / `unavailable`, never 0 for "not seen"), a `describe()` that echoes no
      sensitive content, measurement inside the capability's effect budget that never
      changes the outcome, and a trace round trip test.
- [ ] Every param traced **both** directions — descriptor → agent output model, and
      `Params` → `execute` → event → handler-actually-reads-it.
- [ ] `_GOLDEN_PROSE` entry added (or edited, with a comment).
- [ ] Regenerated if you added a knob, and the knob is reachable from YAML.

One last triage note, from an agent who got 1 failure, then 72, then 0 from identical
source: other streams edit this tree concurrently. Before believing a red full-suite
run, check whether the failing file appears in *your* `git diff`. `ruff format --check`
is also independently dirty here (88-vs-100 column drift) — run `--diff` and confirm no
hunk overlaps your edit rather than "fixing" a shared tree.
