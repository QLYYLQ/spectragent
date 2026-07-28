from collections.abc import Sequence
from typing import Any, overload

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartRefusalParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL
from openai.types.chat.chat_completion_message_function_tool_call_param import Function

from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPartImageParam,
    ContentPartRefusalParam,
    ContentPartTextParam,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from spectragent.core.llm.messages import Function as SpectraFunction
from spectragent.core.llm.tools import ToolDefinition


class OpenAIMessageSerializer:
    """Serializer for converting between custom message types and OpenAI message param types."""

    @staticmethod
    def _serialize_content_part_text(
        part: ContentPartTextParam,
    ) -> ChatCompletionContentPartTextParam:
        return ChatCompletionContentPartTextParam(text=part.text, type="text")

    @staticmethod
    def _serialize_content_part_image(
        part: ContentPartImageParam,
    ) -> ChatCompletionContentPartImageParam:
        return ChatCompletionContentPartImageParam(
            image_url=ImageURL(url=part.image_url.url, detail=part.image_url.detail),
            type="image_url",
        )

    @staticmethod
    def _serialize_content_part_refusal(
        part: ContentPartRefusalParam,
    ) -> ChatCompletionContentPartRefusalParam:
        return ChatCompletionContentPartRefusalParam(refusal=part.refusal, type="refusal")

    @staticmethod
    def _serialize_user_content(
        content: str | list[ContentPartTextParam | ContentPartImageParam],
    ) -> str | list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam]:
        """Serialize content for user messages (text and images allowed)."""
        if isinstance(content, str):
            return content

        serialized_parts: list[
            ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam
        ] = []
        for part in content:
            if part.type == "text":
                serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
            elif part.type == "image_url":
                serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_image(part))
        return serialized_parts

    @staticmethod
    def _serialize_system_content(
        content: str | list[ContentPartTextParam],
    ) -> str | list[ChatCompletionContentPartTextParam]:
        """Serialize content for system messages (text only)."""
        if isinstance(content, str):
            return content

        serialized_parts: list[ChatCompletionContentPartTextParam] = []
        for part in content:
            if part.type == "text":
                serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
        return serialized_parts

    @staticmethod
    def _serialize_assistant_content(
        content: str | list[ContentPartTextParam | ContentPartRefusalParam] | None,
    ) -> (
        str
        | list[ChatCompletionContentPartTextParam | ChatCompletionContentPartRefusalParam]
        | None
    ):
        """Serialize content for assistant messages (text and refusal allowed)."""
        if content is None:
            return None
        if isinstance(content, str):
            return content

        serialized_parts: list[
            ChatCompletionContentPartTextParam | ChatCompletionContentPartRefusalParam
        ] = []
        for part in content:
            if part.type == "text":
                serialized_parts.append(OpenAIMessageSerializer._serialize_content_part_text(part))
            elif part.type == "refusal":
                serialized_parts.append(
                    OpenAIMessageSerializer._serialize_content_part_refusal(part)
                )
        return serialized_parts

    @staticmethod
    def _serialize_tool_call(tool_call: ToolCall) -> ChatCompletionMessageFunctionToolCallParam:
        return ChatCompletionMessageFunctionToolCallParam(
            id=tool_call.id,
            function=Function(name=tool_call.function.name, arguments=tool_call.function.arguments),
            type="function",
        )

    # endregion

    # region - Serialize overloads
    @overload
    @staticmethod
    def serialize(message: UserMessage) -> ChatCompletionUserMessageParam: ...

    @overload
    @staticmethod
    def serialize(message: SystemMessage) -> ChatCompletionSystemMessageParam: ...

    @overload
    @staticmethod
    def serialize(message: AssistantMessage) -> ChatCompletionAssistantMessageParam: ...

    @overload
    @staticmethod
    def serialize(message: ToolMessage) -> ChatCompletionToolMessageParam: ...

    @staticmethod
    def serialize(message: BaseMessage) -> ChatCompletionMessageParam:
        """Serialize a custom message to an OpenAI message param."""

        if isinstance(message, UserMessage):
            user_result: ChatCompletionUserMessageParam = {
                "role": "user",
                "content": OpenAIMessageSerializer._serialize_user_content(message.content),
            }
            if message.name is not None:
                user_result["name"] = message.name
            return user_result

        elif isinstance(message, SystemMessage):
            system_result: ChatCompletionSystemMessageParam = {
                "role": "system",
                "content": OpenAIMessageSerializer._serialize_system_content(message.content),
            }
            if message.name is not None:
                system_result["name"] = message.name
            return system_result

        elif isinstance(message, AssistantMessage):
            # Handle content serialization
            content = None
            if message.content is not None:
                content = OpenAIMessageSerializer._serialize_assistant_content(message.content)

            assistant_result: ChatCompletionAssistantMessageParam = {"role": "assistant"}

            # Only add content if it's not None
            if content is not None:
                assistant_result["content"] = content

            if message.name is not None:
                assistant_result["name"] = message.name
            if message.refusal is not None:
                assistant_result["refusal"] = message.refusal
            if message.tool_calls:
                assistant_result["tool_calls"] = [
                    OpenAIMessageSerializer._serialize_tool_call(tc) for tc in message.tool_calls
                ]

            return assistant_result

        elif isinstance(message, ToolMessage):
            # OpenAI's tool message content is text-only. Images travel in a
            # companion user message that `serialize_messages` appends — see
            # `_serialize_tool_message_images`.
            #
            # `is_error` has no wire representation here: OpenAI's convention is
            # that the failure is described by the content itself. The flag is
            # preserved on the message object (and therefore in the trace); it
            # is not smuggled into the text, which would be inventing a format.
            tool_result: ChatCompletionToolMessageParam = {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.text,
            }
            return tool_result

        else:
            raise ValueError(f"Unknown message type: {type(message)}")

    @staticmethod
    def _serialize_tool_message_images(
        message: ToolMessage,
    ) -> ChatCompletionUserMessageParam | None:
        """Carry a tool result's images in a companion user message.

        OpenAI rejects image parts inside a ``tool`` message, and dropping them
        would blind any screenshot-returning tool. Emitting them as the user
        turn that immediately follows the tool result is OpenAI's own documented
        workaround and keeps the pairing contract intact.
        """
        if isinstance(message.content, str):
            return None
        images = [part for part in message.content if part.type == "image_url"]
        if not images:
            return None

        label = f"Image output of tool call {message.tool_call_id}:"
        parts: list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam] = [
            ChatCompletionContentPartTextParam(text=label, type="text")
        ]
        parts.extend(OpenAIMessageSerializer._serialize_content_part_image(p) for p in images)
        return {"role": "user", "content": parts}

    @staticmethod
    def serialize_messages(messages: Sequence[BaseMessage]) -> list[ChatCompletionMessageParam]:
        """Serialize a message list, keeping each run of tool results contiguous.

        OpenAI requires the ``tool`` messages answering an assistant turn to
        follow it with nothing in between ("An assistant message with
        'tool_calls' must be followed by tool messages responding to each
        tool_call_id"). Parallel tool calls therefore produce a *run* of tool
        messages, so an image companion emitted inline after the first result
        would split the run and fail the whole request. Companions are buffered
        until the run ends and flushed together — the same coalescing the
        Anthropic and Bedrock serializers do for their tool_result blocks.
        """
        serialized: list[ChatCompletionMessageParam] = []
        pending_images: list[ChatCompletionMessageParam] = []
        for message in messages:
            if not isinstance(message, ToolMessage) and pending_images:
                serialized.extend(pending_images)
                pending_images = []
            serialized.append(OpenAIMessageSerializer.serialize(message))
            if isinstance(message, ToolMessage):
                companion = OpenAIMessageSerializer._serialize_tool_message_images(message)
                if companion is not None:
                    pending_images.append(companion)
        serialized.extend(pending_images)
        return serialized

    # region - Tool declarations
    @staticmethod
    def serialize_tool(tool: ToolDefinition) -> dict[str, Any]:
        """Convert a :class:`ToolDefinition` to an OpenAI tool param."""
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if tool.strict:
            function["strict"] = True
        return {"type": "function", "function": function}

    @staticmethod
    def serialize_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        return [OpenAIMessageSerializer.serialize_tool(t) for t in tools]

    @staticmethod
    def parse_tool_calls(raw: Sequence[Any] | None) -> list[ToolCall]:
        """Read tool calls off an OpenAI-shaped response message."""
        if not raw:
            return []
        parsed: list[ToolCall] = []
        for call in raw:
            function = getattr(call, "function", None)
            if function is None:
                continue
            parsed.append(
                ToolCall(
                    id=getattr(call, "id", "") or "",
                    function=SpectraFunction(
                        name=function.name,
                        arguments=function.arguments or "{}",
                    ),
                )
            )
        return parsed

    # endregion
