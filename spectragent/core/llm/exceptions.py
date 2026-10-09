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

    ``raw_output`` carries the reply as the *validator* saw it, so the
    caller can put it back through the agent's own parser: the agent knows
    the negotiated verb set and produces one located error, where the
    provider can only report a schema mismatch.

    Deliberately not "as the model wrote it", which is what this said while
    being untrue on most paths: a provider unwraps a Markdown fence
    (``core/llm/fences.py``), and some trim surrounding prose or parse the
    text to a dict, before validating. The unwrapped form is the one the
    agent's parser wants, so it is the one kept here — and when it differs
    from the wire text the provider says so in ``message``, because "fenced
    *and* the wrong shape" is the signature of a gateway that dropped
    ``response_format`` and would otherwise leave no trace of itself.

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


class ModelOutputTruncatedError(ModelProviderError):
    """Generation stopped at the token ceiling, mid-answer.

    The reply is half-written, so whatever reads it next fails — the JSON
    does not close, the agent's parser finds no actions. Letting it fail
    there names the wrong culprit: the run ends saying the model would not
    follow the schema, when the budget cut it off. On an evaluation
    platform that is the expensive half of the bug, because the benchmark
    then attributes *our* ceiling to the model under test.

    Separate from :class:`StructuredOutputValidationError` for the reason
    that class exists: that one is the error the *model* can fix, and this
    one is not. Re-asking cannot help — the ceiling is unchanged and the
    history is now longer, so the retry truncates earlier. Everything the
    Loop does for a parse failure (feed the error back, re-ask, count a
    streak) is wasted here, and each wasted attempt is a *maximum-length*
    completion, the most expensive kind there is.

    **``status_code`` is 400 on purpose and load-bearing.** The base
    defaults to 502, which sits inside ``ChatGoogle.retryable_status_codes``
    — so a truncation raised with the default would be retried five times
    with backoff by the provider's own loop, turning one wasted generation
    into five. 400 also says the right thing: the request asked for more
    than it allowed itself, which is the caller's error, not the server's.

    ``usage`` carries what the truncated call actually cost. It is billed
    like any other, and the paths that raise this never reach the normal
    return, so without it the most expensive call of the run is the one
    that lands in the metrics as zero.

    ``partial_output`` is what the reply had generated when the ceiling cut
    it: the half-written JSON or prose, and on Anthropic the serialised
    ``tool_use`` input that carries a forced-tool structured reply. The Loop
    aborts the step on this error, so the trace's failure record is the
    only place that text survives — without it a reader sees *that* the
    budget ran out and never *what* the model was spending it on.
    ``None`` means the provider handed back no text at all; ``""`` means it
    handed back an empty one. Deliberately not called ``raw_output``: on
    :class:`StructuredOutputValidationError` that name means the reply *as
    the validator saw it*, and nothing validated this one. Never part of
    the message — it can be tens of thousands of tokens long.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        stop_reason: str | None = None,
        ceiling_field: str = "max_tokens",
        ceiling: int | None = None,
        usage: "ChatInvokeUsage | None" = None,
        partial_output: str | None = None,
    ) -> None:
        spent = getattr(usage, "completion_tokens", None)
        message = (
            f"Response truncated at the completion-token ceiling: "
            f"stop_reason={stop_reason!r}, {ceiling_field}={ceiling}"
            + (f", completion_tokens={spent}" if spent is not None else "")
            + f". The reply is incomplete — raise {ceiling_field}, or reduce "
            "what the step asks the model to write."
        )
        super().__init__(message, 400, model)
        self.stop_reason = stop_reason
        self.ceiling_field = ceiling_field
        self.ceiling = ceiling
        self.usage = usage
        self.partial_output = partial_output


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
