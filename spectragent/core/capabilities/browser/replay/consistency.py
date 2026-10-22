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
- what becomes of a tab the focus leaves differs (``background_tabs``: a
  recording whose left tabs ran, replayed with them frozen, or the other way
  round), or the source recorded no ``background_tabs``;
- the launch-flag digests differ — this run's computed from the provider's
  ``planned_launch_flags`` (tier + isolation switches, and the tier's launch
  environment, ``TZ``) before launch, both sides without the network routing
  switches, so isolation is never part of the comparison (the cooperation
  check asserts it instead);
- the browser's defaults differ — the time zone and locale a frame no
  override reaches falls back to (``browser_defaults`` in the fingerprint,
  measured right after the recording's launch). Before launch, each value
  the provider reads from this host's OS (``planned_browser_defaults``) is
  compared; after launch (:func:`browser_default_problems`, in setup before
  the recorder prepares any target) Chrome's own answer is compared, which
  covers what the OS could not say (the locale off macOS). A source that
  recorded no defaults is refused;
- ``base_time: from_archive`` and the source recorded no T0;
- a ``pinned`` source that recorded no client hints (recorded before the
  user agent override carried the browser's own); and, after launch
  (:func:`client_hint_problems`, in setup before the recorder prepares any
  target), when the tier pins a user agent, client hints other than the
  recording's — the identifier is compared before launch as a parameter, the
  hints the browser stands behind it with only once it runs.

Tolerated, recorded as warnings in the replay fingerprint (after launch,
:func:`version_warnings`): another Chrome version (product or revision),
another browser user agent, and other client hints, when the tier does not
pin a user agent (both carry the version), another redaction policy (POSTs
with credential fields may not key alike). What could not be compared at
all — a side without a launch-flag digest — is a note
(:func:`consistency_notes`). A ``none`` recording's DPR is a measured host
fact and is not compared; its time zone and locale are the browser's
defaults and are.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from spectragent.core.capabilities.browser.archive.fingerprint import flags_digest
from spectragent.core.capabilities.browser.archive.redaction import BROWSER_REDACTION_POLICY
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.client_hints import ClientHints
from spectragent.core.capabilities.browser.color_scheme import ColorScheme, color_scheme_from_record
from spectragent.core.capabilities.browser.platform import InputPlatform, input_platform_from_record
from spectragent.core.capabilities.browser.replay.archive_index import (
    NETWORK_ARCHIVER,
    RecordedArchive,
)
from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityPlan,
    ReplayabilityProtocol,
)

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


def replay_color_scheme(recorded: RecordedArchive) -> ColorScheme:
    """The source recording's media theme; a missing fact is never a light default."""
    then = (recorded.fingerprint or {}).get("replayability") or {}
    value = then.get("color_scheme")
    scheme = color_scheme_from_record(value)
    if scheme is None:
        raise ValueError(
            f"source run {recorded.run_id!r} replayability.color_scheme={value!r} ↔ "
            "pinned replay: the source must record a measured 'light' or 'dark' theme; "
            "record the run again"
        )
    return scheme


def _source(recorded: RecordedArchive) -> str:
    return f"source run {recorded.run_id!r}"


def input_platform_problems(
    recorded: RecordedArchive, measured: InputPlatform | None = None
) -> list[str]:
    """A missing source platform is refused; a measured replay must match it."""
    value = (recorded.fingerprint or {}).get("input_platform")
    platform = input_platform_from_record(value)
    if platform is None:
        return [
            f"{_source(recorded)} input_platform={value!r} ↔ browser replay: no supported measured input platform was recorded; record the run again"
        ]
    if measured is not None and measured != platform:
        return [
            f"{_source(recorded)} input_platform={platform!r} ↔ this browser input_platform={measured!r}: recorded key actions require the same actual platform"
        ]
    return []


def _current_plan(replayability: ReplayabilityProtocol) -> dict[str, Any]:
    """This run's tier as a fingerprint records it (T0 and UA do not matter here)."""
    return replayability.plan(t0_ms=0, browser_user_agent="").fingerprint()


#: Why the browser's defaults must match: what a frame no override reaches reads.
_DEFAULTS_WHY = (
    "a frame no override reaches (an iframe whose process lost the frame that set the "
    "overrides, a cross-site popup that keeps its opener) reads the browser's default"
)


def _defaults_problems(
    source: str,
    recorded: BrowserDefaults,
    current: BrowserDefaults,
    *,
    how: str,
    every_value: bool,
) -> list[str]:
    problems: list[str] = []
    for name in BrowserDefaults.NAMES:
        then, now = recorded.value(name), current.value(name)
        if now is None and not every_value:
            continue  # not known before launch: compared after launch
        if then != now:
            problems.append(
                f"{source} browser default {name}={then!r} ↔ this browser's default "
                f"{name}={now!r} ({how}): {_DEFAULTS_WHY} {name}, so the replay would see "
                f"another one"
            )
    return problems


def browser_default_problems(recorded: RecordedArchive, measured: BrowserDefaults) -> list[str]:
    """The browser's defaults, measured right after this replay's launch, against the recording's.

    Run by setup before the recorder prepares any target. Every value is
    compared — this is where a value the OS could not say before launch
    (``planned_browser_defaults``) is decided.
    """
    fingerprint = recorded.fingerprint or {}
    then = BrowserDefaults.from_record(fingerprint.get("browser_defaults"))
    if then is None:
        return []  # refused before launch already (consistency_problems)
    return _defaults_problems(
        _source(recorded), then, measured, how="measured after launch", every_value=True
    )


def _hints_text(record: Any) -> str:
    """Recorded client hints, briefly: the ``Sec-CH-UA`` brands, platform and form."""
    hints = ClientHints.from_record(record)
    if hints is None:
        return "none"
    return (
        f"{hints.header() or 'no brands'} (platform {hints.platform!r}, "
        f"mobile {str(hints.mobile).lower()})"
    )


def client_hint_problems(recorded: RecordedArchive, plan: ReplayabilityPlan) -> list[str]:
    """The client hints this replay's user agent override carries, against the recording's.

    Run by setup right after it read the browser's own client hints, before
    the recorder prepares any target. Only when the tier pins a user agent:
    the browser's own identifier and hints may differ with its version, and
    :func:`version_warnings` records that. A source without the record was
    refused before launch (:func:`consistency_problems`).
    """
    replayability = (recorded.fingerprint or {}).get("replayability") or {}
    if "client_hints" not in replayability:
        return []
    if not (replayability.get("params") or {}).get("user_agent"):
        return []
    then = replayability.get("client_hints")
    now = plan.client_hints.as_metadata() if plan.client_hints is not None else None
    if then == now:
        return []
    return [
        f"{_source(recorded)} client hints {_hints_text(then)} ↔ this run's client hints "
        f"{_hints_text(now)}: the pinned user agent would stand behind other client hints than "
        f"the recording's (navigator.userAgentData, Sec-CH-UA)"
    ]


def consistency_problems(
    recorded: RecordedArchive,
    *,
    replayability: ReplayabilityProtocol,
    viewport: tuple[int, int],
    background_tabs: str,
    planned_launch_flags: Sequence[str] | None,
    browser_defaults: BrowserDefaults | None = None,
    expected_platform: InputPlatform | None = None,
) -> list[str]:
    """Every way the source run's fingerprint and this configuration disagree, at once.

    *browser_defaults* is what this host's OS says the browser will fall back
    to (``planned_browser_defaults``); a value it does not know is compared
    after launch instead (:func:`browser_default_problems`).
    """
    source = _source(recorded)
    fingerprint = recorded.fingerprint
    if fingerprint is None:
        return [
            f"{source} ↔ replay_archive: the source run has no environment fingerprint "
            f"(the browser's run record), so nothing says what environment it recorded"
        ]
    problems: list[str] = []
    problems.extend(input_platform_problems(recorded))
    source_platform = input_platform_from_record(fingerprint.get("input_platform"))
    if (
        expected_platform is not None
        and source_platform is not None
        and source_platform != expected_platform
    ):
        problems.append(
            f"{source} input_platform={source_platform!r} ↔ expected_platform={expected_platform!r}: the configured expectation contradicts the recording"
        )
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

    # A frozen tab runs nothing until the focus returns, so a replay whose
    # left tabs run (or freeze) where the recording's froze (or ran) neither
    # asks for what was recorded nor shows what the agent saw coming back.
    recorded_tabs = fingerprint.get("background_tabs")
    if recorded_tabs is None:
        problems.append(
            f"{source} recorded no background_tabs ↔ this run's background_tabs="
            f"{background_tabs!r}: the source predates the setting; record it again"
        )
    elif recorded_tabs != background_tabs:
        problems.append(
            f"{source} background_tabs={recorded_tabs!r} ↔ this run's background_tabs="
            f"{background_tabs!r}: a tab the focus left must be treated as it was recorded"
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

    recorded_defaults = BrowserDefaults.from_record(fingerprint.get("browser_defaults"))
    if recorded_defaults is None:
        problems.append(
            f"{source} recorded no browser defaults (the time zone and locale a frame no "
            f"override reaches falls back to) ↔ network_source: archive: nothing says the "
            f"replay's browser would fall back to the same ones; record the run again"
        )
    elif browser_defaults is not None:
        problems.extend(
            _defaults_problems(
                source,
                recorded_defaults,
                browser_defaults,
                how="read from this host before launch",
                every_value=False,
            )
        )

    if replayability.needs_archive_clock and recorded.t0_ms is None:
        problems.append(
            f"{source} recorded no T0 (replayability {then.get('tier')!r}) ↔ this run's "
            f"base_time=from_archive"
        )
    if then.get("tier") == now.get("tier") != "none" and "client_hints" not in then:
        problems.append(
            f"{source} recorded no client hints (recorded before the user agent override "
            f"carried the browser's own) ↔ replayability {now.get('tier')!r}: this run's pages "
            f"would read other client hints than the recording's; record the run again"
        )
    if replayability.needs_color_scheme and then.get("tier") == now.get("tier"):
        try:
            replay_color_scheme(recorded)
        except ValueError as exc:
            problems.append(str(exc))
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
    client_hints: ClientHints | None = None,
) -> list[str]:
    """Mismatches a replay tolerates and records: Chrome version, unpinned UA and hints, redaction policy.

    *client_hints* are the ones this run's plan carries.
    """
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
        recorded_tier = fingerprint.get("replayability") or {}
        if "client_hints" in recorded_tier:
            then_hints = recorded_tier.get("client_hints")
            now_hints = client_hints.as_metadata() if client_hints is not None else None
            if then_hints != now_hints:
                warnings.append(
                    f"browser client hints differ (the tier does not pin a user agent): "
                    f"{source} {_hints_text(then_hints)}, this run {_hints_text(now_hints)}"
                )
    policy = (fingerprint.get("redaction") or {}).get("policy")
    if policy is not None and policy != BROWSER_REDACTION_POLICY:
        warnings.append(
            f"redaction policy differs: {source} {policy!r}, this run "
            f"{BROWSER_REDACTION_POLICY!r} (POST bodies with credential fields may key differently)"
        )
    return warnings
