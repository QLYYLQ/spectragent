"""Hand an Agent its restored history without the source run's recorded replies.

A branch with ``history_source: restored`` rebuilds the prefix from what actually
executed, and every restored step also carries the source model's reply (its
thought included) as labelled source material (``RestoredHistory.source_reply``,
rendered by ``agents/rendering/prompt_context.py::restored_history_message``). Under a task
variant those thoughts were written for another task. This hook is the ablation
arm: around the Agent's ``build_restored_history`` it passes a copy of the history
whose ``source_reply`` is empty, so the recorded actions and the actual restored
execution stay and the thoughts do not reach the prompt. The trace is unchanged:
the step record's ``source_history`` is the Loop's and keeps the reply as
provenance; the recorded model calls show the prompt the model actually saw.

See docs/architecture_design.md §4.1 ("restored 历史") and §4.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.schemas.prompt import RestoredHistory


@dataclass(eq=False)
class OmitSourceRepliesHook(HookBase):
    """Around ``build_restored_history``: the history the Agent formats has no source reply."""

    __registry_key__ = "omit_source_replies"
    __intercepts__ = [Intercept("build_restored_history", role="agent", advice="around")]

    async def around_build_restored_history(
        self, *, proceed: Any, args: tuple[Any, ...], kwargs: dict[str, Any], **ctx: Any
    ) -> Any:
        history = kwargs.get("history")
        if not isinstance(history, RestoredHistory):
            raise TypeError("omit_source_replies requires keyword-only history: RestoredHistory")
        if history.source_reply:
            # The around chain hands every link and the target the same kwargs mapping;
            # replacing the entry is how an around advice changes what the target receives.
            kwargs["history"] = history.model_copy(update={"source_reply": ""})
        return await proceed()
