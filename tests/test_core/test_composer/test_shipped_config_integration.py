"""Integration tests: compose the *shipped* YAML, then call ``prepare()``.

Every other Composer/Runtime test builds its own synthetic registrar
hierarchy or hand-assembles a ``ComposedRun`` from mocks. That is exactly
how the ``env.capabilities`` blocker survived 560 green tests: the mock
path setattr'd the receptor by hand, so it could not observe that the
real path never injected it.

These tests take the opposite stance — real registrars, real shipped
config files, real classes, **zero monkeypatching** — and assert the
whole assembly chain end to end. Nothing here touches the network or
launches a browser: instantiation and negotiation are pure, only
``env.setup()`` would do I/O and it is never called.
"""

from __future__ import annotations

import copy
import inspect
import random
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

import pytest
import yaml

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.input import WindMouseTrajectory
from spectragent.core.composer import (
    ComposeError,
    ConfigComposer,
    DuplicateInstanceIdError,
)
from spectragent.core.negotiation import IncompatibleActionError
from spectragent.core.runtime import AgentRuntime
from spectragent.scripts.generate_all import _collect_registrars

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"
SHIPPED_CONFIGS = sorted(CONFIG_DIR.glob("*.yaml"))

# A capability block that satisfies Pydantic on the agent's
# declaration-only ``capability`` wiring entry. The Composer must build
# nothing from it.
# Slot-less wiring entries (``observation`` / ``action``) are optional in
# the schema as of conscribe 1.2.0, so they no longer appear here — the
# negotiation engine reads them off ``__wiring__``, not off config.
_CAPABILITY: dict[str, Any] = {
    "capability_id": "browser",
    "mouse_trajectory": {"trajectory_type": "instant"},
    "typing_rhythm": {"rhythm_type": "instant"},
    "dom_cleaner": {"cleaner_type": "browser_use"},
    "dom_serializer": {"serializer_type": "text"},
    "backend": {"provider_id": "local_chrome"},
}

_DIRECT_LLM: dict[str, Any] = {
    "model_type": "openai",
    "model": "gpt-4o",
    "provider": {"name": "direct"},
}


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars=_collect_registrars())


def _agent_entry(
    *,
    instance_id: str,
    max_steps: int,
    max_messages: int,
    llm: dict[str, Any],
) -> dict[str, Any]:
    return {
        "agent_id": "browser_use",
        "id": instance_id,
        "agent_loop": {"loop_id": "react", "max_steps": max_steps},
        "llm": llm,
        "memory": {"memory_id": "simple", "max_messages": max_messages},
    }


def _env_entry(instance_id: str = "browser_env") -> dict[str, Any]:
    return {
        "env_type": "browser",
        "id": instance_id,
        "capabilities": [_CAPABILITY],
    }


class TestShippedConfigs:
    """The exact files under ``configs/`` must compose and prepare."""

    def test_configs_dir_is_not_empty(self) -> None:
        assert SHIPPED_CONFIGS, "no shipped YAML found — did configs/ move?"

    @pytest.mark.parametrize("config_path", SHIPPED_CONFIGS, ids=lambda p: p.name)
    def test_compose_then_prepare_without_patching(
        self,
        composer: ConfigComposer,
        config_path: Path,
    ) -> None:
        run = composer.compose(yaml.safe_load(config_path.read_text()))

        # 1. env.capabilities is a real dict of live capability instances,
        #    injected by the Composer — no setattr workaround anywhere.
        env = run.instances("environment")[0]
        assert isinstance(env.capabilities, dict)
        assert env.capabilities, "capabilities receptor was never injected"
        for instance_id, cap in env.capabilities.items():
            assert instance_id in run.by_layer["capability"]
            assert cap is run.by_layer["capability"][instance_id]
            assert cap.capability_id == "browser"

        # Routing still works off ``capability_id`` even though the dict
        # is keyed by instance id.
        assert env.capability_ids() == frozenset({"browser"})
        assert env.has_capability("browser")
        assert "browser.click" in env.provided_actions

        # 2. The full assembly chain runs.
        agent_id = run.ids("agent")[0]
        runtime = AgentRuntime.for_agent(run, agent_id)
        prepared = runtime.prepare()
        assert callable(prepared.run)

        # 3. No stray instance was setattr'd from a declaration-only
        #    wiring entry.
        agent = run.instance("agent", agent_id)
        assert not hasattr(agent, "action")
        assert "capability" not in agent.__dict__
        assert "observation" not in agent.__dict__

        # 4. The agent's own receptors carry its loop / llm / memory.
        assert agent.agent_loop is not None
        assert agent.llm is not None
        assert agent.memory is not None


class TestProviderSubConfig:
    """The ``provider:`` block must reach the client, not the floor."""

    def test_azure_provider_fields_reach_the_client(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=10,
                        max_messages=50,
                        llm={
                            "model_type": "openai",
                            "model": "gpt-4o",
                            "provider": {
                                "name": "azure",
                                "azure_endpoint": "https://X.openai.azure.com",
                                "api_version": "2024-06-01",
                                "azure_deployment": "my-deployment",
                            },
                        },
                    )
                ],
                "environment": [_env_entry()],
            }
        )
        llm = run.instance("agent", "main").llm
        assert llm.azure_endpoint == "https://X.openai.azure.com"
        assert llm.api_version == "2024-06-01"
        assert llm.azure_deployment == "my-deployment"

    def test_unset_provider_fields_keep_client_defaults(
        self,
        composer: ConfigComposer,
    ) -> None:
        """Splatting must only carry fields the config actually set.

        Every ``provider:`` sub-model is fully populated with defaults, so
        splatting it wholesale would silently overwrite the client's own
        defaults with the sub-model's.
        """
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=10,
                        max_messages=50,
                        llm={
                            "model_type": "openai",
                            "model": "gpt-4o",
                            # Only the discriminator tag is set.
                            "provider": {"name": "azure"},
                        },
                    )
                ],
                "environment": [_env_entry()],
            }
        )
        llm = run.instance("agent", "main").llm
        assert llm.model == "gpt-4o"
        assert llm.use_responses_api == "auto"
        assert llm.max_retries == 5  # class default survived


class TestMultipleAgents:
    """Two instances of one class must not collapse into each other."""

    @pytest.fixture
    def two_agent_run(self, composer: ConfigComposer) -> Any:
        return composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="orch",
                        max_steps=30,
                        max_messages=200,
                        llm={
                            "model_type": "openai",
                            "model": "gpt-4o",
                            "provider": {"name": "direct"},
                        },
                    ),
                    _agent_entry(
                        instance_id="worker",
                        max_steps=99,
                        max_messages=5,
                        llm={
                            "model_type": "openai",
                            "model": "gpt-4o-mini",
                            "provider": {"name": "direct"},
                        },
                    ),
                ],
                "environment": [_env_entry()],
            }
        )

    def test_each_agent_keeps_its_own_config(self, two_agent_run: Any) -> None:
        assert two_agent_run.ids("agent") == ["orch", "worker"]
        orch, worker = two_agent_run.instances("agent")

        assert orch.id == "orch"
        assert worker.id == "worker"

        assert orch.agent_loop is not worker.agent_loop
        assert orch.agent_loop.max_steps == 30
        assert worker.agent_loop.max_steps == 99

        assert orch.memory is not worker.memory
        assert orch.memory.max_messages == 200
        assert worker.memory.max_messages == 5

        assert orch.llm is not worker.llm
        assert orch.llm.model == "gpt-4o"
        assert worker.llm.model == "gpt-4o-mini"

    def test_each_agent_gets_its_own_runtime(self, two_agent_run: Any) -> None:
        orch_rt = AgentRuntime.for_agent(two_agent_run, "orch")
        worker_rt = AgentRuntime.for_agent(two_agent_run, "worker")

        assert orch_rt.agent is two_agent_run.instance("agent", "orch")
        assert worker_rt.agent is two_agent_run.instance("agent", "worker")
        # The single environment is shared — explicitly, not accidentally.
        assert orch_rt.env is worker_rt.env

        orch_rt.prepare()
        worker_rt.prepare()

    def test_prepared_agents_do_not_share_loop_context(
        self,
        two_agent_run: Any,
    ) -> None:
        """F3: a second ``weave`` must not rewrite the first agent's deps.

        Both agents are prepared through the real runtime; each
        ``agent.run`` closure has to keep pointing at the memory that
        agent was prepared with.
        """
        orch = two_agent_run.instance("agent", "orch")
        worker = two_agent_run.instance("agent", "worker")

        AgentRuntime.for_agent(two_agent_run, "orch").prepare()
        AgentRuntime.for_agent(two_agent_run, "worker").prepare()

        assert not hasattr(orch.agent_loop, "ctx")
        assert not hasattr(worker.agent_loop, "ctx")

        orch_ctx = orch.run.__closure__ and _closure_ctx(orch.run)
        worker_ctx = _closure_ctx(worker.run)
        assert orch_ctx["memory"] is orch.memory
        assert worker_ctx["memory"] is worker.memory
        assert orch_ctx["memory"] is not worker_ctx["memory"]

    def test_shared_loop_instance_stays_stateless(
        self,
        two_agent_run: Any,
    ) -> None:
        """Even one loop object woven onto both agents keeps deps apart."""
        orch = two_agent_run.instance("agent", "orch")
        worker = two_agent_run.instance("agent", "worker")
        loop = orch.agent_loop

        loop.weave(orch, env="ENV1", llm="LLM1", memory="MEM1", act_fn="ACT1")
        loop.weave(worker, env="ENV2", llm="LLM2", memory="MEM2", act_fn="ACT2")

        assert not hasattr(loop, "ctx")
        assert _closure_ctx(orch.run)["env"] == "ENV1"
        assert _closure_ctx(worker.run)["env"] == "ENV2"


class TestExplicitSharing:
    def test_two_environments_force_an_explicit_choice(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=5,
                        max_messages=10,
                        llm=_DIRECT_LLM,
                    )
                ],
                "environment": [_env_entry("env_a"), _env_entry("env_b")],
            }
        )
        assert run.ids("environment") == ["env_a", "env_b"]
        # Two capability instances, one per environment — no collapse.
        assert len(run.instances("capability")) == 2

        with pytest.raises(ComposeError, match="pass env_id"):
            AgentRuntime.for_agent(run, "main")

        runtime = AgentRuntime.for_agent(run, "main", env_id="env_b")
        assert runtime.env is run.instance("environment", "env_b")

    def test_duplicate_instance_id_is_rejected(
        self,
        composer: ConfigComposer,
    ) -> None:
        with pytest.raises(DuplicateInstanceIdError, match="clash"):
            composer.compose(
                {
                    "agent": [
                        _agent_entry(
                            instance_id="clash",
                            max_steps=1,
                            max_messages=1,
                            llm=_DIRECT_LLM,
                        ),
                        _agent_entry(
                            instance_id="clash",
                            max_steps=2,
                            max_messages=2,
                            llm=_DIRECT_LLM,
                        ),
                    ],
                    "environment": [_env_entry()],
                }
            )

    def test_environment_adopts_the_pool_when_capabilities_omitted(
        self,
        composer: ConfigComposer,
    ) -> None:
        """The declarative multi-instance receptor path (no config field)."""
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=5,
                        max_messages=10,
                        llm=_DIRECT_LLM,
                    )
                ],
                "capability": [_CAPABILITY],
                "environment": [{"env_type": "browser", "id": "env0"}],
            }
        )
        env = run.instance("environment", "env0")
        assert list(env.capabilities) == run.ids("capability")


def _closure_ctx(run_fn: Any) -> dict[str, Any]:
    """Pull the ``ctx`` dict out of a woven ``agent.run`` closure."""
    names = run_fn.__code__.co_freevars
    cells = run_fn.__closure__ or ()
    return dict(zip(names, cells, strict=True))["ctx"].cell_contents


# ── The declared config surface must actually be reachable ───────────
#
# Every bug this class covers was invisible to a test that only asserts
# on the generated config *model*: the value validated fine and then went
# nowhere. So each test follows one YAML key all the way onto the live
# object that consumes it.


def _capability(**overrides: Any) -> dict[str, Any]:
    return {**_CAPABILITY, **overrides}


class TestDeclaredConfigSurfaceIsReachable:
    def test_trace_dir_from_yaml_reaches_the_writer(
        self,
        composer: ConfigComposer,
        tmp_path: Path,
    ) -> None:
        """``trace_dir`` used to be a hard ``extra_forbidden`` error.

        ``TraceWriter`` declared it as a class-level ``Annotated`` field on
        a ``**kwargs``-only ``__init__``; conscribe reads the ``__init__``
        signature, found no named parameter and emitted a
        discriminator-only model with ``extra="forbid"``.
        """
        target = tmp_path / "my_traces"
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=3,
                        max_messages=10,
                        llm=_DIRECT_LLM,
                    )
                ],
                "environment": [_env_entry()],
                "hook": [{"hook_id": "trace_writer", "trace_dir": str(target)}],
            }
        )
        writer = run.single("hook")
        assert writer.trace_dir == str(target)

    async def test_trace_dir_decides_where_the_wal_lands(
        self,
        composer: ConfigComposer,
        tmp_path: Path,
    ) -> None:
        """Not just the attribute — the directory the run actually writes."""
        target = tmp_path / "wal_root"
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=3,
                        max_messages=10,
                        llm=_DIRECT_LLM,
                    )
                ],
                "environment": [_env_entry()],
                "hook": [{"hook_id": "trace_writer", "trace_dir": str(target)}],
            }
        )
        writer = run.single("hook")
        await writer.on_run_start(run_id="abc123", task="t", task_id="t-1")
        assert writer.run_dir == target / "abc123"
        assert writer.run_dir.is_dir()

    def test_per_action_knob_from_yaml_reaches_the_action_instance(
        self,
        composer: ConfigComposer,
    ) -> None:
        """The design doc's ``actions:`` list, end to end."""
        run = composer.compose(
            {
                "environment": [
                    {
                        "env_type": "browser",
                        "capabilities": [
                            _capability(
                                actions=[
                                    {
                                        "action_id": "browser.scroll",
                                        "default_amount": 600,
                                        "max_amount": 5000,
                                    },
                                    {
                                        "action_id": "browser.navigate",
                                        "wait_until": "networkidle",
                                        "post_delay_ms": 1500,
                                    },
                                ]
                            )
                        ],
                    }
                ],
            }
        )
        cap = run.single("capability")
        scroll = cap._actions_by_name["browser.scroll"]
        navigate = cap._actions_by_name["browser.navigate"]
        assert (scroll.default_amount, scroll.max_amount) == (600, 5000)
        assert navigate.wait_until == "networkidle"
        assert navigate.post_delay_ms == 1500

    def test_post_delay_ms_reaches_every_settleable_action(
        self,
        composer: ConfigComposer,
    ) -> None:
        """Six actions silently lost this knob to a PEP 563 trap.

        ``SettleableAction`` lived in a module with ``from __future__
        import annotations``, so the subclass's dataclass-synthesised
        ``__init__`` carried the annotation as a *string* that its own
        module's globals could not resolve (no ``PydanticField`` bound
        there). Type-hint resolution failed, ``field_info`` came back
        ``None`` and ``__config_annotated_only__`` dropped the field.
        ``browser.navigate`` was unaffected only because its module
        happens to import ``PydanticField``.
        """
        settleable = [
            "browser.click",
            "browser.click_coordinate",
            "browser.go_back",
            "browser.go_forward",
            "browser.select_dropdown",
            "browser.send_keys",
        ]
        run = composer.compose(
            {
                "environment": [
                    {
                        "env_type": "browser",
                        "capabilities": [
                            _capability(
                                actions=[
                                    {"action_id": name, "post_delay_ms": 250}
                                    for name in settleable
                                ]
                            )
                        ],
                    }
                ],
            }
        )
        cap = run.single("capability")
        assert sorted(cap._actions_by_name) == sorted(settleable)
        for name in settleable:
            assert cap._actions_by_name[name].post_delay_ms == 250, name

    def test_omitting_the_list_yields_every_wired_action(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {"environment": [{"env_type": "browser", "capabilities": [_CAPABILITY]}]}
        )
        cap = run.single("capability")
        assert cap.provided_actions == frozenset(
            BrowserCapability.__wiring__["actions"][1]
        )

    def test_listing_a_subset_ablates_the_rest(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(
            {
                "environment": [
                    {
                        "env_type": "browser",
                        "capabilities": [
                            _capability(
                                actions=[
                                    {"action_id": "browser.click"},
                                    {"action_id": "browser.done"},
                                ]
                            )
                        ],
                    }
                ],
            }
        )
        env = run.instances("environment")[0]
        assert env.provided_actions == frozenset(
            {"browser.click", "browser.done"}
        )

    def test_ablating_a_required_action_fails_negotiation_by_name(
        self,
        composer: ConfigComposer,
    ) -> None:
        """The intended consequence of ablation, with a message that says so."""
        run = composer.compose(
            {
                "agent": [
                    _agent_entry(
                        instance_id="main",
                        max_steps=3,
                        max_messages=10,
                        llm=_DIRECT_LLM,
                    )
                ],
                "environment": [
                    {
                        "env_type": "browser",
                        "capabilities": [
                            _capability(
                                actions=[
                                    {"action_id": "browser.click"},
                                    {"action_id": "browser.type"},
                                    {"action_id": "browser.navigate"},
                                ]
                            )
                        ],
                    }
                ],
            }
        )
        runtime = AgentRuntime.for_agent(run, "main")
        with pytest.raises(IncompatibleActionError) as exc_info:
            runtime.prepare()
        message = str(exc_info.value)
        # Named the way the *agent* wrote it, which is the line the user
        # has to edit; the ablation list it is missing from is quoted
        # alongside, fully qualified.
        assert "'done'" in message
        assert "browser.click" in message
        assert "ablation" in message

    def test_mouse_trajectory_knobs_reach_the_strategy_instance(
        self,
        composer: ConfigComposer,
    ) -> None:
        """Per-strategy knobs lived behind an untyped, unreachable dict."""
        run = composer.compose(
            {
                "environment": [
                    {
                        "env_type": "browser",
                        "capabilities": [
                            _capability(
                                mouse_trajectory={
                                    "trajectory_type": "windmouse",
                                    "gravity": 12.5,
                                    "seed": 7,
                                },
                            )
                        ],
                    }
                ],
            }
        )
        cap = run.single("capability")
        assert isinstance(cap.mouse_trajectory, WindMouseTrajectory)
        # Private storage names — the point is that the YAML numbers
        # travelled into the constructed strategy, not just the model.
        assert cap.mouse_trajectory._g == 12.5
        assert cap.mouse_trajectory._rng.random() == random.Random(7).random()
        # ...and the watchdog context hands the watchdogs that same
        # instance, not a registry key plus an untyped kwargs dict.
        cap._cdp = object()  # type: ignore[assignment] — only stored, never called
        ctx = cap._watchdog_context("sid-1")
        assert ctx.mouse_trajectory is cap.mouse_trajectory

    def test_top_level_action_section_is_no_longer_a_silent_no_op(
        self,
        composer: ConfigComposer,
    ) -> None:
        """The escape hatch the Composer's own error message recommends.

        It used to compose successfully, build the configured actions into
        ``run.instances("action")`` — and leave the capability holding a
        completely separate set of default-constructed ones.
        """
        run = composer.compose(
            {
                "action": [
                    {"action_id": "browser.scroll", "default_amount": 900},
                    {"action_id": "browser.done"},
                ],
                "environment": [
                    {"env_type": "browser", "capabilities": [_CAPABILITY]}
                ],
            }
        )
        cap = run.single("capability")
        assert set(cap._actions_by_name) == {"browser.scroll", "browser.done"}
        assert cap._actions_by_name["browser.scroll"] is run.instances("action")[0]
        assert cap._actions_by_name["browser.scroll"].default_amount == 900


# ── Every value the shipped YAML states must reach the object that reads it ──
#
# This replaces two hand-typed assertions (``backend.headless is True`` and
# ``loop.max_steps == 30``) that had opposite, equally bad failure modes.
# ``headless is True`` had already stopped working: ``True`` is also
# ``LocalChromeProvider``'s class default, so the assertion held whether or
# not the YAML value ever reached the object — deleting the key from the
# config left the test green. ``max_steps == 30`` still had teeth, but only
# by luck: it is a tunable, and retuning it to 100 would have made it
# vacuous the same way, while retuning it to anything else would have failed
# the suite for an ordinary config edit.
#
# The structural cause is that the right-hand side was typed by a human
# instead of read from the artefact under test, so nothing kept the two
# coupled. Hence the walk below: every scalar leaf of every tracked block is
# compared against the live attribute, with no literal anywhere. Coupling
# the gate to the YAML layout is not a cost here — the shipped layout is
# precisely this file's subject, and a restructure should be noticed.
#
# A walk alone would not be enough. Where the shipped value happens to equal
# the class default, ``getattr(obj, key) == value`` cannot tell injection
# from fallback — and every single scalar in browser_use_local.yaml's
# ``backend:`` block is in that state, so the whole block is unobservable to
# a plain comparison. Hence the second half: perturb each numeric/boolean
# knob, recompose, and require the live attribute to follow. That is what
# makes the gate falsifiable per field, and it is why the shipped configs
# were left untouched — tuning a production config so that a test has
# something to look at would be backwards, and that block is there to
# document the knobs a local run can turn.

_NO_DEFAULT = object()

#: Scalars deliberately outside the walk, and why.
_UNTRACKED_SCALARS: frozenset[str] = frozenset(
    {
        # The provider sub-model is splatted onto the client rather than
        # attached, so ``name`` maps to ``llm.provider``, not ``llm.name``.
        # ``TestProviderSubConfig`` covers that hop with non-default values.
        "agent[0].llm.provider.name",
    }
)

#: Scalars the walk checks but cannot falsify: they restate the class
#: default and are strings, which this schema never allows to be synthetic
#: (they select classes, name profiles, or are filesystem paths). A new
#: entry here means a new YAML line that proves nothing — give it a value
#: the operator actually chose, or drop the line.
_UNPROVABLE_SCALARS: dict[str, frozenset[str]] = {
    "browser_use_local.yaml": frozenset(
        {
            "environment[0].capabilities[0].profile",
            "environment[0].capabilities[0].backend.startup_url",
            "hook[0].trace_dir",
        }
    ),
    "browser_use_skyvern.yaml": frozenset(
        {
            "environment[0].capabilities[0].profile",
            "hook[0].trace_dir",
        }
    ),
}


@dataclass(frozen=True)
class _Block:
    """One YAML mapping and the live object that is supposed to carry it."""

    path: str
    raw: Callable[[dict[str, Any]], dict[str, Any]]
    live: Callable[[Any], Any]


def _scalar_items(block: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if not isinstance(v, dict | list)}


def _leaf_paths(node: Any, prefix: str = "") -> Iterator[str]:
    """Every scalar leaf in a parsed YAML document, as ``a[0].b.c``."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaf_paths(value, f"{prefix}[{index}]")
    else:
        yield prefix


def _tracked_blocks(root: dict[str, Any]) -> list[_Block]:
    """Resolve each shipped block to the instance that consumes it."""
    blocks: list[_Block] = []

    def agent_of(run: Any, index: int, entry_id: str | None) -> Any:
        return (
            run.instance("agent", entry_id)
            if entry_id is not None
            else run.instances("agent")[index]
        )

    def env_of(run: Any, index: int, entry_id: str | None) -> Any:
        return (
            run.instance("environment", entry_id)
            if entry_id is not None
            else run.instances("environment")[index]
        )

    for i, entry in enumerate(root.get("agent", [])):
        aid = entry.get("id")
        blocks.append(
            _Block(
                f"agent[{i}]",
                lambda r, i=i: r["agent"][i],
                lambda run, i=i, aid=aid: agent_of(run, i, aid),
            )
        )
        for child in ("agent_loop", "llm", "memory"):
            if child in entry:
                blocks.append(
                    _Block(
                        f"agent[{i}].{child}",
                        lambda r, i=i, c=child: r["agent"][i][c],
                        lambda run, i=i, aid=aid, c=child: getattr(
                            agent_of(run, i, aid), c
                        ),
                    )
                )

    for j, entry in enumerate(root.get("environment", [])):
        eid = entry.get("id")
        blocks.append(
            _Block(
                f"environment[{j}]",
                lambda r, j=j: r["environment"][j],
                lambda run, j=j, eid=eid: env_of(run, j, eid),
            )
        )
        for k, cap_entry in enumerate(entry.get("capabilities", [])):
            blocks.append(
                _Block(
                    f"environment[{j}].capabilities[{k}]",
                    lambda r, j=j, k=k: r["environment"][j]["capabilities"][k],
                    lambda run, j=j, eid=eid, k=k: list(
                        env_of(run, j, eid).capabilities.values()
                    )[k],
                )
            )
            for child in (
                "mouse_trajectory",
                "typing_rhythm",
                "dom_cleaner",
                "dom_serializer",
                "backend",
            ):
                if child in cap_entry:
                    blocks.append(
                        _Block(
                            f"environment[{j}].capabilities[{k}].{child}",
                            lambda r, j=j, k=k, c=child: r["environment"][j][
                                "capabilities"
                            ][k][c],
                            lambda run, j=j, eid=eid, k=k, c=child: getattr(
                                list(env_of(run, j, eid).capabilities.values())[k], c
                            ),
                        )
                    )

    for h in range(len(root.get("hook", []))):
        blocks.append(
            _Block(
                f"hook[{h}]",
                lambda r, h=h: r["hook"][h],
                lambda run, h=h: run.instances("hook")[h],
            )
        )
    return blocks


def _class_default(obj: Any, key: str) -> Any:
    """The value ``key`` would hold if the config never reached ``obj``.

    ``_NO_DEFAULT`` means there is no such value — a required constructor
    parameter, or a property computed from the registry key — so injection
    cannot silently fall back into it.
    """
    cls = type(obj)
    try:
        param = inspect.signature(cls.__init__).parameters.get(key)
    except (TypeError, ValueError):
        param = None
    if param is not None and param.default is not inspect.Parameter.empty:
        return param.default
    attr = inspect.getattr_static(cls, key, _NO_DEFAULT)
    if attr is _NO_DEFAULT or isinstance(attr, property) or callable(attr):
        return _NO_DEFAULT
    return attr


def _literal_alternative(obj: Any, key: str, value: Any) -> Any:
    """Another member of *key*'s ``Literal``, or ``None`` if it has none.

    A synthetic string is useless here — it would fail validation, and a
    rejected config proves nothing about injection. A *declared* member is
    the opposite: it is guaranteed to validate and guaranteed to differ, so
    an enum knob restating its class default becomes falsifiable instead of
    joining the ledger of blind spots.
    """
    try:
        annotation = get_type_hints(type(obj), include_extras=True).get(key)
    except Exception:
        return None
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) is not Literal:
        return None
    return next((member for member in get_args(annotation) if member != value), None)


def _perturb(value: Any, obj: Any = None, key: str = "") -> Any:
    """A different but still valid value for a knob, or ``None`` if unsafe.

    Booleans, numbers, and any field whose annotation is a ``Literal``.
    Every *other* string in this schema either selects a class
    (``provider_id``), names something that has to exist (``profile``) or is
    an identity, so a synthetic string would be rejected by validation and a
    rejected config proves nothing about injection.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return value + 7.0
    if isinstance(value, str) and obj is not None:
        return _literal_alternative(obj, key, value)
    return None


class TestShippedValuesReachTheObjectsThatReadThem:
    """The shipped YAML, key by key, against the objects it configures."""

    @pytest.mark.parametrize("config_path", SHIPPED_CONFIGS, ids=lambda p: p.name)
    def test_every_scalar_in_the_file_arrives(
        self,
        composer: ConfigComposer,
        config_path: Path,
    ) -> None:
        root = yaml.safe_load(config_path.read_text())
        run = composer.compose(copy.deepcopy(root))

        for block in _tracked_blocks(root):
            obj = block.live(run)
            for key, value in _scalar_items(block.raw(root)).items():
                assert hasattr(obj, key), (
                    f"{config_path.name}: {block.path}.{key} is accepted by the "
                    f"schema but {type(obj).__name__} has no such attribute — "
                    f"the value validated and then went nowhere"
                )
                assert getattr(obj, key) == value, (
                    f"{config_path.name}: {block.path}.{key} is {value!r} in the "
                    f"YAML but {type(obj).__name__}.{key} is "
                    f"{getattr(obj, key)!r}"
                )

    @pytest.mark.parametrize("config_path", SHIPPED_CONFIGS, ids=lambda p: p.name)
    def test_no_scalar_escapes_the_walk(self, config_path: Path) -> None:
        """Adding a key to a shipped config must extend the gate, not slip past it."""
        root = yaml.safe_load(config_path.read_text())
        covered = {
            f"{block.path}.{key}"
            for block in _tracked_blocks(root)
            for key in _scalar_items(block.raw(root))
        }
        assert set(_leaf_paths(root)) - covered == _UNTRACKED_SCALARS

    @pytest.mark.parametrize("config_path", SHIPPED_CONFIGS, ids=lambda p: p.name)
    def test_every_knob_is_falsifiable(
        self,
        composer: ConfigComposer,
        config_path: Path,
    ) -> None:
        """Recompose with every numeric/boolean knob changed; all must follow.

        This is what the arrival test cannot do on its own. ``headless: true``
        matches ``LocalChromeProvider.headless``'s default, so comparing them
        is true whether or not the value was injected — the exact state the
        old ``headless is True`` assertion had already collapsed into.
        """
        root = yaml.safe_load(config_path.read_text())
        # Composed once unperturbed, only so ``_perturb`` can read a field's
        # annotation off the live object and pick another member of its
        # ``Literal`` — an enum knob is otherwise unfalsifiable whenever the
        # shipped value happens to be the class default.
        run = composer.compose(copy.deepcopy(root))
        probe_root = copy.deepcopy(root)
        blocks = _tracked_blocks(root)
        probe_blocks = {block.path: block for block in _tracked_blocks(probe_root)}

        expected: dict[str, Any] = {}
        for block in blocks:
            probe_raw = probe_blocks[block.path].raw(probe_root)
            for key, value in _scalar_items(block.raw(root)).items():
                probe = _perturb(value, block.live(run), key)
                if probe is None:
                    continue
                probe_raw[key] = probe
                expected[f"{block.path}.{key}"] = (block.path, key, probe)

        assert expected, f"{config_path.name} declares no perturbable knob at all"

        probe_run = composer.compose(probe_root)
        by_path = {block.path: block for block in blocks}
        for label, (block_path, key, probe) in expected.items():
            obj = by_path[block_path].live(probe_run)
            assert getattr(obj, key) == probe, (
                f"{config_path.name}: {label} was changed to {probe!r} in the "
                f"config and {type(obj).__name__}.{key} did not follow "
                f"({getattr(obj, key)!r}) — the shipped value only matched "
                f"because it equals the class default"
            )

    @pytest.mark.parametrize("config_path", SHIPPED_CONFIGS, ids=lambda p: p.name)
    def test_the_unprovable_scalars_are_exactly_the_declared_ones(
        self,
        composer: ConfigComposer,
        config_path: Path,
    ) -> None:
        """Keep the gate's blind spot enumerated instead of invisible.

        A scalar is unprovable when it restates the class default *and* is a
        string, so neither the arrival comparison nor a perturbation can see
        injection break for it. That is a property of the config file, so it
        belongs in a ledger the file's author has to update.
        """
        declared = _UNPROVABLE_SCALARS.get(config_path.name)
        assert declared is not None, (
            f"{config_path.name} is a new shipped config; add it to "
            f"_UNPROVABLE_SCALARS (an empty set is the right answer if every "
            f"one of its scalars is observable)"
        )
        root = yaml.safe_load(config_path.read_text())
        run = composer.compose(copy.deepcopy(root))
        unprovable = {
            f"{block.path}.{key}"
            for block in _tracked_blocks(root)
            for key, value in _scalar_items(block.raw(root)).items()
            if _perturb(value, block.live(run), key) is None
            and _class_default(block.live(run), key) == value
        }
        assert unprovable == declared
