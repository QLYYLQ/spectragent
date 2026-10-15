"""Public model execution lifecycle, independent of request and recording policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientCleanupDiagnostic:
    """A resource that could not be confirmed closed; never a model-call failure."""

    model: str
    resource: str
    status: Literal["failed", "timeout", "in_use", "cancelled"]
    detail: str


class ModelClientLifecycle(Protocol):
    """The resource-owning surface needed by execution entry points."""

    @property
    def name(self) -> str: ...

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]: ...


def report_cleanup(diagnostic: ClientCleanupDiagnostic) -> None:
    """Make teardown trouble visible even when recording is disabled."""
    logger.warning(
        "Model client cleanup %s: model=%s resource=%s: %s",
        diagnostic.status,
        diagnostic.model,
        diagnostic.resource,
        diagnostic.detail,
    )


def _consume_task(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


async def close_models(
    models: Iterable[ModelClientLifecycle], *, timeout: float = 5.0
) -> tuple[ClientCleanupDiagnostic, ...]:
    """End the models explicitly owned by this execution, independently and bounded.

    Consumers pass their actual dependencies, never a discovered object graph. A
    cleanup exception is reported separately; cancellation of the caller propagates
    while already-started bounded cleanup continues. No client is opened here.
    """
    if timeout <= 0:
        raise ValueError("Model cleanup timeout must be positive")

    async def close_one(model: ModelClientLifecycle) -> tuple[ClientCleanupDiagnostic, ...]:
        name = model.name
        try:
            task = asyncio.create_task(model.aclose(timeout=timeout))
            # Protect consumers from an extension that ignores the public budget.
            done, _ = await asyncio.wait({task}, timeout=timeout + 0.05)
            if not done:
                task.cancel()
                task.add_done_callback(_consume_task)
                diagnostic = ClientCleanupDiagnostic(
                    name, "model", "timeout", "Model cleanup did not finish within its budget"
                )
            else:
                try:
                    return task.result()
                except asyncio.CancelledError:
                    diagnostic = ClientCleanupDiagnostic(
                        name, "model", "cancelled", "Model cleanup cancelled itself"
                    )
        except Exception as exc:
            diagnostic = ClientCleanupDiagnostic(
                name, "model", "failed", f"{type(exc).__name__}: {exc}"
            )
        report_cleanup(diagnostic)
        return (diagnostic,)

    unique = {id(model): model for model in models}
    if not unique:
        return ()

    async def finish() -> tuple[ClientCleanupDiagnostic, ...]:
        results = await asyncio.gather(*(close_one(model) for model in unique.values()))
        return tuple(item for result in results for item in result)

    task = asyncio.create_task(finish())
    task.add_done_callback(_consume_task)
    return await asyncio.shield(task)


@asynccontextmanager
async def model_session(*models: ModelClientLifecycle, timeout: float = 5.0) -> AsyncIterator[None]:
    """A direct-Python execution: lazy reuse inside, explicit release on every exit."""
    if timeout <= 0:
        raise ValueError("Model cleanup timeout must be positive")
    try:
        yield
    finally:
        await close_models(models, timeout=timeout)
