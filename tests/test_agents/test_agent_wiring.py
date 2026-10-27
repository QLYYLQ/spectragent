"""Tests for AgentBase __wiring__ — M12."""

from spectragent.agents._registrar import AgentBase, AgentRegistrar


class TestAgentBaseWiring:
    def test_default_wiring_has_agent_loop(self):
        """AgentBase default wiring allows all loops (Mode 1 auto-discovery)."""
        assert "agent_loop" in AgentBase.__wiring__
        assert AgentBase.__wiring__["agent_loop"] == "agent_loop"

    def test_default_wiring_has_llm(self):
        """AgentBase default wiring allows all LLMs (Mode 1 auto-discovery)."""
        assert "llm" in AgentBase.__wiring__
        assert AgentBase.__wiring__["llm"] == "llm"

    def test_agent_id_is_the_registry_key(self):
        class _TestAgent(AgentBase):
            __registry_key__ = "_test_wiring_domain"

            @property
            def agent_name(self):
                return "test"

            @property
            def system_prompt(self):
                return None

        agent = _TestAgent()
        assert agent.agent_id == "_test_wiring_domain"

    def test_subclass_narrows_wiring(self):
        """Subclass can narrow __wiring__ via MRO merge."""

        class _NarrowAgent(AgentBase):
            __registry_key__ = "_test_narrow_wiring"
            __wiring__ = {
                "agent_loop": ("agent_loop", ["react"]),
            }

            @property
            def agent_name(self):
                return "narrow"

            @property
            def system_prompt(self):
                return None

        # Subclass wiring narrows agent_loop to react only
        assert _NarrowAgent.__wiring__["agent_loop"] == ("agent_loop", ["react"])

    def test_subclass_adds_observation_wiring(self):
        """Subclass can add observation wiring."""

        class _ObsAgent(AgentBase):
            __registry_key__ = "_test_obs_wiring"
            __wiring__ = {
                "observation": ("observation", ["dom", "screenshot"], ["browser_meta"]),
            }

            @property
            def agent_name(self):
                return "obs"

            @property
            def system_prompt(self):
                return None

        wiring = _ObsAgent.__wiring__
        assert "observation" in wiring
        obs_wiring = wiring["observation"]
        assert obs_wiring[0] == "observation"
        assert "dom" in obs_wiring[1]
        assert "screenshot" in obs_wiring[1]

    def test_registration(self):
        """AgentBase subclasses auto-register via conscribe."""

        class _RegTestAgent(AgentBase):
            __registry_key__ = "_test_reg_agent"

            @property
            def agent_name(self):
                return "reg"

            @property
            def system_prompt(self):
                return None

        cls = AgentRegistrar.get("_test_reg_agent")
        assert cls is _RegTestAgent
