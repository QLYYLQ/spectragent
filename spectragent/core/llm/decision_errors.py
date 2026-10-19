"""Decision failures never enter a Chat model's structured-output retry path."""

from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.views import ModelInvokeUsage


class DecisionError(ModelProviderError):
    def __init__(
        self,
        message: str,
        *,
        model: str,
        status_code: int = 400,
        usage: ModelInvokeUsage | None = None,
        raw_output: str | None = None,
    ) -> None:
        super().__init__(message, status_code, model)
        self.usage = usage
        self.raw_output = raw_output


class DecisionInputError(DecisionError):
    """The logical request or selected modality cannot be served."""


class DecisionLimitError(DecisionError):
    """The request exceeds a byte, image or provider context limit."""


class DecisionResponseError(DecisionError):
    """The service answered but violated the requested native answer contract."""


class DecisionHTTPError(DecisionError):
    """An HTTP failure, retaining any usage the server reported."""


class DecisionTimeoutError(DecisionError):
    """A model request timed out; no unreported usage is invented."""


class DecisionTransportError(DecisionError):
    """The HTTP transport failed without a valid response."""
