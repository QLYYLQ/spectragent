"""``reasoning_effort`` is a YAML knob that reaches the wire whenever it is set.

It used to be a ``str = "low"`` that was sent only when the model name
matched a hard-coded list of OpenAI reasoning models. A gemini reasoning
model behind an OpenAI-compatible gateway never matched, so YAML could not
set its effort at all — and the default effort there made one grid-crop
call take ~120 s against ~8 s at ``low``.

Pinned here: the value domain is validated at config time, an explicit
value is sent for any model, and a config that never names the knob sends
exactly what it sent before (``low`` for listed models, nothing otherwise).
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
from spectragent.scripts.generate_all import _collect_registrars

SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"


class TestWhatGoesOnTheWire:
    def test_unset_on_a_listed_reasoning_model_keeps_the_old_default(self) -> None:
        params = ChatOpenAI(model="gpt-5-mini")._common_model_params()
        assert params["reasoning_effort"] == "low"
        # OpenAI's reasoning models reject sampling knobs.
        assert "temperature" not in params
        assert "frequency_penalty" not in params

    def test_unset_on_any_other_model_sends_nothing(self) -> None:
        params = ChatOpenAI(model="gpt-4o")._common_model_params()
        assert "reasoning_effort" not in params
        assert params["temperature"] == 0.2

    def test_explicit_value_reaches_a_model_the_list_does_not_know(self) -> None:
        params = ChatOpenAI(
            model="gemini-3.5-flash", reasoning_effort="low"
        )._common_model_params()
        assert params["reasoning_effort"] == "low"
        # Not an OpenAI reasoning model: sampling knobs stay.
        assert params["temperature"] == 0.2

    def test_explicit_value_overrides_the_listed_default(self) -> None:
        params = ChatOpenAI(model="gpt-5", reasoning_effort="high")._common_model_params()
        assert params["reasoning_effort"] == "high"

    def test_azure_responses_path_follows_the_same_rule(self) -> None:
        chat = ChatAzureOpenAI(model="gemini-3.5-flash", reasoning_effort="minimal")
        assert chat._effective_reasoning_effort() == "minimal"
        assert ChatAzureOpenAI(model="gpt-4o")._effective_reasoning_effort() is None

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

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )
        monkeypatch.setattr(chat, "get_client", lambda: fake_client)

        await chat.ainvoke([UserMessage(content="hi")])
        assert seen["reasoning_effort"] == "low"


def _compose_with_llm(**llm_overrides: Any) -> Any:
    raw = yaml.safe_load(SHIPPED_CONFIG.read_text())
    raw = copy.deepcopy(raw)
    raw["agent"][0]["llm"].update(llm_overrides)
    composer = ConfigComposer(registrars=_collect_registrars())
    run = composer.compose(raw)
    agent = run.instance("agent", run.ids("agent")[0])
    return agent.llm


class TestReachableFromYaml:
    def test_yaml_sets_it(self) -> None:
        llm = _compose_with_llm(model="gemini-3.5-flash", reasoning_effort="low")
        assert llm.reasoning_effort == "low"
        assert llm._common_model_params()["reasoning_effort"] == "low"

    def test_omitting_it_leaves_it_unset(self) -> None:
        llm = _compose_with_llm()
        assert llm.reasoning_effort is None

    def test_a_value_outside_the_domain_fails_at_config_time(self) -> None:
        with pytest.raises(Exception, match="reasoning_effort"):
            _compose_with_llm(model="gemini-3.5-flash", reasoning_effort="lowest")
