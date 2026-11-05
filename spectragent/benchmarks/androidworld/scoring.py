"""A task's judgment, read from the AndroidWorld service its setup component started.

``docs/architecture_design.md`` §11.2. At the Runtime's scoring point (§4.2),
within the run's ``verify_timeout``: when the run's answer goes to the
service, it goes first as an ``answer`` action (``/execute_action``), as
AndroidWorld's own runner hands it over, unless the run ended without one;
then ``/task/score``. HTTP 500, a broken reply or a dead service raise
``AndroidWorldError``, so the environment records a verdict that judged
nothing.

:class:`AndroidWorldServiceVerifier` is the live verifier AndroidWorld's
(``verifier.py``) and VenusBench's share: it calls :func:`read_score` at the
scoring point, on the service and task its benchmark's setup component
started, and the environment keeps the judgment as the run's live verdict
(§4.10.1, §5.3 "判定统一存放"). Which runs' answers it hands over is the
subclass's (:meth:`AndroidWorldServiceVerifier.hands_over_the_answer`).
It is abstract and this module registers nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar

from spectragent.benchmarks.androidworld.service import (
    AndroidWorldError,
    AndroidWorldService,
    params_digest,
)
from spectragent.benchmarks.androidworld.service_setup import AndroidWorldServiceSetup
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTask
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession, DeviceNeeds
from spectragent.core.capabilities.android.benchmark_setup import AndroidBenchmarkSetup
from spectragent.schemas.verdict import Judgment, VerifierIdentity
from spectragent.verifiers._registrar import VerifierBase
from spectragent.verifiers.base import VerifierNeed

#: The capability that hosts it: the one whose device it judges.
_ANDROID = "android"


async def read_score(
    service: AndroidWorldService,
    task: AndroidWorldTask,
    *,
    answer: str | None,
    on_the_answer: bool,
    device_goal: str | None,
) -> Judgment:
    """*task*'s judgment from *service*: its score and what was judged.

    With *on_the_answer* the run's *answer* goes to the service first (none
    goes when the run ended without one), and the evidence says whether one
    went. *device_goal* is the goal the device made at initialize, recorded
    in the evidence when there is one.

    Raises:
        AndroidWorldError: The service refused the answer or gave no finite score.
    """
    if on_the_answer and answer is not None:
        # As AndroidWorld's own runner: the agent's answer action reaches the
        # service before the score, and the service keeps the answer for it
        # (initialize and reset clear it). With no answer nothing is sent, and
        # a task scored on it scores 0, as it does when an agent never answers.
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


@dataclass
class AndroidWorldServiceVerifier(VerifierBase):
    """A benchmark's own judgment of a task, read from the service its setup component started.

    A subclass registers under its benchmark's verifier kind and names the
    setup component it judges through (:attr:`__setup__`), whose installation
    holds the pinned code its verdicts are versioned by.
    """

    __abstract__ = True
    __needs__: ClassVar[frozenset[VerifierNeed]] = frozenset({"live_environment", "answer"})
    #: The setup component whose service it judges through.
    __setup__: ClassVar[type[AndroidWorldServiceSetup]]

    @property
    def capability_id(self) -> str:
        return _ANDROID

    @property
    def device_needs(self) -> DeviceNeeds:
        # The check reads apps' databases and settings as root, through the
        # service's connection to the emulator over its gRPC. Requirements
        # only: the setup component boots the device with them.
        return DeviceNeeds(emulator_grpc=True, root=True)

    def identity(self) -> VerifierIdentity:
        """Its kind at the pinned commit the service runs; no setting of its changes a verdict."""
        return VerifierIdentity(
            verifier_id=self.verifier_id,
            version=self.__setup__.__installation__.code.commit,
            config_signature=self.config_signature(),
        )

    def hands_over_the_answer(self, task: AndroidWorldTask) -> bool:
        """Whether the run's answer goes to the service before *task* is scored."""
        raise NotImplementedError

    async def judge(
        self,
        device: AndroidProviderSession,
        setup: AndroidBenchmarkSetup | None,
        *,
        answer: str | None,
    ) -> Judgment:
        """The task's judgment from the service *setup* started, given the run's *answer*.

        *device* is not read: the benchmark's own check reads the device
        through its service.

        Raises:
            AndroidWorldError: *setup* is not :attr:`__setup__`'s kind, no
                task was initialized, or the service refused the answer or
                gave no finite score.
        """
        expected = self.__setup__
        if not isinstance(setup, expected):
            hosted = "none" if setup is None else repr(setup.setup_id)
            raise AndroidWorldError(
                f"the {self.verifier_id} verifier judges through the service the "
                f"{expected.__registry_key__} setup component started, and the android "
                f"capability hosts {hosted}"
            )
        service, task = setup.started()
        return await read_score(
            service,
            task,
            answer=answer,
            on_the_answer=self.hands_over_the_answer(task),
            device_goal=setup.made_goal(),
        )


__all__ = ["AndroidWorldServiceVerifier", "read_score"]
