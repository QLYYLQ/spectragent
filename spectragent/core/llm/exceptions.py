from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — types only, no import at runtime
    from spectragent.core.llm.views import ChatInvokeUsage


class ModelError(Exception):
    pass


class ModelProviderError(ModelError):
    """Exception raised when a model provider returns an error."""

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        model: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.model = model


class ModelRateLimitError(ModelProviderError):
    """Exception raised when a model provider returns a rate limit error."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        model: str | None = None,
    ):
        super().__init__(message, status_code, model)


class StructuredOutputValidationError(ModelProviderError):
    """The model answered, and the answer does not fit the requested schema.

    Separated from every other provider error because it is the only one
    the *model* can fix. A 429, a dead socket or a 500 end the run; a reply
    that names an action the run does not have is a mistake the model
    corrects when it is told about it — which is what the Loop does with a
    ``ParseError``, and what it could not do while this arrived as a
    generic :class:`ModelProviderError` and aborted the run at the LLM
    phase.

    ``raw_output`` carries the text as the model wrote it, so the caller
    can put it back through the agent's own parser: the agent knows the
    negotiated verb set and produces one located error, where the provider
    can only report a schema mismatch.

    ``usage`` is whatever the call actually cost — the provider's own
    ``ChatInvokeUsage``, typed, because the Loop puts it straight into a
    ``ChatInvokeCompletion`` and ``MetricsCollector`` adds it to the run's
    total. A failed validation is still billed, and dropping it here is
    how a run's token total came out lower than the bill.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_output: str,
        model: str | None = None,
        usage: "ChatInvokeUsage | None" = None,
    ) -> None:
        super().__init__(message, 422, model)
        self.raw_output = raw_output
        self.usage = usage


class ToolsUnsupportedError(ModelProviderError):
    """Raised when ``tools=`` is passed to a provider or path that cannot honour it.

    Dropping the argument instead would leave the model unable to call anything
    while the caller believes it can — a silent failure that looks like a bad
    prompt. This is a client-side contract error, hence 400 rather than 502.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        model: str | None = None,
    ):
        super().__init__(message, status_code, model)
