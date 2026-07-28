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
