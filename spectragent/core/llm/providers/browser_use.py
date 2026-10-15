"""ChatBrowserUse — Client for browser-use cloud API."""

import asyncio
import logging
import os
import random
from collections.abc import Sequence
from typing import Annotated, Any, TypeVar, overload

import httpx
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.client_resources import ClientResources
from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ChatBrowserUse(ChatModelBase):
    __registry_key__ = "browser_use.default"

    def __init__(
        self,
        model: Annotated[str, Field(description="Browser-use model name")] = "bu-latest",
        api_key: Annotated[
            str | None,
            Field(
                description="Browser-use API key. None (the default): read from BROWSER_USE_API_KEY."
            ),
        ] = None,
        base_url: Annotated[
            str | None,
            Field(
                description=(
                    "API base URL. None (the default): read from BROWSER_USE_LLM_URL, else "
                    "https://llm.api.browser-use.com."
                )
            ),
        ] = None,
        timeout: Annotated[float, Field(description="Request timeout in seconds")] = 120.0,
        max_retries: Annotated[int, Field(description="Maximum retry attempts")] = 5,
        retry_base_delay: Annotated[
            float, Field(description="Base delay for exponential backoff")
        ] = 1.0,
        retry_max_delay: Annotated[
            float, Field(description="Maximum delay between retries")
        ] = 60.0,
        request_type: Annotated[
            str, Field(description="Browser-use API request_type tag")
        ] = "browser_agent",
        http_client: httpx.AsyncClient | None = None,
    ):
        valid_models = ["bu-latest", "bu-1-0", "bu-2-0"]
        is_valid = model in valid_models or model.startswith("browser-use/")
        if not is_valid:
            raise ValueError(
                f"Invalid model: '{model}'. Must be one of {valid_models} or start with 'browser-use/'"
            )

        self.model = "bu-1-0" if model == "bu-latest" else model
        self.fast = False
        self.api_key = api_key or os.getenv("BROWSER_USE_API_KEY")
        self.base_url = base_url or os.getenv(
            "BROWSER_USE_LLM_URL", "https://llm.api.browser-use.com"
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.request_type = request_type
        self.http_client = http_client

        if not self.api_key:
            raise ValueError(
                "You need to set the BROWSER_USE_API_KEY environment variable. "
                "Get your key at https://cloud.browser-use.com/new-api-key"
            )

    @property
    def model_type(self) -> str:
        return "browser_use"

    @property
    def name(self) -> str:
        return self.model

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: None = None,
        request_type: str | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T],
        request_type: str | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    @property
    def supports_tools(self) -> bool:
        """The cloud endpoint exposes a fixed browser-agent contract, not tools."""
        return False

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        request_type: str | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        # The Browser Use cloud endpoint answers a fixed browser-agent request
        # shape; it has no field for caller-supplied tool declarations.
        self.reject_unsupported_tools(tools)

        session_id = kwargs.get("session_id")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(msg) for msg in messages],
            "fast": self.fast,
            # Configurable: previously hardwired to "browser_agent" with no way to
            # override it from config, so every caller was tagged as a browser run.
            "request_type": request_type if request_type is not None else self.request_type,
        }
        if session_id:
            payload["session_id"] = session_id
        if output_format is not None:
            payload["output_format"] = output_format.model_json_schema()

        clients = self.client_resources
        async with clients.use():
            last_error: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    result = await self._make_request(payload, clients)
                    break
                except httpx.HTTPStatusError as e:
                    last_error = e
                    if (
                        e.response.status_code in RETRYABLE_STATUS_CODES
                        and attempt < self.max_retries - 1
                    ):
                        delay = min(self.retry_base_delay * (2**attempt), self.retry_max_delay)
                        await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                        continue
                    self._raise_http_error(e)
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    last_error = e
                    if attempt < self.max_retries - 1:
                        delay = min(self.retry_base_delay * (2**attempt), self.retry_max_delay)
                        await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                        continue
                    raise ValueError(
                        f"Failed to connect after {self.max_retries} attempts: {e}"
                    ) from e
                except Exception as e:
                    raise ValueError(f"Failed to connect to browser-use API: {e}") from e
            else:
                if last_error is not None:
                    if isinstance(last_error, httpx.HTTPStatusError):
                        self._raise_http_error(last_error)
                    raise ValueError(
                        f"Request failed after {self.max_retries} attempts: {last_error}"
                    )
                raise RuntimeError("Retry loop completed without return or exception")

        usage = None
        if "usage" in result and result["usage"] is not None:
            from spectragent.core.llm.views import ChatInvokeUsage

            usage = ChatInvokeUsage(**result["usage"])

        if output_format is not None:
            # Pydantic already coerces nested models, including discriminated
            # unions inside list fields. The old pre-pass hardcoded the field name
            # "action" and validated *every* element against `get_args(...)[0]` —
            # i.e. an arbitrary member of the union — so heterogeneous action lists
            # were coerced to the wrong type.
            completion = self.validate_structured(output_format, result["completion"], usage=usage)
        else:
            completion = result["completion"]

        # The endpoint's reply carries no reasoning, only ``completion`` and
        # ``usage``, so ``thinking`` stays ``None`` (docs/architecture_design.md §4.7).
        return ChatInvokeCompletion(completion=completion, usage=usage)

    async def _make_request(self, payload: dict, clients: ClientResources) -> dict:
        client = self.http_client
        if client is None:
            client = clients.get_or_create(
                "browser_use",
                lambda: httpx.AsyncClient(timeout=self.timeout),
                close=lambda value: value.aclose(),
            )
        response = await client.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()

    def _raise_http_error(self, e: httpx.HTTPStatusError) -> None:
        try:
            error_detail = e.response.json().get("detail", str(e))
        except Exception:
            error_detail = str(e)
        sc = e.response.status_code
        if sc == 401:
            raise ModelProviderError(
                message=f"Invalid API key. {error_detail}", status_code=401, model=self.name
            )
        elif sc == 402:
            raise ModelProviderError(
                message=f"Insufficient credits. {error_detail}", status_code=402, model=self.name
            )
        elif sc == 429:
            raise ModelRateLimitError(
                message=f"Rate limit exceeded. {error_detail}", status_code=429, model=self.name
            )
        elif sc in {500, 502, 503, 504}:
            raise ModelProviderError(
                message=f"Server error. {error_detail}", status_code=sc, model=self.name
            )
        else:
            raise ModelProviderError(
                message=f"API request failed: {error_detail}", status_code=sc, model=self.name
            )

    def _serialize_message(self, message: BaseMessage) -> dict:
        msg_dict = message.model_dump()
        return {"role": msg_dict["role"], "content": msg_dict["content"]}
