"""One unwrapper for "the model wrapped its JSON in a Markdown fence".

Six providers had written their own, with four behaviours between them,
and ``openai_base`` — the base every OpenAI-compatible *gateway* goes
through, and so the widest structured-output path in the tree — had none.
Which of a fenced reply and a bare one parsed therefore depended on which
provider key the YAML happened to name.

The second half of this file is the scan that keeps it that way: no module
under ``providers/`` may grow a second implementation.

**Not a fix for the Claude-via-compat failure.** Measured on
``claude-haiku-4-5`` through an OpenAI-compatible route, 0/3: the reply is
fenced *and* the keys inside the fence are not the schema's, because the
gateway dropped ``response_format`` on the way to Anthropic's own API.
Unwrapping that gets you valid JSON of the wrong shape. The fix for it is
native routing; this is a duplication and coverage fix.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import spectragent.core.llm.providers as providers_pkg
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import UserMessage

_PROVIDERS_DIR = Path(providers_pkg.__file__).parent


class _Shape(BaseModel):
    index: int


_PAYLOAD = '{"index": 3}'


# ── 1. The helper ──────────────────────────────────────────────────


class TestStripCodeFence:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(f"```json\n{_PAYLOAD}\n```", _PAYLOAD, id="language-tag"),
            pytest.param(f"```\n{_PAYLOAD}\n```", _PAYLOAD, id="bare-fence"),
            pytest.param(f"```json {_PAYLOAD}```", _PAYLOAD, id="no-newline"),
            pytest.param(f"  ```json\n{_PAYLOAD}\n```  ", _PAYLOAD, id="outer-whitespace"),
            pytest.param(_PAYLOAD, _PAYLOAD, id="already-bare"),
            pytest.param(f"  {_PAYLOAD}  ", _PAYLOAD, id="bare-with-whitespace"),
            pytest.param("plain prose", "plain prose", id="not-json-at-all"),
        ],
    )
    def test_shapes_every_provider_saw(self, raw: str, expected: str) -> None:
        assert strip_code_fence(raw) == expected

    def test_a_preamble_before_the_fence(self) -> None:
        """The Groq salvage shape: header tokens, then the fenced block."""
        raw = f"<|header_start|>assistant<|header_end|>```json\n{_PAYLOAD}\n```</function>"
        assert strip_code_fence(raw) == _PAYLOAD

    def test_an_unterminated_fence_still_unwraps(self) -> None:
        """A reply cut off at the token ceiling.

        The content is unparseable either way, but ``{"index":`` is a JSON
        error a caller can report and ```` ```json\\n{"index": ```` is a
        question about backticks.
        """
        assert strip_code_fence('```json\n{"index":') == '{"index":'

    def test_backticks_inside_a_string_value_are_left_alone(self) -> None:
        """The regression a naive "split on ```" would introduce.

        A browser agent narrating a page it just read writes this. The old
        Groq implementation — ``content.split("```")[1]`` — would have
        returned ``Run`` here.
        """
        raw = '{"memory": "I clicked ```Run```", "index": 3}'
        assert strip_code_fence(raw) == raw
        assert json.loads(strip_code_fence(raw))["index"] == 3

    def test_a_fenced_array_unwraps(self) -> None:
        assert strip_code_fence("```json\n[1, 2]\n```") == "[1, 2]"


# ── 2. The base that had none ──────────────────────────────────────


class TestOpenAIBaseUnwraps:
    """``openai_base`` is the path every gateway goes through.

    It is also the path most likely to be talking to a model whose own API
    would have constrained the output, via a translation that lost the
    constraint — i.e. the one where a fenced reply is *most* likely, and
    the one that had no unwrapping at all.
    """

    def _chat(self, content: str):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        class _Completions:
            async def create(self, **_kw: Any) -> Any:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content, tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        chat = ChatOpenAI(model="gpt-4o", api_key="stub", base_url="https://gw.invalid/v1")
        chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        return chat

    async def test_a_fenced_reply_parses(self) -> None:
        chat = self._chat(f"```json\n{_PAYLOAD}\n```")
        result = await chat.ainvoke([UserMessage(content="hi")], _Shape)
        assert result.completion.index == 3

    async def test_a_bare_reply_still_parses(self) -> None:
        chat = self._chat(_PAYLOAD)
        result = await chat.ainvoke([UserMessage(content="hi")], _Shape)
        assert result.completion.index == 3

    async def test_a_reply_that_does_not_fit_is_still_recoverable(self) -> None:
        """Unwrapping must not turn a schema violation into a transport error."""
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        chat = self._chat('```json\n{"index": "not-an-int"}\n```')
        with pytest.raises(StructuredOutputValidationError) as exc:
            await chat.ainvoke([UserMessage(content="hi")], _Shape)
        assert exc.value.status_code == 422

    async def test_the_unwrapping_is_still_visible_in_the_failure(self) -> None:
        """Fenced *and* the wrong shape is a diagnosis, not two facts.

        ``raw_output`` carries the unwrapped body, because that is what the
        Loop feeds back through the agent's parser. So the trace would
        otherwise show bare JSON of the wrong shape with no sign it arrived
        fenced — and "fenced and wrong-shaped" is the signature of a
        gateway that dropped ``response_format`` on the way to a vendor's
        native API, which is the one thing this module says it does *not*
        fix and the reader needs to be able to recognise.
        """
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        chat = self._chat('```json\n{"index": "not-an-int"}\n```')
        with pytest.raises(StructuredOutputValidationError) as exc:
            await chat.ainvoke([UserMessage(content="hi")], _Shape)
        assert "unwrapped" in str(exc.value)
        assert exc.value.raw_output == '{"index": "not-an-int"}'

    async def test_an_unfenced_failure_does_not_claim_an_unwrapping(self) -> None:
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        chat = self._chat('{"index": "not-an-int"}')
        with pytest.raises(StructuredOutputValidationError) as exc:
            await chat.ainvoke([UserMessage(content="hi")], _Shape)
        assert "unwrapped" not in str(exc.value)


# ── 3. Nobody writes a second one ──────────────────────────────────


def _fence_literals(path: Path) -> list[str]:
    """String literals containing a Markdown fence, as ``file:line``.

    Reported relative to ``providers/`` so a subpackage hit names its
    directory rather than a bare filename two of which could collide.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "```" in node.value
        ):
            where = path.relative_to(_PROVIDERS_DIR)
            found.append(f"{where}:{node.lineno}: {node.value!r}")
    return found


def test_no_provider_reimplements_fence_stripping() -> None:
    """A ``"```"`` literal in a provider is a second implementation.

    Docstrings are **not** excluded: ``ast.walk`` reaches every
    ``ast.Constant``, a module or function docstring included. That is
    deliberate — prose about what a fence looks like drifts from the code
    that unwraps one just as readily as a second regex does. Say it in a
    ``#`` comment, which the AST does not carry, or point at
    ``spectragent.core.llm.fences``.

    ``rglob``, not ``glob``: a provider added in a subpackage under
    ``providers/`` is exactly the one that would go unscanned.
    """
    offenders: list[str] = []
    for path in sorted(_PROVIDERS_DIR.rglob("*.py")):
        offenders.extend(_fence_literals(path))
    assert offenders == [], (
        "These modules carry their own idea of what a Markdown fence is, so "
        "whether a fenced reply parses depends on which provider key the "
        "YAML names. Use "
        "spectragent.core.llm.fences.strip_code_fence:\n  " + "\n  ".join(offenders)
    )
