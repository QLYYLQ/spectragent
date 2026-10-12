"""Generalized task schema for benchmark data loaders.

``TaskDefinition`` is the generic, environment-agnostic task model used
by every benchmark data loader. Per-environment requirements (a browser
``start_url``, a SWE-bench container image + checkout commit, an OSWorld
VM snapshot) live inside the discriminated ``setup`` union; truly
benchmark-specific fields go into ``metadata``.

The discriminator on ``TaskSetup`` is the ``setup_type`` field. Every
variant subclasses :class:`SetupBase` and declares three things:

``__env_types__``   which ``TaskDefinition.env_type`` values select it —
                    this is what lets a loader derive its ``setup_type``
                    from ``self.env_type`` instead of hardcoding one.
``__raw_keys__``    top-level raw-record keys a loader grafts onto the
                    setup instead of leaving them as untyped extras
                    (``start_url`` for browser, ``base_commit`` /
                    ``FAIL_TO_PASS`` / ... for docker).
``to_context()``    the per-domain runtime context forwarded to
                    ``Capability.setup()``.

Adding a new environment therefore means adding exactly one class here —
no loader edit, no lookup table to keep in sync.

See docs/architecture_design.md §4.10 and docs/execution_dag.md M15.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from spectragent.schemas.restoration import BranchProvenance

# ── Setup variants ────────────────────────────────────────────────────


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty-container entries from a context dict."""
    return {
        k: v
        for k, v in data.items()
        if v is not None and not (isinstance(v, (list, dict, str)) and len(v) == 0)
    }


class SetupBase(BaseModel):
    """Common contract shared by every ``TaskSetup`` variant.

    ``extra="allow"`` mirrors ``TaskDefinition``: a nested ``setup`` block
    carrying keys this variant does not model is preserved rather than
    silently dropped.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: ``TaskDefinition.env_type`` values that select this variant.
    __env_types__: ClassVar[tuple[str, ...]] = ()

    #: Top-level raw-record keys a loader grafts onto this setup.
    __raw_keys__: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def setup_type_value(cls) -> str:
        """The literal ``setup_type`` discriminator value of this variant."""
        return str(cls.model_fields["setup_type"].default)

    def to_context(self) -> dict[str, Any]:
        """Per-domain runtime context handed to ``Capability.setup()``.

        Each variant decides what its capability needs to know; nothing
        outside this module should introspect the setup type.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement to_context()")


class BrowserSetup(SetupBase):
    """Browser session requirements for a task."""

    __env_types__: ClassVar[tuple[str, ...]] = ("browser",)
    __raw_keys__: ClassVar[tuple[str, ...]] = ("start_url",)

    setup_type: Literal["browser"] = "browser"

    start_url: str | None = None
    login_required: bool = False
    login_type: str | None = None  # "account_password", "sso", "oauth"
    credentials_ref: str | None = None  # key into credential store, never plaintext
    risk_control: bool = False
    risk_control_types: list[str] = Field(default_factory=list)

    def to_context(self) -> dict[str, Any]:
        return _prune(
            {
                "start_url": self.start_url,
                "login_required": self.login_required or None,
                "login_type": self.login_type,
                "credentials_ref": self.credentials_ref,
                "risk_control": self.risk_control or None,
                "risk_control_types": self.risk_control_types,
            }
        )


class DockerSetup(SetupBase):
    """Container requirements for a task (SWE-bench and friends).

    Field names follow the SWE-bench record vocabulary so a raw upstream
    instance validates directly. ``fail_to_pass`` / ``pass_to_pass``
    additionally accept the dataset's uppercase key names and its
    JSON-encoded-string encoding of those lists.
    """

    __env_types__: ClassVar[tuple[str, ...]] = (
        "docker",
        "docker_terminal",
        "container",
    )
    __raw_keys__: ClassVar[tuple[str, ...]] = (
        "image",
        "instance_id",
        "repo",
        "base_commit",
        "environment_setup_commit",
        "version",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
    )

    setup_type: Literal["docker"] = "docker"

    # ── Container identity ──
    image: str | None = None  # e.g. "swebench/sweb.eval.x86_64.django_1776_django-11333"
    workdir: str | None = None  # None = use the image's own WORKDIR ("/testbed" for SWE-bench)
    env: dict[str, str] = Field(default_factory=dict)

    # ── Repository checkout ──
    instance_id: str | None = None  # SWE-bench native record identity
    repo: str | None = None  # "django/django"
    base_commit: str | None = None  # commit the agent starts from
    environment_setup_commit: str | None = None  # commit the image was built at
    version: str | None = None  # SWE-bench repo version, selects the image tag

    # ── Commands ──
    setup_commands: list[str] = Field(default_factory=list)
    test_command: str | None = None  # how to invoke the test node-ids below

    # ── Scoring targets (test node-ids) ──
    fail_to_pass: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("fail_to_pass", "FAIL_TO_PASS"),
    )
    pass_to_pass: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("pass_to_pass", "PASS_TO_PASS"),
    )

    @field_validator("fail_to_pass", "pass_to_pass", mode="before")
    @classmethod
    def _decode_test_list(cls, value: Any) -> Any:
        """Accept SWE-bench's JSON-encoded-string form of these lists."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    def to_context(self) -> dict[str, Any]:
        return _prune(
            {
                "image": self.image,
                "workdir": self.workdir,
                "env": self.env,
                "instance_id": self.instance_id,
                "repo": self.repo,
                "base_commit": self.base_commit,
                "environment_setup_commit": self.environment_setup_commit,
                "version": self.version,
                "setup_commands": self.setup_commands,
                "test_command": self.test_command,
                "fail_to_pass": self.fail_to_pass,
                "pass_to_pass": self.pass_to_pass,
            }
        )


class VMSetup(SetupBase):
    """Desktop virtual-machine requirements for a task (OSWorld and friends).

    ``config`` is the benchmark's own ordered list of setup actions
    (OSWorld ships e.g. ``{"type": "launch", "parameters": {...}}``);
    it stays opaque here because only the desktop capability knows how to
    replay it.
    """

    __env_types__: ClassVar[tuple[str, ...]] = ("vm", "desktop", "desktop_vm")
    __raw_keys__: ClassVar[tuple[str, ...]] = (
        "snapshot",
        "vm_image",
        "os_type",
        "config",
        "related_apps",
    )

    setup_type: Literal["vm"] = "vm"

    snapshot: str | None = None  # named base snapshot to revert to, e.g. "chrome"
    vm_image: str | None = None  # disk image / .vmx path when not snapshot-based
    os_type: str | None = None  # "Ubuntu", "Windows", ...
    config: list[dict[str, Any]] = Field(default_factory=list)  # setup action list
    related_apps: list[str] = Field(default_factory=list)

    def to_context(self) -> dict[str, Any]:
        return _prune(
            {
                "snapshot": self.snapshot,
                "vm_image": self.vm_image,
                "os_type": self.os_type,
                "config": self.config,
                "related_apps": self.related_apps,
            }
        )


#: Every registered setup variant, in discriminator order.
TASK_SETUP_VARIANTS: Final[tuple[type[SetupBase], ...]] = (
    BrowserSetup,
    DockerSetup,
    VMSetup,
)


# Discriminated union of all setup variants.
TaskSetup = Annotated[
    BrowserSetup | DockerSetup | VMSetup,
    Field(discriminator="setup_type"),
]


def setup_class_for_env(env_type: str) -> type[SetupBase]:
    """Return the ``TaskSetup`` variant serving ``env_type``.

    Raises ``ValueError`` — loudly, at load time — when no variant claims
    the environment, rather than silently defaulting to a browser setup.
    """
    for variant in TASK_SETUP_VARIANTS:
        if env_type in variant.__env_types__:
            return variant
    known = sorted(e for v in TASK_SETUP_VARIANTS for e in v.__env_types__)
    raise ValueError(
        f"No TaskSetup variant declares env_type={env_type!r} "
        f"(known: {', '.join(known)}). Add a SetupBase subclass to "
        f"spectragent/schemas/task.py and list it in TASK_SETUP_VARIANTS."
    )


# ── Reference / scoring ───────────────────────────────────────────────


class ScoringItem(BaseModel):
    """Individual scoring criterion in a rubric."""

    name: str
    score: int
    description: str = ""


class ScoringRubric(BaseModel):
    """Structured scoring criteria for rubric-based evaluation."""

    total: int = 100
    threshold: int = 60
    items: list[ScoringItem] = Field(default_factory=list)


class TaskReference(BaseModel):
    """Rich reference answer for evaluation.

    Supports rubric-based (steps + scoring), simple answer, and encrypted
    modes (e.g. BrowseComp).
    """

    steps: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    scoring: ScoringRubric | None = None
    # Encrypted benchmark support (BrowseComp)
    encrypted_question: str | None = None
    encrypted_answer: str | None = None


# ── Top-level task definition ─────────────────────────────────────────


class TaskDefinition(BaseModel):
    """Generic benchmark task definition.

    A ``TaskDefinition`` describes a single benchmark task and is
    independent of the environment type. Per-environment data lives
    inside ``setup`` (one of the ``TaskSetup`` variants) or in the
    open-ended ``metadata`` dict.

    Loaders are responsible for normalizing raw benchmark data into this
    shape via their ``_normalize_raw()`` method.
    """

    model_config = ConfigDict(extra="allow")

    # ── Identity ──
    task_id: str
    benchmark: str
    env_type: str = "browser"  # "browser" | "docker" | "desktop" | ...

    # ── Instruction ──
    task_text: str = ""
    language: str = "en"

    # ── Per-env setup (discriminated union) ──
    setup: TaskSetup = Field(default_factory=BrowserSetup)

    # ── Evaluation ──
    expected_answer: str | None = None  # simple string answer (exact-match scenarios)
    reference: TaskReference | None = None  # rich reference (rubric / encrypted)

    # ── Classification ──
    difficulty: str | None = None  # "easy", "medium", "hard"
    domain: str | None = None  # "ecommerce", "social_lifestyle", ...
    tags: list[str] = Field(default_factory=list)  # free-form: ["L2", "multi_step", "T1"]

    # ── Catch-all for truly benchmark-specific fields ──
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskContext(BaseModel):
    """What one task hands to three different consumers, kept apart.

    Returned by ``TaskLoader.get_task_context(task_id)`` and split by the
    caller (``BenchmarkRunner.run_task``) across ``AgentRuntime.execute``
    (``docs/architecture_design.md`` §4.2, §4.10):

    - ``env`` → ``execute(env_context=...)``: the environment setup context
      (``start_url``, ``replay_archive`` …), routed to the capabilities by
      their ``setup()`` signatures and recorded as ``TraceConfig.task_setup``;
    - ``loop`` → ``execute(loop_inputs=...)``: the inputs the run's Loop
      declares in ``__loop_inputs__`` (``checkpoint`` for ``BranchLoop``),
      checked at ``prepare()``;
    - ``branch`` → ``execute(branch=...)``: where a branch task came from,
      recorded as ``TraceConfig.branch`` and read by nothing else.

    None of them leaks into another: the environment never sees a
    checkpoint, the Loop never sees the setup context.
    """

    env: dict[str, Any] = Field(default_factory=dict)
    loop: dict[str, Any] = Field(default_factory=dict)
    branch: BranchProvenance | None = None


#: The environment setup context key naming the recorded run a replay reads —
#: a run directory (``docs/architecture_design.md`` §4.10, §4.14).
REPLAY_ARCHIVE_KEY: Final = "replay_archive"


def resolve_env_context_paths(env: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """*env* with its ``replay_archive`` made absolute against the current directory.

    ``replay_archive`` is the one setup context key that names a filesystem
    path. A relative one is relative to the directory the process was
    launched from (nothing in af changes the working directory, so that is
    the current one), with ``~`` expanded first as for every other path
    knob. ``AgentRuntime.execute`` resolves it here for every caller alike —
    the CLI, a task loader, a Python caller — before the context is recorded
    as ``TraceConfig.task_setup`` and before setup opens it
    (``docs/architecture_design.md`` §4.10). A capability-scoped block
    (``{"browser": {...}}``) is resolved the same way.

    Returns:
        A new mapping (*env* is never mutated), or ``None`` for ``None``. A
        value that is not a non-empty path is left as it is, for setup to
        refuse by name.
    """
    if env is None:
        return None
    resolved: dict[str, Any] = {}
    for key, value in env.items():
        if key == REPLAY_ARCHIVE_KEY:
            value = _absolute_path(value)
        elif isinstance(value, Mapping) and REPLAY_ARCHIVE_KEY in value:
            value = {**value, REPLAY_ARCHIVE_KEY: _absolute_path(value[REPLAY_ARCHIVE_KEY])}
        resolved[key] = value
    return resolved


def _absolute_path(value: Any) -> Any:
    if isinstance(value, str | os.PathLike) and os.fspath(value):
        return str(Path(value).expanduser().resolve())
    return value


__all__ = [
    "SetupBase",
    "BrowserSetup",
    "DockerSetup",
    "VMSetup",
    "TaskSetup",
    "TASK_SETUP_VARIANTS",
    "setup_class_for_env",
    "ScoringItem",
    "ScoringRubric",
    "TaskReference",
    "TaskDefinition",
    "TaskContext",
    "REPLAY_ARCHIVE_KEY",
    "resolve_env_context_paths",
]
