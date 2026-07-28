"""Tests for :class:`ConfigComposer`.

The tests build their own isolated registrar hierarchy (so they don't
depend on the real spectragent layers) and then exercise every shape
the Composer must handle: single-instance wired fields, dict-annotated
multi-instance wired fields, ``__init__``-injected wired fields,
setattr-injected wired fields, dependency topology, and error paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

import pytest
from conscribe import create_registrar
from pydantic import Field

from spectragent.core.composer import (
    ComposedRun,
    ComposeError,
    ConfigComposer,
    DuplicateInstanceIdError,
    UnresolvedWiringError,
)

# ── Shared fixture: fresh protocol/registrar per test module ───────
# We build a miniature multi-layer ecosystem — a mock "Backend" layer
# that Capabilities wire to; a "Capability" layer; a "Memory" layer; and
# an "Agent" layer at the top that wires to all three.


@runtime_checkable
class _BackendP(Protocol):
    name: str


@runtime_checkable
class _CapabilityP(Protocol):
    capability_id: str


@runtime_checkable
class _MemoryP(Protocol):
    memory_id: str


@runtime_checkable
class _AgentP(Protocol):
    agent_id: str


BackendReg = create_registrar("cmp_backend", _BackendP, discriminator_field="name")
CapReg = create_registrar("cmp_cap", _CapabilityP, discriminator_field="capability_id")
MemReg = create_registrar("cmp_mem", _MemoryP, discriminator_field="memory_id")
AgentReg = create_registrar("cmp_agent", _AgentP, discriminator_field="agent_id")


# ── Backend layer ──────────────────────────────────────────────────
@dataclass
class _BackendBase(metaclass=BackendReg.Meta):
    __abstract__ = True
    __config_annotated_only__ = True


@dataclass
class _LocalBackend(_BackendBase):
    __registry_key__: ClassVar[str] = "local"
    port: Annotated[int, Field(description="Port")] = 9222

    @property
    def name(self) -> str:
        return self.__registry_key__


@dataclass
class _RemoteBackend(_BackendBase):
    __registry_key__: ClassVar[str] = "remote"
    url: Annotated[str, Field(description="Remote URL")] = "wss://example"

    @property
    def name(self) -> str:
        return self.__registry_key__


# ── Capability layer (single-instance wired backend) ────────────────
@dataclass
class _CapBase(metaclass=CapReg.Meta):
    __abstract__ = True
    __config_annotated_only__ = True
    __wiring__: ClassVar[dict[str, Any]] = {}
    id: Annotated[str | None, Field(description="Instance id")] = None


@dataclass
class _BrowserCap(_CapBase):
    __registry_key__: ClassVar[str] = "browser"
    __wiring__: ClassVar[dict[str, Any]] = {"backend": "cmp_backend"}
    viewport_width: Annotated[int, Field(description="Viewport")] = 1280
    # Class-level annotation → triggers WiredField receptor pattern.
    backend: _BackendP = field(init=False, repr=False)

    @property
    def capability_id(self) -> str:
        return self.__registry_key__


# ── Memory layer ───────────────────────────────────────────────────
@dataclass
class _MemBase(metaclass=MemReg.Meta):
    __abstract__ = True
    __config_annotated_only__ = True


@dataclass
class _SimpleMem(_MemBase):
    __registry_key__: ClassVar[str] = "simple"
    max_messages: Annotated[int, Field(description="Cap")] = 10

    @property
    def memory_id(self) -> str:
        return self.__registry_key__


@dataclass
class _WindowMem(_MemBase):
    __registry_key__: ClassVar[str] = "window"
    window_size: Annotated[int, Field(description="Window")] = 20

    @property
    def memory_id(self) -> str:
        return self.__registry_key__


# ── Agent layer (wires llm-like = memory, cap = dict multi) ────────
@dataclass
class _AgentBaseForTest(metaclass=AgentReg.Meta):
    __abstract__ = True
    __config_annotated_only__ = True
    __wiring__: ClassVar[dict[str, Any]] = {
        "memory": "cmp_mem",  # single
        "capabilities": ("cmp_cap", ["browser"]),  # dict-form multi
        # Declaration-only: no attribute slot anywhere on the class, so
        # the Composer must build nothing and set nothing for it.
        "backend": "cmp_backend",
    }
    id: Annotated[str | None, Field(description="Instance id")] = None


@dataclass
class _SingleAgent(_AgentBaseForTest):
    __registry_key__: ClassVar[str] = "single"
    max_steps: Annotated[int, Field(description="steps")] = 5
    # Receptors: annotation-only → WiredField via conscribe.
    memory: _MemoryP = field(init=False, repr=False)
    capabilities: dict[str, _CapabilityP] = field(init=False, repr=False)

    @property
    def agent_id(self) -> str:
        return self.__registry_key__


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def registrars() -> dict[str, Any]:
    return {
        "cmp_backend": BackendReg,
        "cmp_cap": CapReg,
        "cmp_mem": MemReg,
        "cmp_agent": AgentReg,
    }


@pytest.fixture
def composer(registrars: dict[str, Any]) -> ConfigComposer:
    # Discover root doesn't matter for these tests; registries are already
    # populated via metaclass at import time.
    return ConfigComposer(registrars=registrars, discover_roots=())


# ── Tests ──────────────────────────────────────────────────────────


class TestBasicCompose:
    def test_dependency_order_leaves_first(
        self,
        composer: ConfigComposer,
    ) -> None:
        order = composer.dependency_order
        assert order.index("cmp_backend") < order.index("cmp_cap")
        assert order.index("cmp_cap") < order.index("cmp_agent")
        assert order.index("cmp_mem") < order.index("cmp_agent")

    def test_top_level_fields_one_per_layer(
        self,
        composer: ConfigComposer,
    ) -> None:
        names = set(composer.top_level_type.model_fields)
        assert names == {"cmp_backend", "cmp_cap", "cmp_mem", "cmp_agent"}

    def test_compose_produces_composed_run(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {
                "cmp_agent": [
                    {
                        "agent_id": "single",
                        "max_steps": 7,
                        "memory": {
                            "memory_id": "simple",
                            "max_messages": 3,
                        },
                        "capabilities": [
                            {
                                "capability_id": "browser",
                                "viewport_width": 800,
                                "backend": {
                                    "name": "local",
                                    "port": 9999,
                                },
                            },
                        ],
                    },
                ],
            },
        )
        assert isinstance(run, ComposedRun)
        agent = run.single("cmp_agent")
        assert isinstance(agent, _SingleAgent)
        assert agent.max_steps == 7

    def test_single_wired_field_instantiated_and_set(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = _compose_minimal(composer, mem_id="window", window_size=99)
        agent = run.single("cmp_agent")
        assert isinstance(agent.memory, _WindowMem)
        assert agent.memory.window_size == 99

    def test_dict_form_wiring_produces_dict_keyed_by_instance_id(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = _compose_minimal(composer)
        agent = run.single("cmp_agent")
        caps = agent.capabilities
        assert isinstance(caps, dict)
        # Instance id, not registry key — that is what lets two
        # capabilities of the same class coexist.
        assert set(caps) == {"browser#0"}
        assert isinstance(caps["browser#0"], _BrowserCap)

    def test_nested_single_wired_field_on_capability(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = _compose_minimal(composer, backend_port=12345)
        agent = run.single("cmp_agent")
        backend = agent.capabilities["browser#0"].backend
        assert isinstance(backend, _LocalBackend)
        assert backend.port == 12345

    def test_by_layer_keyed_by_instance_id(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = _compose_minimal(composer)
        assert set(run.by_layer["cmp_cap"]) == {"browser#0"}
        assert set(run.by_layer["cmp_backend"]) == {"local#0"}
        assert set(run.by_layer["cmp_mem"]) == {"simple#0"}
        assert set(run.by_layer["cmp_agent"]) == {"single#0"}

    def test_declaration_only_wiring_is_not_injected(
        self,
        composer: ConfigComposer,
    ) -> None:
        """``backend`` is wired on the agent but has no slot on the class.

        The old ``is_receptor`` heuristic accepted it (conscribe attaches a
        ``WiredField`` descriptor to *every* wiring entry), which is how a
        stray action instance ended up setattr'd onto every agent.
        """
        run = _compose_minimal(composer)
        agent = run.single("cmp_agent")
        assert "backend" not in agent.__dict__
        # ... and no orphan instance was built for it either: the only
        # backend in the run is the ``local`` one the capability wired.
        assert set(run.by_layer["cmp_backend"]) == {"local#0"}

    def test_populated_declaration_only_wiring_raises(
        self,
        composer: ConfigComposer,
    ) -> None:
        """A *filled-in* slotless wiring block must not be dropped in silence.

        Pass A skips wiring names with no receptor slot and Pass B skips
        every name that appears in ``__wiring__`` at all, so a populated
        block for a slotless entry fell through both passes: ``compose()``
        returned clean, nothing was built, and the value was gone — the
        exact silent drop the ``UnresolvedWiringError`` for non-wiring
        orphan fields already existed to prevent.
        """
        entry = _agent_entry()
        entry["backend"] = {"name": "remote", "url": "wss://never-used"}

        with pytest.raises(UnresolvedWiringError) as exc_info:
            composer.compose({"cmp_agent": [entry]})

        message = str(exc_info.value)
        assert "cmp_agent[0].backend" in message
        assert "silently dropped" in message
        # The dropped block really would have vanished: no _RemoteBackend
        # is anywhere in the run, which is why this has to be an error.
        assert "cmp_backend" in message


class TestInstanceIdentity:
    def test_explicit_id_becomes_the_key_and_the_attribute(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {"cmp_agent": [_agent_entry(id_="orch", max_steps=7)]},
        )
        assert run.ids("cmp_agent") == ["orch"]
        agent = run.instance("cmp_agent", "orch")
        assert agent.id == "orch"
        assert agent.max_steps == 7

    def test_ordinal_fallback_when_id_omitted(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {"cmp_agent": [_agent_entry(), _agent_entry()]},
        )
        assert run.ids("cmp_agent") == ["single#0", "single#1"]

    def test_two_instances_of_one_class_keep_their_own_children(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {
                "cmp_agent": [
                    _agent_entry(id_="orch", max_steps=30, max_messages=200),
                    _agent_entry(id_="worker", max_steps=99, max_messages=5),
                ],
            },
        )
        orch, worker = run.instances("cmp_agent")
        assert (orch.max_steps, worker.max_steps) == (30, 99)
        assert orch.memory is not worker.memory
        assert orch.memory.max_messages == 200
        assert worker.memory.max_messages == 5
        assert orch.capabilities != worker.capabilities

    def test_duplicate_id_is_a_hard_error(
        self,
        composer: ConfigComposer,
    ) -> None:
        with pytest.raises(DuplicateInstanceIdError, match="dup"):
            composer.compose(
                {
                    "cmp_agent": [
                        _agent_entry(id_="dup"),
                        _agent_entry(id_="dup"),
                    ],
                },
            )

    def test_id_collides_with_ordinal_fallback(
        self,
        composer: ConfigComposer,
    ) -> None:
        with pytest.raises(DuplicateInstanceIdError):
            composer.compose(
                {
                    "cmp_agent": [
                        _agent_entry(),  # -> single#0
                        _agent_entry(id_="single#0"),
                    ],
                },
            )

    def test_instance_lookup_by_id(self, composer: ConfigComposer) -> None:
        run = composer.compose({"cmp_agent": [_agent_entry(id_="a")]})
        assert run.instance("cmp_agent", "a").id == "a"
        with pytest.raises(ComposeError, match="no instance"):
            run.instance("cmp_agent", "missing")


class TestErrors:
    def test_empty_layer_raises_on_construction(self) -> None:
        # Register a layer with no implementations — build_composed_config
        # would crash; ConfigComposer surfaces a clearer error first.
        @runtime_checkable
        class _EmptyP(Protocol):
            key: str

        EmptyReg = create_registrar("cmp_empty", _EmptyP, discriminator_field="key")
        with pytest.raises(ComposeError, match="cmp_empty"):
            ConfigComposer(
                registrars={"cmp_empty": EmptyReg},
                discover_roots=(),
            )

    def test_invalid_yaml_rejected_by_pydantic(
        self,
        composer: ConfigComposer,
    ) -> None:
        from pydantic import ValidationError

        # ``agent_id: does_not_exist`` is not in the Literal discriminator.
        with pytest.raises(ValidationError):
            composer.compose({"cmp_agent": [{"agent_id": "nope"}]})


def _agent_entry(
    *,
    id_: str | None = None,
    max_steps: int = 5,
    max_messages: int = 10,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "agent_id": "single",
        "max_steps": max_steps,
        "memory": {"memory_id": "simple", "max_messages": max_messages},
        "capabilities": [
            {
                "capability_id": "browser",
                "backend": {"name": "local"},
            },
        ],
        # ``backend`` is declaration-only on the agent and is deliberately
        # *omitted*: the schema makes it optional, and populating it is a
        # hard error (see ``test_populated_declaration_only_wiring_raises``).
    }
    if id_ is not None:
        entry["id"] = id_
    return entry


def _compose_minimal(
    composer: ConfigComposer,
    *,
    mem_id: str = "simple",
    window_size: int | None = None,
    backend_port: int = 9222,
) -> ComposedRun:
    mem_cfg: dict[str, Any] = {"memory_id": mem_id}
    if mem_id == "window":
        mem_cfg["window_size"] = window_size if window_size is not None else 20
    else:
        mem_cfg["max_messages"] = 10
    return composer.compose(
        {
            "cmp_agent": [
                {
                    "agent_id": "single",
                    "memory": mem_cfg,
                    "capabilities": [
                        {
                            "capability_id": "browser",
                            "backend": {"name": "local", "port": backend_port},
                        },
                    ],
                },
            ],
        },
    )
