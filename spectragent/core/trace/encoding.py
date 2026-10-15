"""The on-disk encoding of a run directory — one rule, three users.

``TraceWriter`` encodes with it, ``RunTrace`` decodes with it, and the test
fixtures that build traces by hand call the same functions, so a fixture
cannot drift into a shape production never writes
(``docs/architecture_design.md`` §5.2–§5.3). Every file name a trace
contains is spelled here and nowhere else.

What this module produces is bytes plus paths — :class:`TraceFile`,
:class:`EncodedFragment`, :class:`EncodedLLMCall` — and it does no I/O.
:class:`~spectragent.core.trace.wal.WALWriter` validates and commits them;
:class:`~spectragent.core.trace.reader.RunTrace` reads them back through
the manifest.

**The writer never validates what it records.** A prompt, a completion and
an exception's attributes are snapshotted into their JSON form exactly as
they arrived, with :func:`snapshot` standing in for anything JSON cannot
hold. ``TraceWriter`` is a blocking hook, so a validation error raised here
would end the run it is recording — over a typing question about the
trace. The reader validates instead (``RecordedLLMCall``), where a record
that does not fit is reported as an unreadable trace file.

**An image a prompt sent inline is stored once per run.** A call record
holds the whole prompt, and an agent that keeps every screenshot in its
memory sends all of them on every call. :func:`store_prompt_images` puts a
:class:`~spectragent.core.trace.models.PromptImageRef` where each one stood
and hands the image over as a content-addressed file under
``messages/images/``; :func:`restore_prompt_images` is the reader's inverse,
and the round trip is lossless (``docs/architecture_design.md`` §5.3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_json, to_jsonable_python

from spectragent.core.llm.views import ChatInvokeUsage
from spectragent.core.trace.models import ArchiveMeta, OutputFormatRef, PromptImageRef
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.dynamic_model import iter_fragments
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.image import image_suffix

logger = logging.getLogger(__name__)

# ── names ─────────────────────────────────────────────────────

MANIFEST_FILE = "manifest.json"
CONFIG_FILE = "config.json"
RESULT_FILE = "result.json"
STEPS_DIR = "steps"
OBSERVATIONS_DIR = "observations"
MESSAGES_DIR = "messages"
OUTPUT_FORMATS_DIR = f"{MESSAGES_DIR}/output_formats"
PROMPT_IMAGES_DIR = f"{MESSAGES_DIR}/images"
EVALS_DIR = "evals"
METRICS_DIR = "metrics"

#: Names the WAL and this module decide. An extra file may not land on one:
#: a producer that could write ``steps/007.json`` could forge a step.
WAL_OWNED_FILES = frozenset({MANIFEST_FILE, CONFIG_FILE, RESULT_FILE})
WAL_OWNED_DIRS = frozenset({STEPS_DIR, OBSERVATIONS_DIR, MESSAGES_DIR, EVALS_DIR, METRICS_DIR})


def step_record_path(step: int) -> str:
    """``steps/NNN.json`` — the step's ``StepRecord``."""
    return f"{STEPS_DIR}/{step:03d}.json"


def step_extra_index_path(step: int) -> str:
    """``steps/NNN.extra.json`` — the step's ``ExtraFileIndex``."""
    return f"{STEPS_DIR}/{step:03d}.extra.json"


def trailing_extra_index_path(position: int) -> str:
    """``steps/trailing_KK.extra.json`` — *position* is the entry's place in ``manifest.trailing``."""
    return f"{STEPS_DIR}/trailing_{position:02d}.extra.json"


def fragment_path(step: int, key: str) -> str:
    """``observations/NNN_<key>.json`` — named by the fully-qualified key.

    Not the negotiated local name: that is a per-run artefact of which
    capability won negotiation, so ``000_screenshot.json`` would mean a
    different thing in a browser run and a desktop run.
    """
    return f"{OBSERVATIONS_DIR}/{step:03d}_{key}.json"


def artifact_path(step: int, key: str, suffix: str) -> str:
    """``observations/NNN_<key><suffix>`` — *suffix* comes from the bytes' own header."""
    return f"{OBSERVATIONS_DIR}/{step:03d}_{key}{suffix}"


def call_path(step: int, call: int) -> str:
    """``messages/NNN_call_KK.json`` — *call* is the start order within the step."""
    return f"{MESSAGES_DIR}/{step:03d}_call_{call:02d}.json"


def output_format_path(sha256: str) -> str:
    """``messages/output_formats/<sha256>.json`` — content-addressed."""
    return f"{OUTPUT_FORMATS_DIR}/{sha256}.json"


def prompt_image_path(sha256: str, suffix: str) -> str:
    """``messages/images/<sha256><suffix>`` — content-addressed, one file per distinct image."""
    return f"{PROMPT_IMAGES_DIR}/{sha256}{suffix}"


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def prompt_image_digest(rel_path: str) -> str | None:
    """The sha256 a prompt image's path is named by, or ``None`` when it is named by none.

    The path is directly under ``messages/images/``, and its name is 64
    lowercase hex digits followed by nothing or a suffix.
    """
    directory, _, name = rel_path.rpartition("/")
    stem = name.partition(".")[0]
    if directory != PROMPT_IMAGES_DIR or not _SHA256_HEX.fullmatch(stem):
        return None
    return stem


# ── environment evidence (env/) ───────────────────────────────

#: The root of every environment-evidence file. The WAL does not know it:
#: archive files are ordinary extra files (§5.2), and this module is the
#: one place their names are spelled.
ENV_DIR = "env"

#: The ``ExtraFileRecord.meta`` key whose value is an
#: :class:`~spectragent.core.trace.models.ArchiveMeta`. An extra file
#: carrying it is an archive file; one without it is not, wherever it lies.
ARCHIVE_META_KEY = "archive"


def archive_step_path(step: int, archiver_id: str, name: str) -> str:
    """``env/steps/NNN/<archiver_id>/<name>`` — what step NNN's commit took."""
    return f"{ENV_DIR}/steps/{step:03d}/{archiver_id}/{name}"


def archive_trailing_path(position: int, archiver_id: str, name: str) -> str:
    """``env/trailing/KK/<archiver_id>/<name>`` — *position* is the entry's place in ``manifest.trailing``.

    The same number as the commit's ``steps/trailing_KK.extra.json``.
    """
    return f"{ENV_DIR}/trailing/{position:02d}/{archiver_id}/{name}"


def archive_blob_path(sha256: str) -> str:
    """``env/blobs/sha256/<ab>/<rest>`` — a content-addressed archive file, sharded."""
    return f"{ENV_DIR}/blobs/sha256/{sha256[:2]}/{sha256[2:]}"


def archive_run_path(capability_id: str, version: int) -> str:
    """``env/run.<capability_id>.json`` for version 0, ``env/run.<capability_id>.<k>.json`` after.

    A committed path is immutable, so a run record whose content changed is
    committed as the next version rather than rewritten. A capability id
    never contains a ``.`` (it is the first segment of an archiver key), so
    ``run.a.1.json`` cannot be read as version 0 of a capability ``a.1``.

    Raises:
        ValueError: *version* is negative.
    """
    if version < 0:
        raise ValueError(f"a run record's version is >= 0, got {version}")
    if version == 0:
        return f"{ENV_DIR}/run.{capability_id}.json"
    return f"{ENV_DIR}/run.{capability_id}.{version}.json"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_relative_path(rel_path: str) -> None:
    """Refuse anything but a POSIX path that stays inside the run directory.

    Relative, ``/``-separated, with no empty, ``.`` or ``..`` segment, no
    backslash and no NUL. Every path a trace records has this shape: the
    WAL refuses to write any other, and ``RunTrace`` refuses to open one a
    manifest or an extra index lists anyway — those files are data, and a
    damaged or crafted trace can list ``/home/me/.ssh/id_rsa``.

    Raises:
        ValueError: Naming the path and the rule it breaks.
    """
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path or "\x00" in rel_path:
        raise ValueError(f"{rel_path!r} is not a relative POSIX path inside the run directory")
    if any(part in ("", ".", "..") for part in rel_path.split("/")):
        raise ValueError(f"{rel_path!r} has an empty, '.' or '..' segment")


def fold_path(rel_path: str) -> str:
    """The key two paths share when a filesystem would store them as one file.

    macOS's default APFS is case-insensitive *and* normalization-insensitive:
    ``env/A.txt`` and ``env/a.txt`` are one file, and so are ``café.json``
    spelled precomposed (NFC) and decomposed (NFD). A trace must survive
    being copied to such a filesystem without one file replacing another, so
    every comparison of paths — the WAL's immutability, its file-or-directory
    rule, a producer's batch check — compares these keys.

    The key is Unicode canonical caseless matching (Unicode §3.13, D145):
    ``NFD(casefold(NFD(path)))``. Plain ``casefold`` misses normalization
    variants, and ``casefold(NFC(path))`` still misses pairs whose case
    folding leaves a decomposed sequence (``Ś`` and ``ſ`` + combining acute).
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", rel_path).casefold())


# ── what the WAL commits ──────────────────────────────────────


@dataclass(frozen=True)
class TraceFile:
    """One file to commit: where it goes, and its bytes.

    Attributes:
        rel_path: POSIX path relative to the run directory.
        data: The bytes, written as given.
        content_addressed: The path is derived from ``sha256(data)``. The
            WAL checks that it is, writes the file once per run however
            many commits carry it, and lists it in every commit's index.
        meta: Producer metadata, recorded in the extra-file index entry.
    """

    rel_path: str
    data: bytes
    content_addressed: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EncodedFragment:
    """One fragment of one step, ready to commit.

    Attributes:
        key: Fully-qualified fragment key.
        file: ``observations/NNN_<key>.json``.
        artifact: ``observations/NNN_<key><suffix>``, when the fragment
            carries a binary payload.
        failed: ``file`` holds a :class:`FragmentFailure`. Recorded in the
            fragment index because the failure's JSON cannot say so itself
            (see :class:`~spectragent.core.trace.models.FragmentFiles`).
    """

    key: str
    file: TraceFile
    artifact: TraceFile | None
    failed: bool


@dataclass(frozen=True)
class EncodedOutputFormat:
    """A requested reply schema: the reference a call record carries, and the file it names."""

    ref: OutputFormatRef
    file: TraceFile


@dataclass(frozen=True)
class EncodedLLMCall:
    """One LLM call, ready to commit.

    Attributes:
        file: ``messages/NNN_call_KK.json`` — a ``RecordedLLMCall``, its
            inline images stored apart (:func:`store_prompt_images`).
        output_format: The requested schema the record points at,
            content-addressed. The WAL writes it with the first commit that
            carries it and skips it after that.
        images: Every distinct image the record refers to,
            ``messages/images/<sha256><suffix>``, content-addressed: written
            with the first commit that carries it, like the schema.
    """

    file: TraceFile
    output_format: TraceFile | None = None
    images: tuple[TraceFile, ...] = ()
    metrics: TraceFile | None = None


# ── observations ──────────────────────────────────────────────


def encode_fragment(step: int, value: Any) -> EncodedFragment | None:
    """Encode one observation slot, or ``None`` when it holds no fragment.

    A :class:`FragmentBase` lands as its own JSON —
    ``model_dump(mode="json", exclude=trace_json_exclude())``, nothing added
    — plus its :meth:`~FragmentBase.trace_binary` bytes as an artifact. A
    :class:`FragmentFailure` lands under the key that failed, flagged.
    Anything else has no key to be filed under and no ``from_trace`` to be
    read back by, so it is not recorded.
    """
    if isinstance(value, FragmentFailure):
        key = value.fragment_id
        return EncodedFragment(
            key=key,
            file=TraceFile(fragment_path(step, key), value.model_dump_json(indent=2).encode()),
            artifact=None,
            failed=True,
        )
    if not isinstance(value, FragmentBase):
        logger.debug("trace encoding: step %d slot holds %s, not a fragment", step, type(value))
        return None

    key = value.fragment_id
    if not key:
        # Only an unregistered FragmentBase subclass has no key, and its
        # file could never be resolved to a class again.
        logger.warning(
            "trace encoding: %s has no fragment_id (no __registry_key__); not recorded",
            type(value).__name__,
        )
        return None
    artifact: TraceFile | None = None
    binary = value.trace_binary()
    if binary is not None:
        suffix, data = binary
        artifact = TraceFile(artifact_path(step, key, suffix), data)
    payload = value.model_dump_json(indent=2, exclude=value.trace_json_exclude())
    return EncodedFragment(
        key=key,
        file=TraceFile(fragment_path(step, key), payload.encode()),
        artifact=artifact,
        failed=False,
    )


def encode_observation(step: int, observation: Any) -> list[EncodedFragment]:
    """Every fragment the observation carries, in field order.

    An observation that is not a fragment model (or ``None``) records
    nothing: the step's fragment index is empty. There used to be a
    fallback that dumped such an object whole as ``NNN_observation.json``,
    which no accessor could read back; production always hands over a
    ``NegotiatedObservation``.
    """
    if observation is None:
        return []
    encoded: list[EncodedFragment] = []
    for _name, value in iter_fragments(observation):
        item = encode_fragment(step, value)
        if item is not None:
            encoded.append(item)
    return encoded


def decode_fragment(
    key: str, cls: type[FragmentBase], payload: dict[str, Any], artifact: bytes | None
) -> FragmentBase:
    """The inverse of :func:`encode_fragment` for a fragment that was captured.

    The payload must name the key it is filed under: a sidecar whose
    ``fragment_id`` disagrees is not the file the index says it is.

    Raises:
        ValueError: The payload is filed under the wrong key, or does not
            decode as *cls* (``from_trace`` reports every failure as a
            ``ValueError``, pydantic's ``ValidationError`` included).
    """
    recorded = payload.get("fragment_id")
    if recorded != key:
        raise ValueError(f"the payload names fragment {recorded!r}, but is filed under {key!r}")
    return cls.from_trace(payload, artifact)


# ── LLM calls ─────────────────────────────────────────────────


def snapshot(value: Any) -> Any:
    """The JSON form of *value*, taken now, never raising.

    Pydantic models, dataclasses, mappings and sequences convert
    structurally; anything JSON cannot hold becomes its ``str()``, and bytes
    their base64. The result holds no reference to anything mutable in
    *value* (strings are shared, not copied — they cannot change), so a
    caller that mutates its objects later does not rewrite the record.
    """
    try:
        return to_jsonable_python(value, fallback=_unknown, bytes_mode="base64")
    except Exception:  # the record must never be what fails a run
        return f"<unserializable {type(value).__name__}>"


def _unknown(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # a broken __str__ is still not the run's problem
        return f"<unserializable {type(value).__name__}>"


_MISSING = object()


def _attr(value: Any, name: str, default: Any = None) -> Any:
    """``getattr`` that a raising property cannot turn into the call's failure."""
    try:
        return getattr(value, name, default)
    except Exception:  # a broken property is still not the run's problem
        return default


def _usage(usage: Any) -> Any:
    """Usage in ``ChatInvokeUsage``'s shape.

    A model or mapping converts as it is; any other object is read field by
    field, so a provider (or a test double) that reports usage as a plain
    object is recorded rather than stringified. A field it does not have
    reads as ``None`` — "not reported", which is what ``None`` means there.
    """
    if usage is None or isinstance(usage, (BaseModel, Mapping)):
        return snapshot(usage)
    return {name: snapshot(_attr(usage, name)) for name in ChatInvokeUsage.model_fields}


def _completion(result: Any, *, capture_metrics: bool = True) -> dict[str, Any]:
    """``RecordedCompletion``'s JSON form for what ``ainvoke`` returned.

    Read by attribute, which covers ``ChatInvokeCompletion`` and every
    completion-shaped object alike. A result without a ``completion`` is
    recorded as the reply itself.
    """
    reply = _attr(result, "completion", _MISSING)
    if reply is _MISSING:
        return {"completion": snapshot(result)}
    return {
        "completion": snapshot(reply),
        "thinking": snapshot(_attr(result, "thinking")),
        "redacted_thinking": snapshot(_attr(result, "redacted_thinking")),
        "usage": _usage(_attr(result, "usage")) if capture_metrics else None,
        "stop_reason": snapshot(_attr(result, "stop_reason")),
        "tool_calls": snapshot(_attr(result, "tool_calls") or []),
    }


#: Exception attributes a failure record keeps when — and only when — the
#: exception carries them. Text-valued: ``CallFailure`` declares them ``str``.
_FAILURE_TEXT = ("raw_output", "partial_output", "stop_reason")


def _failure(exc: BaseException) -> dict[str, Any]:
    """``CallFailure``'s JSON form: what the exception carried, and nothing invented.

    ``raw_output`` for a refused structured reply, ``partial_output`` for a
    truncated one, ``stop_reason`` and ``usage`` because a failed call is
    still billed. A dead socket carries none of them and its record is
    ``error`` and ``detail`` only.
    """
    record: dict[str, Any] = {"error": type(exc).__name__, "detail": _unknown(exc)}
    for name in _FAILURE_TEXT:
        value = _attr(exc, name)
        if value is not None:
            record[name] = value if isinstance(value, str) else _unknown(value)
    usage = _attr(exc, "usage")
    if usage is not None:
        record["usage"] = _usage(usage)
    return record


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


CallStatus = Literal["completed", "failed", "cancelled"]


@dataclass
class PendingLLMCall:
    """One LLM call as ``TraceWriter`` registered it, before it went out.

    Registered in ``around_ainvoke`` *before* ``proceed()``, so the prompt is
    on record whatever happens next — including a ``CancelledError``, which
    no ``after`` advice and no ``except Exception`` ever sees. The outcome
    is filled in by exactly one of :meth:`complete`, :meth:`fail` or
    :meth:`cancel`; the first one wins.

    Everything is held as its JSON snapshot (:func:`snapshot`), taken when
    it happened: the prompt at registration, the reply or the exception at
    the outcome. A memory, serializer or loop that mutates a message object
    afterwards cannot change what the record says the model was asked.

    Attributes:
        messages: The prompt as handed to ``ainvoke``, snapshotted.
        output_format: The requested reply schema, already encoded — the
            writer caches it per class (``model_json_schema()`` once per
            reply model per run).
        started_at: Wall clock at registration.
        status: ``None`` while the call is in flight.
    """

    messages: Any
    output_format: EncodedOutputFormat | None = None
    capture_trajectory: bool = True
    capture_metrics: bool = True
    capture_errors: bool = True
    call_id: str = ""
    step: int = 0
    call: int = 0
    caller: str = "agent"
    purpose: str = "agent_decision"
    model: str = ""
    ended_at: float = 0.0
    usage: Any = None
    started_at: float = field(default_factory=time.time)
    status: CallStatus | None = None
    latency_ms: int | None = None
    completion: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    _started: float = field(default_factory=time.perf_counter, init=False, repr=False)

    def __post_init__(self) -> None:
        self.messages = snapshot(self.messages) if self.capture_trajectory else None
        self._started = time.perf_counter()
        self.started_at = time.time()

    def complete(self, result: Any) -> None:
        """The call returned *result*."""
        if self._settle("completed"):
            self.usage = _usage(_attr(result, "usage")) if self.capture_metrics else None
            if self.capture_trajectory:
                self.completion = _completion(result, capture_metrics=self.capture_metrics)

    def fail(self, exc: BaseException) -> None:
        """The call raised *exc* — an ``Exception``: a refusal, a truncation, a dead socket."""
        if self._settle("failed"):
            self.usage = _usage(_attr(exc, "usage")) if self.capture_metrics else None
            if self.capture_trajectory and self.capture_errors:
                self.failure = _failure(exc)
                if not self.capture_metrics:
                    self.failure.pop("usage", None)
            elif self.capture_errors:
                self.failure = {"error": type(exc).__name__, "detail": _unknown(exc)}

    def cancel(self, exc: BaseException) -> None:
        """The call was cancelled — a ``BaseException`` (``CancelledError`` and its kin)."""
        if self._settle("cancelled"):
            self.usage = _usage(_attr(exc, "usage")) if self.capture_metrics else None
            if self.capture_trajectory and self.capture_errors:
                self.failure = _failure(exc)
                if not self.capture_metrics:
                    self.failure.pop("usage", None)
            elif self.capture_errors:
                self.failure = {"error": type(exc).__name__, "detail": _unknown(exc)}

    def _settle(self, status: CallStatus) -> bool:
        if self.status is not None:
            return False
        self.status = status
        self.ended_at = time.time()
        self.latency_ms = _elapsed_ms(self._started)
        return True


def encode_output_format(output_format: Any) -> EncodedOutputFormat:
    """The requested reply schema, as canonical JSON at its content address.

    Canonical — sorted keys, compact separators, UTF-8 — so one schema has
    one digest however often and wherever it is encoded.

    Raises:
        TypeError: *output_format* is not a pydantic model class.
        Exception: Whatever ``model_json_schema()`` raises for it.
    """
    schema_of = getattr(output_format, "model_json_schema", None)
    if not callable(schema_of):
        raise TypeError(f"output_format {output_format!r} is not a pydantic model class")
    data = json.dumps(
        schema_of(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    digest = sha256_hex(data)
    name = str(getattr(output_format, "__name__", type(output_format).__name__))
    return EncodedOutputFormat(
        ref=OutputFormatRef(name=name, sha256=digest),
        file=TraceFile(output_format_path(digest), data, content_addressed=True),
    )


def encode_llm_call(step: int, call: int, pending: PendingLLMCall) -> EncodedLLMCall:
    """``messages/NNN_call_KK.json`` for one registered call, and the images it refers to.

    A call still in flight when its step is committed or the run is closed
    out is recorded as ``cancelled`` with no failure record: the trace ends
    there, no exception reached the hook, and its prompt is kept. Every
    image the prompt sent inline is stored apart (:func:`store_prompt_images`):
    the record holds a reference in its place, and ``image_refs`` — written
    only when there is one — lists where each reference stands.
    """
    latency = pending.latency_ms
    if latency is None:
        latency = _elapsed_ms(pending._started)
    stored = store_prompt_images(pending.messages)
    record: dict[str, Any] = {
        "step": step,
        "call": call,
        "call_id": pending.call_id or f"{step}:{call}",
        "caller": pending.caller,
        "purpose": pending.purpose,
        "model": pending.model,
        "ended_at": pending.ended_at,
        "status": pending.status or "cancelled",
        "started_at": pending.started_at,
        "latency_ms": latency,
        "messages": stored.messages,
    }
    if stored.image_refs:
        record[IMAGE_REFS_KEY] = stored.image_refs
    record["output_format"] = (
        pending.output_format.ref.model_dump(mode="json") if pending.output_format else None
    )
    record["completion"] = pending.completion
    record["failure"] = pending.failure
    data = to_json(record, indent=2, fallback=_unknown, bytes_mode="base64")
    return EncodedLLMCall(
        file=TraceFile(call_path(step, call), data),
        output_format=pending.output_format.file if pending.output_format else None,
        images=stored.images,
        metrics=(
            encode_call_metrics(step, call, pending)
            if pending.capture_metrics or pending.capture_errors
            else None
        ),
    )


def encode_call_metrics(step: int, call: int, pending: PendingLLMCall) -> TraceFile:
    """Encode the invocation ledger without touching messages, schema or images."""
    failure = pending.failure or {}
    value = {
        "call_id": pending.call_id or f"{step}:{call}",
        "step": step,
        "call": call,
        "caller": pending.caller,
        "purpose": pending.purpose,
        "model": pending.model,
        "status": pending.status or "cancelled",
        "started_at": pending.started_at,
        "ended_at": pending.ended_at,
        "latency_ms": pending.latency_ms or 0,
        "usage": pending.usage,
        "error": failure.get("error"),
        "detail": failure.get("detail"),
    }
    return TraceFile(f"{METRICS_DIR}/{step:03d}_call_{call:02d}.json", to_json(value))


def encode_llm_calls(
    step: int, pending: Iterable[PendingLLMCall], *, first_call: int = 0
) -> list[EncodedLLMCall]:
    """Encode calls in start order, numbered from *first_call*."""
    return [encode_llm_call(step, first_call + offset, item) for offset, item in enumerate(pending)]


# ── prompt images ─────────────────────────────────────────────

#: The call record's key listing where each ``PromptImageRef`` stands in its
#: ``messages``: the keys and indices leading to it. Written only when the
#: prompt sent an image inline.
IMAGE_REFS_KEY = "image_refs"

#: An inline image: a ``data:`` URL whose payload is base64 (RFC 2397), with
#: whatever parameters and letter case it came with.
_BASE64_DATA_URL = re.compile(r"data:[^,]*;base64,", re.IGNORECASE)

#: The suffix of a stored image whose header names no image this framework
#: knows, and of one stored as its URL's own text.
_UNKNOWN_IMAGE_SUFFIX = ".bin"
_TEXT_SUFFIX = ".txt"

#: A position in a prompt's JSON: the keys and indices leading to a value.
Position = list[str | int]


@dataclass(frozen=True)
class StoredPrompt:
    """A prompt as its call record holds it, and the image files it refers to.

    Attributes:
        messages: The prompt's JSON form with every inline image replaced by
            a ``PromptImageRef`` (its JSON form) — the very object passed in
            when nothing was replaced.
        image_refs: Where each reference stands in *messages*, in document
            order.
        images: The files the references name, each distinct file once, in
            the order first referenced.
    """

    messages: Any
    image_refs: list[Position]
    images: tuple[TraceFile, ...]


def store_prompt_images(messages: Any) -> StoredPrompt:
    """*messages* — a prompt's JSON snapshot — with every inline image stored apart.

    An inline image is the ``url`` of an object under an ``image_url`` key
    when it is a base64 ``data:`` URL: where ``ContentPartImageParam`` puts
    one, in a user and a tool message alike. Each becomes a
    ``PromptImageRef`` naming a file under ``messages/images/``, which holds

    - the decoded bytes, when they are not empty and re-encoding them in
      standard base64 gives the payload back exactly — named by their sha256
      and the suffix their header gives (``.bin`` when it names none); the
      reference keeps the URL up to its comma;
    - otherwise the URL's own text, UTF-8, as ``<sha256>.txt``: decoding a
      payload with line breaks, missing padding or another alphabet would
      lose them.

    A URL that is not even UTF-8 (a lone surrogate) stays where it is, and so
    does every other string — an image sent by URL, ``data:`` inside a text
    part. *messages* itself is not modified: the containers on the way to a
    replaced URL are copied.

    Never raises: a prompt it cannot walk is recorded as it is, its images
    inline. Either way the record reads back exactly as it was sent.
    """
    try:
        replacements: list[tuple[tuple[str | int, ...], dict[str, Any]]] = []
        files: dict[str, TraceFile] = {}
        for at, url in _inline_images(messages):
            stored = _stored_image(url)
            if stored is None:
                continue
            ref, file = stored
            replacements.append((at, ref))
            files.setdefault(file.rel_path, file)
        if not replacements:
            return StoredPrompt(messages, [], ())
        return StoredPrompt(
            _replaced(messages, replacements),
            [list(at) for at, _ in replacements],
            tuple(files.values()),
        )
    except Exception:  # the record must never be what fails a run
        logger.warning(
            "trace encoding: the prompt could not be walked; its images are recorded inline",
            exc_info=True,
        )
        return StoredPrompt(messages, [], ())


def restore_prompt_images(record: Any, text_of: Callable[[PromptImageRef], str]) -> None:
    """Put back, in a parsed call record, the text each ``PromptImageRef`` stands for.

    The inverse of :func:`store_prompt_images`: at each position the
    record's ``image_refs`` lists, the reference becomes ``text_of(ref)``,
    and ``image_refs`` is removed — in place. A record without
    ``image_refs`` sent no image inline and is left as it is.

    Raises:
        ValueError: ``image_refs`` is not a list of positions, a position
            leads nowhere in ``messages``, or what stands there is not a
            reference (pydantic's ``ValidationError`` is a ``ValueError``).
            Whatever *text_of* raises propagates as it is.
    """
    if not isinstance(record, dict) or IMAGE_REFS_KEY not in record:
        return
    positions = record.pop(IMAGE_REFS_KEY)
    if not isinstance(positions, list):
        raise ValueError(f"{IMAGE_REFS_KEY} is a {type(positions).__name__}, not a list")
    messages = record.get("messages")
    for at in positions:
        holder, key = _locate(messages, at)
        ref = PromptImageRef.model_validate(holder[key])
        holder[key] = text_of(ref)


def prompt_image_text(ref: PromptImageRef, data: bytes) -> str:
    """The text a stored prompt image stands for: its ``data:`` URL, exactly as the prompt sent it.

    Raises:
        ValueError: The file is to hold the text itself, and is not UTF-8.
    """
    if ref.prefix is not None:
        return ref.prefix + base64.b64encode(data).decode("ascii")
    return data.decode("utf-8")


def _inline_images(messages: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """``(position, url)`` of every inline image in *messages*, in document order.

    Walked with a stack, not by recursion: how deep a prompt nests is the
    caller's business, and the walk must not be what fails on it.
    """
    found: list[tuple[tuple[str | int, ...], str]] = []
    stack: list[tuple[Any, tuple[str | int, ...]]] = [(messages, ())]
    while stack:
        value, at = stack.pop()
        if isinstance(value, list):
            stack.extend((item, (*at, index)) for index, item in reversed(list(enumerate(value))))
        elif isinstance(value, dict):
            if at and at[-1] == "image_url":
                url = value.get("url")
                if isinstance(url, str) and _BASE64_DATA_URL.match(url):
                    found.append(((*at, "url"), url))
            stack.extend(
                (item, (*at, key))
                for key, item in reversed(list(value.items()))
                if isinstance(key, str)
            )
    return found


def _stored_image(url: str) -> tuple[dict[str, Any], TraceFile] | None:
    """The reference that stands for the inline image *url*, and its file; ``None``: keep it inline."""
    match = _BASE64_DATA_URL.match(url)
    if match is None:
        return None
    prefix, payload = url[: match.end()], url[match.end() :]
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError:  # binascii.Error, or a payload that is not ASCII
        data = b""
    if data and base64.b64encode(data).decode("ascii") == payload:
        rel_path = prompt_image_path(sha256_hex(data), image_suffix(data) or _UNKNOWN_IMAGE_SUFFIX)
        file = TraceFile(rel_path, data, content_addressed=True)
    else:
        try:
            text = url.encode("utf-8")
        except UnicodeEncodeError:
            return None
        rel_path = prompt_image_path(sha256_hex(text), _TEXT_SUFFIX)
        prefix = None
        file = TraceFile(rel_path, text, content_addressed=True)
    ref = PromptImageRef(stored_image=rel_path, prefix=prefix)
    return ref.model_dump(mode="json"), file


def _replaced(root: Any, replacements: list[tuple[tuple[str | int, ...], Any]]) -> Any:
    """*root* with each position set to its value — copying only the containers on the way."""
    copies: set[int] = set()

    def own(container: Any) -> Any:
        copy = list(container) if isinstance(container, list) else dict(container)
        copies.add(id(copy))
        return copy

    root = own(root)
    for at, value in replacements:
        holder = root
        for key in at[:-1]:
            child = holder[key]
            if id(child) not in copies:
                child = own(child)
                holder[key] = child
            holder = child
        holder[at[-1]] = value
    return root


def _locate(messages: Any, at: Any) -> tuple[Any, str | int]:
    """The container *at* — keys and indices from *messages* — ends in, and its last key.

    Raises:
        ValueError: *at* is not a non-empty list, or it leads nowhere.
    """
    if not isinstance(at, list) or not at:
        raise ValueError(f"{at!r} is not a position: a non-empty list of keys and indices")
    holder = messages
    for key in at[:-1]:
        holder = holder[_key_in(holder, key, at)]
    return holder, _key_in(holder, at[-1], at)


def _key_in(holder: Any, key: Any, at: list[Any]) -> str | int:
    if isinstance(holder, list) and type(key) is int and 0 <= key < len(holder):
        return key
    if isinstance(holder, dict) and isinstance(key, str) and key in holder:
        return key
    raise ValueError(f"the position {at!r} leads nowhere in messages (at {key!r})")


# ── environment evidence ──────────────────────────────────────


def encode_archive_file(
    file: Any,
    *,
    step: int | None = None,
    trailing: int | None = None,
    run_version: int | None = None,
) -> TraceFile:
    """One ``ArchiveFile`` as the extra file a commit carries.

    Read by its documented fields — ``archiver_id``, ``name``,
    ``media_type``, ``data``, ``content_addressed``, ``scope`` and the
    provenance ``meta`` (``captured_at``, ``cycle``, ``ids``) — rather than
    by type, for the reason ``TraceNegotiation.from_results`` is typed
    ``Any``: the trace layer imports nothing of ``core/archive`` but its
    privacy rules, so the WAL's interface stays ``TraceFile``.

    Where it lands:

    - a run record (``scope == "run"``) at
      :func:`archive_run_path` ``(capability_id, run_version)`` — the
      version is the caller's to assign (a committed path is immutable);
    - a content-addressed file at :func:`archive_blob_path` of its data's
      sha256, written once per run however many commits list it;
    - anything else under the commit that takes it: exactly one of *step*
      (:func:`archive_step_path`) or *trailing* (:func:`archive_trailing_path`).

    The extra-file index entry's ``meta`` is ``{ARCHIVE_META_KEY:
    ArchiveMeta(...)}`` in JSON form; ``content_addressed`` is copied.

    Raises:
        ValueError: An unknown scope; a run record without a version, or a
            content-addressed one; a version on a file that is not a run
            record; both *step* and *trailing*, or neither where the path
            needs one.
    """
    if step is not None and trailing is not None:
        raise ValueError("an archive file is committed by one commit: pass step or trailing")
    archiver_id: str = file.archiver_id
    name: str = file.name
    scope = file.scope
    content_addressed = bool(file.content_addressed)
    data: bytes = file.data
    if scope == "run":
        if run_version is None:
            raise ValueError(f"run record {archiver_id}/{name} needs its run_version")
        if content_addressed:
            raise ValueError(f"run record {archiver_id}/{name} cannot be content-addressed")
        rel_path = archive_run_path(archiver_id.partition(".")[0], run_version)
    elif scope == "step":
        if run_version is not None:
            raise ValueError(
                f"{archiver_id}/{name} is step evidence; only a run record has a run_version"
            )
        if content_addressed:
            rel_path = archive_blob_path(sha256_hex(data))
        elif step is not None:
            rel_path = archive_step_path(step, archiver_id, name)
        elif trailing is not None:
            rel_path = archive_trailing_path(trailing, archiver_id, name)
        else:
            raise ValueError(
                f"{archiver_id}/{name}: pass the step or trailing commit that takes it"
            )
    else:
        raise ValueError(f"{archiver_id}/{name}: unknown archive scope {scope!r}")

    provenance = file.meta
    meta = ArchiveMeta(
        archiver_id=archiver_id,
        name=name,
        media_type=file.media_type,
        scope=scope,
        captured_at=provenance.captured_at,
        cycle=provenance.cycle,
        ids=dict(provenance.ids),
        run_version=run_version,
    )
    return TraceFile(
        rel_path,
        data,
        content_addressed=content_addressed,
        meta={ARCHIVE_META_KEY: meta.model_dump(mode="json")},
    )
