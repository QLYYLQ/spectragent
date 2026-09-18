import base64
import json
from collections.abc import Sequence
from typing import Any

from google.genai.types import (
    Content,
    ContentListUnion,
    FunctionDeclarationDict,
    Part,
    ToolDict,
)

from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from spectragent.core.llm.tools import ToolDefinition


class GoogleMessageSerializer:
    """Serializer for converting messages to Google Gemini format."""

    @staticmethod
    def _tool_call_part(tool_call: ToolCall) -> Part:
        """Convert an outbound tool call to a Gemini ``function_call`` part."""
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"arguments": tool_call.function.arguments}
        if not isinstance(args, dict):
            args = {"arguments": args}
        return Part.from_function_call(name=tool_call.function.name, args=args)

    @staticmethod
    def _tool_result_parts(message: ToolMessage, names_by_id: dict[str, str]) -> list[Part]:
        """Convert a tool result to Gemini ``function_response`` parts.

        Gemini is the one supported API with no id-based correlation — the
        response is keyed by the tool's *name*. When the message does not carry
        one, recover it from the matching :class:`ToolCall`; if that fails there
        is no way to build a valid part, so raise rather than emit a response
        the model cannot attribute.
        """
        name = message.name or names_by_id.get(message.tool_call_id)
        if not name:
            raise ValueError(
                f"Cannot serialize ToolMessage(tool_call_id={message.tool_call_id!r}) "
                f"for Gemini: it carries no `name`, and no preceding AssistantMessage "
                f"declares a tool call with that id. Gemini keys function responses by "
                f"tool name, so the name must be recoverable."
            )

        key = "error" if message.is_error else "output"
        parts = [Part.from_function_response(name=name, response={key: message.text})]

        # function_response holds no image payload; images ride alongside it in
        # the same turn rather than being dropped.
        if not isinstance(message.content, str):
            for part in message.content:
                if part.type == "image_url":
                    _header, data = part.image_url.url.split(",", 1)
                    parts.append(
                        Part.from_bytes(
                            data=base64.b64decode(data),
                            mime_type=part.image_url.media_type,
                        )
                    )
        return parts

    @staticmethod
    def _content_to_parts(content: object) -> list[Part]:
        """Convert a message's ``content`` into Gemini ``Part`` objects."""
        if content is None:
            return []
        if isinstance(content, str):
            return [Part.from_text(text=content)]

        parts: list[Part] = []
        for part in content:  # type: ignore[union-attr]
            if part.type == "text":
                parts.append(Part.from_text(text=part.text))
            elif part.type == "refusal":
                parts.append(Part.from_text(text=f"[Refusal] {part.refusal}"))
            elif part.type == "image_url":
                # Format: data:image/jpeg;base64,<data>
                url = part.image_url.url
                _header, data = url.split(",", 1)
                image_bytes = base64.b64decode(data)
                # media_type from ImageURL correctly identifies the image format
                parts.append(Part.from_bytes(data=image_bytes, mime_type=part.image_url.media_type))
        return parts

    @staticmethod
    def serialize_messages(
        messages: Sequence[BaseMessage], include_system_in_user: bool = False
    ) -> tuple[ContentListUnion, str | None]:
        """
        Convert a list of BaseMessages to Google format, extracting system message.

        Google handles system instructions separately from the conversation, so we need to:
        1. Extract any system messages and return them separately as a string (or include in first user message if flag is set)
        2. Convert the remaining messages to Content objects

        Args:
            messages: List of messages to convert
            include_system_in_user: If True, system/developer messages are prepended to the first user message

        Returns:
            A tuple of (formatted_messages, system_message) where:
            - formatted_messages: List of Content objects for the conversation
            - system_message: System instruction string or None
        """

        messages = [m.model_copy(deep=True) for m in messages]

        formatted_messages: ContentListUnion = []
        system_message: str | None = None
        system_parts: list[str] = []

        # Gemini keys function responses by tool name, so a ToolMessage that
        # omits one has to be matched back to the call that produced it.
        names_by_id: dict[str, str] = {
            call.id: call.function.name
            for m in messages
            if isinstance(m, AssistantMessage)
            for call in m.tool_calls
        }

        for message in messages:
            role = message.role if hasattr(message, "role") else None

            # Handle system/developer messages
            if isinstance(message, SystemMessage) or role in ["system", "developer"]:
                # Extract system message content as string
                if isinstance(message.content, str):
                    if include_system_in_user:
                        system_parts.append(message.content)
                    else:
                        system_message = message.content
                elif message.content is not None:
                    # Handle Iterable of content parts
                    parts = []
                    for part in message.content:
                        if part.type == "text":
                            parts.append(part.text)
                    combined_text = "\n".join(parts)
                    if include_system_in_user:
                        system_parts.append(combined_text)
                    else:
                        system_message = combined_text
                continue

            # Determine the role for non-system messages
            if isinstance(message, UserMessage):
                role = "user"
            elif isinstance(message, AssistantMessage):
                role = "model"
            elif isinstance(message, ToolMessage):
                # Gemini has no `tool` role — a result is a function_response
                # part carried by a user turn.
                role = "user"
            else:
                # BaseMessage is a closed union. Defaulting an unrecognised type
                # to "user" would ship its content under the wrong role and look
                # like a prompting problem, so surface it instead.
                raise ValueError(f"Unknown message type: {type(message)}")

            if isinstance(message, ToolMessage):
                body_parts = GoogleMessageSerializer._tool_result_parts(message, names_by_id)
            else:
                body_parts = GoogleMessageSerializer._content_to_parts(message.content)
                if isinstance(message, AssistantMessage) and message.tool_calls:
                    body_parts = body_parts + [
                        GoogleMessageSerializer._tool_call_part(call) for call in message.tool_calls
                    ]
            message_parts: list[Part] = body_parts

            # Flush pending system text into the first conversation turn.
            # NB: the system text is *prepended* to this message's own parts — it
            # must never replace them, or a vision turn would ship the system
            # prompt and silently drop the user's text and screenshot.
            if include_system_in_user and system_parts and not formatted_messages:
                system_text = "\n\n".join(system_parts)
                system_parts = []
                if role == "user" and isinstance(message.content, str):
                    # Both are plain text — merge into a single part.
                    message_parts = [Part.from_text(text=f"{system_text}\n\n{message.content}")]
                elif role == "user":
                    message_parts = [Part.from_text(text=system_text), *body_parts]
                else:
                    # First turn is from the model — Gemini would reject system text
                    # under a "model" role, so emit it as its own user turn.
                    system_turn = Content(role="user", parts=[Part.from_text(text=system_text)])
                    formatted_messages.append(system_turn)  # type: ignore

            # Create the Content object
            if message_parts:
                final_message = Content(role=role, parts=message_parts)
                # for some reason, the type checker is not able to infer the type of formatted_messages
                formatted_messages.append(final_message)  # type: ignore

        # No conversation turn ever arrived to carry the system text (e.g. a
        # system-only message list) — emit it rather than dropping it.
        if system_parts:
            trailing = Content(role="user", parts=[Part.from_text(text="\n\n".join(system_parts))])
            formatted_messages.append(trailing)  # type: ignore

        return formatted_messages, system_message

    # region - Tool declarations
    @staticmethod
    def serialize_tools(tools: Sequence[ToolDefinition]) -> list[ToolDict]:
        """Convert tool declarations to Gemini's ``Tool`` shape.

        Gemini's schema dialect rejects ``additionalProperties`` and ``$ref``,
        so every parameter schema goes through :func:`fix_gemini_schema` —
        the same treatment the structured-output path applies.

        ``strict`` is not forwarded: Gemini has no strict dialect.
        """
        from spectragent.core.llm.schema.gemini import fix_gemini_schema

        declarations: list[FunctionDeclarationDict] = [
            FunctionDeclarationDict(
                name=tool.name,
                description=tool.description,
                parameters=fix_gemini_schema(dict(tool.parameters)),  # type: ignore[arg-type]
            )
            for tool in tools
        ]
        return [ToolDict(function_declarations=declarations)]

    @staticmethod
    def parse_tool_calls(response: Any) -> list[ToolCall]:
        """Read ``function_call`` parts off a Gemini response.

        Gemini usually returns no call id, so one is minted by
        :func:`synthesize_tool_call_id`. It is never sent back to Gemini
        (responses correlate by name); it exists so the provider-neutral
        :class:`ToolCall` /
        :class:`~spectragent.core.llm.messages.ToolMessage` pairing still holds,
        which requires it to be unique across the conversation rather than only
        within this one response.
        """
        from spectragent.core.llm.messages import Function, synthesize_tool_call_id

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []

        calls: list[ToolCall] = []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call is None:
                continue
            name = getattr(function_call, "name", "") or ""
            args = getattr(function_call, "args", None) or {}
            calls.append(
                ToolCall(
                    id=getattr(function_call, "id", None) or synthesize_tool_call_id(name),
                    function=Function(name=name, arguments=json.dumps(args)),
                )
            )
        return calls

    # endregion
