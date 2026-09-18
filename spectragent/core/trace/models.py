"""Trace data models — manifest, config snapshot, and run status.

These Pydantic models define the on-disk format for WAL persistence.
The manifest is the atomic commit point; config is a frozen snapshot
of the run's configuration.

See docs/execution_dag.md M10 for design details.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas._types import UTCDatetime


class RunStatus(StrEnum):
    """Status of a trace run."""

    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class StepManifestEntry(BaseModel):
    """Per-step metadata pointing to file paths within the trace directory."""

    model_config = ConfigDict(extra="allow")

    step: int
    step_file: str  # relative path, e.g. "steps/000.json"
    observation_files: list[str] = Field(default_factory=list)
    # Canonical (first) LLM call of the step — kept as scalars so simple
    # readers stay simple.
    message_input_file: str | None = None
    message_output_file: str | None = None
    # Every LLM call of the step, in order. A loop that reflects or
    # retries issues more than one call per step; those extra calls used
    # to overwrite each other on disk.
    message_files: list[str] = Field(default_factory=list)


class TraceManifest(BaseModel):
    """Atomic commit point for WAL persistence.

    Written via write-then-rename to guarantee atomicity.
    Recovery reads this to determine the last complete step.
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    last_step_completed: int = -1  # -1 means no step completed yet
    status: RunStatus = RunStatus.RUNNING
    created_at: UTCDatetime
    updated_at: UTCDatetime
    config_hash: str = ""
    steps: list[StepManifestEntry] = Field(default_factory=list)
    # LLM calls that belong to a step the Loop never surrendered — a run
    # killed by the timeout or an exception between the completion and
    # ``record_step``. They are not a completed step, so they get no
    # ``StepManifestEntry``; they are listed here so the last exchange
    # before the run died is still findable.
    unfinished_message_files: list[str] = Field(default_factory=list)


class TraceNegotiation(BaseModel):
    """What negotiation actually bound, for this run.

    The composed config says which components were assembled; this says
    what they agreed on once assembled — which observation fragments are
    live, which of them the run cannot continue without, which actions
    the model was allowed to emit, and what each capability-relative
    name in a ``__wiring__`` declaration resolved to in *this*
    environment.

    Every key here is **fully qualified** (``browser.dom``,
    ``tool_sandbox.call_tool``), because that is the only form that says
    which capability produced or executed something. The bindings carry
    the other half: ``{"click": "browser.click"}`` is the run's answer to
    "what did ``click`` mean here", and without it a trace cannot be
    replayed against a differently-composed environment.

    An evaluator reading a ``RunTrace`` has the files but not the object
    graph, so this is the only place that answer survives the run.
    """

    model_config = ConfigDict(extra="allow")

    active_fragments: list[str] = Field(default_factory=list)
    #: Fragments the agent or loop declared *required*. The difference
    #: between "I cannot work without a DOM" and "use a screenshot if
    #: there is one" decides whether a capture failure ended the run or
    #: was recorded as a ``FragmentFailure`` — so a trace without it
    #: cannot explain its own degraded steps.
    required_fragments: list[str] = Field(default_factory=list)
    env_sourced_fragments: list[str] = Field(default_factory=list)
    loop_sourced_fragments: list[str] = Field(default_factory=list)
    fragment_bindings: dict[str, str] = Field(default_factory=dict)
    active_actions: list[str] = Field(default_factory=list)
    action_bindings: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_results(
        cls, observation: Any = None, action: Any = None
    ) -> TraceNegotiation | None:
        """Build from the two negotiation results ``AgentRuntime`` holds.

        Args:
            observation: An
                :class:`~spectragent.core.negotiation.observation.ObservationNegotiationResult`,
                or ``None`` for a run that never negotiated one.
            action: An
                :class:`~spectragent.core.negotiation.action.ActionNegotiationResult`,
                or ``None``.

        Returns:
            ``None`` when neither result exists, so that
            :attr:`TraceConfig.negotiation` can mean it. An empty
            instance would be a different claim — "negotiation ran and
            bound nothing", which is a real and diagnosable state (an
            environment whose capabilities provide none of what the agent
            asked for) — and the two must not read identically on disk.
            This is the same distinction redaction draws between ``null``
            and ``"<omitted>"``.

        Typed as ``Any`` to keep ``core/trace`` free of an import back
        into ``core/negotiation`` — the trace layer is downstream of
        everything and must stay importable on its own. The attributes
        read below are the results' documented fields, not a duck-typed
        guess: a result missing one is a bug in negotiation, and an
        ``AttributeError`` here is the right way to find out.

        Everything is sorted. These are ``frozenset``s upstream, whose
        iteration order varies between processes — an unsorted dump would
        make ``config_hash`` differ for two identical runs.
        """
        if observation is None and action is None:
            return None
        fields: dict[str, Any] = {}
        if observation is not None:
            fields.update(
                active_fragments=sorted(observation.active_fragments),
                required_fragments=sorted(observation.required_fragments),
                env_sourced_fragments=sorted(observation.env_sourced),
                loop_sourced_fragments=sorted(observation.loop_sourced),
                fragment_bindings=dict(sorted(observation.fragment_bindings.items())),
            )
        if action is not None:
            fields.update(
                active_actions=sorted(action.active_actions),
                action_bindings=dict(sorted(action.action_bindings.items())),
            )
        return cls(**fields)


class TraceConfig(BaseModel):
    """Frozen configuration snapshot for a run.

    Three layers, and they answer different questions.

    **Identity** (``agent_id`` … ``task_id``) is what *joins* the trace
    back to the dataset it came from. ``(benchmark, subset, task_id)`` is
    the key an Evaluator needs to look up ground truth:
    ``EvalLoaderRegistrar.get(benchmark)(subset=subset)[task_id]``.
    Without all three, a trace directory is an orphan — you cannot even
    say which task it ran.

    **Configuration** (:attr:`resolved`, :attr:`negotiation`) is what the
    run was actually *set up as*. Identity alone cannot compare two runs:
    it says both used ``browser_use`` on ``react``, not that one ran in
    ``observation_mode: vision`` with a ``learned`` mouse trajectory
    against a JPEG screenshot budget and the other did not. Every
    cross-run comparison used to depend on an external harness keeping
    its own notes.

    **Provenance** (:attr:`secrets_recorded`) says how much of the above
    is real. A trace recorded with ``record_secrets`` on contains
    credentials verbatim and must not be shared; one without it contains
    ``"<omitted>"`` wherever a field was set but not marked trace-safe.

    Component identity appears twice by design and the two are not
    equals. The top-level :attr:`agent_id` / :attr:`loop_id` /
    :attr:`env_type` are resolved by ``AgentRuntime`` from the **live
    objects** it is driving, and they are the authority: they are the
    only ones that exist for a run assembled without the Composer, where
    :attr:`resolved` is ``{}``. The same names also appear inside
    :attr:`resolved` as conscribe's discriminators (``resolved["agent"]
    [0]["agent_id"]``) — those describe what the *config* asked for. They
    agree for every Composer-driven run; if they ever disagree, the
    top-level fields say what actually ran and the nested ones say what
    was requested, and the gap between them is the bug.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str = ""
    loop_id: str = ""
    env_type: str = ""
    llm_model: str = ""
    benchmark: str = ""
    subset: str = ""
    task: str = ""
    task_id: str = ""
    #: Caller-curated context, recorded verbatim. ``BenchmarkRunner`` puts
    #: the task's difficulty/domain/tags here — the axes you slice results
    #: by. **Not** a config surface and not redacted: whoever passes it
    #: chose every key in it. Real configuration belongs in
    #: :attr:`resolved`, which is.
    extra: dict[str, Any] = Field(default_factory=dict)
    #: The composed, validated configuration — defaults filled in, wired
    #: receptors inlined — passed through field-level redaction.
    #: ``"<omitted>"`` means "set, withheld"; ``null`` means "never
    #: configured". See :mod:`spectragent.core.trace.redaction`.
    resolved: dict[str, Any] = Field(default_factory=dict)
    #: What negotiation bound, or ``None`` when the run never negotiated.
    #: An *empty* :class:`TraceNegotiation` is the different, diagnosable
    #: claim that it ran and bound nothing.
    negotiation: TraceNegotiation | None = None
    #: True when this trace was written with redaction disabled. It is
    #: the one field a reader must check before sharing the directory.
    secrets_recorded: bool = False

    def configuration_digest(self, *, resolved: dict[str, Any] | None = None) -> str:
        """Stable hash of *how this run was configured*.

        "Same hash means same configuration" only becomes true once the
        hash covers the configuration and nothing else. Hashing the whole
        snapshot — as the WAL did — folds in ``task`` and ``task_id``, so
        every task in one sweep got a different ``config_hash`` and the
        field answered no question anybody had.

        Included: the component identity, the benchmark and subset the
        run was pointed at, the resolved config and the negotiation
        result. Excluded: ``task`` / ``task_id`` (which task, not which
        configuration) and ``extra`` (per-task caller metadata).

        Args:
            resolved: The **unredacted** config dump
                (``redact_config(cfg, record_secrets=True)``). Pass it —
                :class:`~spectragent.core.hooks.trace_writer.TraceWriter`
                does, always, whatever ``record_secrets`` is set to.

                Omitting it falls back to :attr:`self.resolved`, the
                *recorded* view, and that view cannot carry a sweep: it
                replaces every unmarked value with the same
                ``"<omitted>"`` string, so ``max_steps: 10`` and
                ``max_steps: 100`` — and every ``temperature``,
                ``viewport_width``, ``dom_max_elements`` — collapse into
                one hash. A digest that merges the arms of a sweep is
                worse than no digest, so the two fidelities are kept
                distinguishable: the payload records which one it used,
                and a reader-side digest therefore never equals the
                writer's, even by accident.

        The consequence worth stating: a reader of a finished trace
        **cannot recompute** the manifest's ``config_hash``, because the
        trace deliberately does not contain the values it covers. Readers
        compare two manifests' hashes; they do not verify them. Nothing
        in the repo does otherwise.

        And the price of full fidelity, stated plainly: the digest is a
        sha256 over material that includes credentials. It is a hash, not
        a disclosure, and recovering a value from it means already
        knowing every other value in the config — but it is not *nothing*
        for a low-entropy secret such as a proxy password. The answer to
        that is to rotate the credential, not to keep a hash that cannot
        tell two experiments apart.

        ``secrets_recorded`` does **not** move the digest: it changes
        what lands on disk, not what the run was configured as, so a
        debugging run and a shared run of the same YAML compare equal.
        """
        source = self.resolved if resolved is None else resolved
        payload = {
            "agent_id": self.agent_id,
            "loop_id": self.loop_id,
            "env_type": self.env_type,
            "llm_model": self.llm_model,
            "benchmark": self.benchmark,
            "subset": self.subset,
            # Which fidelity produced ``source``. Without this a trace
            # whose every field happens to be closed-set would hash the
            # same from both sides, and one unmarked field later it
            # would silently stop — a comparison that works until it
            # quietly does not.
            "resolved_fidelity": "recorded" if resolved is None else "full",
            "resolved": source,
            "negotiation": (
                self.negotiation.model_dump(mode="json")
                if self.negotiation is not None
                else None
            ),
        }
        blob = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]
