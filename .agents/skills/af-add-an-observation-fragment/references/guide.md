# Adding an observation fragment

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

An **observation fragment** is one independently registered piece of what the
agent perceives. `Observation` is not a god-object: DOM, screenshot, terminal,
tool result, task, step info are each their own registered class, negotiated
separately, traced separately, and assembled at run start into one dynamically
built `NegotiatedObservation` so the agent reads `obs.dom.element_tree`.

Read `docs/architecture_design.md` §4.4 before a non-trivial change — it is the
source of truth, this skill is the path through the code.

Neighbouring skills: `af-add-a-capability` if you are creating the whole
environment domain rather than one fragment of an existing one;
`af-add-an-action` for the symmetric axis; `af-regenerate-and-verify` once you
have touched `__wiring__` or an `Annotated` constructor field.

## 0. The short version

Adding a fragment is **one class plus one resolver**. For the browser capability
it is one class plus a watchdog; the capability itself does not change.

1. Declare the fragment: subclass `FragmentBase`, set `__registry_key__ =
   "<source>.<local_id>"` and `__source__`.
2. Produce it from a Resolver inside the capability that owns the data.
3. Declare it in an agent's or loop's `__wiring__` — **relative**, no prefix.
4. If it costs anything measurable, give it a budget surface and a field that
   reports when the budget cut.
5. If you wrote a new capability, add a `CaptureHarness` to
   `tests/_helpers/capability_contract.py`.

Everything below is why each of those is shaped the way it is.

## 1. The fragment class

Register by subclassing. The registrar lives at
`spectragent/schemas/observation/_registrar.py:26`:

```python
ObservationRegistrar = create_registrar(
    "observation", ObservationFragment,
    discriminator_field="fragment_id",
    key_separator=FRAGMENT_KEY_SEPARATOR,   # "."
)
```

A concrete fragment — `spectragent/core/capabilities/browser/observations.py:383`
is the canonical one:

```python
class DOMObs(FragmentBase):
    __registry_key__ = "browser.dom"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    element_tree: Any = None
    selector_map: dict[int, Any] = Field(default_factory=dict)
    budget: DOMBudgetReport | None = None
```

### `fragment_id` is a real field, not a property

`FragmentBase.fragment_id` (`schemas/observation/_registrar.py:93`) defaults from
`__registry_key__` via a `model_validator(mode="after")` at line 118 — a
per-subclass default is not expressible as a field default, and the value has to
survive a JSON round trip. It **used to be a `@property`**, which meant
`model_dump()` omitted it and every fragment reached the trace anonymous. Do not
"simplify" it back.

### `__source__` decides who fills the slot

- `"env"` — a Capability resolver produces it. `EnvironmentBase.observe` fills it.
- `"loop"` — the Loop produces it after `observe()` returns, through
  `populate_loop_fragments` (`spectragent/core/loops/_loop_fragments.py`).
  Examples: `loop.task`, `loop.step_info`, `loop.history`, `loop.action_result`
  in `spectragent/schemas/observation/fragments.py`. Registering one is not enough:
  negotiation offers a loop-source fragment only from a Loop whose `__loop_fragments__`
  declares it (`AgentLoopBase` defaults to `LOOP_FRAGMENT_KEYS`, the keys
  `loop_fragment_builders` builds). Add the builder and the declaration on every Loop that
  builds it; an agent requiring it from a Loop that does not is refused at `prepare()`,
  naming the fragment and the Loop.

Loop-sourced slots are generated as `Fragment | None` with `default=None` so
`env.observe()` can construct the model before the Loop fills them
(`schemas/observation/dynamic_model.py:113`). Env-sourced slots are required.

### Where the class file lives

A fragment owns its own JSON shape. `DOMObs` cannot be encoded without knowing
`DOMElementNode` is a cyclic graph, so it lives inside the package that owns that
type, not in the shared schemas package. `spectragent/schemas/observation/` names
no field of any domain — keep it that way.

**A fact every platform has gets an abstract, platform-neutral base there.** The
precedent is `CaptureStateFragment` (`schemas/observation/capture_state.py`,
`__abstract__ = True`; `docs/architecture_design.md` §4.4 "跨平台采集状态"): four facts —
settle, foreground change, pointer, failed visible resources — each with its own missing
semantics, in words that name no page, tab or URL. Each capability registers its own
concrete subclass under its prefix (`browser.capture_state` is `BrowserCaptureStateObs`
in `browser/observations.py`; a desktop or Android one later inherits the same base), and
may project facts from records its capture already keeps instead of measuring them twice
(`settle_state` reads the capture's `settle`, `foreground_change` its `focus_moved`).
Agents declare the relative key (`capture_state`, optional) and render it through one
shared renderer (`agents/rendering/capture_state.py::render_capture_state`) that reads fields
structurally and imports no capability class, so a cross-platform Agent stays free of
browser types. An Agent switch that drops the fragment narrows the Agent's *demand*
(`capture_state_demand`, through `bounded_demand`) rather than hiding it after capture —
and because that changes negotiation, the switch is part of branch and export identity.

**Sidecar metadata on an existing fragment** is the other shape, for facts that must come
from the same instant as the pixels. `ScreenshotObs` carries three: `source_url` (the
document read in the same cycle), `cursor` (`None` = off; a cursor without a position =
on but unknown) and `media` (`MediaState`, `browser/media.py`: every `<video>` /
`<audio>` read just before and just after `Page.captureScreenshot` by the same resolver,
each read bounded by the capability's `media_state_timeout_s`). Every knob-bounded probe
of a batched capture shares what the capture's fixed bounds leave of its 30 s event: the
browser refuses `capture_state_timeout_s + 2 × media_state_timeout_s` at or past
`CAPTURE_PROBE_ROOM_S` (4 s), so a new probe with its own budget must join that sum and its
test (`test_browser_observation_bounds.py`). A sidecar read must never
fail the fragment it rides on: a read that runs out or errs is recorded inside it as
`unknown` with a `detail`, and `None` means "not measured" — `media` is then left out of
the trace JSON (`trace_json_exclude`), so a screenshot without it writes what it always
wrote. Prefer a separate fragment when a measurement could otherwise degrade a required
one (that is why `capture_state` is not screenshot fields), and sidecar metadata when the
fact is meaningless apart from the pixels.

Three methods carry a fragment to the trace and back, all on `FragmentBase`
(`_registrar.py:157`, `:165`, `:173`):

- `trace_binary() -> (suffix, bytes) | None` — binary payloads must **not** be
  base64'd into the JSON record. `ScreenshotObs` (`observations.py:578`) returns
  the real bytes and its suffix comes from the bytes' own header, so a JPEG run
  writes `000_browser.screenshot.jpg`.
- `trace_json_exclude() -> set[str]` — keep those bytes out of the sidecar.
- `from_trace(payload, artifact) -> Self` (classmethod) — the documented inverse
  of the two: rebuild the fragment from its sidecar (the parsed JSON) and the
  artifact bytes. It is what `RunTrace.observation_fragment` calls, so what an
  evaluator reads back is the type that was written. The default validates the
  sidecar and **refuses** an artifact rather than dropping it: bytes there mean
  the class overrode `trace_binary` without overriding `from_trace`, and the
  sidecar alone would read back as a fragment with its payload silently missing.
  So override them together: `ScreenshotObs.from_trace` (`observations.py:606`)
  puts the bytes back into `image_bytes`. A fragment whose JSON shape is its own
  (a `field_serializer` encoding a graph) owns the decoder for that shape too:
  `DOMObs.from_trace` (`observations.py:465`) gives back a `DOMState` and
  `DOMElementNode`s, so no reader ever writes a second decoder. That decoder plans from
  the dataclass annotations, as the encoder does; a traced field annotated with a shape
  it has no rule for (it knows scalars, `Any`, dataclasses, `X | None`, scalar unions,
  `Literal`, `list`, `tuple`, `set` / `frozenset`, `dict`) is a `TypeError` naming the
  annotation when the plan is built, so teach `_GraphDecoder` the shape first. Report
  every failure as a `ValueError` (pydantic's `ValidationError` is one); the reader
  turns it into a `TraceSchemaError` naming the file. Write whatever your encoding
  still loses in the class docstring (`DOMObs`'s lists four losses), and do not
  invent it back on read.

Plus `summary() -> str` — one line for `StepRecord.observation_summary`. Empty
means "skip me". This is what makes a trace skimmable without loading megabytes
of DOM per step.

### Getting it registered

`ObservationRegistrar.get(key)` has to find the class, so the module must be
imported. The browser fragments are imported by the watchdogs that produce them
(`dom_watchdog.py:49`, `screenshot_watchdog.py:39`,
`browser_meta_watchdog.py:22`) — the resolver importing its own fragment is the
natural registration point. Config generation runs `discover("spectragent")`
(`spectragent/scripts/generate_all.py:113`), which walks the package.

## 2. The Resolver pattern

A Capability does not know about fragments one at a time. It declares what it
provides and routes; the **Resolvers** inside it produce the data.

For the Browser Capability the Resolver is a **Watchdog**
(`spectragent/core/capabilities/browser/watchdogs/base.py`). `BrowserWatchdog`
adds three things to `agent_cdp`'s registrar:

- `level` — `"tab"` or `"browser"` scope.
- `provides: ClassVar[frozenset[str]]` (`base.py:174`) — the fully-qualified
  fragment keys it resolves. Empty for behavioural watchdogs (action, security,
  popups, crash) which observe nothing.
- `build(ctx: WatchdogContext)` — construction from one context object, never a
  bespoke lambda and **never** a `Registrar.get(name)` call inside the resolver
  (`tests/test_core/test_capabilities/test_receptor_hygiene.py` Rule 2 fails you
  for that — the configured object must be the one that runs).

`BrowserCapability.declared_fragments` (`capability.py:694`) is
`fragments_provided_by(self._watchdog_classes().values())`
(`watchdogs/base.py:232`). **That is the whole reason the capability does not
change when you add a fragment**: it derives its `provides` set from the
watchdogs the profile leaves installed. It also means the `vision_only` profile,
which excludes the `dom` watchdog, genuinely cannot serve `browser.dom` — an
agent that requires it fails negotiation before Chrome launches, rather than
receiving an empty tree every step.

The smallest complete example is
`spectragent/core/capabilities/browser/watchdogs/browser_meta_watchdog.py` — 97
lines, one `provides`, one `build`, one handler:

```python
class BrowserMetaWatchdog(BrowserWatchdog):
    provides: ClassVar[frozenset[str]] = frozenset({"browser.browser_meta"})

    async def _on_browser_state_request(self, event) -> BrowserStateSummary | None:
        if not (event.fragments & self.provides):
            return None          # this cycle did not negotiate my fragment
        ...
        return BrowserStateSummary(fragments={"browser.browser_meta": BrowserMetaObs(...)})
```

`BrowserStateSummary.fragments` (`watchdogs/events.py:23`) is keyed by
fully-qualified fragment key. **The producing watchdog builds its own fragment;
the capability only merges the dicts**, so it needs no per-fragment knowledge and
has no `if/elif` chain. The `event.fragments & self.provides` guard is what makes
a fragment nobody negotiated cost nothing.

To add a fragment to the browser: new `FragmentBase` subclass in
`observations.py`, new `BrowserWatchdog` subclass declaring `provides` and
returning it in a `BrowserStateSummary`. Nothing else.

For a capability that is not the browser, "Resolver" is whatever plays that role
in your backend; `CapabilityBase.declared_fragments`
(`core/capabilities/_registrar.py:120`) defaults to reading
`__wiring__["observation"]`, and you override it if your set is dynamic.

## 3. How fragments become `obs.dom`

Three-party negotiation runs once, before the run:
`negotiate_observation(agent, loop, env)` at `spectragent/core/runtime.py:298` →
`spectragent/core/negotiation/observation.py:166`. It produces an
`ObservationNegotiationResult` (`observation.py:108`) carrying
`active_fragments`, `env_sourced`, `loop_sourced`, `fragment_bindings` and
`required_fragments`, all fully qualified.

`build_observation_model_from_result` (`schemas/observation/dynamic_model.py:126`)
turns that into a Pydantic model via `create_model("NegotiatedObservation", ...)`
at line 123. The attribute name is the **local segment** of the key —
`fragment_field_name` (`schemas/observation/_registrar.py:34`) does the `rsplit`,
in one place, for the three callers that need the answer. So `browser.dom`
becomes `obs.dom` and the agent's access pattern does not depend on which
capability won.

The model is built afresh from the current negotiated classes and optional-failure
shape. There is no module-level model cache: independent runs must not retain
registry classes or share mutable model state through a process-global cache.

## 4. Fully-qualified vs relative keys

This is the rule agents and capabilities get wrong in opposite directions.

| Where | Form | Why |
|---|---|---|
| `__registry_key__` | `browser.dom` | Registration is global; two capabilities may both offer a `screenshot`. |
| Capability `provides` | `browser.dom` | The environment routes on the prefix. |
| WAL filename | `000_browser.dom.json` | A trace must say *which capability* produced it. |
| `fragment_id` field | `browser.dom` | Same reason, carried in the data. |
| Agent / Loop `__wiring__` | `dom`, `screenshot`, `task` | The agent does not know which capability this run assembled. |

`fragment_path` (`spectragent/core/trace/encoding.py:77`) names observation
files after `fragment_id`, explicitly not after the negotiated local name:

> Not the negotiated local name: that is a per-run artefact of which capability
> won negotiation, so `000_screenshot.json` would mean a different thing in a
> browser run and a desktop run.

Binary artifacts land beside the sidecar as `observations/{step:03d}_{key}{suffix}`
(`artifact_path`, `encoding.py:87`). The sidecar is the fragment's own JSON and
nothing else; it holds no pointer to the artifact. The manifest's per-step fragment index
(`FragmentFiles.artifact`) is the only pointer, and `from_trace` is handed the
bytes.

The relative→qualified binding happens exactly once, in `resolve_demand`
(`spectragent/core/negotiation/_base.py:265`), and the result is
`demand.bindings`. Everything downstream — routing, the generated model, the
trace — sees only the full key.

### Two providers of one relative key raises

`resolve_demand` at `_base.py:307` raises `AmbiguousKeyError`
(`_base.py:213`; the observation subclass is `AmbiguousFragmentError`,
`negotiation/observation.py:79`) naming **both candidates**:

```
Capability-relative observation fragment key 'screenshot' is provided by more
than one capability in this environment: ['browser.screenshot',
'desktop.screenshot']. ... Name the one you mean by writing its fully-qualified
key in __wiring__ (one of [...]).
```

Required and optional alike — an optional key with two providers is a config the
user must decide, not one to drop. Writing `browser.screenshot` in `__wiring__`
pins it.

A separate failure catches the pinned-both case: `DuplicateFragmentNameError`
(`negotiation/observation.py:85`) refuses two active fragments whose **local**
names collide, because the generated model has one attribute per local name.
This is why a desktop capability that wants a screenshot registers
`desktop.screenshot` as its own fragment rather than reusing
`browser.screenshot` (`observations.py:534`) — and why stub fragments in tests
must be named distinctly.

The declaration form for an agent (`spectragent/agents/browser_use/agent.py:285`):

```python
"observation": (
    "observation",
    ["screenshot", "task"],                              # required
    ["dom", "browser_meta", "step_info", "history"],     # optional
),
```

The three-element tuple is `(registry, required, optional)` and it is the
keystone of the whole design — conscribe turns it into `Literal[...]` config
constraints, and negotiation turns it into the active set.

## 5. THE OBSERVATION CYCLE CONTRACT

**Read this before writing a resolver that batches.** It was lifted out of
`BrowserCapability`'s private convention and made a framework contract; it is the
part a new fragment author most needs and the part least visible from the code
you copy.

### The rule

`Environment.observe` owns a **monotonically increasing cycle counter**. It is an
instance attribute (`core/environments/_registrar.py`, set in `__init__`; two
environments in one process count independently — Zero Global State). Per
observation:

1. `observe()` increments the counter — `_registrar.py:396`.
2. **Phase 1**: it calls `begin_observation(cycle)` on **every participating
   capability, before any fragment of that cycle is resolved** (`_registrar.py:402`).
3. **Phase 2**: it resolves the fragments (`_registrar.py:407`).

Between two `begin_observation` calls, every `resolve_fragment` belongs to **one
observation**. A capability that gets several fragments from one round trip
captures once and serves the rest from it, however many times each key is asked
for. The next cycle must re-capture.

The Protocol declaration is `spectragent/core/capabilities/base.py:107`; the
no-op default is `spectragent/core/capabilities/_registrar.py:186`.

### It is the caller's *statement*, not the capability's inference

This is the load-bearing sentence. Before the contract existed, the rule lived
inside `BrowserCapability` as "have I already served this key?" — an inference
standing in for information the caller had all along. It was right for the Loop's
access pattern and wrong for every other caller: a hook or an action reading a
fragment twice silently bought a second scrape, and the trace recorded two
captures for one observation.

**Capabilities must not infer the boundary.** Overriding `begin_observation` is a
statement that you batch; the default no-op is correct if you capture per
fragment.

### Where it came from: defect F11

The browser's cycle cache used to live until the next `execute_action`. A Loop
step that ran no action — a model reply that failed to parse — was served **the
previous step's screenshot, byte for byte**, seconds after the page had changed,
and the agent clicked on what was no longer there. The history is kept in the
docstring at `capability.py:184`.

The corollary, stated bluntly in the Loop: **`observe()` is not idempotent and is
not a getter.** An in-step retry that called it would re-sample, and the model
would be answering a follow-up question about a screenshot it was never shown —
F11 from the other side. `ReactLoop`'s parse-retry re-reads
`memory.get_context_messages()` and nothing else.

### Invalidation has three inputs, not one

Do not read "the cycle number" as the whole staleness rule — an earlier write-up
did, and it invites the next reader to delete a path that matters.
`_ObserveCycleCache` (`capability.py:146`) carries:

| Input | Means | Advances the cycle? |
|---|---|---|
| `cycle` (`:199`), checked in `_populate_cycle` (`:1200`) | "that observation is over" | yes — `observe()` set it |
| `execute_action`'s pop of the acted tab (`:1096`) | "this tab changed under me" | **no** |
| `error` (`:204`) | "this cycle's capture already failed" | no |

`execute_action` invalidates **before** dispatching, so a later
`resolve_fragment` sees post-action state. It does not advance the counter,
because the Loop opens no new cycle between its act and whatever reads next. The
cycle number alone does not cover post-action staleness.

Separately, `EnvironmentBase.act` calls `_invalidate_foreign_captures`
(`core/environments/_registrar.py:291`) after routing an action: it opens a fresh
cycle on **peer** capabilities that declare
`__invalidated_by_foreign_actions__ = True` (`core/capabilities/base.py:34`,
defaulted `False` at `_registrar.py:90`). Default False means "capabilities are
independent worlds" — a `browser.click` must not cost an Android capability an
ADB round trip. Set it True only when two capabilities genuinely share a surface
(a desktop VM whose screen also hosts the browser). For a plain
observe→act→observe loop this changes nothing, because `observe()` opens a new
cycle for everyone anyway; it is the guarantee for reads *between* observes.

### If you override `begin_observation`, you owe the failure path too

Recording the cycle number is not the whole obligation. `Environment.observe`
isolates failures **per fragment**, so after your first fragment raises it asks
you for the next one. A shared capture that does not remember it already failed
re-enters the whole retry budget, once per fragment.

Measured before the fix: one failed `{browser.dom, browser.screenshot}`
observation emitted 4 `BrowserStateRequestEvent`s instead of 2; a realistic
dom+screenshot+meta run spent 6 full DOM extractions and 6 overlay toggle pairs
to fail one step — on exactly the path that was already degraded.

The fix is `_ObserveCycleCache.error`: `_populate_cycle` wraps the capture in one
try/except (`capability.py:1218`), records the exception on the cycle, and
re-raises **the same exception object** for every other fragment of that cycle
(`:1209`). The capture itself moved into `_capture_once` (`:1231`) so every way it
can fail passes through that one `except`. One failed capture produces one reason
across the slice — otherwise the trace would suggest several independent problems
where there was one.

This is pinned by `assert_a_failed_cycle_costs_one_capture`
(`tests/_helpers/capability_contract.py:209`). No happy-path test catches it.

## 6. Multi-capability reality

### Concurrent across capabilities, sequential within one

`observe()` (`core/environments/_registrar.py:344`) groups the cycle's fragments
into one slice per capability (`_observation_slices`, `:457`), then runs one task
per capability (`_resolve_slice`, `:508`), sequential inside each.

- **Concurrent across** because capabilities are independent worlds: a 0.5 s
  browser scrape and a 3 s Android capture cost 3 s, not 3.5 s, and the two
  samples land as close together as the machine allows.
- **Sequential within** because a capability's fragments share one capture.
  Firing them at once would duplicate the capture or race for it.

Two implementation notes you will otherwise rediscover the hard way:

- **`CapabilityBase` is unhashable.** It is a `@dataclass` with default
  `eq=True`, so `__hash__` is `None` and `{cap: keys}` raises `TypeError` at
  runtime — pyright will not warn you. Group by `id(cap)` (`_registrar.py:483`).
- **`asyncio.gather(..., return_exceptions=True)` swallows `CancelledError`**, so
  a cancelled run comes back looking completed with an exception in a list. Both
  helpers here catch `Exception` and *return* it (`_begin_cycle` `:494`,
  `_resolve_slice` `:508`), leaving `BaseException` to propagate.

### `captured_at` on every fragment

`FragmentBase.captured_at` (`schemas/observation/_registrar.py:116`) is
`Field(default_factory=time.time)`, stamped where the fragment is **built** —
inside the capture, for a batching capability — so a fragment served twice from
one cycle keeps the capture's time.

It exists because a browser fragment and an android fragment are **not** from the
same instant, and the agent is handed both as if they were. Without a per-fragment
capture time that skew is invisible in the run and unrecoverable from the trace.
Wall clock rather than `perf_counter`, so it is comparable to
`StepRecord.timestamp`.

**Do not declare `captured_at` yourself — inherit it**, or the contract's
capture-time clause fails.

### Partial failure: required raises, optional records

`observe()` reads `required = frozenset(negotiated.required_fragments)` at
`_registrar.py:394` — **directly, never through `getattr(..., frozenset())`**. A
result without the field would silently mean "nothing is required", which turns a
failed *required* DOM into a recorded failure and lets the run continue on a slot
the reader believes is real data. This invariant fails closed on purpose; the
same rule holds in `build_observation_model_from_result`
(`dynamic_model.py:139`) and in the negotiation projection
(`negotiation/observation.py:234`, which indexes `demand.bindings` rather than
filtering with an `if`).

After the gather:

- `failures ∩ required_fragments` non-empty → raise `ObservationFailedError`
  (`core/environments/errors.py:14`), chained from the first fatal one and naming
  **every** failure of the cycle (every capability has already finished, so the
  message is complete).
- Otherwise each failed optional slot gets a `FragmentFailure`
  (`spectragent/schemas/observation/failure.py:62`) and the observation still
  assembles from everything its peers captured.

`FragmentFailure` is a plain `BaseModel`, deliberately **not** a `FragmentBase`:
no registry key, never negotiates, so `isinstance(x, FragmentBase)` stays a
reliable "this is real data" test. The trace records it under the key that
failed and flags it `failed: true` in the step's fragment index
(`encode_fragment`, `core/trace/encoding.py:174`), because its JSON carries no
discriminator: every field of `ScreenshotObs` / `DOMObs` / `BrowserMetaObs` has a
default, so the same bytes would validate silently as an empty fragment of the
class the key names. `RunTrace.observation_fragment` reads a flagged file back as
the `FragmentFailure`. It mirrors the trace's record of a failed LLM call
(`CallFailure`, `core/trace/models.py:172`) in its two required fields:
`{error, detail}`.

**Never fabricate a value.** A reader of the trace has to be able to tell "the
environment is like this" from "we failed to sample it". Filling the slot with an
empty `DOMObs`, or a defaulted `url=""`, is the framework inventing data — the
exact failure `failure.py` exists to refuse. From your resolver: **raise**. Do
not return `None`, do not return an empty fragment. Zero bytes count as empty:
`ScreenshotWatchdog._capture` raises when `Page.captureScreenshot` returns no image
data. It used to hand over `b""`, which the trace committed as a 0-byte artifact
that its reader then refused, failing every screenshot of the run. The environment
decides what your failure costs by consulting what the run declared required;
softening it in the capability removes that decision from the only place that can make it.
(`_expect_failure` in the contract suite, `capability_contract.py:254`, fails a
harness whose `resolve_fragment` returns after `break_capture()`.)

**The one exception: an optional fact inside a batched capture.** The browser serves
every negotiated browser fragment from one capture, and a capture missing any of them is
re-fired and then fails whole — the screenshot with it (`_capture_once`; a TODO there
records that partial capture of optional fragments may be allowed later). An optional
fragment whose own probe raised would therefore take a required screenshot down with it.
So the `browser.capture_state` resolver (`watchdogs/capture_state_watchdog.py`) never
raises and always answers: a probe that runs out of `capture_state_timeout_s`, errs or
answers with the wrong shape records *that fact* as `unknown` with a short `detail`
inside the fragment. This is not fabrication: `unknown` is a typed gap in the fragment's
own contract, distinct from a measured zero and from `unavailable`. Copy it only for an
optional fragment in a batched capture, with every fact's gap typed; a required
fragment, or one whose whole content failed, still raises. When the entire capture fails,
the optional slot becomes a `FragmentFailure` like its peers, and the renderer says the
facts are unknown this step.

Which slots may carry a failure is derived in one place:
`env_sourced - required_fragments` (`dynamic_model.py:142`), widening exactly
those to `Fragment | FragmentFailure` (`:119`). Required slots stay narrow —
a required failure must not be *representable*, because the answer to it is to
raise.

### Reading an optional fragment — the guard that works

The obvious guard does not. An optional slot holds `Fragment | FragmentFailure`;
it is **never `None`**, and a `FragmentFailure` is truthy:

```python
dom = getattr(obs, "dom", None)
if dom is not None:
    dom.element_tree          # ← raises, on exactly the path the widening survives
```

Test the type instead:

```python
if isinstance(dom, DOMObs):        # or FragmentBase, for "any real data"
    dom.element_tree
```

`FragmentFailure.__getattr__` (`failure.py:96`) raises an `AttributeError` that
names the slot, the capability that owed it, the original error and this guard —
rather than pydantic's bare *'FragmentFailure' object has no attribute
'element_tree'*. It stays an `AttributeError` so `hasattr` and
`getattr(x, name, default)` keep working, and dunder/private names fall through
untouched so `copy.deepcopy`'s `__deepcopy__` probe and pydantic's private-attr
machinery are unaffected.

Making a fragment survivable needs no code: declare it in the **optional** half
of the `__wiring__` 3-tuple.

## 7. The shared contract test

`tests/_helpers/capability_contract.py` + `tests/test_core/test_capabilities/test_capability_contract.py`.

Six capabilities are planned and one exists. The cycle rule used to live only
inside `BrowserCapability`, so the second capability's author would have had to
read that file and reinvent it — and the failure mode when they got it wrong is a
trace that lies. So the rule lives as assertions and **every capability inherits
them instead of reading docstrings**.

Six clauses, at `capability_contract.py`:

| Assertion | Line | Pins |
|---|---|---|
| `assert_one_cycle_is_one_capture` | 99 | two reads of N fragments in one cycle = 1 capture, same objects |
| `assert_next_cycle_resamples` | 119 | crossing a boundary re-captures; no object survives |
| `assert_begin_observation_clears_the_cache` | 140 | cycle 1 reads only key A, cycle 2 reads only key B — the "have I served this key" heuristic passes everything else and fails this |
| `assert_fragments_carry_capture_time` | 180 | `captured_at` is a float inside the real resolve window |
| `assert_a_failed_cycle_costs_one_capture` | 209 | failing a cycle that read N fragments costs what failing one that read 1 costs |
| `assert_foreign_action_policy_is_declared` | 269 | `__invalidated_by_foreign_actions__` is a declared bool, not an assumption |

### Opting a capability in

Write a `CaptureHarness` factory (`capability_contract.py:60`) and add it to
`HARNESS_FACTORIES` (`:357`). `browser_harness` (`:323`) is the model. A harness
needs:

- `capability` — an instance wired to a **fake transport**. A contract test that
  needs a real Chrome is a contract test nobody runs; `browser_harness` reaches
  into `_scopes` / `_default_session_id` and installs a counting stub scope.
- `fragments` — **at least two** keys served from one capture (enforced in
  `__post_init__`, `:88`).
- `captures()` — count **attempts**, not successes. Clause 5 is about a retry
  budget being spent more than once; `browser_harness` counts CDP emissions.
- `break_capture()` — make every capture from now on fail. Required, not
  optional: what a capability does on its bad day is half of this contract.
  Break the *capture*, not `begin_observation` — failing to open a cycle is a
  different clause, checked at the environment level.

`test_every_registered_capability_has_a_contract_harness`
(`test_capability_contract.py:86`) fails the moment a new capability is
registered and names the file to edit.

Two test-writing traps, both paid for already:

- A stub capability must **not** define its own `__init__`: `CapabilityBase.id`
  is an `Annotated` field, and a custom `__init__` hides it from conscribe's
  config extractor, which `tests/test_generated/test_no_dropped_annotated_fields.py`
  fails on. Set attributes after construction — the `prime()` pattern
  (`test_capability_contract.py:130`).
- The capability registry is global and shared with test fixtures. Enumerating it
  at *collection* time goes red when another module registers a stub. Filter to
  `cls.__module__.startswith("spectragent.")` and loop at run time
  (`test_capability_contract.py:97`).

Clauses 1 and 5 assume a capability whose fragments **share** a capture. One that
genuinely captures per fragment should come to this file and change it, not
quietly report one capture per resolve.

## 8. Cost: a budget surface, not an on/off switch

A fragment an agent can only turn on or off is a fragment whose cost the user
cannot manage. Give it knobs.

Two shipped examples:

- **Screenshot**: `screenshot_format` / `screenshot_quality`, `Annotated` scalars
  on `BrowserCapability.__init__` (`capability.py:456`, `:470`), delivered to
  `ScreenshotWatchdog.build` through `WatchdogContext`.
- **DOM**: `spectragent/core/capabilities/browser/dom/budget.py` — four scalars
  (`dom_max_elements` `capability.py:483`, `dom_max_serialized_chars` `:502`,
  `dom_interactive_only` `:521`, `dom_viewport_only` `:535`) grouped into a frozen `DOMBudget`
  (`budget.py:63`) behind the `dom_budget` property (`capability.py:659`).

### The rules, in order

1. **Pick the right home.** A switch that changes how many round trips a
   *capture* spends belongs on `BrowserProfileConfig`
   (`js_click_listener_detection`, `cross_site_iframe_capture`). A switch that
   changes how much the resulting *observation* costs the prompt belongs as an
   `Annotated` scalar on the capability's `__init__`. Without that sentence
   there are two plausible homes and a 50% chance of splitting one concern
   across both.
2. **Scalars, not a strategy**, when there is nothing to swap. Give them real
   constraints (`ge=1`) — a nonsense value must fail Pydantic validation, not the
   run.
3. **Group them into a value object behind a `@property`** so the resolver takes
   one argument instead of N. The scalars stay the config surface; the object is
   the transport. `DOMBudget.is_open` (`budget.py:88`) short-circuits the whole
   pass when nothing is set, so unbudgeted behaviour is byte-identical.
4. **Apply the budget where the data is still structured**, not on the rendered
   string. `DOMBudgetPass` mutates the node graph — clears `element_index`, pops
   the `selector_map` entry — so every registered serializer *and* the action
   layer see the same reduced truth: an element the budget removed is not
   routable by `build_guarded_act`, because the map it would be looked up in no
   longer holds it.
5. **One method that owns its own ordering.** The DOM budget spans
   serialization: element cuts must land before the serializer runs (or the
   string is paid for and discarded) and the length cut can only land after. That
   was once `prune` / `trim` / `report` plus a docstring asking the caller to
   order them — and skipping `trim` meant `max_serialized_chars` was never
   applied *and* never reported. It is now one call taking the other step as an
   argument (`budget.py:139`), used as a single line in the resolver
   (`dom_watchdog.py:196`):

   ```python
   budget = DOMBudgetPass(self._budget).apply(state, self._serializer.serialize)
   ```

### When a budget truncates, the fragment must say so in a field

This is not optional polish. Silent truncation makes **"the page did not have
it"** indistinguishable from **"we cut it"** — the lying-trace class of defect.
`42 interactive elements` in a trace is ambiguous between "the page had 42" and
"the page had 900 and the run paid for 42", and the evaluator reading it months
later cannot tell.

So `DOMObs.budget: DOMBudgetReport | None` (`observations.py:440`), where
**`None` is load-bearing**: it is the encoding of "nothing was cut", so the
field's mere presence is the signal. `DOMBudgetReport` (`budget.py:99`) records
`applied`, `elements_before` / `elements_after`, `nodes_dropped`,
`serialized_chars_before` / `_after`, and feeds `DOMObs.summary()`
(`observations.py:442`) so the step line says it too.

Checklist for a new cost knob:

- [ ] `<X>Report | None` field on the fragment; `None` means nothing was cut.
- [ ] It survives `model_dump(mode="json")` — the WAL writes JSON, and a report
      that only exists in memory is no report.
- [ ] If the cut changes what the **model** reads, tell the model too. The DOM
      length budget appends `... [dom budget: N of M characters shown]`
      (`budget.py:59`). The note is metadata about the cut and is not charged
      against the budget.
- [ ] **Describe the cut exactly.** "Not advertised to the model" is a stronger
      claim than "removed from the selector map", and the serializers do not
      honour the stronger one: a de-indexed scroll container still prints
      `|scroll element|`, a de-indexed iframe still prints `|IFRAME|`. The
      invariant to write down is **unindexed means unreachable**, not "gone".
      Pin the surviving half with a test too.

## 9. Checklist

- [ ] `FragmentBase` subclass with `__registry_key__ = "<source>.<local_id>"` and
      `__source__`.
- [ ] Lives in the package that owns the types it encodes; `field_serializer` for
      anything pydantic cannot encode; `trace_binary` / `trace_json_exclude` for
      bytes; a `summary()` worth reading.
- [ ] `from_trace` inverts what you write: overrode `trace_binary` → override
      `from_trace` to put the artifact back; encoded a graph in a
      `field_serializer` → decode it in `from_trace`. Add the class to `SAMPLES`
      in `tests/_helpers/fragment_samples.py` so
      `tests/test_schemas/test_observation/test_fragment_from_trace.py` round-trips
      it (encode → decode → encode is unchanged);
      `test_every_shipped_fragment_class_has_a_sample` fails until you do.
- [ ] Did **not** declare `captured_at` or `fragment_id` — inherited both.
- [ ] Produced by a Resolver that declares it (`provides` for a watchdog) and
      imports the fragment class, so it registers.
- [ ] Resolver **raises** on failure; never returns `None` or an empty fragment. (Sole
      exception: an optional fragment inside a batched capture records each probe
      problem as a typed `unknown` inside itself, so it cannot fail the batch — the
      `capture_state` precedent in §6.)
- [ ] A fact every platform has: an abstract platform-neutral base in
      `schemas/observation/`, one concrete subclass per capability, one shared renderer.
- [ ] Sidecar metadata on an existing fragment never fails that fragment; `None` means
      not measured and a failed read is `unknown` inside it.
- [ ] Declared relatively in the agent's / loop's `__wiring__` 3-tuple, in the
      required or optional half deliberately — optional is what makes it
      survivable.
- [ ] If the capability batches: `begin_observation` records the cycle *and* the
      failure path records `error` and refuses the rest of the cycle with it.
- [ ] Cost knobs as constrained `Annotated` scalars + a report field whose `None`
      means "nothing was cut".
- [ ] Tests mirror the tree (`tests/test_core/test_capabilities/`,
      `tests/test_core/test_environments/test_observe.py`); a new capability adds
      a `CaptureHarness` to `HARNESS_FACTORIES`.
- [ ] Changed `__wiring__` or an `Annotated` `__init__` param — **including its
      description string**? `spectragent/generated/` is now stale, is gitignored,
      and `tests/conftest.py` only regenerates it when `_composed.py` is
      *missing*. Regenerate — see `af-regenerate-and-verify`.
- [ ] `docs/architecture_design.md` §4.4 still describes what you built. If it
      does not, that contradiction is the finding — surface it.
