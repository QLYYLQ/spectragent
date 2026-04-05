"""Stub for observability — replaced when full observability module is ported."""

from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def observe(
    func: F | None = None, *, name: str | None = None, **kwargs: Any
) -> F | Callable[[F], F]:
    """No-op decorator until the observability module is ported."""
    if func is not None:
        return func

    def wrapper(fn: F) -> F:
        return fn

    return wrapper
