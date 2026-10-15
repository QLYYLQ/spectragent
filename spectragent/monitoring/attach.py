"""Attach transient observation before the runtime assembles its AOP proxies."""

from typing import TYPE_CHECKING

from spectragent.monitoring.protocols import ProgressSink

if TYPE_CHECKING:
    from spectragent.core.composer import ComposedRun


def attach_progress(run: "ComposedRun", state: ProgressSink) -> None:
    """Bind a fresh sink, adding environment interceptions only when it exists.

    Reuse a configured monitor if present; configuring the hook and selecting
    a display must not count every model call twice.
    """
    from spectragent.core.hooks.live_monitor import LiveMonitor

    monitors = [hook for hook in run.hooks if getattr(hook, "hook_id", None) == "live_monitor"]
    if len(monitors) > 1:
        raise ValueError("Progress display requires at most one live_monitor hook")
    monitor = monitors[0] if monitors else LiveMonitor()
    monitor.bind(state, environment=bool(run.instances("environment")))

    def trace_location() -> str | None:
        for hook in run.hooks:
            location = getattr(hook, "run_dir", None)
            if location is not None:
                return str(location)
        return None

    state.set_trace_locator(trace_location)
    if not monitors:
        hooks = run.by_layer.setdefault("hook", {})
        key = "live_monitor"
        while key in hooks:
            key = f"_{key}"
        hooks[key] = monitor
