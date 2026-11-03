"""AndroidWorld's judgment of a task, read from the service its setup component started.

``docs/architecture_design.md`` §11.2. At the Runtime's scoring point (§4.2),
within the run's ``verify_timeout``: for an information-retrieval task, scored
on the agent's answer, the run's answer goes to the service first as an
``answer`` action (``/execute_action``), as AndroidWorld's own runner hands it
over, unless the run ended without one; then ``/task/score``. HTTP 500, a
broken reply or a dead service raise ``AndroidWorldError``, so the environment
records a verdict that judged nothing.

Until AndroidWorld's live verifier lands (§4.10.1 "分段实施"), the
``androidworld`` setup component hands this judgment to the Android
capability's ``read_score`` (``TaskOracle``), and the environment keeps it as
the run's live verdict under :func:`identity`. The verifier will call
:func:`read_score` itself, on the service and task it shares with the setup
component.
"""

from __future__ import annotations

import math

from spectragent.benchmarks.androidworld.service import (
    PINNED_COMMIT,
    AndroidWorldError,
    AndroidWorldService,
    params_digest,
)
from spectragent.benchmarks.androidworld.tasks import INFORMATION_RETRIEVAL, AndroidWorldTask
from spectragent.schemas.verdict import Judgment, VerifierIdentity

#: Who judges, the verifier kind every verdict it gives names: the
#: benchmark's primary verifier (``AndroidWorldBenchmark``).
VERIFIER_ID = "androidworld"


def identity() -> VerifierIdentity:
    """AndroidWorld at the pinned commit its service runs, with no setting that changes a verdict."""
    return VerifierIdentity(verifier_id=VERIFIER_ID, version=PINNED_COMMIT)


async def read_score(
    service: AndroidWorldService,
    task: AndroidWorldTask,
    *,
    answer: str | None,
    device_goal: str | None,
) -> Judgment:
    """*task*'s judgment from *service*, given the run's *answer*: its score and what was judged.

    *device_goal* is the goal the device made at initialize, recorded in the
    evidence when there is one.

    Raises:
        AndroidWorldError: The service refused the answer or gave no finite score.
    """
    on_the_answer = task.family == INFORMATION_RETRIEVAL
    if on_the_answer and answer is not None:
        # As AndroidWorld's own runner: the agent's answer action reaches the
        # service before the score, and the service keeps the answer for it
        # (initialize and reset clear it). With no answer nothing is sent, and
        # the task scores 0, as it does when an agent never answers there.
        await service.call(
            "POST",
            "/execute_action",
            {},
            body={"action_type": "answer", "text": answer},
            timeout_s=None,
        )
    reply = await service.call(
        "GET", "/task/score", {"task_type": task.task, "task_idx": task.index}, timeout_s=None
    )
    # Some tasks answer a bool (RetroPlaylistDuration returns False for a
    # wrongly named playlist); AndroidWorld's own runner counts it as 0 or 1.
    score = reply.get("score")
    if not isinstance(score, int | float) or not math.isfinite(score):
        raise AndroidWorldError(f"GET /task/score answered no finite score: {reply!r}")
    provenance = {
        "family": task.family,
        "task": task.task,
        "index": str(task.index),
        "suite_seed": str(task.suite_seed),
        "seed": str(task.params["seed"]),
        "params_sha256": params_digest(task.params),
    }
    if device_goal is not None:
        provenance["goal"] = device_goal
    if on_the_answer:
        # Whether there was an answer to score; its text is in result.json.
        provenance["answer"] = "sent" if answer is not None else "none"
    return Judgment(score=float(score), evidence=dict(provenance))


__all__ = ["VERIFIER_ID", "identity", "read_score"]
