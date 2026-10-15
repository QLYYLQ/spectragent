"""Serializer regressions around system messages (§6.4.2, §6.4.7)."""

from __future__ import annotations

import base64
from typing import cast

import pytest
from google.genai.types import Content

from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer
from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer
from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer

# 1x1 transparent PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


def _image_part() -> ContentPartImageParam:
    return ContentPartImageParam(image_url=ImageURL(url=_PNG_DATA_URL, media_type="image/png"))


def _serialize_google(messages, *, include_system_in_user: bool):
    """Serialize and narrow ``ContentListUnion`` to the list-of-Content it really is."""
    raw, system = GoogleMessageSerializer.serialize_messages(
        messages, include_system_in_user=include_system_in_user
    )
    return cast(list[Content], raw), system


def _all_text(content: Content) -> str:
    return "\n".join(p.text for p in (content.parts or []) if p.text)


class TestGoogleIncludeSystemInUser:
    """§6.4.2 — the first user message used to be dropped entirely."""

    def test_multimodal_user_message_survives(self) -> None:
        messages = [
            SystemMessage(content="SYS"),
            UserMessage(
                content=[ContentPartTextParam(text="USER TEXT"), _image_part()],
            ),
        ]
        contents, system = _serialize_google(messages, include_system_in_user=True)

        assert system is None
        assert len(contents) == 1
        parts = contents[0].parts or []
        # system text + user text + image — the old code emitted only the first.
        assert _all_text(contents[0]) == "SYS\nUSER TEXT"
        assert any(p.inline_data is not None for p in parts)

    def test_plain_text_user_message_is_merged(self) -> None:
        messages = [SystemMessage(content="SYS"), UserMessage(content="USER TEXT")]
        contents, system = _serialize_google(messages, include_system_in_user=True)
        assert system is None
        assert len(contents) == 1
        assert _all_text(contents[0]) == "SYS\n\nUSER TEXT"

    def test_multiple_system_messages_all_included(self) -> None:
        messages = [
            SystemMessage(content="SYS A"),
            SystemMessage(content="SYS B"),
            UserMessage(content="HELLO"),
        ]
        contents, _ = _serialize_google(messages, include_system_in_user=True)
        text = _all_text(contents[0])
        assert "SYS A" in text and "SYS B" in text and "HELLO" in text

    def test_system_only_list_is_not_dropped(self) -> None:
        contents, system = _serialize_google(
            [SystemMessage(content="SYS")], include_system_in_user=True
        )
        assert system is None
        assert len(contents) == 1
        assert _all_text(contents[0]) == "SYS"

    def test_assistant_first_gets_its_own_user_turn(self) -> None:
        messages = [
            SystemMessage(content="SYS"),
            AssistantMessage(content="PRIOR"),
            UserMessage(content="NOW"),
        ]
        contents, _ = _serialize_google(messages, include_system_in_user=True)
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert _all_text(contents[0]) == "SYS"

    def test_flag_off_keeps_system_separate(self) -> None:
        messages = [SystemMessage(content="SYS"), UserMessage(content="USER TEXT")]
        contents, system = _serialize_google(messages, include_system_in_user=False)
        assert system == "SYS"
        assert _all_text(contents[0]) == "USER TEXT"

    def test_caller_messages_are_not_mutated(self) -> None:
        user = UserMessage(content="USER TEXT")
        GoogleMessageSerializer.serialize_messages(
            [SystemMessage(content="SYS"), user], include_system_in_user=True
        )
        assert user.content == "USER TEXT"


class TestAnthropicMultipleSystemMessages:
    """§6.4.7 — only the *last* SystemMessage used to survive."""

    def test_both_system_prompts_reach_the_request(self) -> None:
        messages = [
            SystemMessage(content="FIRST"),
            SystemMessage(content="SECOND"),
            UserMessage(content="hello"),
        ]
        _serialized, system = AnthropicMessageSerializer.serialize_messages(messages)

        assert isinstance(system, list)
        texts = [block["text"] for block in system]
        assert texts == ["FIRST", "SECOND"]

    def test_single_system_message_unchanged(self) -> None:
        _serialized, system = AnthropicMessageSerializer.serialize_messages(
            [SystemMessage(content="ONLY"), UserMessage(content="hi")]
        )
        assert system == "ONLY"

    def test_no_system_message(self) -> None:
        _serialized, system = AnthropicMessageSerializer.serialize_messages(
            [UserMessage(content="hi")]
        )
        assert system is None

    def test_order_matches_openai_serializer(self) -> None:
        """The same list must not produce a different prompt on the two providers."""
        from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer

        messages = [
            SystemMessage(content="FIRST"),
            SystemMessage(content="SECOND"),
            UserMessage(content="hello"),
        ]
        _serialized, anthropic_system = AnthropicMessageSerializer.serialize_messages(messages)
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)

        openai_system_text = [m["content"] for m in openai_messages if m["role"] == "system"]
        anthropic_text = [b["text"] for b in anthropic_system]  # type: ignore[index]
        assert anthropic_text == openai_system_text == ["FIRST", "SECOND"]


class TestSeparateSystemMessages:
    @pytest.mark.parametrize("serializer", [GoogleMessageSerializer, AWSBedrockMessageSerializer])
    def test_all_system_content_survives_in_order(self, serializer) -> None:
        messages = [
            SystemMessage(content="FIRST"),
            UserMessage(content="hello"),
            SystemMessage(
                content=[ContentPartTextParam(text="SECOND"), ContentPartTextParam(text="THIRD")]
            ),
        ]
        original = [message.model_dump() for message in messages]
        conversation, system = serializer.serialize_messages(messages)

        if serializer is GoogleMessageSerializer:
            assert system == "FIRST\n\nSECOND\nTHIRD"
            assert _all_text(conversation[0]) == "hello"
        else:
            assert system == [{"text": "FIRST"}, {"text": "SECOND"}, {"text": "THIRD"}]
            assert conversation == [{"role": "user", "content": [{"text": "hello"}]}]
        assert [message.model_dump() for message in messages] == original

    @pytest.mark.parametrize("serializer", [GoogleMessageSerializer, AWSBedrockMessageSerializer])
    @pytest.mark.parametrize("text", ["ONLY", ""])
    def test_single_system_message_keeps_its_wire_shape(self, serializer, text: str) -> None:
        conversation, system = serializer.serialize_messages([SystemMessage(content=text)])
        assert conversation == []
        assert system == (text if serializer is GoogleMessageSerializer else [{"text": text}])

    @pytest.mark.parametrize("serializer", [GoogleMessageSerializer, AWSBedrockMessageSerializer])
    def test_no_system_message_stays_absent(self, serializer) -> None:
        _conversation, system = serializer.serialize_messages([UserMessage(content="hello")])
        assert system is None


def _cache_marked(blocks: object) -> list[int]:
    """Indices of the content blocks carrying a ``cache_control`` marker.

    Bare-string content counts as "nothing marked": the serializer collapses an
    uncached single text block back to a plain string, which is exactly the
    shape a message left behind by ``_clean_cache_messages`` takes.
    """
    if not isinstance(blocks, list):
        return []
    return [i for i, b in enumerate(blocks) if b.get("cache_control")]


class TestAnthropicCacheBreakpoints:
    """``cache=True`` must actually reach the wire.

    The regression this guards: a vision step's content is ``[text, image]``,
    and the serializer used to put the marker on the last *text* part. Text is
    not last there and the image branch carried no marker, so every vision run
    silently sent zero breakpoints — a 200 with a full-price prefix, which no
    existing assertion could see.
    """

    def test_text_only_user_message_is_marked(self) -> None:
        serialized, _system = AnthropicMessageSerializer.serialize_messages(
            [UserMessage(content="state", cache=True)]
        )
        assert _cache_marked(serialized[0]["content"]) == [0]

    def test_vision_user_message_is_marked_on_the_image(self) -> None:
        message = UserMessage(
            content=[ContentPartTextParam(text="state"), _image_part()],
            cache=True,
        )
        serialized, _system = AnthropicMessageSerializer.serialize_messages([message])

        blocks = serialized[0]["content"]
        assert _cache_marked(blocks) == [1], "the breakpoint must land on the trailing image"
        assert blocks[1]["type"] == "image"  # type: ignore[index]

    def test_vision_user_message_without_cache_is_unmarked(self) -> None:
        message = UserMessage(content=[ContentPartTextParam(text="state"), _image_part()])
        serialized, _system = AnthropicMessageSerializer.serialize_messages([message])
        assert _cache_marked(serialized[0]["content"]) == []

    def test_system_and_tail_are_both_marked(self) -> None:
        """The two breakpoints the agents place: static prefix + growing tail."""
        serialized, system = AnthropicMessageSerializer.serialize_messages(
            [
                SystemMessage(content="SYS", cache=True),
                UserMessage(content="step 1", cache=True),
                AssistantMessage(content="ok"),
                UserMessage(content="step 2", cache=True),
            ]
        )
        assert _cache_marked(system) == [0]
        # Only the last cached message keeps its marker; the earlier step's is
        # cleared, so the request stays well under Anthropic's 4-breakpoint cap.
        marked_messages = [i for i, m in enumerate(serialized) if _cache_marked(m["content"])]
        assert marked_messages == [2]
