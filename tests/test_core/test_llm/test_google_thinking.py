"""A configured Gemini thinking level survives composition and SDK encoding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from google.genai import types

from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.google import ChatGoogle
from spectragent.core.registries import collect_registrars

SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"


def _composed_google(model: str, **overrides: Any) -> ChatGoogle:
    raw = yaml.safe_load(SHIPPED_CONFIG.read_text())
    raw["agent"][0]["llm"] = {
        "model_type": "google",
        "model": model,
        "api_key": "test-only",
        "base_url": "https://gateway.example.test",
        "max_retries": 1,
        **overrides,
    }
    run = ConfigComposer(registrars=collect_registrars()).compose(raw)
    return run.instance("agent", run.ids("agent")[0]).llm


async def _wire_config(chat: ChatGoogle) -> dict[str, Any]:
    """Use the real Google SDK, with only its HTTP transport replaced."""
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.url.path == f"/v1beta/models/{chat.model}:generateContent"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        chat.http_options = types.HttpOptions(base_url=chat.base_url, httpx_async_client=client)
        result = await chat.ainvoke([UserMessage(content="hi")])
    assert result.completion == "ok"
    assert len(requests) == 1
    return requests[0]["generationConfig"]


@pytest.mark.parametrize("level", ["low", "medium", "high"])
async def test_explicit_flash_level_from_yaml_reaches_native_wire(level: str) -> None:
    config = await _wire_config(_composed_google("gemini-3.8-flash", thinking_level=level))
    assert config["thinkingConfig"] == {"thinking_level": level.upper()}


@pytest.mark.parametrize("level", ["minimal", "lowest"])
async def test_unsupported_flash_level_is_refused_before_any_request(level: str) -> None:
    chat = _composed_google("gemini-3.8-flash", thinking_level=level)
    with pytest.raises(ValueError, match="choose one of low, medium, high"):
        await _wire_config(chat)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-3.8-flash", None),
        ("gemini-3.9-flash", None),
        ("gemini-3.8-flash-unverified-alias", None),
        ("gemini-3.5-flash", None),
        ("gemini-2.0-flash", None),
        ("gemini-3-pro-preview", {"thinking_level": "LOW"}),
        ("gemini-3-flash-preview", {"thinking_budget": -1}),
        ("gemini-2.5-flash", {"thinking_budget": -1}),
        ("gemini-flash-latest", {"thinking_budget": -1}),
    ],
)
async def test_omitted_thinking_keeps_existing_request_defaults(model: str, expected: Any) -> None:
    config = await _wire_config(_composed_google(model))
    assert config.get("thinkingConfig") == expected


@pytest.mark.parametrize("level", ["minimal", "medium"])
async def test_legacy_pro_keeps_its_level_fallback(level: str) -> None:
    config = await _wire_config(_composed_google("gemini-3-pro-preview", thinking_level=level))
    assert config["thinkingConfig"] == {"thinking_level": "LOW"}


async def test_unknown_version_does_not_inherit_verified_level_support() -> None:
    config = await _wire_config(_composed_google("gemini-3.9-flash", thinking_level="low"))
    assert "thinkingConfig" not in config


async def test_explicit_budget_and_level_precedence_are_unchanged() -> None:
    config = await _wire_config(_composed_google("gemini-3.8-flash", thinking_budget=1024))
    assert config["thinkingConfig"] == {"thinking_budget": 1024}
    config = await _wire_config(
        _composed_google("gemini-3.8-flash", thinking_level="low", thinking_budget=1024)
    )
    assert config["thinkingConfig"] == {"thinking_level": "LOW"}
