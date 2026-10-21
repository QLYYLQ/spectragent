"""Finding recorded runs under a directory and reading them through ``RunTrace``.

A run is a directory holding a ``manifest.json`` that ``RunTrace`` reads; the
walk does not descend into a run (its ``env/`` holds archived web content) and
skips exported datasets, whose manifest lists samples. What a run is (a
recording, an offline replay, a branch) and which run a branch came from are
read from the recorded configuration and provenance, never from paths.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lookahead.rows import FrameRow, RunKind
from spectragent.core.capabilities.browser.archive.frames import iter_frames, read_frame_image
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    ScreenshotObs,
)
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.models import StepManifestEntry, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import task_identity, unreadable_reason
from spectragent.schemas.step_record import StepRecord

#: The task-setup key a replaying run records its source directory under.
REPLAY_ARCHIVE_KEY = "replay_archive"
SCREENSHOT_KEY = "browser.screenshot"
CAPTURE_STATE_KEY = "browser.capture_state"
DOM_RAW_ARCHIVER_ID = "browser.dom_raw"


class LookaheadInputError(ValueError):
    """The command's input cannot be used as given (a path, a root, an option)."""


def find_run_dirs(root: Path) -> list[Path]:
    """Every directory under *root* (itself included) holding a ``manifest.json``, sorted."""
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        path = Path(current)
        if "manifest.json" in files:
            if not _is_dataset(path / "manifest.json"):
                found.append(path)
            directories.clear()
            continue
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".")
            and name not in {"__pycache__", "node_modules"}
            and not (path / name).is_symlink()
        )
    return sorted(found)


def _is_dataset(manifest: Path) -> bool:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "samples" in data and "exporter" in data


def relative_key(path: Path, viewer_root: Path) -> str:
    """*path* as a run key relative to the viewer root (POSIX), or a refusal naming both."""
    resolved, base = path.resolve(), viewer_root.resolve()
    if not resolved.is_relative_to(base):
        raise LookaheadInputError(
            f"{resolved} is not under the viewer root {base}: queue items name runs "
            f"relative to the root the viewer is started with (--viewer-root)"
        )
    return resolved.relative_to(base).as_posix() or "."


def network_sources(config: TraceConfig) -> set[str]:
    """Every ``network_source.source_id`` the run's composed configuration names."""
    found: set[str] = set()
    for environment in config.resolved.get("environment") or []:
        if not isinstance(environment, dict):
            continue
        for capability in environment.get("capabilities") or []:
            source = (
                (capability or {}).get("network_source") if isinstance(capability, dict) else None
            )
            if isinstance(source, dict) and isinstance(source.get("source_id"), str):
                found.add(source["source_id"])
    return found


def run_kind(config: TraceConfig) -> RunKind:
    """``branch`` (it records branch provenance), ``replay`` (its network came from an archive), or ``record``.

    The network source is read from the composed configuration. A run composed
    without one (no ``resolved`` configuration) is a replay when its recorded
    task setup names the archive it replayed.
    """
    if config.branch is not None:
        return "branch"
    sources = network_sources(config)
    if sources:
        return "replay" if "archive" in sources else "record"
    setup = config.task_setup
    if setup is not None and REPLAY_ARCHIVE_KEY in setup.context:
        return "replay"
    return "record"


def site_of(config: TraceConfig) -> str | None:
    """The host of the (source) task's start URL, or ``None`` when none was recorded."""
    start_url = task_identity(config).start_url
    if not start_url:
        return None
    host = urlsplit(start_url).hostname
    return host or None


@dataclass
class Run:
    """One readable run: its key relative to the viewer root, and its trace."""

    key: str
    path: Path
    trace: RunTrace
    _screenshots: dict[int, ScreenshotObs | None] = field(default_factory=dict, repr=False)

    @cached_property
    def config(self) -> TraceConfig:
        return self.trace.config

    @cached_property
    def kind(self) -> RunKind:
        return run_kind(self.config)

    @cached_property
    def run_id(self) -> str:
        return self.trace.run_id

    @cached_property
    def site(self) -> str | None:
        return site_of(self.config)

    @cached_property
    def records(self) -> dict[int, StepRecord]:
        return {record.step: record for record in self.trace.step_records}

    @cached_property
    def entries(self) -> dict[int, StepManifestEntry]:
        return {entry.step: entry for entry in self.trace.manifest.steps}

    @cached_property
    def complete_through(self) -> int:
        return self.trace.manifest.complete_through

    @cached_property
    def closed(self) -> bool:
        return self.trace.manifest.status != "writing"

    @cached_property
    def env_fragments(self) -> set[str] | None:
        negotiation = self.config.negotiation
        return set(negotiation.env_sourced_fragments) if negotiation is not None else None

    def steps(self) -> list[int]:
        return sorted(self.records)

    def has_observation(self, step: int) -> bool:
        """Whether *step* recorded an environment observation (at least one fragment read)."""
        entry = self.entries.get(step)
        if entry is None:
            return False
        env = self.env_fragments
        for key, files in entry.fragments.items():
            is_env = key in env if env is not None else not key.startswith("loop.")
            if is_env and not files.failed:
                return True
        return False

    def screenshot(self, step: int) -> ScreenshotObs | None:
        """*step*'s screenshot fragment without its image bytes, or ``None``.

        Kept per run, so the image bytes are dropped: read them with
        :meth:`screenshot_bytes` when they are needed.
        """
        if step not in self._screenshots:
            fragment = self._fragment(step, SCREENSHOT_KEY)
            self._screenshots[step] = (
                fragment.model_copy(update={"image_bytes": None})
                if isinstance(fragment, ScreenshotObs)
                else None
            )
        return self._screenshots[step]

    def has_screenshot(self, step: int) -> bool:
        """Whether *step* committed a screenshot image."""
        entry = self.entries.get(step)
        files = entry.fragments.get(SCREENSHOT_KEY) if entry is not None else None
        return files is not None and not files.failed and files.artifact is not None

    def screenshot_bytes(self, step: int) -> bytes | None:
        try:
            return self.trace.screenshot(step)
        except RecordingScopeError:
            return None

    def capture_state(self, step: int) -> BrowserCaptureStateObs | None:
        fragment = self._fragment(step, CAPTURE_STATE_KEY)
        return fragment if isinstance(fragment, BrowserCaptureStateObs) else None

    def _fragment(self, step: int, key: str) -> Any:
        entry = self.entries.get(step)
        if entry is None or key not in entry.fragments or entry.fragments[key].failed:
            return None
        try:
            return self.trace.observation_fragment(step, key)
        except RecordingScopeError:
            return None

    def raw_dom(self, step: int) -> bool:
        return bool(self.trace.archive_files(step, DOM_RAW_ARCHIVER_ID))

    @cached_property
    def frames(self) -> list[FrameRow]:
        """Every ``browser.frames`` record of the run, in commit order."""
        rows: list[FrameRow] = []
        for batch_step, record in iter_frames(self.trace, include_trailing=True):
            row = FrameRow(batch_step=batch_step, **asdict(record))
            row._record = record
            rows.append(row)
        return rows

    def frame_image(self, frame: FrameRow) -> bytes:
        """The PNG of a captured frame of this run."""
        assert frame._record is not None, "frames of a run carry the reader's record"
        return read_frame_image(self.trace, frame.batch_step, frame._record)

    @cached_property
    def cycles(self) -> dict[int, int | None]:
        """The observation cycle of each step's observation.

        The capability numbers observation cycles itself; the step number is
        not one. Read, in order of preference, from the ``browser.dom_raw``
        files the step's commit took (the raw DOM of the step's observation),
        from the ``browser.frames`` frames taken before the step's actions
        (each names the cycle it follows), and from a delayed frame requested
        between this observation's capture and the next one's.
        """
        cycles: dict[int, int | None] = {}
        frames = self.frames
        steps = self.steps()
        for position, step in enumerate(steps):
            cycle = _max_cycle(
                record.cycle for record in self.trace.archive_files(step, DOM_RAW_ARCHIVER_ID)
            )
            if cycle is None:
                cycle = _max_cycle(
                    frame.cycle
                    for frame in frames
                    if frame.batch_step == step and frame.kind == "before_action"
                )
            if cycle is None:
                cycle = self._cycle_by_time(step, steps[position + 1 :], frames)
            cycles[step] = cycle
        return cycles

    def _cycle_by_time(self, step: int, later: list[int], frames: list[FrameRow]) -> int | None:
        if not frames:
            return None
        shot = self.screenshot(step)
        if shot is None:
            return None
        start = shot.captured_at
        following = (self.screenshot(other) for other in later)
        upcoming = next((other for other in following if other is not None), None)
        end = upcoming.captured_at if upcoming is not None else float("inf")
        found = {
            frame.cycle
            for frame in frames
            if frame.kind == "after_observation"
            and frame.requested_at is not None
            and start <= frame.requested_at < end
        }
        return found.pop() if len(found) == 1 else None

    def frames_after(self, step: int) -> list[FrameRow]:
        """The frames that follow *step*'s observation (same cycle), in commit order."""
        cycle = self.cycles.get(step)
        if cycle is None:
            return []
        return [frame for frame in self.frames if frame.cycle == cycle]


def _max_cycle(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


@dataclass
class RunSet:
    """The readable runs under a root, and why each other run directory was left out."""

    root: Path
    viewer_root: Path
    runs: list[Run] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @cached_property
    def by_run_id(self) -> dict[str, list[Run]]:
        found: dict[str, list[Run]] = {}
        for run in self.runs:
            found.setdefault(run.run_id, []).append(run)
        return found

    def source_of(self, run: Run) -> Run | None:
        """A branch run's source run among these runs, by its recorded provenance.

        The source's run id first (when exactly one run carries it); among
        several, the one whose path ends with the recorded ``source_trace``.
        ``None`` when the source is not among the runs read.
        """
        branch = run.config.branch
        if branch is None:
            return None
        candidates = [
            other for other in self.by_run_id.get(branch.source_run_id, []) if other is not run
        ]
        candidates = [other for other in candidates if other.kind != "branch"] or candidates
        if len(candidates) == 1:
            return candidates[0]
        suffix = branch.source_trace.strip("/")
        matching = [
            other
            for other in candidates
            if suffix and (other.path.as_posix().rstrip("/").endswith("/" + suffix))
        ]
        return matching[0] if len(matching) == 1 else None


def open_runs(root: Path, viewer_root: Path | None = None) -> RunSet:
    """Every readable run under *root*, keyed relative to *viewer_root* (default: *root*)."""
    root = root.expanduser()
    if not root.is_dir():
        raise LookaheadInputError(f"--root {root} is not a directory")
    viewer = (viewer_root or root).expanduser()
    if not viewer.is_dir():
        raise LookaheadInputError(f"--viewer-root {viewer} is not a directory")
    relative_key(root, viewer)  # refuse a root outside the viewer root up front
    result = RunSet(root=root, viewer_root=viewer)
    for directory in find_run_dirs(root):
        key = relative_key(directory, viewer)
        try:
            run = Run(key=key, path=directory, trace=RunTrace(directory))
            run.config  # noqa: B018 — read now, so an unreadable configuration is skipped here
            run.records  # noqa: B018
        except RecordingScopeError as exc:
            result.skipped[key] = str(exc)
            continue
        except TraceSchemaError as exc:
            result.skipped[key] = unreadable_reason(directory, exc)
            continue
        result.runs.append(run)
    return result
