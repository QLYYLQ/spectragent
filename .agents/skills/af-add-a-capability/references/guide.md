# Adding a Capability

> Source line numbers are historical navigation hints in this mid-refactor repository.
> Locate the named symbol with `rg` and read the current implementation before editing.

A Capability is the atomic unit of environment ability: **ActionGroup** (declares and
executes actions in its scope) + **Resolver** (provides observation fragments on demand)
+ its own backend lifecycle. Capabilities are **flat peers** — no base sandbox, no
hierarchy. `docs/architecture_design.md` §4.9 (line 1352) is the source of truth;
`docs/execution_dag.md` M06 (line 390) has the six-capability table and the priorities.

Today there is exactly one: `browser`. Five are planned and unbuilt:

| capability_id | module | priority | benchmarks |
|---|---|---|---|
| `browser` | M13 | P1 ✅ shipped | WebArena, Mind2Web |
| `docker` | M19 | P2 | SWE-bench, Terminal-Bench |
| `tool_sandbox` | M23 | P3 | τ-bench, ToolBench |
| `gpu_profiling` | M25 | P3 | KernelBench |
| `desktop_vm` / `android` | M26 | P3 | OSWorld, AndroidWorld |

That ratio is the whole problem this skill exists for. Every framework rule currently has
exactly one witness, so **browser-specific accidents and framework contracts look
identical from the inside**. The contract test is how you tell them apart.

**Sibling skills** — this one covers the capability as a unit. For the pieces inside it:
`af-add-an-action` (a new verb), `af-add-an-observation-fragment` (a new thing to read),
`af-add-a-receptor` (a swappable strategy axis with its own knobs — backends are one),
`af-regenerate-and-verify` (the codegen loop).

---

## Step 0 — make the contract test pass before you write a feature

**This is the main deliverable of the work, and it comes first.** Not last, not
"after the happy path works".

`typing.Protocol` expresses method signatures. It cannot express *temporal* invariants —
"one `observe()` is one cycle", "the next cycle re-captures", "a failed capture costs one
capture, not one per fragment". Those invariants live in a shared test or they live
nowhere. Before this suite existed they lived as a docstring inside `BrowserCapability`
plus a "have I served this key before" guess, and the failure mode when the second
capability got it wrong was *a trace that lies*: an agent shown a screenshot taken before
the click it is reacting to, with nothing on disk to say so.

### Opt in

`tests/_helpers/capability_contract.py` holds the assertions.
`tests/test_core/test_capabilities/test_capability_contract.py` runs them.
You write **one factory** and inherit the whole suite:

```python
# tests/_helpers/capability_contract.py
def docker_harness() -> CaptureHarness:
    cap = DockerCapability()
    fake = _CountingExecTransport()          # counts round trips; never runs docker
    cap._exec = fake
    cap.apply_negotiation({"docker.terminal", "docker.filesystem"})
    return CaptureHarness(
        capability=cap,
        fragments=("docker.terminal", "docker.filesystem"),
        captures=lambda: fake.calls,          # ATTEMPTS, not successes
        break_capture=fake.make_it_fail,      # required, not optional
    )

HARNESS_FACTORIES: dict[str, Callable[[], CaptureHarness]] = {   # line 357
    "browser": browser_harness,
    "docker": docker_harness,                                     # ← add yours
}
```

`test_every_registered_capability_has_a_contract_harness`
(`test_capability_contract.py:86`) fails the moment your class registers and names the
file to edit. Do not delete or skip it.

`CaptureHarness` (`capability_contract.py:60-93`) demands four things, each for a reason:

- **`capability`** — already "set up" *as far as resolving goes*: no network, no
  container, no emulator. `browser_harness` (line 323) reaches into `cap._scopes` and
  `cap._default_session_id` rather than calling `setup()`. A contract test that needs a
  real Chrome is a contract test nobody runs; the suite is 25 tests in 0.21 s today.
- **`fragments`** — at least two keys served **from one capture**. `__post_init__` rejects
  fewer: the contract is about what several fragments of one cycle share.
- **`captures()`** — count *attempts*, not successes. Clause 5 is precisely about a retry
  budget being spent more than once.
- **`break_capture()`** — break the *capture*, not `begin_observation`. What your
  capability does on its bad day is half of this contract.

### What you inherit

| Assertion (`capability_contract.py`) | What it pins |
|---|---|
| `assert_one_cycle_is_one_capture` :99 | Reading N fragments twice inside one cycle costs **one** capture, and returns the *same objects* |
| `assert_next_cycle_resamples` :119 | Crossing a cycle boundary re-captures; no fragment survives it |
| `assert_begin_observation_clears_the_cache` :140 | Cycle 1 reads only fragment A, cycle 2 only fragment B — the boundary is the **announcement**, not your read history |
| `assert_fragments_carry_capture_time` :180 | Every fragment has a real `captured_at` inside the window it was resolved in |
| `assert_a_failed_cycle_costs_one_capture` :209 | A failed cycle reading N fragments costs what a failed cycle reading 1 fragment costs |
| `assert_foreign_action_policy_is_declared` :269 | `__invalidated_by_foreign_actions__` is a declared bool, not an environment-side assumption |

Plus the multi-capability half, which only an `Environment` can exercise, in
`test_capability_contract.py`: concurrency (`:249`), partial-failure isolation (`:317`),
your real capability surviving a peer's failure (`:396`), a failed observation not scaling
with fragment count through the real `observe()` (`:428`), and the foreign-action policy
(`:488`).

The suite has teeth, deliberately: `_WrongCapability` (`:549`) implements the two ways of
getting this wrong as runnable code, and `TestTheContractHasTeeth` (`:613`) asserts each
clause fails the mistake it exists to catch. Note which clause catches which — a
capability that ignores `begin_observation` **passes** `assert_one_cycle_is_one_capture`,
and the "served-key heuristic" **passes** `assert_next_cycle_resamples`. No single clause
is sufficient.

> **If your capability genuinely captures per fragment** (one round trip per fragment, no
> sharing), clauses 1 and 5 do not fit as written. The module docstring says so
> explicitly (`capability_contract.py:37-42`): come change the suite, do not quietly
> report one capture per resolve.

---

## Step 1 — the two contract surfaces, and the hole between them

There are two, and knowing which is which is load-bearing:

- **`BaseCapability`** — `spectragent/core/capabilities/base.py:22`. A
  `@runtime_checkable` `Protocol`. This is what every consumer types against:
  `EnvironmentBase.capabilities` is `dict[str, BaseCapability]`, the fragment router holds
  `BaseCapability`, `CapabilityRegistrar` is created against it.
- **`CapabilityBase`** — `spectragent/core/capabilities/_registrar.py:53`. The concrete
  `@dataclass` with `metaclass=CapabilityRegistrar.Meta`. Registration, the
  `Annotated` `id` field, default no-op lifecycle, `provided_fragments` narrowing.

Protocol-First (AGENTS.md §2.1) means someone **may implement the Protocol structurally
without inheriting the concrete base** — and that is exactly how a hole hides.

### The live example

`provided_actions` existed on `CapabilityBase` (`_registrar.py:161-176`) and was **missing
from the `BaseCapability` Protocol**, while `EnvironmentBase.provided_actions`
(`environments/_registrar.py:211-221`) unions it across capabilities typed as
`BaseCapability` and hands the result to action negotiation. Pyright reported it. It
worked anyway, because Python Protocols are structural and nothing enforced the
declaration — a structural implementer (the usage Protocol-First exists to allow) would
have type-checked clean and then failed at negotiation. It was fixed only just now
(`base.py:52-68`).

**Both axes must be declared on the Protocol.** AGENTS.md §2.5: *"The symmetry is literal,
on both axes… do not restore an asymmetry between them."* If you add a member to one axis,
add its twin:

| | Observation axis | Action axis |
|---|---|---|
| Capability declares | `provided_fragments` (`base.py:96`) | `provided_actions` (`base.py:53`) |
| Environment unions | `provided_fragments` (`environments/_registrar.py:204`) | `provided_actions` (`environments/_registrar.py:212`) |
| Registry key shape | `source.local_id` (`browser.dom`) | `source.local_id` (`browser.click`) |
| `__wiring__` declares | relative (`dom`) | relative (`click`) |

**Rule:** when you touch `base.py`, run `uv run pyright spectragent/core/capabilities
spectragent/core/environments`. Four errors are expected today, all conscribe-metaclass
artifacts (`__name__` on `type[CapabilityBase]`, `ActionBase.execute`). Anything else is
yours.

> Adding a member to a `@runtime_checkable` Protocol **changes `isinstance()` results** on
> 3.12+. A "one-line typing fix" to `base.py` is a behavioural change to a P0 abstraction.
> Search with `rg` for `isinstance(..., BaseCapability)` before you ship one; per AGENTS.md §3.1, ask
> rather than guess.

---

## Step 2 — the skeleton

Layout mirrors the browser (`spectragent/core/capabilities/browser/`):

```
spectragent/core/capabilities/docker/
├── __init__.py          # exports DockerCapability (import-driven registration)
├── capability.py        # the DockerCapability class
├── observations.py      # its FragmentBase subclasses — NOT schemas/observation/
├── actions/             # its ActionBase subclasses, one file each
└── backends/
    ├── _registrar.py    # DockerProviderRegistrar
    ├── base.py          # BaseDockerProvider protocol
    ├── local_docker.py
    └── ssh_docker.py
```

Fragments live **with the capability that owns them**, not in
`spectragent/schemas/observation/`. See the docstring at
`core/capabilities/browser/observations.py:1-16`: a fragment owns its own JSON shape, and
the shared `schemas/observation` package names no field of any domain. `DOMObs` lives next
to `DOMElementNode` because it cannot be encoded without knowing that graph is cyclic.

```python
# spectragent/core/capabilities/docker/observations.py
class TerminalObs(FragmentBase):
    __registry_key__ = "docker.terminal"          # fully qualified: source.local_id
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    stdout: str
    stderr: str = ""
    exit_code: int = 0
```

Inherit `FragmentBase` (`schemas/observation/_registrar.py:51`) and **do not declare
`fragment_id` or `captured_at` yourself** — you get `fragment_id` defaulted from
`__registry_key__` (`:94`, a real serialized field so the WAL says *which capability*
produced it) and `captured_at` stamped at construction (`:116`). Declaring your own breaks
the capture-time clause.

```python
# spectragent/core/capabilities/docker/capability.py
class DockerCapability(CapabilityBase):
    __registry_key__: ClassVar[str] = "docker"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["docker.terminal", "docker.filesystem"]),
        "actions": ("action", ["docker.exec", "docker.write_file", "docker.read_file"]),
        "backend": "docker_provider",
    }
```

Three wiring entries, three different jobs:

- **`"observation"`** — what you *provide*. `CapabilityBase.declared_fragments`
  (`_registrar.py:120-127`) derives from it by default. Override `declared_fragments` if
  your resolver set is dynamic: `BrowserCapability` does (`capability.py:694-703`) because
  the `vision_only` profile genuinely cannot serve `browser.dom`, so an agent requiring it
  must fail **negotiation** rather than receive an empty tree every step.
- **`"actions"`** — the key is the *attribute* the Composer fills (`self.actions`); the
  registry it draws from is `"action"`. The listed keys are the full set you can offer; a
  YAML `actions:` block **narrows** it (ablation), never widens it — see the setter at
  `browser/capability.py:620-654`, which raises naming the unknown keys.
- **`"backend"`** — Mode 1 bare string: any class registered in that registrar is allowed.

`provided_actions` on the concrete base (`_registrar.py:161-176`) reads
`self._actions_by_name` when you have one and falls back to `__wiring__["action"]`
otherwise. If you build a dispatch map, that map is the single source of truth — it is
what an ablated config actually narrows.

### Two shapes that bite

**Dataclass or hand-written `__init__`?** `CapabilityBase` is a `@dataclass`, and
inheriting is the easy path. But conscribe's metaclass decides whether a `__wiring__` entry
is already a constructor parameter by reading `inspect.signature(cls.__init__)` at *class
creation* time — before `@dataclass` has synthesised anything. On a dataclass capability
every wiring key looks unreceived, conscribe installs a `WiredField` descriptor over
whatever the class body bound, and the generated config field becomes **required** with its
default destroyed. `BrowserCapability` is therefore deliberately **not** a dataclass
(`capability.py:254-269`) — hand-writing `__init__` is what lets `actions` be an *optional*
receptor carrying a discriminated union of per-action configs. If any of your wiring keys
needs to be optional, hand-write `__init__`.

**Never write `**kwargs` on `setup()`.** See below.

---

## Step 3 — the observation cycle

The environment owns the cycle. `EnvironmentBase.observe`
(`environments/_registrar.py:344-455`) holds a monotonically increasing counter, increments
it once per observe (`:396`), and in **phase 1** tells every participating capability
before **phase 2** resolves anything (`:399-412`).

`begin_observation(cycle)` is **told, never inferred** (`base.py:107-136`). "Have I served
this key before" is a guess about a fact the caller already had, and it is wrong for any
caller that reads one fragment twice.

**If you capture per fragment:** the default no-op (`_registrar.py:186`) is correct. Skip
to Step 4 — but read the failure-path clause anyway, and re-read
`capability_contract.py:37-42` about the suite's assumption.

**If one round trip yields several fragments:** override it, and take on two obligations.

```python
async def begin_observation(self, cycle: int) -> None:
    self._observation_cycle = cycle        # record; do NOT clear eagerly
```

Recording the number is the whole implementation (`browser/capability.py:1104-1118`). Do
not clear eagerly: a capability nobody observes this cycle should not be touched at all. A
cache entry stamped with an older cycle is simply not served (`_populate_cycle`,
`capability.py:1166`).

**Obligation 1 — record that *this cycle's* capture failed.** This is the clause no amount
of happy-path testing catches. `Environment.observe` isolates failures per *fragment*
(`_resolve_slice`, `environments/_registrar.py:540-543`) — right for a capability that
captures one at a time, and it keeps a peer's good capture alive. But it means that after
your first key raises, the environment **asks you for the next one**. A shared capture that
does not remember it already failed re-enters the whole retry budget, once per negotiated
fragment. Measured: a `dom + screenshot + meta` run spent six full DOM extractions and six
overlay toggles failing a single observation, on exactly the path that was already slow
because something was wrong.

`BrowserCapability` keeps it on `_ObserveCycleCache.error` (`capability.py:204`) and
re-raises **the same exception object**, so the whole slice reports one cause — which is
the truth about what happened.

**Obligation 2 — know your invalidation inputs.** `_ObserveCycleCache`
(`capability.py:146-205`) has three, not one:

| Input | Meaning |
|---|---|
| `cycle` (`:199`) | "that observation is over" |
| `execute_action`'s pop (`:1096`) | "this tab changed under me" — and it does **not** advance the cycle |
| `error` (`:204`) | "this cycle's capture already failed" |

The pop exists because an action's effects must never be read through the capture that
preceded them, and the loop does not open a new cycle between its act and whatever reads
next. Treating the cycle number as the whole staleness rule invites the next reader to
delete the pop.

**Raise when you cannot capture** (`base.py:138-151`). Do not return `None`, do not return
an empty fragment. The environment decides what your failure costs by consulting what the
run declared *required*: a required fragment raises `ObservationFailedError`
(`environments/errors.py:14`), an optional one lands in the observation as a
`FragmentFailure` (`schemas/observation/failure.py`) and the rest survives. Softening it
inside the capability removes that decision from the only place that can make it. The
contract suite asserts this directly — `_expect_failure` (`capability_contract.py:254`)
fails your harness if `resolve_fragment` returns after `break_capture()`.

---

## Step 4 — lifecycle and backends

```python
async def setup(self, *, negotiation: Any = None, image: str = "", timeout: int = 600) -> None:
    await super().setup(negotiation=negotiation)   # records the negotiation result
    ...
```

**`negotiation` is how you stop paying for what nobody asked for.** `super().setup()` calls
`apply_negotiation` (`_registrar.py:143-159`), which narrows `provided_fragments` from
`declared_fragments` to the active set. Without it a vision-only agent still pays for a
full DOM extraction every step. `BrowserCapability` puts the negotiated set on the wire: it
fires one event carrying exactly those keys, so nothing else is collected at all.

**The rest of the signature is your public setup vocabulary — and you must not write
`**kwargs`.** `EnvironmentBase.route_setup_context`
(`environments/_registrar.py:577-651`) reads each capability's `setup()` signature to decide
which task-context keys it may route where. Two forms compose: **flat** (`{"start_url":
...}`, goes to every capability that *names* it) and **namespaced** (`{"docker":
{"timeout": 600}, "browser": {"timeout": 5}}`, private to one capability — this is how two
capabilities take the same option name independently). A key **no capability names raises**
(`:642-650`). A `**kwargs` catch-all declares nothing, receives nothing, and re-opens the
silent-typo hole that check exists to close (`base.py:163-171`).

**`teardown` failures are isolated per capability** (`environments/_registrar.py:653-678`).
Every capability gets its turn even if an earlier one raises; the first error is re-raised
at the end, the rest logged. Two guarantees, both learned from leaked Chrome processes: it
is safe after a partial setup and after no setup at all, because `AgentRuntime` calls it
whenever it *attempted* setup — the capabilities that came up before the failure are
exactly the ones holding OS processes and paid cloud sessions. `observe` now isolates the
same way (`_begin_cycle` at `:493`, `_resolve_slice` at `:507`). **Make your `teardown`
idempotent and safe on a half-built instance.**

### Backends

The backend answers "through what transport does this capability reach its world" —
`local_chrome` / `lexmount` / `skyvern_lite` for browser; `local_docker` / `ssh_docker` for
docker. It is injected through the same IoC path as `agent.llm` and `env.capabilities`
(design doc §4.9.x, line 1415).

```python
# spectragent/core/capabilities/docker/backends/_registrar.py
DockerProviderRegistrar = create_registrar(
    "docker_provider", BaseDockerProvider, discriminator_field="provider_id",
)

class DockerProviderBase(metaclass=DockerProviderRegistrar.Meta):
    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def provider_id(self) -> str:
        return self.__registry_key__
```

Mirror `browser/backends/_registrar.py:9-32`. Then on the capability: `__wiring__["backend"]
= "docker_provider"`, plus a class-level annotation `backend: BaseDockerProvider` marking
the receptor. Until the Composer setattrs it, `self.backend` is a `WiredField` descriptor,
not an instance — `BrowserCapability._resolved_backend` (`capability.py:732-751`) shows the
guard that distinguishes "use the wired backend" from "no backend was injected".

A backend is a wired receptor like any other — see **`af-add-a-receptor`** for the
mechanics, including what happens when a strategy's knobs are declared but nothing can
reach them.

---

## Step 5 — what a single-capability author forgets

You will be the first person writing a capability that has to coexist with another. Five
things the browser never had to prove:

**Capabilities resolve concurrently.** One task per capability; within a task, one fragment
at a time (`environments/_registrar.py:359-368`). Both halves are load-bearing: concurrent
across capabilities because they are independent worlds (a 0.5 s browser scrape and a 3 s
Android capture cost 3 s, not 3.5 s), sequential within one because its fragments share a
capture. **Do not hold a lock across a resolve, and do not assume you are alone.**

**`captured_at` makes the skew visible instead of silently implying simultaneity.** Because
capabilities are sampled concurrently, one observation holds samples taken at different
instants. Wall clock, not `perf_counter`, so it is comparable to `StepRecord.timestamp`
(`schemas/observation/_registrar.py:116-127`). Without it the skew is invisible at runtime
*and* absent from the trace, and an evaluator replaying the run cannot tell how far apart
the halves were.

**`__invalidated_by_foreign_actions__` defaults to `False`** (`_registrar.py:90`,
`base.py:34-48`). Capabilities are independent worlds: a `browser.click` does not change
what an Android emulator is showing, so **it must not cost an ADB round trip**. Set it
`True` only when two capabilities genuinely share one surface — a desktop VM whose screen
also hosts the browser, two views of one container's filesystem. It buys a re-capture per
foreign action, and that is exactly as expensive as re-capturing says it is.

What it actually buys, so nobody mistakes it for more
(`environments/_registrar.py:291-317`): for a plain observe → act → observe loop it changes
**nothing**, because `observe()` opens a new cycle for everyone anyway. It is the guarantee
for reads *between* two observes — an action implementation resolving a fragment, a loop
taking several actions per step. A passing `TestForeignActionPolicy` is proof the policy is
declared and consulted, **not** proof that browser+android sharing a screen is handled.

**Two instances of one capability class cannot coexist in one environment.** Action names
and fragment keys are scoped to the capability *class*, so the second instance would be
started and then never reached — with its Chrome fully launched, held for the whole run
and unreachable. `_by_capability_id` refuses the config, naming the collision
(`environments/_registrar.py:152-195`).

**Your fragments' local ids must not collide with a peer's.** The generated observation
model has one attribute per *local* name (`fragment_field_name`,
`schemas/observation/_registrar.py:33-44` — `browser.dom` → `obs.dom`), so
`docker.status` and `android.status` cannot both be active;
`DuplicateFragmentNameError` refuses the pair at negotiation. Name for the domain, not the
generic role.

---

## Step 6 — registry keys are fully qualified; `__wiring__` is not

AGENTS.md §2.5, and it applies identically on both axes.

**Fully qualified, always** — including in a single-capability environment:

- `__registry_key__` on your actions (`docker.exec`) and fragments (`docker.terminal`).
- `EnvironmentBase.act` routes on the prefix and **refuses a bare name**
  (`environments/_registrar.py:269-277`), returning a failed `ActionResult` that says so.
- `provided_actions` / `provided_fragments`, the trace, descriptors, the WAL filename
  (`000_browser.dom.json`).

**Relative in `__wiring__` — but only for *requires*.** Agents and Loops write `click` and
`screenshot`; negotiation binds each to the one capability in *this* environment that
offers it, recording the choice in `action_bindings` / `fragment_bindings`. Two providers
of one relative key raises, naming both candidates; writing the fully-qualified key in
`__wiring__` pins one. A **Capability's** `__wiring__`, being *provides*, writes the fully
qualified keys — it is stating a registry fact, not a domain need (design doc line 739).

This is what makes an agent portable: `UITarsAgent` declares `["screenshot"]` and the same
wiring holds for browser, desktop_vm and android.

Register your capability in an environment — that is all a single-capability env is:

```python
# spectragent/core/environments/docker.py
class DockerEnvironment(EnvironmentBase):
    __registry_key__: ClassVar[str] = "docker"
    __wiring__: ClassVar[dict[str, Any]] = {"capabilities": ("capability", ["docker"])}
```

Compare `environments/browser.py:15-21`. A multi-capability env just lists more keys.

---

## Step 7 — the config path

Your knobs reach YAML through conscribe codegen, and **nothing about that is automatic**.

1. **`__wiring__`** — declares the allowed keys per registry; conscribe turns them into
   `Literal[...]` constraints, so an illegal combination fails Pydantic validation, never
   at runtime.
2. **`Annotated[...]` constructor fields** — every YAML knob is a constructor parameter
   with a `pydantic.Field` description. See `browser/capability.py:415-460` for the shape.
   `CapabilityBase.id` (`_registrar.py:96-105`) is inherited this way.
3. **A new registrar must be added to `_collect_registrars()`**
   (`spectragent/scripts/generate_all.py:40-97`). `CapabilityRegistrar` is already there
   (`:83`) — so a new *capability* needs no edit. A new **backend registrar** does:
   `DockerProviderRegistrar` must be pinned beside `BrowserProviderRegistrar` (`:73`), or
   its config surface degrades to `Literal[<keys>]` and **every knob it declares becomes
   unreachable from YAML**. (The comment at `:74-77` says "bare `str`" with a silently
   downgraded typo — that describes `mouse_trajectory`'s *pre*-`__wiring__` shape, not
   what an uncollected registrar does today; a `Literal` does reject a typo.) Put it
   early, with the other leaves of the wiring DAG — the dict
   order is deliberate, for deterministic output.
4. **Regenerate**: `uv run spectragent-gen`. A layer with **zero** registered classes is
   skipped (`:118`), so an empty new registrar silently produces nothing.

### The fingerprint staleness trap

Change a `__wiring__` entry or an `Annotated` field and forget to regenerate, and
`spectragent/generated/_composed.py` does not become *broken* — it becomes **stale**. It
still imports, it is still internally consistent, and every test that reads it still
passes. It simply describes an older registry, so your new knob is unreachable from YAML
and a config naming it fails with "extra inputs are not permitted".

Two reasons nothing else catches it: `tests/conftest.py:41` regenerates only when
`_composed.py` is **missing**, and `spectragent/generated/` is gitignored apart from
`__init__.py` and `.fingerprint.json`, so code review sees no signal either.

`tests/test_generated/test_in_sync_with_registries.py` is the guard — it fingerprints the
registries in a **clean subprocess** (`:58-68`), because by the time it runs inside the
full suite other modules have imported stub capabilities into those very registries.
`test_generated_config_matches_the_live_registries` (`:96`) catches drift;
`test_every_collected_registrar_is_fingerprinted` (`:116`) catches a registrar added to
`_collect_registrars` without regenerating. Run both.

See the sibling skill **`af-regenerate-and-verify`** for the full loop and for proving a
knob actually reached YAML.

---

## Checklist

Before you call a capability done:

- [ ] `CaptureHarness` factory added to `HARNESS_FACTORIES`; all clauses green, none skipped
- [ ] `uv run pytest tests/test_core/test_capabilities/test_capability_contract.py` passes
- [ ] Fragments inherit `FragmentBase`; no hand-declared `fragment_id` / `captured_at`
- [ ] `resolve_fragment` **raises** on failure; never returns `None` or an empty fragment
- [ ] If `begin_observation` is overridden: a failed cycle is recorded and re-raised, same exception object, for the rest of the slice
- [ ] `setup()` names every task-context key as a keyword-only param; **no `**kwargs`**
- [ ] `setup()` calls `super().setup(negotiation=...)` and actually narrows its work to the negotiated set
- [ ] `teardown()` is idempotent and safe on a half-built instance
- [ ] `__invalidated_by_foreign_actions__` left at `False` unless a surface is genuinely shared
- [ ] Both axes declared: `provided_fragments` **and** `provided_actions`
- [ ] All registry keys fully qualified; `__wiring__` *requires* stay relative
- [ ] Backend registrar pinned in `_collect_registrars()` if you added one
- [ ] `uv run spectragent-gen`, then `uv run pytest tests/test_generated/`
- [ ] `uv run pyright spectragent tests` — only the four known metaclass artifacts
- [ ] A test on the action seam: negotiation matches **names**, not parameter contracts. An agent may emit a verb the run holds and send params the action cannot read. `tests/_helpers/action_params.py::minimal_params` walks `descriptor().params_schema` and builds the smallest legal payload; use it.
- [ ] `docs/architecture_design.md` §4.9 (line 1352) and `docs/execution_dag.md` M06 updated if you changed the contract. AGENTS.md §0 makes the design doc the source of truth — a contract that lives only in `base.py` and a test helper is the situation this layer exists to end.

## Gotchas

- **`CapabilityBase` is unhashable.** It is a `@dataclass` with default `eq=True`, so
  `__hash__` is `None` and `{cap: keys}` raises `TypeError` at runtime — pyright will not
  warn you. Group by `id(cap)` (`environments/_registrar.py:477-482`).
- **A test stub capability must not define its own `__init__`.** `CapabilityBase.id` is an
  `Annotated` field, and a custom `__init__` hides it from conscribe's config extractor,
  which `tests/test_generated/test_no_dropped_annotated_fields.py` fails on. Set attributes
  after construction — the `prime()` pattern at `test_capability_contract.py:152-159`.
- **`capabilities/_registrar.py` deliberately has no `from __future__ import
  annotations`** (`:11-17`). `CapabilityBase.id` must stay a real `Annotated[...]` object
  so subclasses' dataclass-generated `__init__` carries a resolvable annotation. Do not
  "tidy" that import in.
- **`asyncio.gather(..., return_exceptions=True)` swallows `CancelledError`**, so a
  cancelled run comes back looking like a completed one with an exception in a list. The
  environment's helpers catch `Exception` and *return* it instead, leaving `BaseException`
  to propagate (`environments/_registrar.py:493-505`). Copy that shape.
- **A hand-built negotiation result defaults to `required_fragments=frozenset()`** — i.e.
  nothing is required, so every failure is recorded rather than raised. Fine in a test
  (`_negotiated` at `test_capability_contract.py:234`); a *production* caller that builds
  one by hand silently degrades. `observe()` reads the field directly, never through
  `getattr(..., frozenset())`, precisely so this fails closed (`:390-394`).
- **`getattr(obs, "x", None) is not None` does NOT guard an optional fragment slot.** The
  slot is never `None`, and `FragmentFailure` is truthy — the guard passes and the *next*
  line raises, on exactly the path the widening exists to survive. Use
  `isinstance(value, FragmentBase)` (or the concrete class).
- **`build_observation_model` is `lru_cache`d.** Anything that changes the model's *shape*
  must be in the cache key, or the first run of a process decides the shape for every later
  one.
- **The `.pyi` stub shadows the `.py` for type checkers.** `capability.pyi` is
  conscribe-generated and goes stale silently — a caller typed against the concrete class
  then sees a capability missing the very Protocol member you just added, and nothing in
  the suite catches it. Regenerate after any method or private-attribute change.
- **Inside one capability the browser is all-or-nothing**: `_capture_once`
  (`capability.py:1231`) raises if any negotiated fragment is missing after
  `_OBSERVE_MAX_ATTEMPTS` (`:139`), so a landed screenshot is discarded along with a lost
  DOM. The environment isolates *capabilities*, not your internal batch. If you want
  per-fragment granularity inside a shared capture, you have to build it.
