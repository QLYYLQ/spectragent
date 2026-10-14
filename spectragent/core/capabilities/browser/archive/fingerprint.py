"""The browser's environment fingerprint, ``env/run.browser.json`` (§4.14).

A run record owned by the capability (``archiver_id="browser"``,
``scope="run"``): what a replay must match to call itself the same
environment. Built at each collection from the run's handles and emitted when
it differs from the last one emitted — the first collection always, later
ones only on change (the trace hook versions a changed record).

It holds:

- **Chrome** — ``Browser.getVersion`` (product, revision, protocol, JS
  version).
- **What the page sees** — user agent and UA client hints metadata, time zone,
  locale and languages, DPR: the ``pinned`` tier's own parameters and the
  client hints its override carries (the browser's own, read after launch;
  ``None`` when it carries none), or, at ``none``, measured in the first tab
  on the recorder connection (the control connection is never used for
  evidence) — in an isolated world of the tab's
  main frame (:data:`PROBE_WORLD`), so the page's scripts can neither observe
  the probe nor change what it reads, and once per document (``loaderId``):
  a collection on a document already measured costs one ``Page.getFrameTree``.
  The UA metadata (``navigator.userAgentData``) exists in secure contexts
  only; the last one measured is kept, so a tab on plain http does not flap it.
- **The browser's defaults** (``browser_defaults``) — the time zone and
  locale a frame no override reaches falls back to, measured by the
  capability right after launch, in the first tab before the recorder
  prepared it (``backends/browser_defaults.py``). Under ``pinned`` the time
  zone is the pinned one (``TZ`` at launch) and the locale the host's; a
  replay whose browser falls back to others is refused.
- **The viewport** as configured.
- **The tier** — ``ReplayabilityPlan.fingerprint()``: tier, parameters, T0,
  ``shim_sha256`` and the tier's own launch switches, kept apart from the
  rest of the command line so replay can compare them on their own.
- **Launch flags** — the sha256 of ``ProviderSession.metadata["launch_flags"]``
  (volatile values already replaced by placeholders) without the network
  routing switches (``chrome_switches.NETWORK_ROUTING_SWITCHES``): a
  recording's fixture ``MAP`` rules and a replay's isolation switches stay
  out, so the two sides compare (M30); never the command line itself.
- **The network source** (M30) — ``live``: ``{"source_id": "live"}``; a
  replaying run: its server's block (source run, digests, matcher, policy,
  ``shift_dates``, isolation, warnings).
- **Redaction** — :data:`~.redaction.BROWSER_REDACTION_POLICY` and each bound
  archiver's own facts (which request headers it strips, whether cookie values
  are hashed).
- **Privacy** — ``contains_session_state``, ``fresh_profile``, the evidence
  privacy ``level`` (``session_state`` when any bound archiver keeps session
  state, else ``redacted``) and the bound archivers' markers.

.. note::
   No ``from __future__ import annotations`` — the archive package rule.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import MARKER_SESSION_STATE
from spectragent.core.capabilities.browser.archive.base import BrowserArchiverBase, RecorderHandle
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.archive.redaction import BROWSER_REDACTION_POLICY
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    without_routing_switches,
)
from spectragent.core.capabilities.browser.client_hints import ClientHints
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.recorder import RecorderTarget

logger = logging.getLogger(__name__)

FINGERPRINT_NAME = "fingerprint.json"

#: The isolated world the ``none`` probe runs in (``Page.createIsolatedWorld``).
PROBE_WORLD = "af-fingerprint"

#: Measured in a page at ``none``: what that page's scripts see.
PAGE_PROBE = (
    "JSON.stringify({"
    "userAgent: navigator.userAgent,"
    "userAgentData: navigator.userAgentData ? {brands: navigator.userAgentData.brands,"
    " mobile: navigator.userAgentData.mobile, platform: navigator.userAgentData.platform} : null,"
    "timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,"
    "locale: navigator.language,"
    "languages: navigator.languages,"
    "devicePixelRatio: window.devicePixelRatio"
    "})"
)


def flags_digest(flags: Sequence[str]) -> str:
    """sha256 of launch flags without the network routing switches.

    One function for both sides of a replay: the recording's flags (its
    fixture ``MAP`` rules dropped) and the replay's planned ones (its
    isolation switches dropped) — that isolation is on is asserted by the
    cooperation check, not by the digest.
    """
    return hashlib.sha256(canonical_json(without_routing_switches(flags))).hexdigest()


def launch_flags_digest(metadata: Mapping[str, Any] | None) -> str | None:
    """:func:`flags_digest` of the provider's placeholder-normalised launch flags, if any."""
    flags = (metadata or {}).get("launch_flags")
    if flags is None:
        return None
    return flags_digest([str(flag) for flag in flags])


class BrowserFingerprint:
    """Builds the fingerprint and remembers the last one emitted."""

    def __init__(self, capability_id: str = "browser") -> None:
        self.capability_id = capability_id
        self._last: bytes | None = None
        #: ``navigator.userAgentData`` exists in secure contexts only, so a
        #: tab on plain http reports none: the last one measured is kept, or
        #: the fingerprint would flap with the page the first tab is on.
        self._ua_data: Any = None
        #: The last document probed — (recorder session, loaderId) — and what
        #: the probe read there: a document is measured once.
        self._probed: tuple[tuple[str, str], dict[str, Any]] | None = None

    async def collect(
        self,
        *,
        recorder: RecorderHandle,
        tabs: Sequence[RecorderTarget],
        plan_fingerprint: Mapping[str, Any],
        viewport: tuple[int, int],
        session_metadata: Mapping[str, Any] | None,
        archivers: Sequence[BrowserArchiverBase],
        fresh_profile: bool,
        clock: Callable[[], float],
        network_fingerprint: Mapping[str, Any] | None = None,
        browser_defaults: BrowserDefaults | None = None,
        input_platform: InputPlatform | None = None,
        timeout: float = 1.0,
    ) -> ArchiveFile | None:
        """The fingerprint as a run record, or ``None`` when it did not change."""
        document = await self.measure(
            recorder=recorder,
            tabs=tabs,
            plan_fingerprint=plan_fingerprint,
            viewport=viewport,
            session_metadata=session_metadata,
            archivers=archivers,
            fresh_profile=fresh_profile,
            network_fingerprint=network_fingerprint,
            browser_defaults=browser_defaults,
            input_platform=input_platform,
            timeout=timeout,
        )
        data = canonical_json(document)
        if data == self._last:
            return None
        self._last = data
        return ArchiveFile(
            archiver_id=self.capability_id,
            name=FINGERPRINT_NAME,
            media_type="application/json",
            data=data,
            meta=ArchiveProvenance(captured_at=clock()),
            scope="run",
        )

    async def measure(
        self,
        *,
        recorder: RecorderHandle,
        tabs: Sequence[RecorderTarget],
        plan_fingerprint: Mapping[str, Any],
        viewport: tuple[int, int],
        session_metadata: Mapping[str, Any] | None,
        archivers: Sequence[BrowserArchiverBase],
        fresh_profile: bool,
        network_fingerprint: Mapping[str, Any] | None = None,
        browser_defaults: BrowserDefaults | None = None,
        input_platform: InputPlatform | None = None,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        version = await recorder.browser_version()
        params = dict(plan_fingerprint.get("params") or {})
        pinned = plan_fingerprint.get("tier") not in (None, "none")
        if pinned:
            hints = ClientHints.from_record(plan_fingerprint.get("client_hints"))
            page: dict[str, Any] = {
                "source": "replayability",
                "userAgent": params.get("user_agent") or version.get("userAgent"),
                "userAgentData": hints.low_entropy() if hints is not None else None,
                "timezone": params.get("timezone"),
                "locale": params.get("locale"),
                "devicePixelRatio": params.get("device_scale_factor"),
            }
        else:
            page = {"source": "measured", **await self._probe(recorder, tabs, timeout)}
            if page.get("userAgentData") is not None:
                self._ua_data = page["userAgentData"]
            elif "userAgent" in page:
                page["userAgentData"] = self._ua_data
        markers = sorted({marker for archiver in archivers for marker in archiver.privacy_markers})
        session_state = MARKER_SESSION_STATE in markers
        return {
            "chrome": {
                "product": version.get("product"),
                "revision": version.get("revision"),
                "protocolVersion": version.get("protocolVersion"),
                "jsVersion": version.get("jsVersion"),
            },
            "page": page,
            "browser_defaults": browser_defaults.as_record() if browser_defaults else None,
            "input_platform": input_platform,
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "replayability": dict(plan_fingerprint),
            "launch_flags_sha256": launch_flags_digest(session_metadata),
            "network_source": dict(network_fingerprint or {"source_id": "live"}),
            "redaction": {
                "policy": BROWSER_REDACTION_POLICY,
                "archivers": {
                    archiver.archiver_id: dict(archiver.redaction_facts()) for archiver in archivers
                },
            },
            "privacy": {
                "contains_session_state": session_state,
                "fresh_profile": fresh_profile,
                "level": "session_state" if session_state else "redacted",
                "markers": markers,
            },
        }

    async def _probe(
        self, recorder: RecorderHandle, tabs: Sequence[RecorderTarget], timeout: float
    ) -> dict[str, Any]:
        """What the first tab that answers reports, read in an isolated world.

        Never in the page's main world: page code could watch the reads
        (getter traps) or change their results (a patched ``JSON.stringify``
        or ``navigator``). One measurement per document — the values are the
        document's — so a quiet step costs one frame-tree read.
        """
        for tab in tabs:
            try:
                tree = await recorder.send("Page.getFrameTree", {}, tab.session_id, timeout=timeout)
                frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
                document = (tab.session_id, str(frame.get("loaderId") or ""))
                if self._probed is not None and document[1] and self._probed[0] == document:
                    return dict(self._probed[1])
                world = await recorder.send(
                    "Page.createIsolatedWorld",
                    {"frameId": frame["id"], "worldName": PROBE_WORLD},
                    tab.session_id,
                    timeout=timeout,
                )
                reply = await recorder.send(
                    "Runtime.evaluate",
                    {
                        "expression": PAGE_PROBE,
                        "returnByValue": True,
                        "contextId": (world or {})["executionContextId"],
                    },
                    tab.session_id,
                    timeout=timeout,
                )
                value = ((reply or {}).get("result") or {}).get("value")
                if isinstance(value, str):
                    measured = json.loads(value)
                    if isinstance(measured, dict):
                        self._probed = (document, dict(measured))
                        return measured
            except Exception as exc:  # this tab cannot answer; try the next
                logger.debug("fingerprint probe failed on %s: %s", tab.session_id, exc)
        return {"error": "no tab answered the fingerprint probe"}
