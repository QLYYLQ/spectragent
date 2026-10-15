# Add a wired receptor

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

A **receptor** is a `__wiring__` entry whose owner declares a *slot* for it, so
`ConfigComposer` builds the chosen strategy from YAML — knobs and all — and
`setattr`-injects the **constructed instance** (`core/composer.py:347`). The owner
receives an object. It never receives a name, and never looks one up.

## 0. Why this has its own skill

The same defect has shipped **three times**, and it is silent every time:

| # | Axis | The shape that failed |
|---|---|---|
| 1 | `mouse_trajectory` | An `Annotated[Literal[...]]` scalar plus `mouse_trajectory_overrides: dict[str, Any]`. conscribe drops an untyped dict from the schema entirely — the knobs existed and nothing could set them. A separate `humanize_mouse: bool` sat beside it and silently vetoed it (**defect F4**). |
| 2 | `dom_cleaner` | A strategy *name* on `BrowserProfileConfig`. `DOMWatchdog` did `DOMCleanerRegistrar.get(name)()` — `BrowserUseDom_CleanerConfig` was generated and unreachable (**defect F5**). |
| 3 | `dom_serializer` | The same shape again, fixed only just now. `TextSerializer(max_text_length=200)` and `HtmlSerializer(max_href_length=150)` were unreachable from YAML for the whole life of the layer. |

The symptom never changes: the strategy's config model is generated, YAML validates
against it, the Composer builds a correctly-configured instance — and the consumer
default-constructs a *second* one and runs that. Nothing fails. Nothing warns. You
find out by measuring a run that ignored you.

## 1. Three shapes. Pick deliberately.

**(a) A plain `Annotated` scalar** — when there is nothing to swap. One behaviour,
a number or an enum that tunes it. `screenshot_format`, `screenshot_quality`,
`dom_max_elements` on `BrowserCapability.__init__`. Give it real constraints
(`ge=1`) so a nonsense value fails Pydantic validation, not the run. A cap that can be
lifted is `Annotated[int, Field(ge=1)] | Unlimited` with the word `unlimited`, never
`int | None` (architecture §4.2.1); "not enabled" in a closed set is spelled `none`.

**(b) A strategy *name*** — a `str`/`Literal` field someone downstream resolves
into a class. **Almost always wrong.** It is shape (b) that produced all three
defects above. The moment a strategy has *any* constructor parameter, a name cannot
carry it, and the consumer that resolves the name default-constructs it. Use (b)
only for something that is genuinely a label and never a constructed object.

**(c) A wired receptor** — when the axis has implementations *and* those
implementations have knobs. This skill.

### The generated-config difference, which is the whole proof

In `spectragent/generated/_composed.py`, on `BrowserCapabilityConfig` (grep the
field name — the file is regenerated, so line numbers move):

```python
# receptors — nested discriminated union, required, every knob typed
mouse_trajectory: Union[BezierMouse_TrajectoryConfig, InstantMouse_TrajectoryConfig,
                        LearnedMouse_TrajectoryConfig, WindmouseMouse_TrajectoryConfig] = ...
dom_cleaner:      Union[BrowserUseDom_CleanerConfig, SkyvernDom_CleanerConfig] = ...
dom_serializer:   Union[HtmlDom_SerializerConfig, TextDom_SerializerConfig] = ...

# a __wiring__ entry with NO slot — declaration-only, a name and nothing else
observation: Optional[Literal['browser.browser_meta', 'browser.dom', 'browser.screenshot']] = None
```

`TextDom_SerializerConfig` carries `max_text_length: int = 200`;
`WindmouseMouse_TrajectoryConfig` carries `gravity`, `wind`, `max_step`,
`target_area`, `fitts_a`, `fitts_b`, `min_duration_ms`, `max_duration_ms`. That is
what the union buys you. A `Literal` buys you the name.

conscribe 2.0 exposes the same reflection to the builder and the Composer through
`conscribe.introspect.wiring_slots`, `receptor_slots` and `init_param_names`. A runtime
slot is a constructor parameter or non-`ClassVar` annotation. Negotiation entries use
`{"registry": "observation", "keys": [...], "optional_keys": [...], "declare_only": True}`;
they never get placeholders or stubs. A default-free single-instance receptor remains
required; explicit defaults and optional annotations follow conscribe's documented rules.

## 2. The checklist

Worked example: `dom_serializer`, the most recent one, end to end.

**1. Registrar + Protocol,** in the strategy package's `__init__.py`. Copy
`core/capabilities/browser/dom/serializers/__init__.py`:

```python
@runtime_checkable
class DOMSerializerProtocol(Protocol):          # :16
    def serialize(self, state: DOMState) -> str: ...

DOMSerializerRegistrar = create_registrar(       # :24
    "dom_serializer", DOMSerializerProtocol, discriminator_field="serializer_type",
)

class DOMSerializerBase(metaclass=DOMSerializerRegistrar.Meta):   # :31
    __abstract__ = True
    __registry_key__: str
    @property
    def serializer_type(self) -> str: return self.__registry_key__
```

Registration is the metaclass — **subclassing the base registers the class**, there
is no decorator. Concrete strategies set `__registry_key__` explicitly and take
their knobs as `__init__` keyword params; *those become the generated config fields*.

> conscribe does **not** check Protocol conformance at class creation. A subclass
> missing a Protocol method registers happily and fails with `AttributeError` at
> first use. Adding a member to the Protocol therefore silently breaks every
> duck-typed test double until it is called.

**2. A `default_<thing>()` named constructor** in that same `__init__.py`
(`serializers/__init__.py:42`, `cleaning/__init__.py:77`). Import the concrete class
*lazily inside the function* — the concrete module imports the base from this module.
Do **not** write `Registrar.get("text")()`; that is the shape step 7 forbids. This is
the framework's answer when nothing configured anything, not a configuration surface.

**3. Register the registrar with the Composer** — add it to `_collect_registrars()`
in `spectragent/scripts/generate_all.py:40-97`. **This is the step people skip.**
A registrar the Composer does not collect degrades the field from the nested
discriminated union to a bare discriminator — verified by dropping
`dom_serializer` from the dict and regenerating in memory:

```
collected:     dom_serializer: Union[HtmlDom_SerializerConfig, TextDom_SerializerConfig] = ...
not collected: dom_serializer: Literal['text', 'html'] = ...
```

Every knob is gone from the schema. `mouse_trajectory` shipped uncollected for a
round; `generate_all.py:74-78` keeps the note. For a *brand-new* axis this failure is
silent — no YAML names it yet, so nothing goes red and you never learn the knobs
existed. (Once a YAML block *does* name it, the degraded `Literal` rejects the block
and `tests/test_core/test_composer/test_shipped_config_integration.py` goes red. The
older "a typo silently falls back to the default" phrasing in
`generate_all.py:74-78` and `docs/architecture_design.md:1996` describes the
pre-receptor era, when the axis was a plain scalar; with a `__wiring__` entry the
`Literal` does reject a typo. The knob loss is the part that is still true.)

**4. The `__wiring__` entry** on the owner
(`core/capabilities/browser/capability.py:272` for the dict, `:357` for this entry):

```python
"dom_serializer": ("dom_serializer", ["text", "html"]),
```

`(registry_name, allowed_keys)`. The attribute name is the left side; the registry it
draws from is the right. A bare string (`"backend": "browser_provider"`, `:362`) means
"any registered key".

**5. The annotation-only class attribute** (`capability.py:372`):

```python
dom_serializer: DOMSerializerProtocol  # type: ignore[misc]
```

An annotation-only slot keeps the YAML receptor required; an explicit class default
changes the generated default. conscribe 2.0 preserves class and dataclass defaults. If the owner exposes the name as a `property`, put the annotation on a
mixin instead (`_BrowserReceptors`, `capability.py:239`; same split as
`agents/_registrar.py::AgentReceptors`).

**6. The in-Python default,** assigned in `__init__` over the descriptor
(`capability.py:591`): `self.dom_serializer = default_serializer()`. The bare
annotation is a `WiredField` that raises on read until something injects; this
shadows it with a real instance attribute, so `BrowserCapability()` in a test still
works while YAML stays required. Call the **factory**, not the class — `DOMWatchdog`
needs the same default (`dom_watchdog.py:100`) and one fact in two files drifts.
(`mouse_trajectory` still names `InstantTrajectory()` in two places,
`capability.py:582` and `input/mouse.py:120` — don't copy that.)

**7. Import the concrete classes in the owner's module** so they are registered
*before* the class body's `__wiring__` is evaluated
(`# noqa: F401 — registration`). The lazy import inside `default_*()` is **not**
sufficient — it runs at `__init__` time, long after class creation.

**8. Thread the *instance* to every consumer.** Browser resolvers are built only from
`WatchdogContext` (`watchdogs/base.py:125` for this field), assembled once in
`BrowserCapability._watchdog_context` (`capability.py:1591`) and read in
`DOMWatchdog.build` (`dom_watchdog.py:135,139`). **Never** call `Registrar.get(name)`
/ `.get_or_none(name)` / `.children(prefix)` in a resolver — §5 will fail you. Prefer
making the context field **non-optional** for a brand-new receptor: then the default
has exactly one home by construction rather than by discipline.

**9. Regenerate and prove it landed** — `uv run spectragent config generate`. There is exactly
one entry point (`pyproject.toml:76`); AGENTS.md §3.4 used to name nine
`generate_config.py` files that no longer exist. See the **af-regenerate-and-verify**
skill for the verification loop and the staleness trap.

**10. Add the YAML block and fix every hand-written one.** A new single-instance
receptor is a **required** field, so every place a capability block is written by hand
now fails validation. `grep -rn 'dom_cleaner' tests/ configs/` is the reliable index of
those places. Shipped pattern, `configs/browser_use_local.yaml:171`:

```yaml
dom_serializer:
  serializer_type: text     # the discriminator
  max_text_length: 200      # the knob that was unreachable for the layer's whole life
```

**11. Delete the old name-based field.** Do not leave it. §5 will fail, which is the
guard working.

**12. Document the registrar in the same change** — a row in
`docs/architecture_design.md` §6 *and* in AGENTS.md §2.2, with the discriminator
spelled exactly as `discriminator_field` spells it.
`tests/test_docs/test_registrar_coverage.py` fails otherwise, naming your file. It
AST-walks every `.py` under `spectragent/` for `create_registrar` calls rather than
importing, so an optional extra or an unimported module cannot hide one.

> Nothing enforces step 3. `test_registrar_coverage.py` catches an *undocumented*
> registrar; `tests/test_generated/test_in_sync_with_registries.py` catches a
> *collected-but-stale* one. A created-but-uncollected registrar is caught by neither.
> Do step 3 when you do step 1.

**The one exception: a registrar with no implementation yet.** An empty registry cannot
be a Composer layer, for three separate reasons: `ConfigComposer` and `generate_all` propagate conscribe
`EmptyLayerError` for a collected layer with no implementation; and an owner whose wired slot points at an
uncollected registry fails to compose (`UnresolvedWiringError`) for every config that
names the owner, whether or not it uses the slot. So create and document the registrar
(step 1 and step 12) now, and do steps 3, 4, 5 and 9 (collect, the `__wiring__` entry,
the class-level annotation, regenerate) **in the change that registers its first
implementation**. Until then the owner takes the strategy list as a **plain, non-`Annotated`
constructor parameter**: `__config_annotated_only__` keeps it out of the generated
config, so a YAML block naming it fails validation instead of being dropped. Do not give
it an `Annotated` type before the wiring entry exists: conscribe then generates
`archive: Any` and hands the owner raw dicts, which is the name-not-instance defect of §0
again.

Worked case: `ArchiverRegistrar` (`core/archive/_registrar.py`). M28 created it with only
test fakes registered; `TraceWriter(archive=[...])` is a plain parameter there. M29 adds
`TraceWriter.__wiring__["archive"]`, the `Annotated[list[ArchiverBase] | None,
Field(...)]` type, the class-level `archive: list[ArchiverBase]` annotation, and
`archiver` in `_collect_registrars()` in the same change as `browser.network` /
`browser.storage` / `browser.dom_raw`.

## 3. The strategy IS the switch

**No boolean may sit beside a receptor and disagree with it.**

`BrowserCapability` once carried `humanize_mouse: bool` next to the `mouse_trajectory`
receptor. `trajectory_type: windmouse` + `humanize_mouse: false` validated cleanly and
ran the legacy instant jump: the user configured one strategy and the framework
executed another. Two declaration points for one meaning guarantees a contradictory
combination exists, and nothing anywhere reports it.

So the on/off state is a **property of the strategy**:

```python
# input/trajectory.py:187 — the base default
@property
def humanized(self) -> bool:
    """Every strategy that emits a path is humanized; ``instant`` overrides."""
    return True

# input/instant.py:61 — the only False in the registry
@property
def humanized(self) -> bool:
    return False
```

`typing_rhythm` (`rhythm.py:110`, `instant_rhythm.py:30`) has had this shape from day
one. `HumanMouse` (`mouse.py:122,134`) and `HumanKeyboard` (`keyboard.py:184`) read
`humanized` off the injected strategy and key everything else off it.

Give a base default **only when there is a genuinely safe one.** `humanized -> True`
is safe: a strategy that emits a path is humanized by construction. A *duration* is
not — so `MouseTrajectoryBase.click_hold_ms` (`trajectory.py:191`) raises
`NotImplementedError` naming the class, because `0.0` as a base default *is* the bug
(press and release in the same millisecond), handed silently to every future strategy
that forgot to think about it. `InstantTrajectory` returns an explicit `0.0`
(`instant.py:40`) and pins it with a test.

## 4. A preset supplies capture cost, never a strategy

`BrowserProfileConfig` (`profiles.py:38`) today holds exactly two kinds of thing:
which watchdogs are installed (`watchdogs_exclude`, a *reverse* declaration) and how
many CDP round trips a capture may spend (`js_click_listener_detection`,
`cross_site_iframe_capture`), plus the security watchdog's allow/block lists. It holds
**no strategy name** — not even as a default. A default the profile supplies is a
second place the answer lives, and defects #2 and #3 are exactly what happens next.

The division of labour, since there are otherwise two plausible homes for a new knob:

- Changes how many CDP round trips a **capture** costs → `BrowserProfileConfig`.
- Changes how much the resulting **observation** costs the prompt (`screenshot_quality`,
  `dom_max_elements`) → an `Annotated` scalar on the capability's `__init__`.
- Is a swappable implementation with its own knobs → a receptor.

A profile may still *recommend* a pairing in a comment (`profiles.py:80` pairs
`skyvern` cleaner with `html` serializer), and that is all it may do.

## 5. The structural guard: `test_receptor_hygiene.py`

`tests/test_core/test_capabilities/test_receptor_hygiene.py` stops instance number
four. Two point fixes did not stop a third, so it states the rule about **shape**, not
about the three known names. Read its module docstring before you argue with it.

**Rule 1 — one place to choose** (`:458`). Nothing a capability configures may offer a
second way to pick a strategy it already wires. It scans two surfaces, because the
defect has lived on both: every Pydantic model defined in the capability's package
(where #2 and #3 lived) **and** the capability's own `__init__` parameters (the
surface that becomes its generated config — the most plausible home for the next one).
And it matches two ways: by **name** (receptor name, registry name, or either with its
leading qualifier stripped, so `dom_serializer` covers `serializer`) **and by value**
(a `str`/`Literal` field whose default or arm is a key of a registry the capability
wires). The value check is load-bearing — `BrowserProfileConfig` called them `cleaner`
and `serializer` while the receptors are `dom_cleaner` and `dom_serializer`, so a
name-only check would have missed both.

**Rule 2 — one place to look one up** (`:577`). An AST walk forbidding
`SomeRegistrar.get(key)` / `.get_or_none(key)` / `.children(prefix)` over everything
under `core/capabilities/` (outside the module that *declares* a capability class),
`core/hooks/` and `core/archive/` (no exemption at all there: a hook or an archiver
receives instances, it never resolves a name). A meta test asserts
`core/hooks/trace_writer.py` and `core/archive/_registrar.py` are in the scanned set, so
the roots cannot silently stop matching.
`get_all()`, `keys()` and `tree()` are deliberately absent from `LOOKUP_METHODS`
(`:142`): they take no key, so they are enumeration, not resolution — which is how
`browser_watchdog_classes` derives the installed set without naming a watchdog.

**Rule 3 — a placeholder is never a constructor default.** The guard uses conscribe's
public reflection to reject a `WiredField` default. conscribe 2.0 supports dataclass
receptor fields: tests verify that optional defaults survive and required fields stay
required. Do not restore the 1.x ban on dataclass receptors.

## 6. Choose constructors for lifecycle, not for old descriptor bugs

conscribe 2.0 preserves dataclass field defaults and understands inherited annotations
and `**kwargs` constructor forwarding. A receptor may be a dataclass constructor field,
a hand-written constructor parameter, or an annotation-only attribute injected by setattr.
Use `conscribe.introspect` when inspecting these shapes; class attribute access may raise
for an unfilled annotation-only descriptor.

`BrowserCapability` and `TraceWriter` retain explicit constructors for their state setup.
`BranchLoop` retains a plain receptor mixin and a dataclass constructor. `HookBase` remains
`@dataclass(eq=False)` so stateful hooks compare by identity. These are valid designs, not
mandatory workarounds.

`TraceBranchesTaskLoader` still initializes lazily: its base's `__post_init__` loads data
before setattr injection, so the loader initializes state and its Python default itself
and scans only on first use. conscribe does not change this lifecycle requirement.

## 7. Gotchas worth the thirty seconds

- **Regenerate after *any* `__wiring__` or `Annotated` change** — including a change to
  a description string. `spectragent/generated/` is gitignored, and
  `tests/conftest.py` regenerates only when `_composed.py` is *missing*, so a stale
  artifact is invisible in `git status` and silently wrong. See
  **af-regenerate-and-verify**.
- **An optional single-instance receptor does not work today.** `dom_serializer: null`
  as a "don't do this at all" opt-out fails at *compose* time, not validation time:
  `ConfigComposer._instantiate` defers a `None` receptor and `_resolve_deferred`
  (`composer.py:~440`) then demands exactly one matching instance in that layer's pool,
  which is empty → `UnresolvedWiringError: matched 0 instances`. Container-shaped
  receptors (`actions: list[ActionBase] | None`) *are* optional; scalar Protocol ones
  are not, because conscribe's `_replace_str_with_literal` handles the two differently.
  Model an opt-out as a registered no-op strategy (`instant`) instead.
- **Subclassing a registrar base inside a test registers the class process-wide** and
  will break any test asserting exact registry contents. Test a base's refusal by
  calling it unbound: `MouseTrajectoryBase.click_hold_ms(InstantTrajectory())`.
- **The registries are global and shared with test fixtures.** Never parametrize over
  `Registrar.keys()` at collection time; loop at *run* time and filter to
  `cls.__module__.startswith("spectragent.")`.
- **A receptor is negotiated by name, not by contract.** Nothing checks that the
  strategy you register actually satisfies the Protocol. Put a test on that seam.
- **No registry key or option value may be a word YAML 1.1 reads as something else.**
  PyYAML is YAML 1.1: a bare on / off / yes / no / true / false is a boolean and null / ~
  is null, so such a discriminator arrives as `False` or `None` and validation rejects the
  block. `tests/test_core/test_composer/test_yaml_safe_values.py` refuses every one of
  them (any case) across all registry keys and every schema option value. "Not enabled"
  is spelled `none` (architecture §4.2.1): the replayability tier that used to be `off` is
  `none`, so every block writes `replayability: { replayability_id: none }` unquoted.
  A no-op strategy that does a normal thing keeps a descriptive name (`instant`, `live`).
- **A required single-instance receptor ripples through every hand-written block.**
  Adding `replayability` made it mandatory in the generated schema, so the same change
  edited every capability block that writes one by hand (the set
  `rg -l typing_rhythm configs tests .agents` finds: shipped configs, `configs/README.md`,
  the test configs, this guide's example), and the shipped-config gate
  (`test_shipped_config_integration.py`) proves each new value arrives. `network_source`
  (M30) rippled the same way (`rg -l replayability_id` is the index now), and every
  hand-built `ConfigComposer(registrars={...})` map in the tests had to gain the new
  layer too, or the capability's slot fails to compose (`UnresolvedWiringError`).
- **A strategy can carry its own receptor** (`ArchiveNetworkSource.miss_policy`). The
  composed union nests through every collected layer. Constructor-only optional list or
  dict slots are supported; they no longer need duplicate class annotations for shape.
- **User types and defaults are faithful in conscribe 2.0.** A nested `BaseModel` or
  pydantic dataclass is imported as its original type; ordinary `BaseModel` values reach
  constructors as model instances. `tuple[str, ...]` defaults render correctly, so
  `sources` and `require_archive` use immutable tuple defaults instead of null sentinels.
  Constraints including nested `Annotated[int, Field(ge=1)] | Unlimited` and `min_length`
  survive generated source and JSON Schema. Prefer actual defaults and keep constructor
  validation when direct Python construction also needs it.
- **An optional list or dict receptor omitted is "not set", never empty.** Omitted (or
  `null`), `_resolve_deferred` adopts every matching instance of the layer's top-level
  section; with none to adopt, the owner's own default stands (`capability.actions`: every
  wired action), and only an owner with no default of its own gets an empty container
  (`environment.capabilities`). `[]` always means empty: `actions: []` builds no action.
  Pinned in `tests/test_core/test_composer/test_inline_only.py`.
- **An inline-only layer is declared on its registrar**, with `inline_only=True`.
  `ArchiverRegistrar` uses it; `TraceWriter.archive` stays ordinary wiring. The Composer
  never adopts these instances from a pool. conscribe refuses a top-level `archiver:`
  value, even `[]` or `null`, using the Pydantic error `inline_only_layer` and a message
  naming `hook[trace_writer].archive`. No local `__inline_only__` attribute or custom
  exception is needed. Capability archive provides use `declare_only=True` and remain
  non-writable negotiation declarations.
