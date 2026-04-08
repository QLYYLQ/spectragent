"""Tests for AgentLoop registrar and base class."""

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.compatibility import ReactCompatible


class TestLoopRegistration:
    """Loop subclasses auto-register via conscribe metaclass."""

    def test_register_concrete_loop(self) -> None:
        class DummyLoop(AgentLoopBase):
            __registry_key__ = "test_dummy"
            __requires_protocol__ = ReactCompatible

            async def run(self, agent, env, task, **kwargs):
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

            async def run(self, agent, env, task, **kwargs):
                return {}

        instance = IdLoop()
        assert instance.loop_id == "test_id_loop"
        AgentLoopRegistrar.unregister("test_id_loop")

    def test_requires_protocol_default_none(self) -> None:
        class NoProtoLoop(AgentLoopBase):
            __registry_key__ = "test_no_proto"

            async def run(self, agent, env, task, **kwargs):
                return {}

        assert NoProtoLoop.__requires_protocol__ is None
        AgentLoopRegistrar.unregister("test_no_proto")

    def test_requires_protocol_set(self) -> None:
        class ProtoLoop(AgentLoopBase):
            __registry_key__ = "test_proto"
            __requires_protocol__ = ReactCompatible

            async def run(self, agent, env, task, **kwargs):
                return {}

        assert ProtoLoop.__requires_protocol__ is ReactCompatible
        AgentLoopRegistrar.unregister("test_proto")
