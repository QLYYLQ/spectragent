"""Trace data models — the manifest, the records it indexes, the config snapshot.

These Pydantic models define the on-disk format of a run directory
(``docs/architecture_design.md`` §5.3). The manifest is the commit point
and the reader's only directory; everything else is a record it points at.

The file *names* and the bytes that go into them are decided in one place,
:mod:`spectragent.core.trace.encoding`; this module only says what each
record is.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The one ``core.archive`` module the trace layer imports: the shared
# ``RedactionLedger`` that ``TaskSetupRecord`` carries. Archive *files*
# cross into the trace layer duck-typed, so the WAL's interface stays
# ``TraceFile``.
from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.llm.messages import BaseMessage, ToolCall
from spectragent.core.llm.views import ChatInvokeUsage
from spectragent.schemas._types import UTCDatetime

#: The on-disk layout this build writes and the newest it reads.
#:
#: 1 is every trace written before the manifest carried the field: a flat
#: fragment file list, paired ``NNN_input.json`` / ``NNN_output.json``
#: message files, and a fragment-capture failure written under the
#: fragment's own name with nothing to tell it from the fragment. 2 is the
#: layout of §5.3: a per-step fragment index with an explicit failure
#: marker, one record per LLM call, and an extra-file index.
TRACE_FORMAT_VERSION = 2


class RunStatus(StrEnum):
    """Status of a trace run."""

    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class FragmentFiles(BaseModel):
    """Where one fragment of one step landed.

    ``failed`` is the reason this index exists rather than a flat file
    list. A :class:`~spectragent.schemas.observation.failure.FragmentFailure`
    has no discriminator in its JSON, and every field of ``ScreenshotObs`` /
    ``DOMObs`` / ``BrowserMetaObs`` has a default, so the same bytes
    validate silently as any of them: read back by key, a capture that
    failed would become an empty fragment — the fabrication the failure
    record exists to refuse. The flag is written by the one component that
    knew which it was.
    """

    file: str  # observations/NNN_<key>.json
    artifact: str | None = None  # observations/NNN_<key><suffix>
    failed: bool = False  # True: ``file`` holds a FragmentFailure, not the fragment


class StepManifestEntry(BaseModel):
    """One committed step: its record, and every file it consists of."""

    model_config = ConfigDict(extra="allow")

    step: int
    step_file: str  # steps/NNN.json
    #: Fully-qualified fragment key → its files. A key that is absent was
    #: not recorded for this step (not active in this run, or the
    #: observation carried no such fragment).
    fragments: dict[str, FragmentFiles] = Field(default_factory=dict)
    #: Every LLM call of the step, in start order — parse retries,
    #: refused structured replies, truncations included.
    llm_calls: list[str] = Field(default_factory=list)
    #: ``steps/NNN.extra.json`` when the step committed extra files. The
    #: files are listed there, not here: the manifest is rewritten whole on
    #: every commit and read eagerly by every reader, so inlining a step's
    #: hundred evidence files would make it grow quadratically with the run.
    extra_index: str | None = None


class TrailingEntry(BaseModel):
    """What one ``commit_trailing`` call committed.

    The LLM calls of a step the Loop never surrendered (timeout, external
    cancellation, crash) and files flushed at the end of a run. Not a step:
    claiming one would put an action-less phantom in the manifest.
    """

    llm_calls: list[str] = Field(default_factory=list)
    extra_index: str | None = None  # steps/trailing_KK.extra.json


class TraceManifest(BaseModel):
    """Atomic commit point for WAL persistence.

    Written via write-then-rename to guarantee atomicity. A file it does
    not list was not committed, whatever is on disk.

    ``extra="allow"`` so a format-1 manifest — different field names —
    still parses: its identity, status and step files stay readable, and
    the accessors whose on-disk shape changed refuse it by
    ``format_version`` instead of failing on a field name.
    """

    model_config = ConfigDict(extra="allow")

    #: Absent on every manifest written before the field existed, which is
    #: exactly what makes the default 1 right. ``init_run`` writes
    #: :data:`TRACE_FORMAT_VERSION`.
    format_version: int = 1
    run_id: str
    last_step_completed: int = -1  # -1 means no step completed yet
    status: RunStatus = RunStatus.RUNNING
    created_at: UTCDatetime
    updated_at: UTCDatetime
    config_hash: str = ""
    steps: list[StepManifestEntry] = Field(default_factory=list)
    trailing: list[TrailingEntry] = Field(default_factory=list)
    #: sha256 of every committed output schema
    #: (``messages/output_formats/<sha256>.json``), each listed once.
    output_formats: list[str] = Field(default_factory=list)


class ExtraFileRecord(BaseModel):
    """One file a commit carried beside the step's own records."""

    path: str  # relative to the run directory
    size: int
    sha256: str
    #: True when ``path`` is derived from ``sha256`` — the file is shared by
    #: every commit that references it and written once per run.
    content_addressed: bool = False
    #: Whatever the producer attached. An environment-evidence file carries
    #: ``meta["archive"]``, an :class:`ArchiveMeta` — the one mark that
    #: makes an extra file an archive file for the reader.
    meta: dict[str, Any] = Field(default_factory=dict)


class ExtraFileIndex(BaseModel):
    """``steps/NNN.extra.json`` / ``steps/trailing_KK.extra.json``."""

    files: list[ExtraFileRecord]


class ArchiveMeta(BaseModel):
    """Where one environment-evidence file came from — ``ExtraFileRecord.meta["archive"]``.

    Written by ``TraceWriter`` through
    :func:`~spectragent.core.trace.encoding.encode_archive_file` from the
    ``ArchiveFile`` a capability handed over, so the index — not the path —
    is what says which archiver produced a file, when, and which version of
    a run record it is (``docs/architecture_design.md`` §5.3).

    Attributes:
        archiver_id: Fully-qualified archiver key. A run record may carry
            the bare capability id instead: it belongs to the capability.
        name: The archiver's own file name — one path segment.
        media_type: ``type/subtype`` of the bytes.
        scope: ``step`` (attributed to the commit that took it) or ``run``
            (a capability's run record).
        captured_at: Wall-clock Unix time of the capture, comparable with
            ``StepRecord.timestamp`` and ``RecordedLLMCall.started_at``.
        cycle: The environment's observation cycle, when the evidence
            belongs to one — a counter of the environment's, not a step.
        ids: Capability-scoped identifiers (session / frame / loader …).
        run_version: Run records only: which version, from 0. A committed
            path is immutable, so a changed re-emission is a new version.
    """

    archiver_id: str
    name: str
    media_type: str
    scope: Literal["step", "run"]
    captured_at: float
    cycle: int | None = None
    ids: dict[str, str] = Field(default_factory=dict)
    run_version: int | None = None

    @model_validator(mode="after")
    def _a_version_exactly_when_a_run_record(self) -> ArchiveMeta:
        # ``archive_run_file`` orders run records by version: a run record
        # without one, or a step file claiming one, is not what was written.
        if self.scope == "run" and (self.run_version is None or self.run_version < 0):
            raise ValueError(
                f"a run record carries its version (an int >= 0); got {self.run_version!r}"
            )
        if self.scope == "step" and self.run_version is not None:
            raise ValueError(
                f"only a run record carries a version; this step file says {self.run_version!r}"
            )
        return self


class ArchiveRecord(BaseModel):
    """One committed environment-evidence file, as ``RunTrace`` hands it back.

    An :class:`ExtraFileRecord` flattened together with its
    :class:`ArchiveMeta`. The bytes are ``RunTrace.read_extra(record.path)``,
    checked against ``size`` and ``sha256`` like any extra file.
    """

    path: str
    size: int
    sha256: str
    content_addressed: bool
    archiver_id: str
    name: str
    media_type: str
    scope: Literal["step", "run"]
    captured_at: float
    cycle: int | None = None
    ids: dict[str, str] = Field(default_factory=dict)
    run_version: int | None = None

    @property
    def capability_id(self) -> str:
        """The capability that produced the file — the key's first segment."""
        return self.archiver_id.partition(".")[0]


class OutputFormatRef(BaseModel):
    """The reply model a structured call asked for, by content address."""

    name: str  # the reply model's class name
    sha256: str  # → messages/output_formats/<sha256>.json
    #: The schema the *caller* requested — ``model_json_schema()`` of the
    #: class passed as ``output_format``. Not what the provider compiled
    #: and sent (strictness, Gemini's clean-up, union expansion all differ
    #: per provider, and ``dont_force_structured_output`` sends none), so it
    #: is no evidence that a schema was honoured.
    source: Literal["requested"] = "requested"


class RecordedCompletion(BaseModel):
    """What a call that returned handed back — ``ChatInvokeCompletion``'s fields."""

    #: The reply: ``str`` for text, the reply model's JSON form for a
    #: structured call. Deliberately not rebuilt into the reply model: the
    #: class is the agent's, and the reader may not be able to import it.
    completion: Any
    thinking: str | None = None
    redacted_thinking: str | None = None
    usage: ChatInvokeUsage | None = None
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class CallFailure(BaseModel):
    """Why a call produced no completion — only what the exception carried.

    Not a stand-in completion: a reader must be able to tell a reply the
    model wrote from one the framework invented, and a completion-shaped
    object with an error inside reads as the former. A dead socket leaves
    ``error`` and ``detail`` and nothing else.
    """

    error: str  # exception class name
    detail: str  # str(exc)
    #: The reply as the validator saw it (``StructuredOutputValidationError``).
    raw_output: str | None = None
    #: What a reply cut off at the token ceiling had generated
    #: (``ModelOutputTruncatedError``). Not ``raw_output``: nothing validated it.
    partial_output: str | None = None
    stop_reason: str | None = None
    #: A failed call is billed all the same.
    usage: ChatInvokeUsage | None = None


#: A prompt message, tagged by ``role`` so a malformed one fails naming the
#: role it claimed rather than four union branches.
RecordedMessage = Annotated[BaseMessage, Field(discriminator="role")]


class RecordedLLMCall(BaseModel):
    """``messages/NNN_call_KK.json`` — one LLM call, whatever its outcome."""

    #: The step the Loop was on. For a trailing call, the step it never
    #: surrendered (``last_step_completed + 1``).
    step: int
    #: Start order within the step, from 0.
    call: int
    status: Literal["completed", "failed", "cancelled"]
    #: Wall-clock Unix time, comparable with ``StepRecord.timestamp`` and a
    #: fragment's ``captured_at``.
    started_at: float
    #: From the start to the return, the raise or the cancellation.
    latency_ms: int
    #: The whole prompt, as the caller handed it to ``ainvoke``.
    messages: list[RecordedMessage]
    output_format: OutputFormatRef | None = None  # None: an unstructured call
    completion: RecordedCompletion | None = None  # completed calls only
    failure: CallFailure | None = None  # failed and cancelled calls


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
    #: The archivers this run bound — the third provides axis, fully
    #: qualified. Empty when no hook asked to archive anything: then the
    #: trace has no ``env/`` evidence, and a reader need not look.
    active_archivers: list[str] = Field(default_factory=list)
    #: Archiver key → the ``capability_id`` that provided it. Recorded
    #: verbatim because every value is a registry key, never a name a user
    #: chose.
    archive_bindings: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_results(
        cls, observation: Any = None, action: Any = None, archive: Any = None
    ) -> TraceNegotiation | None:
        """Build from the negotiation results ``AgentRuntime`` holds.

        Args:
            observation: An
                :class:`~spectragent.core.negotiation.observation.ObservationNegotiationResult`,
                or ``None`` for a run that never negotiated one.
            action: An
                :class:`~spectragent.core.negotiation.action.ActionNegotiationResult`,
                or ``None``.
            archive: An
                :class:`~spectragent.core.negotiation.archive.ArchiveNegotiationResult`,
                or ``None`` for a run whose hooks archive nothing (the
                runtime negotiates the archive axis only when one does).

        Returns:
            ``None`` when no result exists at all, so that
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
        if observation is None and action is None and archive is None:
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
        if archive is not None:
            fields.update(
                active_archivers=sorted(archive.active_archivers),
                archive_bindings=dict(sorted(archive.archive_bindings.items())),
            )
        return cls(**fields)


class TaskSetupRecord(BaseModel):
    """The environment setup context a task was started with — redacted.

    What ``AgentRuntime.execute`` handed ``setup_environment`` (``start_url``
    and the like). A branch task (M31) re-creates its source environment
    from this, without the source benchmark's data.

    Redacted **by value**, not by declaration: it is free-form per-task data
    with no field declarations, so the config redaction's deny-by-default
    would withhold every value and leave nothing to rebuild from. The shared
    rules of :mod:`spectragent.core.archive.privacy` apply instead
    (credential-shaped keys, URL userinfo, credential query and fragment
    parameters), and the ledger says where they fired
    (``docs/architecture_design.md`` §5.6).

    Attributes:
        context: The context's JSON snapshot after redaction. A context
            that does not snapshot to a JSON object is stored as
            ``{"value": <snapshot>}``.
        redactions: Every replacement made. Empty when nothing matched — or
            when the trace was written with ``record_secrets``
            (``TraceConfig.secrets_recorded`` says which).
    """

    context: dict[str, Any]
    redactions: RedactionLedger


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

    **Provenance** (:attr:`secrets_recorded`, :attr:`env_evidence_recorded`,
    :attr:`env_evidence_privacy`) says how much of the above is real and
    what else the directory holds. A trace recorded with ``record_secrets``
    on contains credentials verbatim and must not be shared; one without
    it contains ``"<omitted>"`` wherever a field was set but not marked
    trace-safe. A trace whose run bound archivers may also carry
    environment evidence under ``env/`` — the state of the world the agent
    acted in, privacy-marked on its own terms.

    One per-task fact sits beside the layers: :attr:`task_setup`, the
    environment setup context the task started from, redacted by value.

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
    #: Did this run bind archivers — may the directory carry environment
    #: evidence under ``env/``? Answers the second question a reader asks
    #: before sharing a trace. Provenance, so not part of the digest.
    env_evidence_recorded: bool = False
    #: The sorted union of the bound archivers' declared
    #: ``privacy_markers`` (``core/archive/privacy.py``). What the
    #: archivers *declared*, not proof the bytes are clean — the framework
    #: never reads evidence. Provenance, so not part of the digest.
    env_evidence_privacy: list[str] = Field(default_factory=list)
    #: How this task's environment was started — the setup context
    #: ``execute()`` routed to ``setup_environment``, redacted by value —
    #: or ``None`` when it received none. Per task, so not part of the
    #: digest: two tasks differing only in ``start_url`` share a
    #: ``config_hash``.
    task_setup: TaskSetupRecord | None = None

    def configuration_digest(self, *, resolved: dict[str, Any] | None = None) -> str:
        """Stable hash of *how this run was configured*.

        "Same hash means same configuration" only becomes true once the
        hash covers the configuration and nothing else. Hashing the whole
        snapshot — as the WAL did — folds in ``task`` and ``task_id``, so
        every task in one sweep got a different ``config_hash`` and the
        field answered no question anybody had.

        Included: the component identity, the benchmark and subset the
        run was pointed at, the resolved config and the negotiation
        result — the archive binding (``active_archivers`` /
        ``archive_bindings``) moves it through the latter, with no special
        case. Excluded: ``task`` / ``task_id`` (which task, not which
        configuration), ``extra`` and ``task_setup`` (per-task data) and the
        provenance fields ``env_evidence_recorded`` /
        ``env_evidence_privacy``.

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

        One cost, stated: the archive axis added two fields to
        :class:`TraceNegotiation`, so a trace written since then hashes
        differently from a pre-archive trace of the same configuration.
        Compare manifests written by one framework version.
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
                self.negotiation.model_dump(mode="json") if self.negotiation is not None else None
            ),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]
