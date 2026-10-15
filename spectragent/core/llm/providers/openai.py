"""ChatOpenAI — direct OpenAI API provider."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

from dataclasses import dataclass
from typing import Annotated, Any

from openai import AsyncOpenAI
from pydantic import Field

from spectragent.core.llm.providers.openai_base import OpenAICompatBase


@dataclass
class ChatOpenAI(OpenAICompatBase):
    """Direct OpenAI API provider (no sub-provider)."""

    __registry_key__ = "openai.direct"

    # --- Direct OpenAI-specific fields ---
    organization: Annotated[
        str | None,
        Field(description="Organization ID. None (the default): the client reads OPENAI_ORG_ID."),
    ] = None
    project: Annotated[
        str | None,
        Field(description="Project ID. None (the default): the client reads OPENAI_PROJECT_ID."),
    ] = None

    @property
    def provider(self) -> str:
        return "direct"

    def get_client(self) -> AsyncOpenAI:
        params: dict[str, Any] = {"api_key": self.api_key, "max_retries": self.max_retries}
        if self.base_url:
            params["base_url"] = self.base_url
        if self.organization:
            params["organization"] = self.organization
        if self.project:
            params["project"] = self.project
        if self.websocket_base_url:
            params["websocket_base_url"] = self.websocket_base_url
        if self.timeout:
            params["timeout"] = self.timeout
        if self.default_headers:
            params["default_headers"] = self.default_headers
        if self.default_query:
            params["default_query"] = self.default_query
        if self.http_transport is not None:
            params["http_client"] = self.http_transport
        return AsyncOpenAI(**params)

    # ainvoke inherited from OpenAICompatBase — standard text + response_format
