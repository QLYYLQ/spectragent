"""Tests for AgentLoop registrar and base class."""

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar


class TestLoopRegistration:
    """Loop subclasses auto-register via conscribe metaclass."""

    def test_register_concrete_loop(self) -> None:
        class DummyLoop(AgentLoopBase):
            __registry_key__ = "test_dummy"
            __required_slots__ = {"build_react_prompt"}

            async def _execute(self, agent, task, **kwargs):
                return {"done": True}

        assert AgentLoopRegistrar.get("test_dummy") is DummyLoop
        AgentLoopRegistrar.unregister("test_dummy")

    def test_abstract_not_registered(self) -> None:
        class AbstractLoop(AgentLoopBase):
            __abstract__ = True

        assert AgentLoopRegistrar.get_or_none("abstract_loop") is None

    def test_loop_id_property(self) -> None:
        class IdLoop(AgentLoopBase):
            __registry_key__ = "test_id_loop"

            async def _execute(self, agent, task, **kwargs):
                return {}

        instance = IdLoop()
        assert instance.loop_id == "test_id_loop"
        AgentLoopRegistrar.unregister("test_id_loop")

    def test_required_slots_default_empty(self) -> None:
        class NoSlotLoop(AgentLoopBase):
            __registry_key__ = "test_no_slot"

            async def _execute(self, agent, task, **kwargs):
                return {}

        assert NoSlotLoop.__required_slots__ == set()
        AgentLoopRegistrar.unregister("test_no_slot")

    def test_required_slots_declared(self) -> None:
        class SlotLoop(AgentLoopBase):
            __registry_key__ = "test_slot"
            __required_slots__ = {"build_react_prompt", "parse_react_output"}

            async def _execute(self, agent, task, **kwargs):
                return {}

        assert SlotLoop.__required_slots__ == {"build_react_prompt", "parse_react_output"}
        AgentLoopRegistrar.unregister("test_slot")


class TestShippedLoops:
    """The loops and restorers the package ships are registered under their keys."""

    def test_react_and_branch(self) -> None:
        from spectragent.core.loops import BranchLoop, ReactLoop

        assert AgentLoopRegistrar.get("react") is ReactLoop
        assert AgentLoopRegistrar.get("branch") is BranchLoop
        assert issubclass(BranchLoop, ReactLoop)

    def test_react_slot_sets(self) -> None:
        """The ReAct slots, pinned (arch §4.1): the last two are the training export's."""
        from spectragent.core.loops import BranchLoop, ReactLoop

        assert ReactLoop.__required_slots__ == {"build_react_prompt", "parse_react_output"}
        assert ReactLoop.__optional_slots__ == {
            "get_react_output_model",
            "prepare",
            "format_react_output",
            "parse_react_thought",
        }
        assert BranchLoop.__required_slots__ == ReactLoop.__required_slots__
        assert BranchLoop.__optional_slots__ == ReactLoop.__optional_slots__

    def test_prefix_replay_restorer(self) -> None:
        from spectragent.core.loops.restore import (
            PrefixReplayRestorer,
            Restorer,
            RestorerRegistrar,
        )

        assert RestorerRegistrar.get("prefix_replay") is PrefixReplayRestorer
        assert isinstance(PrefixReplayRestorer(), Restorer)
