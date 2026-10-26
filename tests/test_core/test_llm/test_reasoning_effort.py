"""``reasoning_effort`` is a YAML knob that reaches the wire whenever it is set.

It used to be a ``str = "low"`` that was sent only when the model name
matched a hard-coded list of OpenAI reasoning models. A gemini reasoning
model behind an OpenAI-compatible gateway never matched, so YAML could not
set its effort at all — and the default effort there made one grid-crop
call take ~120 s against ~8 s at ``low``.

Pinned here: the value domain is validated at config time, an explicit
value is sent for any model, and the default ``auto`` sends exactly what a
config that never named the knob always sent (``low`` for listed models,
nothing otherwise). ``auto`` is spelled out rather than ``null``: null only
means an optional item is not set (``docs/architecture_design.md`` §4.2.1).
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
import yaml
from openai.types.shared.reasoning_effort import ReasoningEffort

from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
from spectragent.core.llm.providers.openai_base import ReasoningEffortSetting
from spectragent.core.registries import collect_registrars
from tests._helpers.llm_clients import FakeClient

SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"


class TestWhatGoesOnTheWire:
    def test_auto_on_a_listed_reasoning_model_keeps_the_old_default(self) -> None:
        params = ChatOpenAI(model="gpt-5-mini").common_model_params()
        assert params["reasoning_effort"] == "low"
        # OpenAI's reasoning models reject sampling knobs.
        assert "temperature" not in params
        assert "frequency_penalty" not in params

    def test_auto_on_any_other_model_sends_nothing(self) -> None:
        params = ChatOpenAI(model="gpt-4o").common_model_params()
        assert "reasoning_effort" not in params
        assert params["temperature"] == 0.2

    def test_none_is_sent_as_none(self) -> None:
        """``none`` is a wire value (no reasoning), not "not set"."""
        params = ChatOpenAI(model="gpt-5", reasoning_effort="none").common_model_params()
        assert params["reasoning_effort"] == "none"

    def test_explicit_value_reaches_a_model_the_list_does_not_know(self) -> None:
        params = ChatOpenAI(model="gemini-3.5-flash", reasoning_effort="low").common_model_params()
        assert params["reasoning_effort"] == "low"
        # Not an OpenAI reasoning model: sampling knobs stay.
        assert params["temperature"] == 0.2

    def test_explicit_value_overrides_the_listed_default(self) -> None:
        params = ChatOpenAI(model="gpt-5", reasoning_effort="high").common_model_params()
        assert params["reasoning_effort"] == "high"

    def test_azure_responses_path_follows_the_same_rule(self) -> None:
        chat = ChatAzureOpenAI(model="gemini-3.5-flash", reasoning_effort="minimal")
        assert chat.effective_reasoning_effort() == "minimal"
        assert ChatAzureOpenAI(model="gpt-4o").effective_reasoning_effort() is None

    async def test_the_request_actually_carries_it(self, monkeypatch) -> None:
        """Through ``ainvoke``, not just the params helper."""
        chat = ChatOpenAI(model="gemini-3.5-flash", reasoning_effort="low")
        seen: dict[str, Any] = {}

        async def _create(**kwargs: Any) -> Any:
            seen.update(kwargs)
            message = SimpleNamespace(content="ok", tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None,
            )

        fake_client = FakeClient(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
        monkeypatch.setattr(chat, "get_client", lambda: fake_client)

        await chat.ainvoke([UserMessage(content="hi")])
        assert seen["reasoning_effort"] == "low"


def _compose_with_llm(**llm_overrides: Any) -> Any:
    raw = yaml.safe_load(SHIPPED_CONFIG.read_text())
    raw = copy.deepcopy(raw)
    raw["agent"][0]["llm"].update(llm_overrides)
    composer = ConfigComposer(registrars=collect_registrars())
    run = composer.compose(raw)
    agent = run.instance("agent", run.ids("agent")[0])
    return agent.llm


class TestReachableFromYaml:
    def test_yaml_sets_it(self) -> None:
        llm = _compose_with_llm(model="gemini-3.5-flash", reasoning_effort="low")
        assert llm.reasoning_effort == "low"
        assert llm.common_model_params()["reasoning_effort"] == "low"

    def test_omitting_it_is_auto(self) -> None:
        llm = _compose_with_llm()
        assert llm.reasoning_effort == "auto"

    def test_null_is_refused(self) -> None:
        with pytest.raises(Exception, match="reasoning_effort"):
            _compose_with_llm(model="gemini-3.5-flash", reasoning_effort=None)

    def test_a_value_outside_the_domain_fails_at_config_time(self) -> None:
        with pytest.raises(Exception, match="reasoning_effort"):
            _compose_with_llm(model="gemini-3.5-flash", reasoning_effort="lowest")


class TestDeepSeekHasItsOwnSettings:
    """DeepSeek's official API documents ``none | low | high | max``; YAML offers exactly those."""

    def _deepseek(self, effort: str) -> Any:
        return _compose_with_llm(
            provider={"name": "deepseek"}, model="deepseek-flash", reasoning_effort=effort
        )

    def test_max_reaches_the_wire(self) -> None:
        llm = self._deepseek("max")
        assert llm.common_model_params()["reasoning_effort"] == "max"

    def test_an_openai_spelling_deepseek_only_maps_is_refused(self) -> None:
        with pytest.raises(Exception, match="reasoning_effort"):
            self._deepseek("medium")


def test_every_setting_but_auto_is_a_wire_value_the_sdk_accepts() -> None:
    (wire,) = (arg for arg in get_args(ReasoningEffort) if arg is not type(None))
    assert set(get_args(ReasoningEffortSetting)) == {"auto", *get_args(wire)}
