"""Lifecycle event constants.

These events are NOT method interceptions — they represent structural
boundaries that AgentRuntime triggers directly via hook.on_run_start/end/error().

Method-level events (observation, LLM calls, action execution) are handled
by the AOP engine via ``__intercepts__`` declarations.
"""


class LifecycleEvents:
    """AgentRuntime lifecycle event names.

    Used for logging and diagnostics, not for routing. The actual lifecycle
    callbacks are ``on_run_start()``, ``on_run_end()``, ``on_run_error()``
    on the ``RuntimeHook`` Protocol.
    """

    RUN_START = "run_start"
    RUN_END = "run_end"
    RUN_ERROR = "run_error"
