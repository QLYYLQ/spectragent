"""Loop inputs: what a Loop declares in ``__loop_inputs__`` and how it refuses the rest.

``docs/architecture_design.md`` §4.1 "Loop 输入": unknown keys, missing
required keys and mistyped values are reported together, in one
``IncompatibleLoopInputError`` naming the Loop and every key — from
``check_loop_inputs`` and from ``weave()`` alike.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest

from spectragent.core.loops import ReactLoop
from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar, LoopInput
from spectragent.core.loops.validation import IncompatibleLoopInputError
from spectragent.schemas.restoration import Checkpoint


class _Point:
    """A value type only this module knows."""


class _InputLoop(AgentLoopBase):
    __registry_key__ = "test_loop_inputs_loop"
    __required_slots__ = {"build_prompt"}
    __loop_inputs__ = {
        "point": LoopInput(_Point, description="where to start"),
        "hint": LoopInput(str, required=False),
    }

    async def _execute(self, agent: Any, task: str, *, ctx: dict[str, Any], **kwargs: Any) -> Any:
        return ctx["loop_inputs"]


def teardown_module() -> None:
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_loop_inputs_loop") is not None:
        AgentLoopRegistrar.unregister("test_loop_inputs_loop")


class _Agent:
    name = "a"

    def build_prompt(self, obs: Any) -> list[Any]:
        return []


class TestCheckLoopInputs:
    def test_declared_inputs_pass(self) -> None:
        _InputLoop().check_loop_inputs({"point": _Point(), "hint": "left"})

    def test_an_optional_input_may_be_absent(self) -> None:
        _InputLoop().check_loop_inputs({"point": _Point()})

    def test_unknown_key(self) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            _InputLoop().check_loop_inputs({"point": _Point(), "bogus": 1})
        assert err.value.loop_id == "test_loop_inputs_loop"
        assert err.value.unknown == ["bogus"]
        assert (err.value.missing, err.value.mistyped) == ([], {})
        assert "'test_loop_inputs_loop'" in str(err.value)
        assert "bogus" in str(err.value)
        assert "hint, point" in str(err.value)  # what it does accept

    def test_missing_required_key(self) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            _InputLoop().check_loop_inputs({"hint": "left"})
        assert err.value.missing == ["point"]
        assert err.value.unknown == []
        assert "requires point" in str(err.value)

    def test_mistyped_value(self) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            _InputLoop().check_loop_inputs({"point": {"x": 1}})
        assert err.value.mistyped == {"point": ("_Point", "dict")}
        assert "expects point to be _Point, got dict" in str(err.value)

    def test_every_problem_is_reported_at_once(self) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            _InputLoop().check_loop_inputs({"hint": 3, "zeta": 1, "alpha": 2})
        assert err.value.unknown == ["alpha", "zeta"]
        assert err.value.missing == ["point"]
        assert err.value.mistyped == {"hint": ("str", "int")}

    def test_react_accepts_no_input(self) -> None:
        """A checkpoint handed to ``react`` fails loudly instead of running from scratch."""
        assert ReactLoop.__loop_inputs__ == {}
        checkpoint = Checkpoint(trace_dir="/tmp/source-run", step=2)
        with pytest.raises(IncompatibleLoopInputError) as err:
            ReactLoop().check_loop_inputs({"checkpoint": checkpoint})
        assert err.value.loop_id == "react"
        assert err.value.unknown == ["checkpoint"]
        assert "declares: none" in str(err.value)

    def test_react_accepts_an_empty_input_set(self) -> None:
        ReactLoop().check_loop_inputs({})


class TestWeaveChecksToo:
    def test_weave_refuses_before_patching(self) -> None:
        agent = _Agent()
        with pytest.raises(IncompatibleLoopInputError):
            _InputLoop().weave(agent, env=None, loop_inputs={})
        assert not hasattr(agent, "run")

    def test_react_weave_refuses_a_checkpoint(self) -> None:
        class _ReactAgent:
            def build_react_prompt(self, obs: Any) -> list[Any]:
                return []

            def parse_react_output(self, raw: Any) -> list[Any]:
                return []

        checkpoint = Checkpoint(trace_dir="/tmp/source-run", step=0)
        with pytest.raises(IncompatibleLoopInputError, match="checkpoint"):
            ReactLoop().weave(_ReactAgent(), loop_inputs={"checkpoint": checkpoint})

    def test_inputs_reach_execute_read_only(self) -> None:
        agent = _Agent()
        point = _Point()
        _InputLoop().weave(agent, loop_inputs={"point": point})
        seen = asyncio.run(agent.run("t"))  # type: ignore[attr-defined]
        assert isinstance(seen, MappingProxyType)
        assert seen["point"] is point
        with pytest.raises(TypeError):
            seen["point"] = _Point()  # type: ignore[index]

    def test_absent_inputs_reach_execute_as_an_empty_mapping(self) -> None:
        class _Free(AgentLoopBase):
            __registry_key__ = "test_loop_inputs_free"

            async def _execute(
                self, agent: Any, task: str, *, ctx: dict[str, Any], **kwargs: Any
            ) -> Any:
                return ctx["loop_inputs"]

        try:
            agent = _Agent()
            _Free().weave(agent)
            assert asyncio.run(agent.run("t")) == {}  # type: ignore[attr-defined]
        finally:
            AgentLoopRegistrar.unregister("test_loop_inputs_free")

    def test_no_loop_ctx_attribute(self) -> None:
        loop = _InputLoop()
        loop.weave(_Agent(), loop_inputs={"point": _Point()})
        assert not hasattr(loop, "ctx")


class TestDeclaration:
    def test_a_dict_declaration_is_frozen(self) -> None:
        assert isinstance(_InputLoop.__loop_inputs__, MappingProxyType)
        with pytest.raises(TypeError):
            _InputLoop.__loop_inputs__["more"] = LoopInput(int)  # type: ignore[index]

    def test_a_set_of_names_is_refused_at_class_creation(self) -> None:
        with pytest.raises(TypeError, match="LoopInput"):

            class _Bad(AgentLoopBase):
                __registry_key__ = "test_loop_inputs_bad"
                __loop_inputs__ = {"checkpoint"}  # type: ignore[assignment]

        assert AgentLoopRegistrar.get_or_none("test_loop_inputs_bad") is None
