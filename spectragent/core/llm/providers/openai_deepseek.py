"""ChatDeepSeek — DeepSeek's official API, the OpenAI protocol with DeepSeek's own parameters.

Everything below was measured against https://api.deepseek.com on 2026-10-26
(``deepseek-flash`` and ``deepseek-v4-pro``) and matches
https://api-docs.deepseek.com/zh-cn/api/create-chat-completion:

* There is no ``json_schema`` response format: strict or not, ``/v1`` or
  ``/beta``, thinking on or off, it answers HTTP 400 ``This response_format
  type is unavailable now``. The family table says so
  (``DeepSeekFamily.supports_json_schema``), so structured output takes the
  base's prompt-schema path, with ``json_object`` as its response format.
* Thinking is on by default (effort ``high``). In thinking mode a
  ``tool_choice`` naming a function, or ``required``, is refused with HTTP 400
  ``Thinking mode does not support this tool_choice``, so a schema cannot be
  forced through a tool call either.
* ``max_completion_tokens`` is accepted and silently ignored (40 asked, 2457
  generated); ``max_tokens`` is honoured and stops with ``finish_reason:
  length``.
* In thinking mode ``temperature`` has no effect; ``frequency_penalty`` and
  ``presence_penalty`` have none in either mode. Neither is an error.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params.response_format_json_object import ResponseFormatJSONObject
from pydantic import Field

from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase, resolve_http_transport

#: ``reasoning_effort``'s settings on DeepSeek's official API: ``auto`` (send
#: nothing) plus the values DeepSeek documents. The server also maps some
#: OpenAI spellings onto these (``minimal`` → ``low``, ``medium`` → ``high``);
#: they are not offered, so a configuration says what DeepSeek will run.
DeepSeekReasoningEffort = Literal["auto", "none", "low", "high", "max"]


@dataclass
class ChatDeepSeek(OpenAICompatBase):
    """DeepSeek's official API: the OpenAI protocol, adapted to what DeepSeek honours."""

    __registry_key__ = "openai.deepseek"

    reasoning_effort: Annotated[
        DeepSeekReasoningEffort,
        Field(
            description=(
                "DeepSeek's thinking effort, sent as ``reasoning_effort``. 'auto' (the "
                "default) sends nothing, and the server thinks at its default, 'high'. "
                "'none' turns thinking off; 'low', 'high' and 'max' are sent as written. "
                "While thinking is on, ``temperature`` has no effect on DeepSeek and is "
                "not sent."
            )
        ),
    ] = "auto"

    # --- DeepSeek-specific ---
    client_params: Annotated[
        dict[str, Any] | None,
        Field(description="Extra client constructor arguments. None (the default): none."),
    ] = None

    @property
    def provider(self) -> str:
        return "deepseek"

    @property
    def http_transport(self) -> httpx.AsyncClient | None:
        return resolve_http_transport(self.http_client, self.client_params)

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://api.deepseek.com/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        if self.timeout is not None:
            params["timeout"] = self.timeout
        if self.client_params:
            params.update(self.client_params)
        if self.http_transport is not None:
            params["http_client"] = self.http_transport
        return AsyncOpenAI(**params)

    def effective_reasoning_effort(self) -> str | None:
        """Exactly the configured effort, or nothing under ``auto``."""
        return None if self.reasoning_effort == "auto" else self.reasoning_effort

    def common_model_params(self) -> dict[str, Any]:
        """The base's parameters, in the names DeepSeek reads, without those it ignores.

        The output budget goes out as ``max_tokens``, the one DeepSeek
        honours; the YAML knob stays ``max_completion_tokens``, which is also
        the field a truncation error tells the user to raise. A sampling value
        that would not take effect is not sent: ``frequency_penalty`` never,
        ``temperature`` while thinking is on (every effort but ``none``,
        ``auto`` included), so the recorded request does not claim a setting
        the model never ran with.
        """
        params = super().common_model_params()
        budget = params.pop("max_completion_tokens", None)
        if budget is not None:
            params["max_tokens"] = budget
        params.pop("frequency_penalty", None)
        if self.reasoning_effort != "none":
            params.pop("temperature", None)
        return params

    def prompt_schema_response_format(self) -> ResponseFormatJSONObject:
        """``json_object``, the only constrained format DeepSeek has.

        An endpoint rule, true for every request: ``json_schema`` is refused
        with HTTP 400 ``This response_format type is unavailable now``. A
        ``json_object`` request whose prompt never says "json" is refused with
        ``Prompt must contain the word 'json' in some form to use
        'response_format' of type 'json_object'.``; the schema instruction the
        base writes into the system message names JSON, so it always does.
        """
        return {"type": "json_object"}

    def wire_messages(self, messages: Sequence[BaseMessage]) -> list[ChatCompletionMessageParam]:
        """Mark a trailing assistant message as a prefix on DeepSeek's ``/beta`` endpoint.

        Chat prefix completion: the model continues that message instead of
        answering after it.
        """
        wire = super().wire_messages(messages)
        if self.base_url and str(self.base_url).endswith("/beta") and wire:
            last = wire[-1]
            if last["role"] == "assistant":
                # ``prefix`` is DeepSeek's field, absent from the SDK's message types.
                wire[-1] = cast(ChatCompletionMessageParam, {**last, "prefix": True})
        return wire
