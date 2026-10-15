"""TraceWriter — RuntimeHook for full WAL-based trace persistence.

Two intercepts, one job each:

- ``around`` on ``llm.ainvoke`` is the **only** place an LLM call is
  recorded. The call is registered *before* ``proceed()`` — prompt,
  requested reply schema, start time — and its outcome filled in after:
  ``completed``, ``failed`` (an ``Exception``) or ``cancelled`` (a
  ``BaseException`` such as ``CancelledError``). ``after`` advice never
  sees a call that raised, and registering only in ``except Exception``
  would lose the prompt of exactly the call a timeout or a Ctrl-C cut off.
- ``after`` on ``loop.record_step`` is the **commit point**: the moment the
  Loop surrenders a finished :class:`StepRecord`. Everything the step
  consists of is known then — the observation the agent actually saw, every
  LLM call it made, the actions and what they returned — and it goes to
  disk in one ``WALWriter.commit_step``. Commit on ``llm.ainvoke`` would be
  the wrong event twice over: it fires before the actions exist, and once
  per call rather than once per step.

A run that ends between a call and the step (timeout, cancellation, crash)
never reaches that commit point, so run end commits whatever calls are still
registered as *trailing* calls instead of dropping them.

**The trace's own ledger.** The manifest says whether the trace is a
complete record of the run — ``writing``, ``complete``, or ``incomplete``
with the reason and the step it is complete through — never how the agent
ended: that is ``result.json``. A step that cannot be recorded is a hole: the
trace is incomplete from it on, and the run stops there unless the agent had
already called done on that step. Nothing that fails at close-out is raised
over the run's outcome (``docs/architecture_design.md`` §4.6, §5.4).

**Environment evidence.** With a non-empty ``archive`` this hook is the
run's archive consumer
(:class:`~spectragent.core.archive.protocols.ArchiveConsumer`): at each
commit point it pulls ``collect_archive()`` from the *raw* environment
``on_run_start`` handed it, and the batch rides that step's commit under
``env/``; once more at close-out (``docs/architecture_design.md`` §4.6,
§5.2–§5.4). A collection that fails is a hole like any other — the archive
was explicitly requested, and a silently incomplete one poisons replay and
branching, so nothing is collected or committed as if it were whole after
it. With an empty ``archive`` none of that exists: no environment reference
is kept and no archive method is ever called.

Registered via conscribe as hook_id="trace_writer". See
``docs/architecture_design.md`` §4.6 and §5.4.
"""

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, ClassVar, NamedTuple

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.archive import (
    ArchiveBatch,
    ArchiveContractError,
    ArchiveFile,
    ArchiverBase,
    segment_problem,
    text_problem,
)
from spectragent.core.archive.privacy import RedactionLedger, redact_value
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.step_recorder import RecordingFailedError
from spectragent.core.trace.encoding import (
    EncodedOutputFormat,
    PendingLLMCall,
    TraceFile,
    archive_run_path,
    archive_step_path,
    check_relative_path,
    encode_archive_file,
    encode_llm_calls,
    encode_observation,
    encode_output_format,
    fold_path,
    sha256_hex,
    snapshot,
)
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.redaction import redact_config, trace_safe
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: Run-record version table: capability id → (latest committed version, its
#: sha256, the number of the collection it came from — see :class:`_Collected`).
_RunVersions = dict[str, tuple[int, str, int]]


class _Collected(NamedTuple):
    """One collection's checked files, numbered in the order the collections happened.

    The number is what keeps run records in capture order. Commits do not
    follow it: a batch whose step commit failed is committed at close-out,
    after batches collected later. A run record from such an owed batch is
    older than one a later collection already committed, and committing it
    as the next version would make the stale fingerprint the latest.
    """

    number: int
    files: tuple[ArchiveFile, ...]


class _TraceWriterReceptors:
    """Annotation-only declaration of the ``archive`` receptor slot.

    ``ConfigComposer`` reads the *class* annotation to decide between list,
    dict and single injection (``composer._slot_annotation``), and the
    receptor must not be a dataclass field (``af-add-a-receptor`` §6) — so the
    annotation lives on this plain mixin, the ``_BrowserReceptors`` shape.
    """

    archive: list[ArchiverBase]


class TraceWriter(_TraceWriterReceptors, HookBase):
    """Full-trace WAL persistence hook.

    At run start it writes the snapshot of *how the run was configured* —
    identity, the redacted resolved config, the negotiation result — and
    at each step it commits what the run *did*. The configuration half is
    handed to it as values by ``AgentRuntime``: this hook derives nothing
    about how the run was set up, it only chooses what of it to withhold.

    Each LLM call is registered in ``around_ainvoke`` before it goes out
    and committed with its step — every call of the step, in start order —
    when the Loop hands over its ``StepRecord`` (``after_record_step``).
    The ``around`` advice is also the only vantage point that sees a call's
    start, its end and its failure, so it is where a step's model wait is
    measured (:meth:`_stamp_timings`).

    A run that dies before surrendering a step (timeout, cancellation,
    crash) commits the calls it still holds as *trailing* calls at run end
    (``WALWriter.commit_trailing``) instead of dropping them.

    With a non-empty :attr:`archive` it also records environment evidence:
    see the module docstring and ``docs/architecture_design.md`` §5.4.

    The explicit constructor initializes recording state and exposes named
    ``Annotated`` knobs. ``archive`` is a receptor wired from YAML; its
    source annotation is on :class:`_TraceWriterReceptors`.

    **YAML** (``docs/architecture_design.md`` §4.2.1, §5.4): the ``archive:``
    list is the one switch for archiving. Written, it builds the archivers in
    place; omitted, ``null`` or ``[]``, nothing is recorded. A top-level
    ``archiver:`` section is refused at compose time, with a message naming
    ``hook[trace_writer].archive`` (``ArchiverRegistrar.inline_only``).
    """

    __registry_key__ = "trace_writer"
    # The run's archive is the trace hook's: what a run records is a per-run
    # choice (the user's ruling of 2026-10-09, AGENTS.md §2.5), so the receptor is here, never on an agent.
    __wiring__: ClassVar[dict[str, Any]] = {"archive": "archiver"}
    # Archiving has one switch (the user's ruling of 2026-10-13): the list is
    # filled from this hook's own block or not at all, never adopted from a
    # top-level ``archiver:`` section, which ``ConfigComposer`` therefore refuses.
    __intercepts__ = [
        # The one recording point for LLM calls — see :meth:`around_ainvoke`.
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("record_step", role="loop", advice="after"),
    ]

    # Serializing must happen after every enricher has finished mutating
    # the result.
    priority = HookBase.PRIORITY_PERSIST

    def __init__(
        self,
        trace_dir: Annotated[
            str,
            trace_safe(
                "output location — where this trace already is, not a credential",
                default="traces",
                description=(
                    "Base directory for trace output. Each run lands in "
                    "``<trace_dir>/<run_id>``. Relative paths resolve against "
                    "the process working directory."
                ),
            ),
        ] = "traces",
        record_secrets: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Record the resolved configuration verbatim, including "
                    "api keys, CDP URLs carrying a token, session tokens and "
                    "proxy credentials. Off by default: closed-set values and "
                    "numbers are recorded as they are, a free-form string only "
                    "when its field is marked trace-safe, every other one lands "
                    'as ``"<omitted>"``. Turn it on for a local debugging run '
                    "whose trace directory will not be shared — the trace "
                    "records that you did (``config.secrets_recorded``)."
                ),
            ),
        ] = False,
        archive: Annotated[
            list[ArchiverBase] | None,
            Field(
                description=(
                    "Environment evidence this run records (``browser.network``, "
                    "``browser.storage``, ``browser.dom_raw``, ...), each archiver with its "
                    "own knobs; evidence lands under env/ with each step. The one switch "
                    "for archiving: omitted or null (the default) records nothing, and so "
                    "does []. Archivers are listed only here; a top-level ``archiver:`` "
                    "section is refused."
                ),
            ),
        ] = None,
    ) -> None:
        super().__init__()
        self.trace_dir = trace_dir
        self.record_secrets = record_secrets
        #: The archivers whose evidence this hook records — a receptor
        #: (``__wiring__``), so YAML builds each one with its knobs. Non-empty
        #: makes this hook the run's archive consumer.
        self.archive: list[ArchiverBase] = list(archive or [])

        # ── per-run state, reset by on_run_start ──────────────
        self._wal: WALWriter | None = None
        self._steps_written = 0
        # The LLM calls registered since the last commit, in start order.
        self._pending_calls: list[PendingLLMCall] = []
        # Calls of the step in progress already committed as trailing calls,
        # so a later commit of that step numbers its calls after them.
        self._calls_flushed = 0
        # Requested reply schemas, encoded once per class per run; ``None``
        # for a class whose schema could not be generated.
        self._output_formats: dict[Any, EncodedOutputFormat | None] = {}
        # ``perf_counter`` at the last step boundary — run start, then every
        # commit. ``None`` before the run starts, which is what makes an
        # unmeasured step record ``duration_ms=None`` rather than 0.
        self._step_started: float | None = None
        # Model wait accumulated since the last commit, in milliseconds.
        self._pending_llm_ms = 0.0
        # ── the trace's own ledger ──
        # The first step whose recording failed (collection, contract or
        # commit), in plain words; later step failures follow from it.
        self._hole: str | None = None
        # What the close-out could not record, in the order it happened.
        self._close_out_problems: list[str] = []
        # Closed out (``on_run_end``, or an ``on_run_error`` standing in for
        # it): once per run, never retried.
        self._closed = False

        # ── archive state (only ever used with a non-empty archive) ──
        # The *raw* environment the evidence is pulled from — never its AOP
        # proxy, so no hook intercepts the pull. Released at close-out.
        self._env: Any = None
        # The archiver keys this run bound, and the capabilities that hold
        # them (the values of ``archive_bindings``).
        self._archive_keys: frozenset[str] = frozenset()
        self._archive_owners: frozenset[str] = frozenset()
        # Batches a failed step commit left behind — handed over, so owed to
        # the trace; committed as trailing at close-out.
        self._pending_archive: list[_Collected] = []
        # Collections handed over so far; numbers each batch in capture order.
        self._collections = 0
        # Capability id → (latest committed run-record version, its sha256,
        # the number of the collection it came from).
        self._run_versions: _RunVersions = {}
        # The collection failure that made the archive incomplete, and
        # where it happened; from then on nothing is collected or committed
        # as if the archive were whole.
        self._archive_failure: BaseException | None = None
        self._archive_failed_at = ""
        # The close-out collection happened (or was attempted): at most once.
        self._archive_closed = False

    def __repr__(self) -> str:
        # ``getattr``: pyright cannot see ``__name__`` through conscribe's metaclass.
        name = getattr(type(self), "__name__", "TraceWriter")
        return (
            f"{name}(trace_dir={self.trace_dir!r}, "
            f"record_secrets={self.record_secrets!r}, "
            f"archive={[a.archiver_id for a in self.archive]!r})"
        )

    @property
    def blocking(self) -> bool:
        return True

    @property
    def run_dir(self) -> Path | None:
        """Directory this run is being written to — the evaluator seam."""
        return self._wal.run_dir if self._wal is not None else None

    # ── lifecycle hooks ───────────────────────────────────────

    async def on_run_start(
        self,
        *,
        env: Any = None,
        task: str = "",
        task_id: str = "",
        benchmark: str = "",
        subset: str = "",
        run_id: str | None = None,
        config: Any = None,
        agent_id: str = "",
        loop_id: str = "",
        env_type: str = "",
        llm_model: str = "",
        resolved_config: Any = None,
        observation_negotiation: Any = None,
        action_negotiation: Any = None,
        archive_negotiation: Any = None,
        env_evidence_privacy: Sequence[str] | None = None,
        task_setup: Mapping[str, Any] | None = None,
        branch: BranchProvenance | None = None,
        **kwargs: Any,
    ) -> None:
        """Open the trace: directory, config snapshot, initial manifest.

        Everything here arrives as a value. This hook used to reconstruct
        the run's identity by ``getattr``-ing ``agent_id`` / ``loop_id`` /
        ``env_type`` off whatever objects it was handed, which made a
        serializer responsible for *discovering* facts and left the
        configuration itself — the thing the run was actually set up as —
        recorded nowhere. ``AgentRuntime`` is the one component holding
        the composed config and both negotiation results, so it resolves
        them and passes them in; see
        :meth:`spectragent.core.runtime.AgentRuntime.execute`.

        The duck-typing did not vanish, it moved: identity is still read
        off the live objects, by ``AgentRuntime._component_id``, once, in
        the component that knows the run. That is why a run assembled
        without the Composer still reports an ``agent_id`` while its
        ``resolved_config`` is ``None``.

        With a non-empty :attr:`archive`, three things are refused with a
        ``RuntimeError`` — **before** ``init_run``, so a refusal leaves no
        trace directory behind: no ``archive_negotiation``; a negotiation
        whose ``active_archivers`` are not exactly this hook's archive; no
        ``env``. Archivers are negotiated and bound by
        ``AgentRuntime.prepare()``, and nothing else may stand in for that.
        Then the raw *env* is kept until close-out. With an empty archive
        no environment reference is kept at all.

        Args:
            env: The **raw** environment (never its AOP proxy) — what the
                evidence is pulled from, when this hook archives.
            task / task_id / benchmark / subset: Dataset identity — the
                key an Evaluator joins the trace back to ground truth by.
            run_id: Trace directory name. Generated when omitted.
            config: Caller-curated extra context, recorded verbatim (see
                :attr:`~spectragent.core.trace.models.TraceConfig.extra`).
            agent_id / loop_id / env_type / llm_model: Component identity.
            resolved_config: The composed, validated configuration —
                ``ComposedRun.top_level_cfg``. Passed through field-level
                redaction on the way to disk unless
                :attr:`record_secrets` is set. The ``config_hash`` in the
                manifest is computed from it *before* that redaction; the
                recorded view cannot carry a sweep.
            observation_negotiation / action_negotiation / archive_negotiation:
                The negotiation results, recorded as
                :class:`~spectragent.core.trace.models.TraceNegotiation`.
                ``archive_negotiation`` is ``None`` when no hook archives.
            env_evidence_privacy: The privacy markers the bound archivers
                declared — provenance, recorded as given (sorted) — or
                ``None`` when the run bound none (then not written).
            task_setup: The environment setup context this task was started
                with, recorded as a
                :class:`~spectragent.core.trace.models.TaskSetupRecord` —
                redacted by value with the shared privacy rules unless
                :attr:`record_secrets` is set.
            branch: Where a branch run came from, recorded as given
                (:attr:`~spectragent.core.trace.models.TraceConfig.branch`);
                ``None`` for every other run. Not part of the digest.

        Raises:
            RuntimeError: The archive was not negotiated and bound (see above).
        """
        if self.archive:
            self._refuse_an_unbound_archive(archive_negotiation, env)

        resolved_run_id = run_id or str(uuid.uuid4())[:8]
        base_dir = Path(self.trace_dir)

        self._wal = WALWriter(base_dir, resolved_run_id)
        self._steps_written = 0
        self._pending_calls = []
        self._calls_flushed = 0
        self._output_formats = {}
        self._step_started = time.perf_counter()
        self._pending_llm_ms = 0.0
        self._hole = None
        self._close_out_problems = []
        self._closed = False

        self._env = env if self.archive else None
        self._archive_keys = frozenset(a.archiver_id for a in self.archive)
        self._archive_owners = (
            frozenset(archive_negotiation.archive_bindings.values())
            if self.archive
            else frozenset()
        )
        self._pending_archive = []
        self._collections = 0
        self._run_versions = {}
        self._archive_failure = None
        self._archive_failed_at = ""
        self._archive_closed = False

        # Two views of the same object, and they are not interchangeable.
        # ``resolved_full`` never reaches disk: it exists so the digest
        # can cover the model name, a ``user_agent`` and every other
        # unmarked free-form string a sweep varies, which the recorded view
        # collapses into one ``"<omitted>"``. See ``configuration_digest``.
        resolved_full = redact_config(resolved_config, record_secrets=True)
        recorded = resolved_full if self.record_secrets else redact_config(resolved_config)

        trace_config = TraceConfig(
            agent_id=agent_id,
            loop_id=loop_id,
            env_type=env_type,
            llm_model=llm_model,
            benchmark=benchmark,
            subset=subset,
            task=task,
            task_id=task_id,
            extra=config if isinstance(config, dict) else {},
            resolved=recorded,
            negotiation=TraceNegotiation.from_results(
                observation=observation_negotiation,
                action=action_negotiation,
                archive=archive_negotiation,
            ),
            secrets_recorded=bool(self.record_secrets),
            env_evidence_privacy=(
                sorted(set(env_evidence_privacy)) if env_evidence_privacy is not None else None
            ),
            task_setup=_task_setup_record(task_setup, record_secrets=self.record_secrets),
            branch=branch,
        )
        self._wal.init_run(
            trace_config,
            config_hash=trace_config.configuration_digest(resolved=resolved_full),
        )
        if self.record_secrets:
            logger.warning(
                "TraceWriter: record_secrets is on — %s contains credentials "
                "verbatim and must not be shared.",
                self._wal.run_dir,
            )
        logger.info("TraceWriter: initialized run %s at %s", resolved_run_id, self._wal.run_dir)

    async def on_run_end(self, *, result: Any = None, env: Any = None, **kwargs: Any) -> None:
        """Close the trace out: tail, what is owed, result.json, the trace's own status.

        On every termination path, once. The close-out evidence collection
        (archive only) runs at most once and never after a failed one; the
        calls still registered and every batch owed are committed as
        trailing, each on its own; ``result.json`` is written; the manifest
        is closed ``complete`` or ``incomplete``
        (``docs/architecture_design.md`` §4.6, §5.4).

        Nothing that fails to be recorded here is raised. The run has ended
        and its outcome is decided — it is in *result*, which this writes as
        it is — so a failed close-out collection, an owed item the disk
        refuses (dropped, not retried) or a ``result.json`` that cannot be
        written makes the trace ``incomplete``, names the failure in
        ``incomplete_reason`` and is logged; it never replaces the outcome.
        A manifest that cannot be closed out at all (the disk stays full)
        stays ``writing`` and is logged as an error.

        A cancellation of the close-out collection is the one thing
        re-raised, after the close-out: Ctrl-C, or a caller's own
        ``asyncio.timeout``, must still see its cancellation
        (``docs/architecture_design.md`` §4.2, "取消永远优先").
        """
        if self._wal is None or self._closed:
            return
        cancellation = await self._close_out(result)
        logger.info(
            "TraceWriter: closed out run at %s (%d steps)",
            self._wal.run_dir,
            self._steps_written,
        )
        if cancellation is not None:
            raise cancellation

    async def on_run_error(
        self,
        *,
        error: BaseException | None = None,
        partial_steps: Any = None,
        env: Any = None,
        **kwargs: Any,
    ) -> None:
        """Note the run's error; close the trace out only when ``on_run_end`` did not.

        ``AgentRuntime`` dispatches this right after ``on_run_end``, which
        has already closed the trace out: nothing is collected, committed or
        retried again — what the close-out could not commit was dropped. A
        driver that dispatches only this closes the trace out here, with no
        result (so no ``result.json``, and the trace ``incomplete``), by the
        same rules, the cancellation of the close-out collection included.
        """
        if self._wal is None:
            return
        cancellation = None if self._closed else await self._close_out(None)
        logger.warning("TraceWriter: run errored after %d steps: %s", self._steps_written, error)
        if cancellation is not None:
            raise cancellation

    async def _close_out(self, result: Any) -> BaseException | None:
        """The close-out itself; returns a cancellation of the tail collection, raises nothing else.

        In order: the close-out collection; the trailing commits (the calls
        still registered, every owed batch, the tail — each its own commit,
        so one the filesystem refuses costs only what it carried);
        ``result.json`` when there is a result (none means none: a file
        holding ``null`` would read as a result that fails to parse); then
        the terminal status, attempted whatever happened before it. Every
        recording failure becomes part of ``incomplete_reason`` instead of
        an exception. What could not be committed is dropped: nothing
        retries it (``docs/architecture_design.md`` §5.2).
        """
        wal = self._wal
        assert wal is not None
        self._closed = True
        cancellation: BaseException | None = None
        tail: _Collected | None = None
        try:
            tail = await self._collect_tail()
        except Exception as exc:
            self._close_out_failed(
                f"the close-out evidence collection failed: {type(exc).__name__}: {exc}"
            )
        except BaseException as exc:  # held: the close-out runs to its end first
            cancellation = exc
            self._close_out_failed("the close-out evidence collection was cancelled")
        try:
            try:
                self._flush_trailing(tail)
            except Exception as exc:
                self._close_out_failed(
                    f"the close-out could not commit everything the trace was owed (what it "
                    f"could not commit was dropped): {type(exc).__name__}: {exc}"
                )
            if result is None:
                self._close_out_failed(
                    "the run was closed out without a result, so result.json was not written"
                )
            else:
                try:
                    wal.write_result(result)
                except Exception as exc:
                    self._close_out_failed(
                        f"result.json could not be written: {type(exc).__name__}: {exc}"
                    )
        finally:
            reason = self._incomplete_reason()
            try:
                wal.finalize(
                    TraceStatus.INCOMPLETE if reason else TraceStatus.COMPLETE,
                    incomplete_reason=reason,
                )
            except Exception as exc:
                logger.error(
                    "TraceWriter: the trace at %s could not be closed out and stays 'writing': "
                    "%s: %s%s",
                    wal.run_dir,
                    type(exc).__name__,
                    exc,
                    f" (it was incomplete: {reason})" if reason else "",
                    exc_info=exc,
                )
            finally:
                # Closed out: no environment reference, and nothing owed is
                # kept for a retry that is not made.
                self._env = None
                self._pending_calls.clear()
                self._pending_archive.clear()
        if reason:
            logger.warning("TraceWriter: the trace at %s is incomplete: %s", wal.run_dir, reason)
        return cancellation

    def _close_out_failed(self, reason: str) -> None:
        """One thing the close-out could not record, for ``incomplete_reason``."""
        logger.warning("TraceWriter: %s", reason)
        self._close_out_problems.append(reason)

    def _incomplete_reason(self) -> str | None:
        """Why the trace is incomplete — the first lost step, then the close-out — or ``None``."""
        reasons = [reason for reason in (self._hole, *self._close_out_problems) if reason]
        return "; ".join(reasons) or None

    # ── AOP intercept handlers ────────────────────────────────

    async def around_ainvoke(self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any) -> Any:
        """Record one LLM call: registered before it goes out, settled when it ends.

        Registration comes first so that the prompt is on record whatever
        happens next. The outcome is one of three:

        - **completed** — the reply, as ``RecordedCompletion``.
        - **failed** — any ``Exception``, recorded with what it carried and
          nothing invented: ``raw_output`` for a structured reply the
          validator refused (the run *continues* past that one: the Loop
          hands the model its own text back), ``partial_output`` for a
          truncation, ``stop_reason`` and ``usage`` because a failed call is
          billed. The prompt that caused a 429 or a timeout is worth having
          too, so every exception is recorded, not only the recoverable one.
        - **cancelled** — a ``BaseException``: ``CancelledError`` from the
          run's timeout or from outside, which neither ``after`` advice nor
          ``except Exception`` ever sees. Without this the prompt of the
          call in flight when a run was cut off was lost.

        The exception always propagates; this hook observes, it does not
        swallow. One advice records the call from start to end, so nothing
        has to correlate a ``before`` with an ``after`` — which would break
        the day a Loop issues calls concurrently.

        This is also the only place that can measure what the step spent
        waiting on the model; the accumulator is in a ``finally`` because a
        step whose LLM time went on a refused reply spent it all the same.
        """
        call = self._register_call(args, kwargs)
        started = time.perf_counter()
        try:
            result = await proceed()
        except Exception as exc:
            call.fail(exc)
            raise
        except BaseException as exc:
            call.cancel(exc)
            raise
        finally:
            self._pending_llm_ms += (time.perf_counter() - started) * 1000
        call.complete(result)
        return result

    async def after_record_step(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Commit point: the observation, every call of the step, the step record, its evidence.

        With an archive, the evidence is pulled here — after the timing
        stamp, before the manifest commit — so what step *t*'s commit takes
        belongs to step *t*.

        A step that cannot be recorded — its collection raised, its batch
        broke the contract, or the commit failed — is a hole: nothing of it
        is committed, its calls stay registered for the trailing commit, and
        the trace is incomplete, complete through the step before (the WAL
        stops ``complete_through`` there). The run then stops on it: this
        raises :class:`~spectragent.core.step_recorder.RecordingFailedError`
        (the failure chained), so the run's result reads ``recording failed
        at step t``. Not when the step's own record shows the run already
        ended in success — the agent called done on it: raising would only
        replace that outcome, so the failure is logged and the run ends as
        the agent decided (``docs/architecture_design.md`` §4.6). A
        cancellation of the collection is no recording failure and
        propagates as it is — but the step it cut off is lost, so the trace
        is incomplete from there too.
        """
        if self._wal is None:
            return

        record = args[0] if args else kwargs.get("record")
        if record is None:
            return
        observation = kwargs.get("observation")
        step = int(getattr(record, "step", self._steps_written))

        self._stamp_timings(record)
        pending = list(self._pending_calls)
        try:
            batch = await self._step_archive(step) if self.archive else None
            extra, versions = self._encode_archive(batch, step=step)
            try:
                self._wal.commit_step(
                    step,
                    record,
                    fragments=encode_observation(step, observation),
                    llm_calls=encode_llm_calls(step, pending, first_call=self._calls_flushed),
                    extra_files=extra,
                )
            except BaseException:
                # The capability has already forgotten the batch: it is owed
                # to the trace, and run end commits it as trailing evidence.
                if batch is not None and batch.files:
                    self._pending_archive.append(batch)
                raise
        except Exception as exc:
            self._step_not_recorded(record, step, exc)
            return
        except BaseException:
            # A cancellation cut the recording off: no recording failed, but
            # the step's record is lost all the same, so the trace is complete
            # only through the step before. The cancellation goes on as it is.
            if self._hole is None:
                self._hole = f"recording step {step} was cut off by a cancellation"
            raise
        # Only once the commit succeeded: a run-record version, like a call,
        # exists only if it reached disk.
        self._run_versions = versions
        # A call whose step failed to commit is still owed to the trace,
        # and run end commits it as trailing.
        del self._pending_calls[: len(pending)]
        self._calls_flushed = 0

        self._steps_written += 1
        logger.debug(
            "TraceWriter: committed step %d (%d LLM calls, %d archive files)",
            step,
            len(pending),
            len(extra),
        )

    # ── internals ─────────────────────────────────────────────

    def _step_not_recorded(self, record: Any, step: int, exc: Exception) -> None:
        """Note the hole at *step*; stop the run with it unless the run already ended in success.

        Only the first hole is the trace's reason: a step after it (a Loop
        that swallowed the error and went on) fails because of it.

        Raises:
            RecordingFailedError: *record*'s step did not end the run in
                success, so the run must stop here.
        """
        reason = f"recording failed at step {step}: {type(exc).__name__}: {exc}"
        if self._hole is None:
            self._hole = reason
        if _ended_in_success(record):
            logger.error(
                "TraceWriter: %s; the agent called done on that step, so the run's outcome "
                "stands and the trace is incomplete",
                reason,
                exc_info=exc,
            )
            return
        raise RecordingFailedError(step, exc) from exc

    def _stamp_timings(self, record: Any) -> None:
        """Write the step's wall clock and model wait onto its record.

        Measured here rather than in the Loop on purpose: timing is a
        cross-cutting concern, and a Loop that timed itself would have to
        be re-instrumented once per Loop. The two clocks are the only
        ones a hook can see — the boundary between commits, and the
        ``around`` advice on ``ainvoke`` — and both are reset here so the
        next step measures itself, not the run so far.

        A record that already carries a value keeps it: the Loop is
        allowed to know better, and nothing here should overwrite a
        measurement made closer to the work.

        The archive pull happens *after* the stamp, so its time — like the
        commit's I/O — is charged to the next step's ``duration_ms``.

        Two things about this being a *mutation*, both honest limits
        rather than guarantees:

        - The object is the Loop's own ``StepRecord``, which is also the
          payload of ``HistoryObs`` and therefore reachable from prompt
          building. No shipped agent renders these fields (they enumerate
          what they render), and a test pins that; an agent that dumps a
          record wholesale would make its prompt depend on whether
          ``trace_writer`` is in the ``hook:`` list, which is exactly the
          non-transparency AOP exists to prevent.
        - Nothing orders intercept handlers. ``priority`` orders
          *lifecycle* dispatch only (``hooks/base.py``); the Weaver
          indexes interceptors in hook-list order and ``core/aop/`` never
          reads ``priority``. ``TraceWriter`` is today the only hook
          intercepting ``record_step``, so the question does not arise —
          but a second one reading ``record.duration_ms`` would see
          ``None`` or a value purely by YAML order, silently. Treat these
          fields as readable only after the commit point.
        """
        now = time.perf_counter()
        if isinstance(record, StepRecord):
            if record.duration_ms is None and self._step_started is not None:
                record.duration_ms = int((now - self._step_started) * 1000)
            if record.llm_latency_ms is None and self._pending_llm_ms:
                record.llm_latency_ms = int(self._pending_llm_ms)
        self._step_started = now
        self._pending_llm_ms = 0.0

    def _register_call(self, args: tuple, kwargs: dict) -> PendingLLMCall:
        """Put a call on record before it goes out.

        The prompt is ``ainvoke``'s first argument; the requested reply
        model arrives as ``output_format`` (ReactLoop calls
        ``llm.ainvoke(context, output_format=output_model)``). Neither is
        validated — see :mod:`spectragent.core.trace.encoding`.
        """
        messages = args[0] if args else kwargs.get("messages")
        output_format = kwargs.get("output_format", args[1] if len(args) > 1 else None)
        call = PendingLLMCall(messages, self._output_format(output_format))
        self._pending_calls.append(call)
        return call

    def _output_format(self, output_format: Any) -> EncodedOutputFormat | None:
        """The requested schema, generated once per reply model per run.

        A model whose schema cannot be generated is recorded as no schema,
        with a warning, rather than failing the call over the trace: the
        provider builds its request from the same schema and reports that
        failure itself.
        """
        if output_format is None:
            return None
        try:
            return self._output_formats[output_format]
        except KeyError:
            pass
        except TypeError:  # unhashable — not a class, so not worth caching
            return _encode_output_format(output_format)
        encoded = self._output_formats[output_format] = _encode_output_format(output_format)
        return encoded

    # ── archive internals ─────────────────────────────────────

    def _refuse_an_unbound_archive(self, negotiation: Any, env: Any) -> None:
        """Refuse to record an archive ``AgentRuntime.prepare`` did not negotiate and bind."""
        wanted = sorted(a.archiver_id for a in self.archive)
        if negotiation is None:
            problem = "no archive negotiation was passed (archive_negotiation=None)"
        elif set(negotiation.active_archivers) != set(wanted):
            problem = (
                f"the archive negotiation bound {sorted(negotiation.active_archivers)}, "
                f"not this hook's archive"
            )
        elif env is None:
            problem = "no environment was passed to collect from (env=None)"
        else:
            return
        raise RuntimeError(
            f"TraceWriter cannot record the archive {wanted}: {problem}. Archivers are "
            f"negotiated and bound by AgentRuntime.prepare() before on_run_start; nothing "
            f"else may stand in for that."
        )

    async def _step_archive(self, step: int) -> _Collected:
        """Pull and check the evidence *step*'s commit takes.

        Fail closed. Once a collection has failed — raised, or handed over
        a batch that breaks the contract — the archive is incomplete, and a
        Loop that swallowed that error does not get a later step committed
        as if it were whole: this raises, naming where the archive broke,
        without collecting again. Nor after the close-out collection — the
        environment has been released by then.

        A cancellation of the collection is not a collection failure: it
        propagates, the step is not committed, and the close-out still
        collects (``docs/architecture_design.md`` §4.6).
        """
        if self._archive_failure is not None:
            raise RuntimeError(
                f"TraceWriter cannot commit step {step}: the archive has been incomplete since "
                f"the collection at {self._archive_failed_at} failed "
                f"({type(self._archive_failure).__name__}: {self._archive_failure})"
            ) from self._archive_failure
        if self._archive_closed:
            raise RuntimeError(
                f"TraceWriter cannot commit step {step} with its evidence: the archive was "
                f"closed out at run end"
            )
        try:
            files = self._checked(await self._env.collect_archive())
        except Exception as exc:
            self._archive_failure = exc
            self._archive_failed_at = f"step {step}"
            raise
        return self._numbered(files)

    async def _collect_tail(self) -> _Collected | None:
        """The close-out collection: at most once per run, and never after a failure.

        ``None`` when there is none to make.
        """
        if not self.archive or self._archive_failure is not None or self._archive_closed:
            return None
        # Marked before the pull: whatever happens to it, there is no second
        # one. (The environment is released at close-out too, but this flag
        # is the rule — ``on_run_end`` then ``on_run_error`` collect once.)
        self._archive_closed = True
        try:
            files = self._checked(await self._env.collect_archive())
        except Exception as exc:
            self._archive_failure = exc
            self._archive_failed_at = "close-out"
            raise
        return self._numbered(files)

    def _numbered(self, files: tuple[ArchiveFile, ...]) -> _Collected:
        """*files*, handed over by the next collection in capture order."""
        self._collections += 1
        return _Collected(self._collections, files)

    def _checked(self, batch: Any) -> tuple[ArchiveFile, ...]:
        """A collected batch's files — or ``ArchiveContractError`` before any is written.

        The checks are what only this hook can know — which archivers the
        run bound — plus everything that would make the commit fail no
        matter how often it were retried. Step evidence must come from a
        bound archiver, content-addressed bodies included; a run record from
        a capability that holds one, under its bare id or a bound key, at
        most one per capability; ``archiver_id`` and ``name`` must each be
        one path segment a filesystem accepts (``segment_problem``: at most
        255 bytes of UTF-8) and every string the index records must encode
        as UTF-8; no two step files may land on one path as a case- and
        normalization-insensitive filesystem sees it (``fold_path`` —
        macOS's is); every path must pass the WAL's own rule.

        ``ArchiveFile`` refuses most of this at construction; it is checked
        again here because a frozen dataclass can be forged around its own
        validation, and because a batch refused here is one that never
        becomes owed: a commit that failed for a reason like these would
        fail again, the same way, at every retry.
        """
        if not isinstance(batch, ArchiveBatch):
            raise ArchiveContractError(
                f"collect_archive() returned {type(batch).__name__}, not an ArchiveBatch"
            )
        bound = self._archive_keys
        problems: list[str] = []
        landing: dict[str, str] = {}
        run_records: dict[str, int] = {}
        for item in batch.files:
            label = f"{item.archiver_id}/{item.name}"
            unwritable = [
                f"{label!r}: {what} {problem}"
                for what, problem in (
                    ("archiver_id", segment_problem(item.archiver_id)),
                    ("name", segment_problem(item.name)),
                    ("media_type", text_problem(item.media_type)),
                    *(
                        (f"id {key!r}", text_problem(key) or text_problem(value))
                        for key, value in item.meta.ids.items()
                    ),
                )
                if problem is not None
            ]
            if unwritable:
                problems += unwritable
                continue
            if item.scope == "run":
                owner = item.capability_id
                run_records[owner] = run_records.get(owner, 0) + 1
                if owner not in self._archive_owners:
                    problems.append(
                        f"run record {label!r} belongs to capability {owner!r}, which holds "
                        f"none of this run's archivers {sorted(bound)}"
                    )
                elif item.archiver_id != owner and item.archiver_id not in bound:
                    problems.append(
                        f"run record {label!r} names {item.archiver_id!r}, which this run "
                        f"did not bind"
                    )
                path = archive_run_path(owner, 0)
            else:
                if item.archiver_id not in bound:
                    problems.append(
                        f"{label!r} comes from archiver {item.archiver_id!r}, which this run "
                        f"did not bind (bound: {sorted(bound)})"
                    )
                if item.content_addressed:
                    # Its path is its data's digest: always valid, one per content.
                    continue
                # Step 0 stands in for the commit that takes it: the number
                # changes neither validity nor collisions.
                path = archive_step_path(0, item.archiver_id, item.name)
                folded = fold_path(path)
                if folded in landing:
                    problems.append(
                        f"{label!r} and {landing[folded]!r} land on the same path "
                        f"(case- and normalization-insensitively)"
                    )
                landing.setdefault(folded, label)
            try:
                check_relative_path(path)
            except ValueError as exc:
                problems.append(f"{label!r}: {exc}")
        problems += [
            f"capability {owner!r} handed over {count} run records in one batch; a batch "
            f"carries at most one per capability"
            for owner, count in sorted(run_records.items())
            if count > 1
        ]
        if problems:
            raise ArchiveContractError(
                "the collected batch breaks the archiving contract, and none of it was "
                "written: " + "; ".join(sorted(problems))
            )
        return batch.files

    def _encode_archive(
        self,
        batch: _Collected | None,
        *,
        step: int | None = None,
        trailing: int | None = None,
    ) -> tuple[list[TraceFile], _RunVersions]:
        """The extra files for *batch*, and the run-record versions they leave behind.

        Run records keep capture order, whatever order the commits come in.
        One collected before the latest committed record of its capability
        is dropped — an owed batch committed at close-out, after a later
        collection's record already landed: that fingerprint is stale, and
        as the next version it would read back as the latest. One whose
        bytes equal that latest record is dropped too — re-emitting an
        unchanged fingerprint adds nothing — but its collection still counts
        as the latest capture. Any other is committed as the next version
        (the first is 0), because a committed path is immutable. The
        returned table takes effect only once the commit carrying these
        files succeeded, so the versions are recomputed for every commit,
        each trailing one included.
        """
        versions = dict(self._run_versions)
        extra: list[TraceFile] = []
        if batch is None:
            return extra, versions
        for item in batch.files:
            if item.scope != "run":
                extra.append(encode_archive_file(item, step=step, trailing=trailing))
                continue
            digest = sha256_hex(item.data)
            latest = versions.get(item.capability_id)
            if latest is not None and latest[2] > batch.number:
                logger.warning(
                    "TraceWriter: not committing %r from collection %d: collection %d's run "
                    "record of capability %r is newer and already committed",
                    item.name,
                    batch.number,
                    latest[2],
                    item.capability_id,
                )
                continue
            if latest is not None and latest[1] == digest:
                versions[item.capability_id] = (latest[0], digest, batch.number)
                continue
            version = 0 if latest is None else latest[0] + 1
            versions[item.capability_id] = (version, digest, batch.number)
            extra.append(encode_archive_file(item, run_version=version))
        return extra, versions

    def _flush_trailing(self, tail: _Collected | None = None) -> None:
        """Commit what belongs to no committed step: unfinished calls, owed evidence, the tail.

        ``after_record_step`` is the only *commit* point, but it is not the
        only *termination* point: a run can be cut off by the timeout or a
        cancellation mid-call, or crash in parsing or action execution after
        the reply came back. Dropping the registered calls there discards
        precisely the exchange that says what the agent was doing when the
        run died — and abnormal termination is the common case, not the
        exception, in this project's real traces.

        They are recorded under the step index that *would* have been next
        and listed in ``manifest.trailing``, not as a step:
        ``RunTrace.trailing_calls`` reads them back.

        Each thing owed gets a ``commit_trailing`` of its own, in this order:
        the calls still registered; every batch a failed step commit left
        behind (more than one only for a driver that kept going after a
        failed commit — two steps' batches can carry the same file name, so
        each needs its own ``env/trailing/KK/``); *tail*, the close-out
        collection, which is owed like any other batch from here on. A
        commit that fails costs only what it carried, while everything else
        is still committed — a batch the filesystem refuses no longer takes
        the unfinished step's calls down with it. Once everything has been
        tried, the first failure is raised — to the close-out, which makes it
        part of the trace's ``incomplete_reason``.

        What failed is not retried: the close-out happens once and then
        drops whatever is still owed (``docs/architecture_design.md`` §5.2);
        a disk that just refused a write would most likely refuse it again.
        """
        if self._wal is None:
            return
        if tail is not None and tail.files:
            self._pending_archive.append(tail)
        failures: list[Exception] = []
        if self._pending_calls:
            try:
                self._commit_trailing(list(self._pending_calls), None)
            except Exception as exc:  # the calls stay registered; the batches still go
                failures.append(exc)
        index = 0
        while index < len(self._pending_archive):
            try:
                self._commit_trailing([], self._pending_archive[index])
            except Exception as exc:  # not retried by AgentRuntime; the next batch still goes
                failures.append(exc)
                index += 1
                continue
            del self._pending_archive[index]
        if not failures:
            return
        for later in failures[1:]:
            logger.warning(
                "TraceWriter: another trailing commit failed as well: %s: %s",
                type(later).__name__,
                later,
            )
        raise failures[0]

    def _commit_trailing(self, calls: list[PendingLLMCall], batch: _Collected | None) -> None:
        """One ``commit_trailing``: *calls* of the unfinished step, or *batch* at the next position.

        The bookkeeping — calls removed from the registry, run-record
        versions taken — happens only once the commit succeeded, so a
        failed one leaves everything it carried owed.
        """
        wal = self._wal
        if wal is None:
            return
        manifest = wal.manifest
        # The step index the Loop was working on. Derived from the manifest
        # rather than a local counter so it stays right for a Loop that
        # numbers its steps from something other than 0.
        step = manifest.last_step_completed + 1 if manifest is not None else self._steps_written
        # The entry this commit appends — the ``KK`` of its extra index and
        # of its ``env/trailing/KK/``.
        position = len(manifest.trailing) if manifest is not None else 0
        extra, versions = self._encode_archive(batch, trailing=position)
        wal.commit_trailing(
            llm_calls=encode_llm_calls(step, calls, first_call=self._calls_flushed),
            extra_files=extra,
        )
        self._run_versions = versions
        if extra:
            logger.info(
                "TraceWriter: committed %d archive file(s) as trailing entry %d",
                len(extra),
                position,
            )
        if not calls:
            return
        del self._pending_calls[: len(calls)]
        self._calls_flushed += len(calls)
        # No StepRecord exists for these, so the accumulated model wait has
        # nowhere to land. Cleared rather than carried, so a driver that
        # drains and keeps going does not attribute it to a later step.
        self._pending_llm_ms = 0.0
        logger.info(
            "TraceWriter: committed %d LLM call(s) of unfinished step %d as trailing",
            len(calls),
            step,
        )


def _encode_output_format(output_format: Any) -> EncodedOutputFormat | None:
    try:
        return encode_output_format(output_format)
    except Exception:  # the trace must never be what fails a call
        logger.warning(
            "TraceWriter: could not encode the JSON schema of output_format %r; "
            "its calls are recorded without one",
            output_format,
            exc_info=True,
        )
        return None


def _task_setup_record(context: Any, *, record_secrets: bool) -> TaskSetupRecord | None:
    """The setup context as the trace records it: snapshotted, then redacted.

    The record must not be what fails a run: ``encoding.snapshot`` never
    raises and turns anything into plain JSON (stringifying keys and
    whatever JSON cannot hold), and a snapshot that is not a JSON object is
    kept under ``"value"``, so the rules below only ever walk JSON.

    The shared value-level rules
    (:func:`~spectragent.core.archive.privacy.redact_value`) replace
    credential-shaped keys and URL credentials, and the ledger records each
    replacement. ``record_secrets`` disables that exactly as it disables
    config redaction: the context is recorded verbatim with an empty
    ledger, and ``TraceConfig.secrets_recorded`` says so.
    """
    if context is None:
        return None
    taken = snapshot(context)
    document = taken if isinstance(taken, dict) else {"value": taken}
    if record_secrets:
        return TaskSetupRecord(context=document, redactions=RedactionLedger())
    redacted, entries = redact_value(document)
    return TaskSetupRecord(context=redacted, redactions=RedactionLedger(entries=entries))


def _ended_in_success(record: Any) -> bool:
    """Whether *record*'s own step ended the run in success: the agent called done on it.

    The one outcome a step's record decides before it is handed over — a
    Loop builds its done result from the very action result this reads
    (``ActionResult.is_done``). A failing ending is the Loop's to protect:
    it hands that step over through ``ReactLoop._surrender_failing``, which
    keeps the outcome and attaches the recording failure to it.
    """
    return (
        isinstance(record, StepRecord)
        and record.origin == "policy"
        and any(result.is_done for result in record.action_results)
    )
