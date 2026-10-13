"""Does the source run match this replay's configuration? (§4.14 "一致性检查与回放指纹").

Setup opens the source run's archive (``open_archive`` + ``open_replay``)
before the browser starts and folds :func:`consistency_problems` into the same
cooperation error as the configuration's own problems — a replay that would
not reproduce its recording never launches Chrome. Every line names both
sides: the source run's value and this run's.

Refused (before launch):

- the source has no environment fingerprint, or recorded no ``browser.network``
  evidence, or is itself a replay (it holds no WARC — replay the original
  recording);
- the replayability tier differs, or its parameters do (``base_time``
  excepted: ``from_archive`` is exactly what a replay changes), or the shim
  (``shim_sha256``, pinned);
- the viewport differs;
- the launch-flag digests differ — this run's computed from the provider's
  ``planned_launch_flags`` (tier + isolation switches) before launch, both
  sides without the network routing switches, so isolation is never part of
  the comparison (the cooperation check asserts it instead);
- ``base_time: from_archive`` and the source recorded no T0.

Tolerated, recorded as warnings in the replay fingerprint (after launch,
:func:`version_warnings`): another Chrome version (product or revision),
another browser user agent when the tier does not pin one (it carries the
version), another redaction policy (POSTs with credential fields may not key
alike). What could not be compared at all — a side without a launch-flag
digest — is a note (:func:`consistency_notes`). A ``none`` recording's time
zone, locale and DPR are measured host facts and are not compared.

.. note::
   No ``from __future__ import annotations`` — the browser replay package
   rule.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from spectragent.core.capabilities.browser.archive.fingerprint import flags_digest
from spectragent.core.capabilities.browser.archive.redaction import BROWSER_REDACTION_POLICY
from spectragent.core.capabilities.browser.replay.archive_index import (
    NETWORK_ARCHIVER,
    RecordedArchive,
)
from spectragent.core.capabilities.browser.replay.replayability import ReplayabilityProtocol

#: Tier parameters a replay may change: the clock's origin.
REPLAY_MAY_CHANGE = frozenset({"base_time"})


def replay_t0(recorded: RecordedArchive, replayability: ReplayabilityProtocol, now_ms: int) -> int:
    """T0 of this run's clock: the recording's under ``base_time: from_archive``, else *now_ms*.

    Raises:
        ValueError: ``from_archive`` and the source recorded no T0
            (:func:`consistency_problems` refuses that first).
    """
    if not replayability.needs_archive_clock:
        return now_ms
    if recorded.t0_ms is None:
        raise ValueError(f"source run {recorded.run_id!r} recorded no T0")
    return recorded.t0_ms


def _source(recorded: RecordedArchive) -> str:
    return f"source run {recorded.run_id!r}"


def _current_plan(replayability: ReplayabilityProtocol) -> dict[str, Any]:
    """This run's tier as a fingerprint records it (T0 and UA do not matter here)."""
    return replayability.plan(t0_ms=0, browser_user_agent="").fingerprint()


def consistency_problems(
    recorded: RecordedArchive,
    *,
    replayability: ReplayabilityProtocol,
    viewport: tuple[int, int],
    planned_launch_flags: Sequence[str] | None,
) -> list[str]:
    """Every way the source run's fingerprint and this configuration disagree, at once."""
    source = _source(recorded)
    fingerprint = recorded.fingerprint
    if fingerprint is None:
        return [
            f"{source} ↔ replay_archive: the source run has no environment fingerprint "
            f"(the browser's run record), so nothing says what environment it recorded"
        ]
    problems: list[str] = []
    archivers = (fingerprint.get("redaction") or {}).get("archivers") or {}
    if recorded.network_files == 0 or (
        isinstance(archivers, Mapping) and NETWORK_ARCHIVER not in archivers
    ):
        problems.append(
            f"{source} ↔ network_source: archive: the source run recorded no "
            f"{NETWORK_ARCHIVER} evidence; there is nothing to serve"
        )
    recorded_source = (fingerprint.get("network_source") or {}).get("source_id")
    if recorded_source not in (None, "live"):
        problems.append(
            f"{source} ↔ network_source: archive: the source run is itself a replay "
            f"(network_source {recorded_source!r}) and holds no exchanges of its own; "
            f"replay the run it replayed ({(fingerprint.get('network_source') or {}).get('source_run_id')!r})"
        )

    then = fingerprint.get("replayability") or {}
    now = _current_plan(replayability)
    if then.get("tier") != now.get("tier"):
        problems.append(
            f"{source} replayability {then.get('tier')!r} ↔ this run's replayability "
            f"{now.get('tier')!r}: the tier must be the one recorded"
        )
    else:
        old = {k: v for k, v in (then.get("params") or {}).items() if k not in REPLAY_MAY_CHANGE}
        new = {k: v for k, v in (now.get("params") or {}).items() if k not in REPLAY_MAY_CHANGE}
        for name in sorted(set(old) | set(new)):
            if old.get(name) != new.get(name):
                problems.append(
                    f"{source} replayability {name}={old.get(name)!r} ↔ this run's "
                    f"{name}={new.get(name)!r}"
                )
        if then.get("shim_sha256") != now.get("shim_sha256"):
            problems.append(
                f"{source} shim_sha256={then.get('shim_sha256')!r} ↔ this run's "
                f"shim_sha256={now.get('shim_sha256')!r}: the pinned shim changed since the "
                f"recording"
            )

    recorded_viewport = fingerprint.get("viewport") or {}
    wanted = {"width": viewport[0], "height": viewport[1]}
    if recorded_viewport and recorded_viewport != wanted:
        problems.append(
            f"{source} viewport {recorded_viewport.get('width')}x{recorded_viewport.get('height')} "
            f"↔ this run's viewport {viewport[0]}x{viewport[1]}"
        )

    recorded_digest = fingerprint.get("launch_flags_sha256")
    if recorded_digest and planned_launch_flags is not None:
        planned = flags_digest(planned_launch_flags)
        if planned != recorded_digest:
            problems.append(
                f"{source} launch_flags_sha256={recorded_digest} ↔ this run's planned launch "
                f"flags {planned}: the browser would start with other switches (routing "
                f"switches excluded on both sides)"
            )

    if replayability.needs_archive_clock and recorded.t0_ms is None:
        problems.append(
            f"{source} recorded no T0 (replayability {then.get('tier')!r}) ↔ this run's "
            f"base_time=from_archive"
        )
    return problems


def consistency_notes(
    recorded: RecordedArchive, *, planned_launch_flags: Sequence[str] | None
) -> list[str]:
    """What could not be compared (a side without a launch-flag digest), for the replay fingerprint."""
    fingerprint = recorded.fingerprint or {}
    if not fingerprint:
        return []
    if not fingerprint.get("launch_flags_sha256"):
        return [f"launch flags not compared: {_source(recorded)} recorded no launch-flag digest"]
    if planned_launch_flags is None:
        return ["launch flags not compared: the provider plans no launch flags"]
    return []


def version_warnings(
    recorded: RecordedArchive,
    *,
    browser_version: Mapping[str, Any],
    replayability: ReplayabilityProtocol,
) -> list[str]:
    """Mismatches a replay tolerates and records: Chrome version, unpinned UA, redaction policy."""
    fingerprint = recorded.fingerprint or {}
    if not fingerprint:
        return []
    source = _source(recorded)
    warnings: list[str] = []
    chrome = fingerprint.get("chrome") or {}
    for key in ("product", "revision"):
        then, now = chrome.get(key), browser_version.get(key)
        if then is not None and now is not None and then != now:
            warnings.append(f"Chrome {key} differs: {source} {then!r}, this run {now!r}")
    params = _current_plan(replayability).get("params") or {}
    if not params.get("user_agent"):
        then = (fingerprint.get("page") or {}).get("userAgent")
        now = browser_version.get("userAgent")
        if then is not None and now is not None and then != now:
            warnings.append(
                f"browser user agent differs (the tier does not pin one): {source} {then!r}, "
                f"this run {now!r}"
            )
    policy = (fingerprint.get("redaction") or {}).get("policy")
    if policy is not None and policy != BROWSER_REDACTION_POLICY:
        warnings.append(
            f"redaction policy differs: {source} {policy!r}, this run "
            f"{BROWSER_REDACTION_POLICY!r} (POST bodies with credential fields may key differently)"
        )
    return warnings
