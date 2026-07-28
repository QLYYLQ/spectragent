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

from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.composer import (
    ComposeError,
    ConfigComposer,
    DuplicateInstanceIdError,
)
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
