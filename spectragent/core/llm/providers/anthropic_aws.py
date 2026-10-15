"""ChatAnthropicAWS — AWS Bedrock via Anthropic SDK provider."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from anthropic import AsyncAnthropicBedrock
from pydantic import Field

from spectragent.core.llm.providers.anthropic_base import AnthropicCompatBase

if TYPE_CHECKING:
    from boto3.session import Session  # pyright: ignore


@dataclass
class ChatAnthropicAWS(AnthropicCompatBase):
    """AWS Bedrock provider via Anthropic SDK."""

    __registry_key__ = "anthropic.aws"

    # --- AWS-specific fields ---
    aws_access_key: Annotated[
        str | None,
        Field(
            description="AWS access key ID. None (the default): the client resolves AWS credentials."
        ),
    ] = None
    aws_secret_key: Annotated[
        str | None,
        Field(
            description="AWS secret access key. None (the default): the client resolves AWS credentials."
        ),
    ] = None
    aws_session_token: Annotated[
        str | None,
        Field(
            description="AWS session token. None (the default): the client resolves AWS credentials."
        ),
    ] = None
    aws_region: Annotated[
        str | None,
        Field(description="AWS region name. None (the default): the client resolves the region."),
    ] = None
    session: "Session | None" = None

    @property
    def provider(self) -> str:
        return "aws"

    def get_client(self) -> AsyncAnthropicBedrock:
        params: dict[str, Any] = {}
        if self.session:
            creds = self.session.get_credentials()
            params.update(
                {
                    "aws_access_key": creds.access_key,
                    "aws_secret_key": creds.secret_key,
                    "aws_session_token": creds.token,
                    "aws_region": self.session.region_name,
                }
            )
        else:
            if self.aws_access_key:
                params["aws_access_key"] = self.aws_access_key
            if self.aws_secret_key:
                params["aws_secret_key"] = self.aws_secret_key
            if self.aws_region:
                params["aws_region"] = self.aws_region
            if self.aws_session_token:
                params["aws_session_token"] = self.aws_session_token

        if self.max_retries:
            params["max_retries"] = self.max_retries
        if self.default_headers:
            params["default_headers"] = self.default_headers
        if self.default_query:
            params["default_query"] = self.default_query
        if self.http_client is not None:
            params["http_client"] = self.http_client
        return AsyncAnthropicBedrock(**params)

    # ainvoke inherited from AnthropicCompatBase
