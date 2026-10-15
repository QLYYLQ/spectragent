"""Rich's compact progress view, outside the runtime interception path."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from spectragent.monitoring.state import ProgressState

T = TypeVar("T")


def run_with_progress(factory: Callable[[], Awaitable[T]], state: ProgressState) -> T:
    """Run on a fresh loop, preserving Ctrl-C cancellation and its cleanup."""
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    console = Console(stderr=True)

    async def run() -> T:
        if not console.is_terminal:
            return await factory()
        with Live(console=console, auto_refresh=False, transient=True) as live:

            async def render() -> None:
                while True:
                    snapshot = state.snapshot()
                    live.update(
                        Text(
                            f"{snapshot['task_id'] or 'Preparing'} · {snapshot['phase']} · "
                            f"{snapshot['elapsed_s']:.1f}s · {snapshot['policy_steps']} steps · "
                            f"{snapshot['calls']} calls · {snapshot['tokens']:,} known tokens · "
                            f"{snapshot['usage_missing']} calls without usage"
                        ),
                        refresh=True,
                    )
                    await asyncio.sleep(0.15)

            renderer = asyncio.create_task(render())
            try:
                return await factory()
            finally:
                renderer.cancel()
                await asyncio.gather(renderer, return_exceptions=True)

    return asyncio.run(run())
