"""Providers must consult the declarative model_families table (§6.4.5, §6.4.3).

Before this, `core/llm/model_families/` had zero provider references while
`_base.py`'s docstring claimed otherwise, and providers re-sniffed model names
inline in four places.
"""

from __future__ import annotations

import pytest

from spectragent.core.llm.model_families import (
    ClaudeFamily,
    GeminiFamily,
    GenericFamily,
    GPTFamily,
    detect_model_family,
)
from spectragent.core.llm.providers.anthropic import ChatAnthropic
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter
from spectragent.core.llm.providers.openai_vercel import ChatVercel
from tests._helpers.llm_clients import FakeClient


class TestModelFamilyDetection:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-4o", GPTFamily),
            ("openai/gpt-4o", GPTFamily),
            ("claude-sonnet-4-20250514", ClaudeFamily),
            ("anthropic/claude-sonnet-4", ClaudeFamily),
            ("gemini-2.5-pro", GeminiFamily),
            ("google/gemini-2.5-pro", GeminiFamily),
            ("some-unknown-model", GenericFamily),
        ],
    )
    def test_detect(self, model: str, expected: object) -> None:
        assert detect_model_family(model) is expected


class TestProvidersExposeFamily:
    def test_openai_family(self) -> None:
        assert ChatOpenAI(model="gpt-4o").model_family is GPTFamily

    def test_anthropic_family(self) -> None:
        assert ChatAnthropic(model="claude-sonnet-4-20250514").model_family is ClaudeFamily


class TestStrictModeComesFromTable:
    def test_gpt_uses_strict(self) -> None:
        assert ChatOpenAI(model="gpt-4o")._use_strict_schema() is True

    def test_non_gpt_does_not_use_strict(self) -> None:
        """A Llama behind an OpenAI-compatible endpoint has no strict mode."""
        assert ChatOpenAI(model="meta-llama/Llama-3.3-70B")._use_strict_schema() is False

    def test_response_format_strict_flag_tracks_the_table(self) -> None:
        """The `"strict"` sent to the API and the schema rewrite stay in sync."""
        from pydantic import BaseModel

        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        class _Out(BaseModel):
            a: str
            b: str | None = None

        strict_schema = SchemaOptimizer.create_optimized_json_schema(_Out, strict=True)
        loose_schema = SchemaOptimizer.create_optimized_json_schema(_Out, strict=False)
        assert sorted(strict_schema["required"]) == ["a", "b"]
        assert loose_schema["required"] == ["a"]


class TestVercelPromptJsonRouting:
    """`_vercel_needs_prompt_json` used to hardcode startswith("google/")."""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("google/gemini-2.5-pro", True),  # gemini dialect
            ("anthropic/claude-sonnet-4", True),  # no native json_schema
            ("openai/gpt-4o", False),  # native response_format
        ],
    )
    def test_routing(self, model: str, expected: bool) -> None:
        chat = ChatVercel(model=model)
        # Neutralise the orthogonal reasoning-model heuristic.
        chat.reasoning_models = []
        assert chat._vercel_needs_prompt_json() is expected


class TestNoStructuredOutputOptOut:
    """``dont_force_structured_output`` is gone, and must stay gone.

    It sent the request *without* ``response_format`` and hoped the model
    answered JSON anyway. That was never a property of a model — it was a
    workaround for a gateway whose OpenAI-compatible route translates to a
    vendor's native API and loses the schema in the process. The measured
    fix is to stop translating (gemini → ``model_type: google``, claude →
    ``model_type: anthropic``, both at the gateway *root*), so the knob is
    deleted rather than kept as a shim.

    Worth a scan rather than a memory: the flag is what a config author
    reaches for the next time a gateway swallows a schema, and re-adding it
    would put the workaround back in place of the diagnosis.
    """

    def _provider_modules(self):
        import importlib
        import pkgutil

        import spectragent.core.llm.providers as pkg

        for module in pkgutil.iter_modules(pkg.__path__):
            yield importlib.import_module(f"{pkg.__name__}.{module.name}")

    def test_no_provider_declares_it(self) -> None:
        import dataclasses

        offenders: list[str] = []
        for module in self._provider_modules():
            for name, obj in vars(module).items():
                if not dataclasses.is_dataclass(obj) or not isinstance(obj, type):
                    continue
                fields = {f.name for f in dataclasses.fields(obj)}
                if "dont_force_structured_output" in fields:
                    offenders.append(f"{module.__name__}.{name}")
        assert offenders == [], (
            "A gateway that drops response_format is a routing bug, not a "
            "model capability — see the routing rule in "
            ".agents/skills/af-add-an-llm-provider/references/guide.md §1: " + ", ".join(offenders)
        )

    async def test_the_structured_path_always_sends_response_format(self) -> None:
        """The behaviour the flag used to switch off, asserted on the wire.

        Deleting the field is not the same as deleting the branch it
        guarded — a provider could keep an equivalent conditional under
        another name. So this drives the real request and looks at what
        ``create()`` was called with, rather than at the source text: a
        rename or a refactor of the call must not be able to fail this,
        and a second schema-less ``create()`` must not be able to pass it.
        """
        import json
        from types import SimpleNamespace
        from typing import Any

        from pydantic import BaseModel

        from spectragent.core.llm.messages import UserMessage

        class _Shape(BaseModel):
            index: int

        captured: dict[str, Any] = {}

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({"index": 3}), tool_calls=None
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        chat = ChatOpenAI(model="gpt-4o", api_key="stub", base_url="https://gw.invalid/v1")
        chat.get_client = lambda: FakeClient(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        await chat.ainvoke([UserMessage(content="hi")], _Shape)

        response_format = captured.get("response_format")
        assert response_format is not None, (
            "the structured path reached the API with no response_format — "
            "which is exactly what dont_force_structured_output used to do"
        )
        schema = response_format["json_schema"]["schema"]
        assert set(schema["properties"]) == {"index"}

    def test_no_provider_reintroduces_a_schema_knob(self) -> None:
        """The other three booleans of the same shape are gone too.

        ``add_schema_to_system_prompt`` (paste the schema into the system
        message in case the model ignores ``response_format``) and
        ``remove_min_items_from_schema`` / ``remove_defaults_from_schema``
        (let a YAML hand-edit the compiled schema) were the same idea:
        config compensating for a dialect instead of declaring it.
        ``architecture_design.md`` §4.5 puts a dialect rule in the
        optimizer, and a *model-family* rule on ``ModelFamily``. Nothing in
        the repo ever set any of them.
        """
        import dataclasses

        banned = {
            "dont_force_structured_output",
            "add_schema_to_system_prompt",
            "remove_min_items_from_schema",
            "remove_defaults_from_schema",
        }
        offenders: list[str] = []
        for module in self._provider_modules():
            for name, obj in vars(module).items():
                if not dataclasses.is_dataclass(obj) or not isinstance(obj, type):
                    continue
                for field in banned & {f.name for f in dataclasses.fields(obj)}:
                    offenders.append(f"{module.__name__}.{name}.{field}")
        assert offenders == [], (
            "A schema dialect is a fact about a wire format, not a config "
            "knob — put it in SchemaOptimizer's dialect-transforms section "
            "and gate it on ModelFamily: " + ", ".join(sorted(offenders))
        )


class TestAnthropicSeed:
    """§6.4.3 — `AsyncMessages.create` has no `seed` and no `**kwargs`."""

    def test_seed_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="no `seed` parameter"):
            ChatAnthropic(model="claude-sonnet-4-20250514", seed=42)

    def test_no_seed_is_fine(self) -> None:
        chat = ChatAnthropic(model="claude-sonnet-4-20250514")
        assert "seed" not in chat._get_invoke_params()

    def test_sdk_really_has_no_seed(self) -> None:
        """Guard against the fix silently becoming wrong if the SDK adds seed."""
        import inspect

        from anthropic.resources.messages import AsyncMessages

        sig = inspect.signature(AsyncMessages.create)
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        assert "seed" not in sig.parameters
        assert not has_var_kw


class TestAzureResponsesApiFlag:
    """§6.4.3 — the field is typed `str`, so `isinstance(..., bool)` never fired."""

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_strings_enable(self, value: str) -> None:
        chat = ChatAzureOpenAI(model="gpt-4o", use_responses_api=value)
        assert chat._should_use_responses_api() is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
    def test_falsy_strings_disable_even_for_responses_only_models(self, value: str) -> None:
        chat = ChatAzureOpenAI(model="gpt-5-codex", use_responses_api=value)
        assert chat._should_use_responses_api() is False

    def test_auto_falls_back_to_model_list(self) -> None:
        assert ChatAzureOpenAI(model="gpt-5-codex")._should_use_responses_api() is True
        assert ChatAzureOpenAI(model="gpt-4o")._should_use_responses_api() is False

    def test_garbage_raises_instead_of_silently_meaning_auto(self) -> None:
        chat = ChatAzureOpenAI(model="gpt-4o", use_responses_api="ture")
        with pytest.raises(ValueError, match="Invalid use_responses_api"):
            chat._should_use_responses_api()


class TestOpenRouterExtraBody:
    """§6.4.3 — extra_body is a transport param, not a splat of top-level kwargs."""

    async def test_extra_body_is_nested(self) -> None:
        captured: dict = {}

        chat = ChatOpenRouter(model="openai/gpt-4o", extra_body={"provider": {"order": ["X"]}})

        async def _fake_standard(messages, output_format, extra_kwargs, **kwargs):
            captured.update(extra_kwargs)
            return None

        chat._standard_ainvoke = _fake_standard  # type: ignore[method-assign]
        await chat.ainvoke([])

        assert captured["extra_body"] == {"provider": {"order": ["X"]}}
        # The old code splatted the dict, producing a bogus top-level `provider=`.
        assert "provider" not in captured

    async def test_http_referer_still_goes_to_headers(self) -> None:
        captured: dict = {}
        chat = ChatOpenRouter(model="openai/gpt-4o", http_referer="https://example.test")

        async def _fake_standard(messages, output_format, extra_kwargs, **kwargs):
            captured.update(extra_kwargs)
            return None

        chat._standard_ainvoke = _fake_standard  # type: ignore[method-assign]
        await chat.ainvoke([])
        assert captured["extra_headers"] == {"HTTP-Referer": "https://example.test"}
