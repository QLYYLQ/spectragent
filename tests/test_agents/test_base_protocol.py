"""Tests for BaseAgent Protocol after M12 refactoring."""

import inspect

from spectragent.agents.base import BaseAgent


class TestBaseAgentProtocol:
    """Verify the Protocol shape after M12 — passive domain-knowledge provider."""

    def test_has_agent_name(self):
        assert hasattr(BaseAgent, "agent_name")

    def test_has_domain(self):
        assert hasattr(BaseAgent, "domain")

    def test_has_no_observation_types(self):
        """Deleted: it contradicted ``__wiring__`` and nothing read it.

        ``BrowserUseAgent.observation_types`` answered
        ``{"dom", "browser_meta"}`` while its wiring required
        ``["dom", "screenshot", "task"]``, and the two disagreed for months
        because negotiation reads the wiring and never this. What an agent
        observes is now said once, in ``__wiring__``, narrowed per instance
        by ``negotiation_demand``.
        """
        assert not hasattr(BaseAgent, "observation_types")

    def test_has_system_prompt(self):
        assert hasattr(BaseAgent, "system_prompt")

    def test_has_get_action_space(self):
        assert hasattr(BaseAgent, "get_action_space")
        assert callable(getattr(BaseAgent, "get_action_space", None))

    def test_no_step_method(self):
        """step() was removed in M12 — agents no longer drive execution."""
        members = dict(inspect.getmembers(BaseAgent))
        assert "step" not in members or not callable(members.get("step"))

    def test_no_run_method(self):
        """run() was removed in M12 — AgentRuntime drives execution."""
        members = dict(inspect.getmembers(BaseAgent))
        assert "run" not in members or not callable(members.get("run"))

    def test_no_reset_method(self):
        """reset() was removed in M12 — lifecycle managed by Runtime."""
        members = dict(inspect.getmembers(BaseAgent))
        assert "reset" not in members or not callable(members.get("reset"))

    def test_is_runtime_checkable(self):
        """Protocol should be runtime_checkable for isinstance checks."""

        class FakeAgent:
            @property
            def agent_name(self) -> str:
                return "fake"

            @property
            def domain(self) -> str:
                return "test"

            @property
            def system_prompt(self):
                return None

            def get_action_space(self):
                return []

        assert isinstance(FakeAgent(), BaseAgent)
