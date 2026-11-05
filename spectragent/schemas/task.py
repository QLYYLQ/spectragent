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

It may also say where a task starts (:meth:`SetupBase.start_point`): the
framework never reads a capability's own fields to know it.

The setup classes live in their capability packages, each named
``TASK_SETUP`` in that package's ``declarations.py``; this module imports
no capability. ``TaskDefinition.setup`` dispatches on ``setup_type`` among
the registered setups, and ``core/registries.py::setup_class_for_env``
finds the one an env type selects.

See docs/architecture_design.md §4.10 and docs/execution_dag.md M15.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Final, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SerializeAsAny, model_validator

from spectragent.schemas.benchmark_spec import BenchmarkSpec
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

    model_config = ConfigDict(extra="allow")

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

    @classmethod
    def start_point(cls, context: Mapping[str, Any]) -> str | None:
        """Where a task with this environment setup *context* starts, as this capability names it.

        ``docs/architecture_design.md`` §4.10 "任务的起始点归 capability": the
        capability says where its tasks take place (the browser: the start
        URL's site), so the framework never reads a capability's own fields.
        What groups runs by where they took place reads it: the training
        export's ``split.by: site`` and ``exclude_sites``, decontamination,
        the flywheel's sites and the viewer's comparisons. *context* is the
        context :meth:`to_context` gives, or the one a trace recorded
        (``TraceConfig.task_setup``). ``None``: this capability names no start
        point, or the context carries none.
        """
        return None


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
            f"{registered}). Call spectragent.core.registries.capability_declarations() "
            f"before validating a task: it registers every capability's task setup."
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
    """What one task hands to its different consumers, kept apart.

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
      recorded as ``TraceConfig.branch`` and read by nothing else;
    - ``benchmark``, a branch task's only: the benchmark it belongs to, its
      source run's, as the source trace recorded how it was selected
      (``TraceConfig.benchmark_spec``; ``None`` when the source belonged to
      none). The caller composes the task's run with that benchmark beside
      the branch sweep's task source, so the branch is set up, judged and
      configured as its source was (§4.10.1 "分叉 sweep 是一种任务来源"),
      and hands it to ``execute(benchmark_spec=...)``. Every other task
      belongs to the benchmark whose task source gave it.

    None of them leaks into another: the environment never sees a
    checkpoint, the Loop never sees the setup context.
    """

    env: dict[str, Any] = Field(default_factory=dict)
    loop: dict[str, Any] = Field(default_factory=dict)
    branch: BranchProvenance | None = None
    benchmark: BenchmarkSpec | None = None

    @model_validator(mode="after")
    def _benchmark_of_a_branch_task(self) -> TaskContext:
        if self.benchmark is not None and self.branch is None:
            raise ValueError(
                "benchmark names the benchmark a branch task inherits from its source run; a "
                "task with no branch belongs to the benchmark whose task source gave it"
            )
        return self


class SetupTaskText(BaseModel):
    """The run's task text, as a capability's setup completed it (``docs/architecture_design.md`` §4.10).

    Some benchmarks make a task's text in the environment as it starts:
    AndroidWorld's MiniWoB++ pages write their instruction then. The loader's
    ``task_text`` is the part known before, and the capability that completes
    it at setup hands back the whole text, exactly as the agent gets it. The
    environment adds the capability's id (``Environment.task_text_from_setup``)
    and the trace records it as ``task_text.json`` beside the loader's text
    (§5.3).

    Attributes:
        capability_id: The capability whose setup completed the text.
        text: The run's task text; never blank.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    capability_id: str
    text: str

    @model_validator(mode="after")
    def _the_text_says_something(self) -> SetupTaskText:
        if not self.text.strip():
            raise ValueError(
                f"capability {self.capability_id!r} completed the run's task text with "
                f"{self.text!r}: its setup made no text"
            )
        return self


#: The environment setup context key naming the recorded run a replay reads —
#: a run directory (``docs/architecture_design.md`` §4.10, §4.14).
REPLAY_ARCHIVE_KEY: Final = "replay_archive"


@dataclass(frozen=True)
class StartFlag:
    """A single-task start flag a capability declares in its ``declarations.py`` (``START_FLAGS``).

    ``docs/architecture_design.md`` §4.9 "起始参数": ``spectragent run`` adds
    every capability's flags, listed by capability, and refuses one whose
    capability the run's configuration does not name. Its value is the task's
    environment setup context entry *key* (§4.10), routed to the capability's
    ``setup()`` like a loader task's.

    Attributes:
        flag: The command-line flag, ``--start-url``.
        key: The environment setup context key it fills, ``start_url``.
        help: What it sets, for ``--help`` and tab completion.
    """

    flag: str
    key: str
    help: str


__all__ = [
    "SetupBase",
    "TaskSetup",
    "TaskSetupRegistrar",
    "ScoringItem",
    "ScoringRubric",
    "TaskReference",
    "TaskDefinition",
    "TaskContext",
    "SetupTaskText",
    "REPLAY_ARCHIVE_KEY",
    "StartFlag",
]
