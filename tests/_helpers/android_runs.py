"""What tests of runs on Android share: a scripted model, and the AndroidWorld live modules' setup.

:class:`ScriptedModel` stands in for a chat model: it answers each call with
the next reply of its script and keeps the messages of every call, so a reply
goes the whole way through the agent, the Loop and the capability while no
model is called.

The opt-in AndroidWorld live modules (marker ``requires_android_emulator``)
read three environment variables: ``SPECTRAGENT_TEST_AVD``, the AVD
AndroidWorld's one-time setup prepared; :data:`ANDROIDWORLD_VARIABLE`, the
AndroidWorld installation (its venv, its checkout at the pinned commit, its
cached forwarder, and ffmpeg on PATH;
``docs/architecture_design.md`` §11.2); and
:data:`APK_VARIABLE`, the device-side APK. :func:`installation_problem` says
why a module skips, :func:`write_task_list` writes a task list under
AndroidWorld's interpreter, :func:`androidworld_capability` composes the
Android capability from one YAML block, so a receptor the capability starts
to require is written here once (it types with ``key_events`` and
``fold_accents``, as AndroidWorld and MiniWoB++ runs do, for parity with
AndroidWorld's own typing), and :func:`androidworld_setup` builds the
benchmark's setup component for that installation, which a module routes to
the capability with AndroidWorld's live verifier as ``prepare()`` does
(:func:`bind_androidworld`). :func:`androidworld_composed` composes the
environment and the benchmark through the real ``ConfigComposer`` instead, so
the benchmark's part (the AVD, the ``open_app`` table) is merged as a run
merges it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from spectragent import PACKAGE_ROOT
from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
from spectragent.benchmarks.androidworld.service import Installation
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.verifier import AndroidWorldVerifier
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.llm.messages import BaseMessage, UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion

#: The AndroidWorld installation to run; the live modules skip when it is unset.
ANDROIDWORLD_VARIABLE = "SPECTRAGENT_TEST_ANDROIDWORLD"
#: The device-side APK the capability installs.
APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
TASK_LIST_WRITER = PACKAGE_ROOT / "benchmarks/androidworld/androidworld_service/task_list.py"
#: AndroidWorld's runner adds it to M3A's prompt for its MiniWoB tasks (``run.py``).
MINIWOB_GUIDELINE = (
    "This task is running in a mock app, you must stay in this app and DO NOT use the "
    "`navigate_home` action."
)
#: M3A's action-selection prompt says the goal on this line; its summary prompt does not.
M3A_GOAL_LINE = "The current user goal/request is: "
#: AndroidWorld's accessibility forwarder, as the setting names it.
FORWARDER = (
    "com.google.androidenv.accessibilityforwarder/"
    "com.google.androidenv.accessibilityforwarder.AccessibilityForwarder"
)


def prompt_text(messages: Sequence[BaseMessage]) -> str:
    """The text of the last user message: the step's prompt."""
    (prompt,) = [message for message in messages if isinstance(message, UserMessage)][-1:]
    if isinstance(prompt.content, str):
        return prompt.content
    return "\n".join(part.text for part in prompt.content if part.type == "text")


class ScriptedModel:
    """Answers each call with the next reply of its script, and keeps the messages of every call.

    An exception in the script is raised by its call, as a provider raises one.
    """

    inference_kind = "chat"
    model = name = "scripted"

    def __init__(self, *replies: str | BaseException) -> None:
        self.replies = list(replies)
        self.calls: list[list[BaseMessage]] = []

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion:
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("the script has no reply left")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ChatInvokeCompletion(completion=reply, usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def installation_problem() -> str | None:
    """Why the AndroidWorld live modules cannot run here; ``None`` when they can."""
    root = os.environ.get(ANDROIDWORLD_VARIABLE)
    if not root:
        return f"set {ANDROIDWORLD_VARIABLE} to an AndroidWorld installation"
    if not os.environ.get(APK_VARIABLE):
        return f"set {APK_VARIABLE} to the device-side APK"
    return "; ".join(Installation(Path(root).expanduser()).problems()) or None


def installation() -> Installation:
    """The AndroidWorld installation :data:`ANDROIDWORLD_VARIABLE` names."""
    return Installation(Path(os.environ[ANDROIDWORLD_VARIABLE]).expanduser())


def write_task_list(out: Path, task: str, *, family: str = "android_world") -> None:
    """Write the list of *task* (suite seed 42, index 0) of *family* into the data directory *out*."""
    aw = installation()
    subprocess.run(
        [str(aw.python), "-I", str(TASK_LIST_WRITER), "--src", str(aw.src)]
        + ["--out", str(out), "--family", family, "--tasks", task],
        check=True,
        capture_output=True,
        timeout=180,
        env={"PATH": "/usr/bin:/bin", "HOME": str(out)},
    )


def androidworld_setup() -> AndroidWorldSetup:
    """The ``androidworld`` setup component for the installation :data:`ANDROIDWORLD_VARIABLE` names."""
    return AndroidWorldSetup(root=str(installation().root))


def androidworld_benchmark(loader: AndroidWorldTaskLoader) -> AndroidWorldBenchmark:
    """The ``androidworld`` benchmark of *loader*'s tasks, with :func:`androidworld_setup` as its
    setup and AndroidWorld's live verifier as its verifier, as the shipped YAML lists it.

    What ``make_run(benchmark=...)`` takes, so ``prepare()`` routes both to the
    Android capability as a composed run's would be.
    """
    benchmark = AndroidWorldBenchmark()
    benchmark.task_loader = loader
    benchmark.setup = androidworld_setup()
    benchmark.verifiers = [AndroidWorldVerifier()]
    return benchmark


def bind_androidworld(
    env: AndroidEnvironment, context: Mapping[str, JsonValue], setup: AndroidWorldSetup
) -> AndroidWorldVerifier:
    """Route *setup* and AndroidWorld's live verifier to *env*'s capability as ``prepare()`` does.

    Checked with the task's *context* first, then bound; the verifier is
    returned, the one ``env.verify_live`` judges with.
    """
    verifier = AndroidWorldVerifier()
    env.check_setup(context, setups=[setup], live_verifiers=[verifier])
    env.bind_setups([setup])
    env.bind_live_verifiers([verifier])
    return verifier


def androidworld_composed(data: Path) -> ComposedRun:
    """An Android environment and the ``androidworld`` benchmark over *data*, composed as ``run`` composes them.

    Through the real ``ConfigComposer``: the run's Android block writes no
    AVD and no app table, the benchmark's part gives the test AVD, and its
    ``open_app`` table comes from ``open_app.json`` beside the task list
    (``write_task_list`` writes both), merged into the run's block.
    """
    root: dict[str, JsonValue] = {
        "environment": [
            {
                "env_type": "android",
                "capabilities": [
                    {
                        "capability_id": "android",
                        "service_apk": os.environ[APK_VARIABLE],
                        "tap_injector": {"injector_id": "shell_injection"},
                        "typing_backend": {"typing_id": "key_events", "fold_accents": True},
                        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                        "ui_tree_serializer": {"serializer_id": "m3a"},
                    }
                ],
            }
        ],
        "benchmark": [
            {
                "benchmark_id": "androidworld",
                "task_loader": {"benchmark_name": "androidworld", "data_dir": str(data)},
                "setup": {"setup_id": "androidworld", "root": str(installation().root)},
                "verifiers": [{"verifier_id": "androidworld"}],
                "capability": [
                    {
                        "capability_id": "android",
                        "backend": {
                            "provider_id": "local_emulator",
                            "avd": os.environ["SPECTRAGENT_TEST_AVD"],
                        },
                    }
                ],
            }
        ],
    }
    return ConfigComposer.for_config(root).compose(root)


def androidworld_capability() -> AndroidCapability:
    """The Android capability on the test AVD, composed as a run composes it; it hosts no setup yet."""
    raw: dict[str, JsonValue] = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {
                    "provider_id": "local_emulator",
                    "avd": os.environ["SPECTRAGENT_TEST_AVD"],
                },
                "service_apk": os.environ[APK_VARIABLE],
                "tap_injector": {"injector_id": "shell_injection"},
                # AndroidWorld's own typing, for parity (user ruling 2026-11-03): key
                # events, with accents folded away as AndroidWorld folds them.
                "typing_backend": {"typing_id": "key_events", "fold_accents": True},
                "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                "ui_tree_serializer": {"serializer_id": "m3a"},
            }
        ]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    return capability
