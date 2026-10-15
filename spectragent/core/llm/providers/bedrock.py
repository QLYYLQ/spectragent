import base64
from collections.abc import Sequence
from dataclasses import dataclass
from os import getenv
from typing import TYPE_CHECKING, Annotated, Any, TypeVar, overload

from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.model_families import ModelFamily, detect_model_family
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition

#: ``tool_choice`` spelled the Converse way. There is no "none" — Converse
#: cannot advertise tools while forbidding their use.
_BEDROCK_TOOL_CHOICE: dict[str, dict[str, dict]] = {
    "auto": {"auto": {}},
    "required": {"any": {}},
}
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage, join_reasoning

if TYPE_CHECKING:
    from boto3 import client as AwsClient  # type: ignore
    from boto3.session import Session  # type: ignore

T = TypeVar("T", bound=BaseModel)


def _base64(value: object) -> str | None:
    """*value* as text: bytes in base64, a string as it is, anything else ``None``."""
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(value).decode("ascii")
    return value if isinstance(value, str) else None


def _reasoning(content: list[Any]) -> tuple[str | None, str | None]:
    """The reasoning a Converse reply carried in its ``reasoningContent`` blocks.

    A block's ``reasoningText.text`` is the readable reasoning; its
    ``redactedContent`` is the opaque form, bytes that are kept in base64.
    Present only when the response already has them; this provider adds no
    reasoning configuration to the request. Both go through
    :func:`~spectragent.core.llm.views.join_reasoning`.
    """
    blocks = [
        item["reasoningContent"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("reasoningContent"), dict)
    ]
    thinking = join_reasoning((block.get("reasoningText") or {}).get("text") for block in blocks)
    redacted = join_reasoning(_base64(block.get("redactedContent")) for block in blocks)
    return thinking, redacted


@dataclass
class ChatBedrock(ChatModelBase):
    __registry_key__ = "bedrock.default"
    """AWS Bedrock chat model using the boto3 converse API."""

    model: Annotated[str, Field(description="Bedrock model identifier")] = (
        "anthropic.claude-3-5-sonnet-20240620-v1:0"
    )
    max_tokens: Annotated[int | None, Field(description="Maximum tokens to generate")] = 4096
    temperature: Annotated[
        float | None, Field(description="Sampling temperature. None (the default): not sent.")
    ] = None
    top_p: Annotated[
        float | None, Field(description="Top-p sampling parameter. None (the default): not sent.")
    ] = None
    seed: Annotated[
        int | None,
        Field(description="Random seed for reproducibility. None (the default): not sent."),
    ] = None
    stop_sequences: Annotated[
        list[str] | None, Field(description="Stop sequences. None (the default): not sent.")
    ] = None

    # AWS credentials
    aws_access_key_id: Annotated[
        str | None,
        Field(description="AWS access key ID. None (the default): read from AWS_ACCESS_KEY_ID."),
    ] = None
    aws_secret_access_key: Annotated[
        str | None,
        Field(
            description="AWS secret access key. None (the default): read from AWS_SECRET_ACCESS_KEY."
        ),
    ] = None
    aws_session_token: Annotated[
        str | None,
        Field(description="AWS session token. None (the default): read from AWS_SESSION_TOKEN."),
    ] = None
    aws_region: Annotated[
        str | None,
        Field(description="AWS region. None (the default): AWS_REGION, else AWS_DEFAULT_REGION."),
    ] = None
    aws_sso_auth: Annotated[bool, Field(description="Use AWS SSO authentication")] = False
    session: "Session | None" = None

    request_params: dict[str, Any] | None = None

    @property
    def model_type(self) -> str:
        return "bedrock"

    @property
    def name(self) -> str:
        return str(self.model)

    def _get_client(self) -> "AwsClient":  # type: ignore
        try:
            from boto3 import client as AwsClient  # type: ignore
        except ImportError as e:
            raise ImportError(
                "`boto3` not installed. Install with: pip install browser-use[aws]"
            ) from e

        if self.session:
            return self.session.client("bedrock-runtime")

        access_key = self.aws_access_key_id or getenv("AWS_ACCESS_KEY_ID")
        secret_key = self.aws_secret_access_key or getenv("AWS_SECRET_ACCESS_KEY")
        session_token = self.aws_session_token or getenv("AWS_SESSION_TOKEN")
        region = self.aws_region or getenv("AWS_REGION") or getenv("AWS_DEFAULT_REGION")

        if self.aws_sso_auth:
            return AwsClient(service_name="bedrock-runtime", region_name=region)

        if not access_key or not secret_key:
            raise ModelProviderError(
                message="AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.",
                model=self.name,
            )
        return AwsClient(
            service_name="bedrock-runtime",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    def _get_inference_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if self.max_tokens is not None:
            config["maxTokens"] = self.max_tokens
        if self.temperature is not None:
            config["temperature"] = self.temperature
        if self.top_p is not None:
            config["topP"] = self.top_p
        if self.stop_sequences is not None:
            config["stopSequences"] = self.stop_sequences
        if self.seed is not None:
            config["seed"] = self.seed
        return config

    @property
    def model_family(self) -> ModelFamily:
        """Declared capabilities for this model (see ``core/llm/model_families``)."""
        return detect_model_family(str(self.model))

    def _format_tools_for_request(self, output_format: type[BaseModel]) -> list[dict[str, Any]]:
        # Previously this rebuilt the schema one level deep: every property was
        # collapsed to ``{"type": info.get("type", "string")}``, which turned each
        # nested submodel (a bare ``$ref``, no ``type``) into a string and stripped
        # ``items`` off every array. Use the shared flattener instead — it inlines
        # ``$defs`` so the whole tree survives.
        schema = SchemaOptimizer.create_optimized_json_schema(
            output_format, strict=self.model_family.supports_strict_mode
        )
        schema.pop("title", None)
        return [
            {
                "toolSpec": {
                    "name": f"extract_{output_format.__name__.lower()}",
                    "description": f"Extract information in the format of {output_format.__name__}",
                    "inputSchema": {"json": schema},
                }
            }
        ]

    def _get_usage(self, response: dict[str, Any]) -> ChatInvokeUsage | None:
        if "usage" not in response:
            return None
        u = response["usage"]
        return ChatInvokeUsage(
            prompt_tokens=u.get("inputTokens", 0),
            completion_tokens=u.get("outputTokens", 0),
            total_tokens=u.get("totalTokens", 0),
            prompt_cached_tokens=None,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
        )

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        try:
            from botocore.exceptions import ClientError  # type: ignore
        except ImportError as e:
            raise ImportError("`boto3` not installed.") from e

        bedrock_messages, system_message = AWSBedrockMessageSerializer.serialize_messages(messages)

        if tools and output_format is not None:
            # Converse has exactly one `toolConfig`, and structured output has
            # already claimed it (plus its own forced tool choice).
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "Converse exposes a single toolConfig, which its "
                    "structured-output path already uses to carry the schema. "
                    "Pass tools without output_format."
                ),
            )

        try:
            body: dict[str, Any] = {}
            if system_message:
                body["system"] = system_message
            inference_config = self._get_inference_config()
            if inference_config:
                body["inferenceConfig"] = inference_config
            if output_format is not None:
                body["toolConfig"] = {"tools": self._format_tools_for_request(output_format)}
            elif tools:
                if tool_choice not in _BEDROCK_TOOL_CHOICE:
                    # Converse's toolChoice vocabulary is auto|any|tool — there is
                    # no way to advertise tools while forbidding their use.
                    self.reject_unsupported_tools(
                        tools,
                        reason=(
                            f"Bedrock Converse toolChoice accepts only "
                            f"{sorted(_BEDROCK_TOOL_CHOICE)}; tool_choice="
                            f"{tool_choice!r} has no Converse equivalent."
                        ),
                    )
                body["toolConfig"] = {
                    "tools": AWSBedrockMessageSerializer.serialize_tools(tools),
                    "toolChoice": _BEDROCK_TOOL_CHOICE[tool_choice],
                }
            if self.request_params:
                body.update(self.request_params)
            body = {k: v for k, v in body.items() if v is not None}

            clients = self.client_resources

            def request() -> dict[str, Any]:
                client = clients.get_or_create(
                    "bedrock", self._get_client, close=lambda value: value.close()
                )
                return client.converse(modelId=self.model, messages=bedrock_messages, **body)

            response = await clients.run_sync(request)
            usage = self._get_usage(response)

            if "output" in response and "message" in response["output"]:
                message = response["output"]["message"]
                content = message.get("content", [])

                thinking, redacted_thinking = _reasoning(content)
                if output_format is None:
                    text_content = [item["text"] for item in content if "text" in item]
                    return ChatInvokeCompletion(
                        completion="\n".join(text_content),
                        thinking=thinking,
                        redacted_thinking=redacted_thinking,
                        usage=usage,
                        tool_calls=AWSBedrockMessageSerializer.parse_tool_calls(content),
                    )
                else:
                    for item in content:
                        if "toolUse" in item:
                            # ``input`` is a dict on the normal path and a JSON
                            # string when the model stuffed everything into one
                            # argument; ``_validate_structured`` takes either,
                            # and raises the recoverable error for both.
                            tool_input = item["toolUse"].get("input", {})
                            return ChatInvokeCompletion(
                                completion=self._validate_structured(
                                    output_format, tool_input, usage=usage
                                ),
                                thinking=thinking,
                                redacted_thinking=redacted_thinking,
                                usage=usage,
                            )
                    raise ModelProviderError(
                        message="Expected structured output but no tool use found",
                        model=self.name,
                    )

            if output_format is None:
                return ChatInvokeCompletion(completion="", usage=usage)
            raise ModelProviderError(message="No valid content found in response", model=self.name)

        except ModelProviderError:
            # StructuredOutputValidationError included: the Loop distinguishes
            # "the model wrote something wrong" from "the provider failed", and
            # flattening it here would take that choice away.
            raise
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            if error_code in ("ThrottlingException", "TooManyRequestsException"):
                raise ModelRateLimitError(message=error_message, model=self.name) from e
            raise ModelProviderError(message=error_message, model=self.name) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
