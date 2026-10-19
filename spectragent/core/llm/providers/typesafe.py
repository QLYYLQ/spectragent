"""Native TypeSafe and OpenRouter Decisions routes, with no Chat translation."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

import httpx
from PIL import Image
from pydantic import Field, ValidationError

from spectragent.core.llm.decision import (
    DecisionCapabilities,
    DecisionImage,
    DecisionRequest,
    DecisionResult,
    Description,
    ScoreQuestion,
)
from spectragent.core.llm.decision_errors import (
    DecisionHTTPError,
    DecisionInputError,
    DecisionLimitError,
    DecisionResponseError,
    DecisionTimeoutError,
    DecisionTransportError,
)
from spectragent.core.llm.decision_registrar import DecisionModelBase
from spectragent.core.llm.views import ModelInvokeUsage


def native_usage(value: object) -> ModelInvokeUsage | None:
    """Only complete reported token counts become a normalized usage record."""
    if not isinstance(value, dict):
        return None
    counts = [value.get("input_tokens"), value.get("output_tokens")]
    if any(type(n) is not int or n < 0 for n in counts):
        return None
    reported = cast(list[int], counts)
    return ModelInvokeUsage(
        prompt_tokens=reported[0],
        completion_tokens=reported[1],
        total_tokens=sum(reported),
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
    )


def _image_part(image: DecisionImage) -> dict[str, Any]:
    mime = "png" if image.format == "png" else "jpeg"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/{mime};base64,{image.data_base64}"},
    }


def _expand_assets(value: Any, assets: dict[str, DecisionImage]) -> Any:
    """Compile native references, including embedded ones, into image parts.

    Whole-value matches win. Embedded references are matched left to right;
    longest first for references sharing a prefix. Object keys stay untouched.
    """
    if isinstance(value, dict):
        return {key: _expand_assets(item, assets) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_assets(item, assets) for item in value]
    if not isinstance(value, str) or not assets:
        return value
    if value in assets:
        return _image_part(assets[value])
    pattern = re.compile("|".join(re.escape(key) for key in sorted(assets, key=len, reverse=True)))
    pieces: list[Any] = []
    end = 0
    for match in pattern.finditer(value):
        if match.start() > end:
            pieces.append(value[end : match.start()])
        pieces.append(_image_part(assets[match.group()]))
        end = match.end()
    if not pieces:
        return value
    if end < len(value):
        pieces.append(value[end:])
    return pieces


class TypeSafeBase(DecisionModelBase):
    __abstract__ = True

    def __init__(
        self,
        model: Annotated[str, Field(description="Exact Jev model wire ID")] = "jev-1.14.0",
        api_key: Annotated[
            str | None,
            Field(description="API key. None (the default): read api_key_env at invocation."),
        ] = None,
        api_key_env: Annotated[
            str, Field(description="Name of the API-key environment variable")
        ] = "TYPESAFE_API_KEY",
        base_url: Annotated[
            str | None,
            Field(
                description="Service root. None (the default): the selected route's public root. Native roots exclude /v1; OpenRouter includes /api."
            ),
        ] = None,
        timeout: Annotated[
            float,
            Field(
                gt=0,
                allow_inf_nan=False,
                description="HTTP timeout in seconds; one attempt, without automatic retries",
            ),
        ] = 35.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip() or not api_key_env.strip():
            raise ValueError("Decision model and api_key_env must be nonempty")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Decision timeout must be finite and positive")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.timeout = timeout
        self.http_client = http_client
        root = base_url or self.default_root
        parsed = urlsplit(root)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError(
                "Decision base_url must be an HTTP service root without credentials, query or fragment"
            )
        if parsed.path.rstrip("/").endswith(("/v1", "/systemone", "/alpha/decisions")):
            raise ValueError("Decision base_url is the service root, not /v1 or the endpoint")

    @property
    def model_type(self) -> str:
        return "typesafe"

    @property
    def name(self) -> str:
        return self.model

    @property
    def default_root(self) -> str:
        raise NotImplementedError

    @property
    def provider(self) -> str:
        raise NotImplementedError

    @property
    def endpoint_path(self) -> str:
        raise NotImplementedError

    @property
    def capabilities(self) -> DecisionCapabilities:
        version = re.fullmatch(r"(?:typesafe/)?jev-(1\.(?:13|14))(?:\.0|-\d{8})?", self.model)
        known = version is not None
        box = known and version.group(1) == "1.14"
        return DecisionCapabilities(
            question_types=frozenset(
                {"choice", "score", "noul", "box"} if box else {"choice", "score", "noul"}
            ),
            images=known,
            min_image_side=256 if self.provider == "openrouter" else 280,
        )

    def serialize(self, request: DecisionRequest) -> dict[str, Any]:
        """The exact route body. Logical provenance stays in the call record."""
        body = request.logical_dump()
        for question in body["questions"].values():
            if question.get("type") == "noul" and question.get("criteria") is None:
                question.pop("criteria", None)
        if self.provider == "openrouter":
            body.pop("assets")
            body["state"] = _expand_assets(body["state"], request.assets)
            body["questions"] = {
                key: {
                    field: _expand_assets(value, request.assets)
                    if field in {"instructions", "criteria"}
                    else value
                    for field, value in question.items()
                }
                for key, question in body["questions"].items()
            }
        elif body["assets"]:
            for asset in body["assets"].values():
                asset.pop("provenance", None)
        else:
            body.pop("assets")
        return {"model": self.model, **body}

    def validate_request(self, request: DecisionRequest) -> None:
        caps = self.capabilities
        for key, question in request.questions.items():
            fields = question.model_dump(mode="json", by_alias=True, exclude_unset=True)
            if isinstance(question, ScoreQuestion) and any(
                level is None for level in question.criteria
            ):
                raise DecisionInputError(
                    f"{self.provider} does not accept null Score levels (question {key!r})",
                    model=self.model,
                )
            criteria = fields.get("criteria")
            if (
                question.type == "noul"
                and isinstance(criteria, dict)
                and any(value is None for value in criteria.values())
            ):
                raise DecisionInputError(
                    f"{self.provider} does not accept null Noul criteria (question {key!r})",
                    model=self.model,
                )
            if self.provider == "openrouter" and question.instructions is None:
                raise DecisionInputError(
                    f"OpenRouter requires non-null instructions (question {key!r})",
                    model=self.model,
                )
            if question.type not in caps.question_types:
                raise DecisionInputError(
                    f"{self.name} does not declare support for {question.type} (question {key!r}); use a supported fixed version",
                    model=self.model,
                )
        if request.assets and not caps.images:
            raise DecisionInputError(
                f"{self.name} has no declared image capability; select a supported fixed Jev version",
                model=self.model,
            )
        for key, asset in request.assets.items():
            try:
                data = base64.b64decode(asset.data_base64, validate=True)
                if not data or base64.b64encode(data).decode() != asset.data_base64:
                    raise ValueError("requires canonical padded base64")
                if len(data) > caps.max_image_bytes:
                    raise DecisionLimitError(
                        f"Image {key!r} exceeds the file-size limit", model=self.model
                    )
                with Image.open(io.BytesIO(data)) as img:
                    width, height = img.size
                    if img.format != ("PNG" if asset.format == "png" else "JPEG") or getattr(
                        img, "is_animated", False
                    ):
                        raise ValueError("requires a still image matching its declared format")
                    if not (
                        caps.min_image_side <= min(width, height)
                        and max(width, height) <= caps.max_image_side
                        and width * height >= caps.min_image_pixels
                    ):
                        raise DecisionLimitError(
                            f"Image {key!r} dimensions {width}x{height} are outside {self.provider}'s declared limits",
                            model=self.model,
                        )
                    img.verify()
            except DecisionLimitError:
                raise
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
                raise DecisionInputError(f"Invalid image {key!r}: {exc}", model=self.model) from exc

    async def ainvoke(self, request: DecisionRequest) -> DecisionResult:
        if not isinstance(request, DecisionRequest):
            raise DecisionInputError(
                "Decision ainvoke requires a DecisionRequest", model=self.model
            )
        # Revalidate mutable models before sending; never silently drop new fields.
        try:
            request = DecisionRequest.model_validate(request.logical_dump())
        except (ValueError, TypeError) as exc:
            raise DecisionInputError(f"Invalid DecisionRequest: {exc}", model=self.model) from exc
        self.validate_request(request)
        body = self.serialize(request)
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            raise DecisionInputError(
                "Decision request is not finite UTF-8 JSON", model=self.model
            ) from exc
        if len(data) > self.capabilities.max_request_bytes:
            raise DecisionLimitError(
                "Decision request body exceeds 32 MiB", model=self.model, status_code=413
            )
        key = self.api_key or os.getenv(self.api_key_env)
        if not key:
            raise DecisionHTTPError(
                f"Missing API key: set {self.api_key_env}", model=self.model, status_code=401
            )
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self.provider == "direct" and request.assets:
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        resources = self.client_resources
        async with resources.use():
            client = resources.get_or_create(
                "decision.http",
                lambda: self.http_client if self.http_client is not None else httpx.AsyncClient(),
                close=None if self.http_client is not None else lambda client: client.aclose(),
            )
            try:
                response = await client.post(
                    (self.base_url or self.default_root).rstrip("/") + self.endpoint_path,
                    content=data,
                    headers=headers,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                raise DecisionTimeoutError(
                    "Decision request timed out", model=self.model, status_code=504
                ) from exc
            except httpx.RequestError as exc:
                raise DecisionTransportError(
                    f"Decision transport failed: {type(exc).__name__}",
                    model=self.model,
                    status_code=502,
                ) from exc
        try:
            raw = response.json()
        except ValueError as exc:
            error_type = DecisionResponseError if response.is_success else DecisionHTTPError
            raise error_type(
                "Decision response is not JSON",
                model=self.model,
                status_code=response.status_code,
                raw_output=response.text,
            ) from exc
        usage = native_usage(raw.get("usage")) if isinstance(raw, dict) else None
        if not response.is_success:
            text = json.dumps(raw, ensure_ascii=False)
            error_type = (
                DecisionLimitError
                if response.status_code == 413 or "max_tokens_exceeded" in text
                else DecisionHTTPError
            )
            raise error_type(
                f"Decision HTTP {response.status_code}",
                model=self.model,
                status_code=response.status_code,
                usage=usage,
                raw_output=text,
            )
        try:
            if not isinstance(raw, dict):
                raise ValueError("response must be an object")
            result = DecisionResult(
                requested_model=self.model,
                model=raw["model"],
                answers=raw["answers"],
                usage=usage,
                native_usage=raw.get("usage"),
                metadata={
                    key: value
                    for key, value in raw.items()
                    if key not in {"model", "answers", "usage"}
                },
            )
            legends = {
                key: cast(list[Description], body["questions"][key]["criteria"])
                for key, question in request.questions.items()
                if isinstance(question, ScoreQuestion)
            }
            result.check_answers(request, score_legends=legends)
            return result
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DecisionResponseError(
                f"Invalid Decision response: {exc}",
                model=self.model,
                status_code=422,
                usage=usage,
                raw_output=json.dumps(raw, ensure_ascii=False),
            ) from exc


class TypeSafeDirect(TypeSafeBase):
    __registry_key__ = "typesafe.direct"

    @property
    def provider(self) -> str:
        return "direct"

    @property
    def default_root(self) -> str:
        return "https://api.typesafe.ai"

    @property
    def endpoint_path(self) -> str:
        return "/v1/systemone"


class TypeSafeOpenRouter(TypeSafeBase):
    __registry_key__ = "typesafe.openrouter"

    def __init__(
        self,
        model: Annotated[
            str, Field(description="Exact OpenRouter Jev model wire ID")
        ] = "typesafe/jev-1.14",
        api_key_env: Annotated[
            str, Field(description="Name of the API-key environment variable")
        ] = "OPENROUTER_API_KEY",
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, api_key_env=api_key_env, **kwargs)

    @property
    def provider(self) -> str:
        return "openrouter"

    @property
    def default_root(self) -> str:
        return "https://openrouter.ai/api"

    @property
    def endpoint_path(self) -> str:
        return "/alpha/decisions"
