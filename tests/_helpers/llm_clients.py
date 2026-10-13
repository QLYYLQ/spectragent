"""Fake SDK clients for provider tests: what a test gives them, plus the closing a provider owes them.

A provider closes the SDK client it builds for a request before the request returns
(``docs/architecture_design.md`` §4.7). A fake handed to a provider through a
patched ``get_client`` is closed the same way, so it carries the SDK's closing surface —
and counts what the provider did with it:

- :class:`FakeClient` — openai, anthropic, groq: ``await close()`` and ``async with``;
- :class:`FakeGenaiClient` — google-genai: ``await aio.aclose()`` and ``close()``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeClient(SimpleNamespace):
    """An openai / anthropic / groq client: the given attributes (``chat=…``, ``messages=…``)."""

    def __init__(self, **attributes: Any) -> None:
        super().__init__(**attributes)
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


class FakeGenaiClient:
    """A ``google.genai.Client``: ``aio.models`` is *models*; both halves close."""

    def __init__(self, models: Any) -> None:
        self.aio = SimpleNamespace(models=models, aclose=self._aclose)
        self.async_closes = 0
        self.sync_closes = 0

    async def _aclose(self) -> None:
        self.async_closes += 1

    def close(self) -> None:
        self.sync_closes += 1
