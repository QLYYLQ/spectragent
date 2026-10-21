"""A scripted chat transport: the judge's gateway, without a network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from lookahead.judge import ChatReply

USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "completion_tokens_details": {"reasoning_tokens": 5},
}

Script = Callable[[dict[str, Any]], "str | Exception"]


def prompt_text(request: dict[str, Any]) -> str:
    """Every text part of the request's messages, joined."""
    parts: list[str] = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts += [part["text"] for part in content if part.get("type") == "text"]
    return "\n".join(parts)


def images(request: dict[str, Any]) -> list[str]:
    return [
        part["image_url"]["url"]
        for message in request["messages"]
        if not isinstance(message["content"], str)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]


class ScriptedTransport:
    """Answers each request with what *script* returns for it (an exception is raised)."""

    def __init__(self, script: Script, *, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self.closed = False

    async def create(self, **request: Any) -> ChatReply:
        self.requests.append(request)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            result = self.script(request)
        finally:
            self.in_flight -= 1
        if isinstance(result, Exception):
            raise result
        return ChatReply(content=result, finish_reason="stop", model="fake-model", usage=USAGE)

    async def close(self) -> None:
        self.closed = True


def answers(**values: str) -> str:
    """A judgment reply: ``question=answer`` pairs, each with a reason."""
    return json.dumps(
        {
            name: {"answer": answer, "reason": f"{name} is {answer}."}
            for name, answer in values.items()
        }
    )
