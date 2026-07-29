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

import random
from pathlib import Path
from typing import Any

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

    def test_backend_reaches_the_capability(
        self,
        composer: ConfigComposer,
    ) -> None:
        run = composer.compose(yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text()))
        cap = run.single("capability")
        assert cap.__dict__["backend"].provider_id == "local_chrome"
        assert cap.__dict__["backend"].headless is True

    def test_loop_is_the_single_source_of_max_steps(
        self,
        composer: ConfigComposer,
    ) -> None:
        """The shipped YAML sets ``max_steps`` where it is actually read.

        ``ReactLoop.max_steps`` bounds the run *and* is what the prompt
        advertises through ``StepInfoObs``.
        """
        run = composer.compose(yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text()))
        assert run.single("agent_loop").max_steps == 30


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
        assert "browser.done" in message
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
                                humanize_mouse=True,
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
        assert ctx.humanize_mouse is True

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
