"""The task text a setup completes and the episode the environment ends, routed through the environment.

``docs/architecture_design.md`` §4.3 "任务文本与 episode 结束的路由" and §4.9:

- ``task_text_setter(context)`` routes the setup context exactly as setup does,
  asks every ``TaskTextFromSetup`` capability about its own slice, and names the
  one whose setup completes the task text; two are refused, since a run has one
  task text.
- ``task_text_from_setup()`` asks them after setup and builds the record from
  the one text a setup completed, adding the capability's id; two texts, or a
  blank one, are refused.
- ``episode_over()`` asks every ``EpisodeEnd`` capability in order and stops at
  the first ``True``; with none, no capability is touched. Failures propagate.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import EpisodeEnd, TaskTextFromSetup
from spectragent.core.environments import Environment, EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.task import SetupTaskText
from tests._helpers.assembly import make_env


class _Plain(CapabilityBase):
    """A capability that neither completes the task text nor ends an episode."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "text_route_plain"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.asked: list[str] = []

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("neither question executes an action")

    async def setup(self, *, negotiation: Any = None, start_url: str | None = None) -> None:
        raise AssertionError("neither question sets anything up")


class _Setter(_Plain):
    """Completes the task text at setup when *sets*; after setup it hands back *text*.

    Its setup declares ``goal_seed``, so the routed context has a key of its own.
    """

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "text_route_setter"

    def __init__(self, *, sets: bool = True, text: object = "Do it: now.", **kw: Any) -> None:
        super().__init__(**kw)
        self.sets = sets
        self.text = text
        self.contexts: list[Mapping[str, Any]] = []

    async def setup(self, *, negotiation: Any = None, goal_seed: int | None = None) -> None:
        raise AssertionError("neither question sets anything up")

    def sets_task_text(self, context: Mapping[str, Any]) -> bool:
        self.asked.append("sets_task_text")
        self.contexts.append(context)
        return self.sets

    async def task_text_from_setup(self) -> str | None:
        self.asked.append("task_text_from_setup")
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text  # type: ignore[return-value]  # a contract breach on purpose


class _OtherSetter(_Setter):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "text_route_other_setter"


class _Ender(_Plain):
    """Answers *over* when asked whether the episode is over, or raises it when it is an exception."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "text_route_ender"

    def __init__(self, over: object = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.over = over

    async def episode_over(self) -> bool:
        self.asked.append("episode_over")
        if isinstance(self.over, BaseException):
            raise self.over
        return self.over  # type: ignore[return-value]  # a contract breach on purpose


class _OtherEnder(_Ender):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "text_route_other_ender"


def _env(**capabilities: CapabilityBase) -> EnvironmentBase:
    return make_env(EnvironmentBase, dict(capabilities))


class TestTheContract:
    def test_the_protocols_are_structural(self) -> None:
        assert isinstance(_Setter(), TaskTextFromSetup) and not isinstance(_Setter(), EpisodeEnd)
        assert isinstance(_Ender(), EpisodeEnd) and not isinstance(_Ender(), TaskTextFromSetup)
        assert not isinstance(_Plain(), TaskTextFromSetup)
        assert not isinstance(_Plain(), EpisodeEnd)

    def test_the_environment_protocol_declares_them_and_the_base_implements_them(self) -> None:
        for method in ("task_text_setter", "task_text_from_setup", "episode_over"):
            assert callable(getattr(Environment, method))
            assert callable(getattr(EnvironmentBase, method))
        assert not inspect.iscoroutinefunction(EnvironmentBase.task_text_setter), (
            "read in prepare()"
        )
        assert inspect.iscoroutinefunction(EnvironmentBase.task_text_from_setup), (
            "a hook records it"
        )
        assert inspect.iscoroutinefunction(EnvironmentBase.episode_over)

    def test_the_capability_base_gives_no_default(self) -> None:
        for method in ("sets_task_text", "task_text_from_setup", "episode_over"):
            assert not hasattr(CapabilityBase, method)


class TestWhoCompletesTheTaskText:
    def test_no_capability_that_can_complete_it_means_none_and_nobody_is_asked(self) -> None:
        plain = _Plain()
        assert _env(plain=plain).task_text_setter({"start_url": "https://example.com"}) is None
        assert plain.asked == []

    def test_the_capability_whose_setup_completes_it_is_named(self) -> None:
        env = _env(plain=_Plain(), setter=_Setter())
        assert env.task_text_setter({"goal_seed": 7}) == "text_route_setter"

    def test_a_capability_that_will_not_complete_it_this_time_is_not_named(self) -> None:
        assert _env(setter=_Setter(sets=False)).task_text_setter({}) is None

    def test_each_capability_is_asked_about_its_own_slice_only(self) -> None:
        setter = _Setter()
        env = _env(plain=_Plain(), setter=setter)
        env.task_text_setter({"start_url": "https://example.com", "goal_seed": 7})
        env.task_text_setter({"text_route_setter": {"goal_seed": 8}})
        assert setter.contexts == [{"goal_seed": 7}, {"goal_seed": 8}]

    def test_a_key_no_capability_accepts_is_refused_as_setup_refuses_it(self) -> None:
        with pytest.raises(ValueError, match=r"setup key\(s\) \['goal'\] are accepted by none"):
            _env(setter=_Setter()).task_text_setter({"goal": "Do it."})

    def test_two_capabilities_that_would_both_complete_it_are_refused_by_name(self) -> None:
        env = _env(first=_Setter(), second=_OtherSetter())
        with pytest.raises(ValueError) as refused:
            env.task_text_setter({})
        message = str(refused.value)
        assert "['text_route_setter', 'text_route_other_setter']" in message
        assert "a run has one task text" in message

    def test_it_reads_configuration_only(self) -> None:
        setter = _Setter()
        _env(setter=setter).task_text_setter({})
        assert setter.asked == ["sets_task_text"], "nothing after setup was asked for"


class TestTheTextSetupCompleted:
    async def test_the_completed_text_comes_back_with_the_capabilitys_id(self) -> None:
        text = "Follow the instructions shown on the top of the screen: Click on Submit."
        env = _env(plain=_Plain(), setter=_Setter(text=text))
        assert await env.task_text_from_setup() == SetupTaskText(
            capability_id="text_route_setter", text=text
        )

    async def test_no_text_means_none(self) -> None:
        assert await _env(plain=_Plain()).task_text_from_setup() is None
        assert await _env(setter=_Setter(text=None)).task_text_from_setup() is None

    async def test_a_capability_that_completed_none_does_not_count(self) -> None:
        env = _env(first=_Setter(text=None), second=_OtherSetter(text="Do it: now."))
        made = await env.task_text_from_setup()
        assert made is not None and made.capability_id == "text_route_other_setter"

    async def test_two_completed_texts_are_refused_by_name(self) -> None:
        env = _env(first=_Setter(), second=_OtherSetter())
        with pytest.raises(ValueError, match="text_route_other_setter'] each completed"):
            await env.task_text_from_setup()

    @pytest.mark.parametrize("text", ["", "  \n"])
    async def test_a_blank_text_is_refused(self, text: str) -> None:
        with pytest.raises(ValidationError, match="its setup made no text"):
            await _env(setter=_Setter(text=text)).task_text_from_setup()

    async def test_something_other_than_a_string_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="string_type"):
            await _env(setter=_Setter(text=42)).task_text_from_setup()

    async def test_a_capabilitys_failure_propagates(self) -> None:
        with pytest.raises(ConnectionError, match="gone"):
            await _env(setter=_Setter(text=ConnectionError("gone"))).task_text_from_setup()


class TestWhetherTheEpisodeIsOver:
    async def test_no_capability_that_can_end_it_means_not_over_and_nobody_is_asked(self) -> None:
        plain = _Plain()
        assert await _env(plain=plain).episode_over() is False
        assert plain.asked == []

    async def test_every_capability_is_asked_in_order_while_none_says_over(self) -> None:
        first, second = _Ender(False), _OtherEnder(False)
        assert await _env(first=first, plain=_Plain(), second=second).episode_over() is False
        assert (first.asked, second.asked) == (["episode_over"], ["episode_over"])

    async def test_the_first_that_says_over_decides_and_the_rest_are_not_asked(self) -> None:
        first, second = _Ender(True), _OtherEnder(False)
        assert await _env(first=first, second=second).episode_over() is True
        assert second.asked == []

    async def test_a_later_capability_can_say_it_is_over(self) -> None:
        assert await _env(first=_Ender(False), second=_OtherEnder(True)).episode_over() is True

    async def test_a_capabilitys_failure_propagates(self) -> None:
        with pytest.raises(ConnectionError, match="the service is gone"):
            await _env(ender=_Ender(ConnectionError("the service is gone"))).episode_over()

    async def test_a_cancellation_propagates(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _env(ender=_Ender(asyncio.CancelledError())).episode_over()

    @pytest.mark.parametrize("answer", [None, 1, "yes"])
    async def test_an_answer_that_is_not_a_bool_is_refused(self, answer: object) -> None:
        with pytest.raises(TypeError, match="answered episode_over\\(\\) with .*, not a bool"):
            await _env(ender=_Ender(answer)).episode_over()
