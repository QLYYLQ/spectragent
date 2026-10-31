"""Generalized task schema for benchmark data loaders.

``TaskDefinition`` is the generic, environment-agnostic task model used
by every benchmark data loader. Per-environment requirements (a browser
``start_url``, a container image, a VM snapshot) live in its ``setup``, an
instance of the task setup class a capability package registers; truly
benchmark-specific fields go into ``metadata``.

A task setup subclasses :class:`SetupBase`, which registers it in
:data:`TaskSetupRegistrar` under its ``__registry_key__`` (its
``setup_type``), and declares three things:

``__env_types__``   which ``TaskDefinition.env_type`` values select it —
                    this is what lets a loader derive its setup class
                    from ``self.env_type`` instead of hardcoding one.
``__raw_keys__``    top-level raw-record keys a loader grafts onto the
                    setup instead of leaving them as untyped extras
                    (``start_url`` for browser).
``to_context()``    the per-domain runtime context forwarded to
                    ``Capability.setup()``.

The setup classes live in their capability packages, each named
``TASK_SETUP`` in that package's ``declarations.py``; this module imports
no capability. ``TaskDefinition.setup`` dispatches on ``setup_type`` among
the registered setups, and ``core/registries.py::setup_class_for_env``
finds the one an env type selects.

See docs/architecture_design.md §4.10 and docs/execution_dag.md M15.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Final, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SerializeAsAny, model_validator

from spectragent.schemas.restoration import BranchProvenance

# ── Task setups ───────────────────────────────────────────────────────


@runtime_checkable
class TaskSetup(Protocol):
    """A task's environment requirements, as one capability models them."""

    def to_context(self) -> dict[str, Any]:
        """Per-domain runtime context handed to ``Capability.setup()``."""
        ...


TaskSetupRegistrar = create_registrar(
    "task_setup",
    TaskSetup,
    discriminator_field="setup_type",
)


class SetupBase(TaskSetupRegistrar.bridge(BaseModel)):  # type: ignore[misc]
    """Common contract of every task setup; subclassing it registers the setup.

    ``extra="allow"`` mirrors ``TaskDefinition``: a nested ``setup`` block
    carrying keys this setup does not model is preserved rather than
    silently dropped.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    __abstract__ = True
    #: The ``setup_type`` this setup registers under.
    __registry_key__: ClassVar[str]

    #: ``TaskDefinition.env_type`` values that select this setup.
    __env_types__: ClassVar[tuple[str, ...]] = ()

    #: Top-level raw-record keys a loader grafts onto this setup.
    __raw_keys__: ClassVar[tuple[str, ...]] = ()

    #: The registry key, carried in the data so that a dumped task validates
    #: back to this class. Filled in from ``__registry_key__``; any other
    #: value is refused.
    setup_type: str = ""

    @model_validator(mode="before")
    @classmethod
    def _setup_type_is_the_registry_key(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        key = cls.__registry_key__
        declared = data.get("setup_type", key)
        if declared != key:
            raise ValueError(
                f"setup_type {declared!r} does not name {cls.__name__}, whose setup_type is {key!r}"
            )
        return {**data, "setup_type": key}

    def to_context(self) -> dict[str, Any]:
        """Per-domain runtime context handed to ``Capability.setup()``.

        Each setup decides what its capability needs to know; nothing
        outside its class should introspect the setup type.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement to_context()")


def _registered_setup(value: object) -> object:
    """Validate a ``setup`` mapping as the registered setup its ``setup_type`` names."""
    if not isinstance(value, Mapping):
        return value
    setup_type = value.get("setup_type")
    setup = TaskSetupRegistrar.get_or_none(setup_type) if isinstance(setup_type, str) else None
    if setup is None or not issubclass(setup, SetupBase):
        registered = ", ".join(sorted(TaskSetupRegistrar.keys())) or "none"
        raise ValueError(
            f"setup_type {setup_type!r} names no registered task setup (registered: "
            f"{registered}). A capability package registers its task setup and names it "
            f"TASK_SETUP in its declarations.py."
        )
    return setup.model_validate(value)


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
    inside ``setup`` (the registered task setup its ``setup_type`` names,
    or ``None`` when the task has none) or in the open-ended ``metadata``
    dict.

    Loaders are responsible for normalizing raw benchmark data into this
    shape via their ``normalize_raw()`` method.
    """

    model_config = ConfigDict(extra="allow")

    # ── Identity ──
    task_id: str
    benchmark: str
    env_type: str = "browser"  # "browser" | "docker" | "desktop" | ...

    # ── Instruction ──
    task_text: str = ""
    language: str = "en"

    # ── Per-env setup: the registered setup class its ``setup_type`` names ──
    # ``SerializeAsAny`` dumps the setup's own fields, so a dump validates back.
    setup: Annotated[SerializeAsAny[SetupBase] | None, BeforeValidator(_registered_setup)] = None

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


__all__ = [
    "SetupBase",
    "TaskSetup",
    "TaskSetupRegistrar",
    "ScoringItem",
    "ScoringRubric",
    "TaskReference",
    "TaskDefinition",
    "TaskContext",
    "REPLAY_ARCHIVE_KEY",
]
