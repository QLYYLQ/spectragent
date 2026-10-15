"""``browser.dom_raw`` — the raw DOM of every committed observation cycle (§4.12, §4.14).

Two sources, one output:

- **The cycle's own capture** (DOM negotiated). ``DOMWatchdog`` hands every
  raw capture it makes to ``WatchdogContext.raw_capture_sink``; the
  capability parks it on the observe cycle and commits it into the
  :class:`~.base.RawCaptureStore` only when the cycle succeeds. Nothing is
  captured again and the archive path sends **no** ``DOM.*`` command: backend
  node ids are assigned lazily, and a second capture could perturb the one
  the agent is served.
- **A self-capture** (no DOM negotiated — a vision-only agent such as
  UI-TARS). The session wants observation cycles: the ArchiveWatchdog
  delivers each tab's cycle into the archive scope, inside the cycle, and the
  session captures the tab once on the recorder connection with the same
  capture function BrowserUse uses (``dom/capture.py``: ``DOM.getDocument``,
  ``DOMSnapshot`` with paint order, the accessibility tree, the document
  stamp) plus the DPR. It parks the result on the cycle the same way, so it
  is committed only with a successful cycle. It returns nothing to the cycle:
  its document stamp never re-fires it, and the observation is the same with
  archiving on or off. The cost is one full DOM capture per step; frames of
  other processes (out-of-process iframes) are not followed.

Each committed capture becomes one gzip JSON file, content-addressed (the same
page across steps is stored once), carrying the capture redacted
(``archive/redaction.redact_raw_capture``), the DPR, the viewport, the cleaner
key, the tab and the document stamp; the cycle and capture time are its
provenance. With the Skyvern cleaner the capture is the page script's raw
output plus ``DOM.describeNode`` identities, redacted the same way.
"""

import asyncio
import gzip
import json
from collections.abc import Callable, Mapping
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.capabilities.browser.archive.base import (
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    RawCapture,
)
from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.archive.redaction import (
    browser_ledger,
    redact_raw_capture,
)
from spectragent.core.capabilities.browser.dom.capture import capture_target
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
)
from spectragent.core.capabilities.browser.dom.js.skyvern_capture import INTEGER_KEYED_MAPS
from spectragent.core.capabilities.browser.dom.models import TargetCapture
from spectragent.core.capabilities.browser.recorder import CycleRequest

#: The fragment whose presence means the cycle captures the DOM itself.
DOM_FRAGMENT = "browser.dom"


class DomRawArchiver(BrowserArchiverBase):
    """The raw DOM capture of every committed observation cycle."""

    __registry_key__ = "browser.dom_raw"
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset({"raw_dom", "observation_cycle"})

    def __init__(
        self,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept recording a browser that did not start from a fresh profile "
                    "(logged-in pages). Sensitive form values are withheld either way."
                ),
            ),
        ] = False,
        self_capture_timeout_s: Annotated[
            float,
            Field(
                gt=0,
                description=(
                    "Seconds a vision-only run's self-capture of one tab may take. It runs "
                    "inside the observation cycle, so it bounds what archiving can add to "
                    "a step; a capture that times out fails the next collection."
                ),
            ),
        ] = 10.0,
    ) -> None:
        self.allow_session_state = allow_session_state
        self.self_capture_timeout_s = self_capture_timeout_s

    def open(self, ctx: BrowserArchiveContext) -> "DomRawSession":
        return DomRawSession(self, ctx)


class _RecorderCommands:
    """The recorder connection in the shape ``capture_target`` sends through."""

    def __init__(self, send: Callable[..., Any], timeout: float) -> None:
        self._send = send
        self._timeout = timeout

    @property
    def is_connected(self) -> bool:
        return True

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        return await self._send(method, params, session_id, timeout=self._timeout)

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        raise NotImplementedError("a dom_raw self-capture subscribes to no event")

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        raise NotImplementedError("a dom_raw self-capture subscribes to no event")


class DomRawSession(BrowserArchiveSession):
    """Turns committed raw captures into archive files; self-captures when DOM is not negotiated."""

    archiver: DomRawArchiver

    def __init__(self, archiver: DomRawArchiver, ctx: BrowserArchiveContext) -> None:
        super().__init__(archiver, ctx)
        self._failures: list[str] = []

    @property
    def wants_cycles(self) -> bool:
        return DOM_FRAGMENT not in self.ctx.fragments

    async def on_cycle(self, cycle: CycleRequest) -> None:
        if DOM_FRAGMENT in cycle.fragments:
            return  # the cycle captures the DOM itself; its sink brings it here
        session = self.ctx.recorder.session_for_target(cycle.target_id) if cycle.target_id else None
        if session is None:
            self._failures.append(f"no recorder session for tab {cycle.target_id!r}")
            return
        try:
            capture, dpr = await asyncio.wait_for(
                self._self_capture(session), self.archiver.self_capture_timeout_s
            )
        except Exception as exc:
            self._failures.append(f"self-capture of tab {cycle.target_id!r} failed: {exc!r}")
            return
        self.ctx.park_raw(cycle.control_session_id, capture, dpr)

    async def _self_capture(self, session: str) -> tuple[TargetCapture, float]:
        cdp = _RecorderCommands(self.ctx.recorder.send, self.archiver.self_capture_timeout_s)

        async def device_pixel_ratio() -> float:
            reply = await cdp.send(
                "Runtime.evaluate",
                {"expression": "window.devicePixelRatio || 1", "returnByValue": True},
                session,
            )
            value = ((reply or {}).get("result") or {}).get("value")
            return float(value) if isinstance(value, int | float) and value > 0 else 1.0

        capture, dpr = await asyncio.gather(
            capture_target(
                cdp,
                session,
                frames=None,
                js_click_listeners=False,
                required_computed_styles=REQUIRED_COMPUTED_STYLES,
            ),
            device_pixel_ratio(),
        )
        return capture, dpr

    async def collect(self) -> list[ArchiveFile]:
        if self._failures:
            failures, self._failures = self._failures, []
            raise RuntimeError(
                f"{self.archiver.archiver_id}: raw DOM missing for committed cycles: "
                + "; ".join(failures)
            )
        return [self._file(raw) for raw in self.ctx.raw_captures.drain()]

    def _file(self, raw: RawCapture) -> ArchiveFile:
        data, entries = redact_raw_capture(raw.capture)
        document = {
            "capture": data,
            "device_pixel_ratio": raw.device_pixel_ratio,
            "viewport": list(raw.viewport),
            "cleaner": raw.cleaner,
            "source": raw.source,
            "tab": raw.tab,
            "loader_id": raw.capture.loader_id,
            "url": raw.capture.url,
            "redaction": browser_ledger(entries).model_dump(mode="json"),
        }
        ids = {"tab": raw.tab}
        if raw.capture.loader_id:
            ids["loader_id"] = raw.capture.loader_id
        return ArchiveFile(
            archiver_id=self.archiver.archiver_id,
            name=f"dom-{raw.cycle}-{raw.tab[:16] or 'tab'}.json.gz",
            media_type="application/gzip",
            data=gzip_json(document),
            meta=ArchiveProvenance(captured_at=raw.captured_at, cycle=raw.cycle, ids=ids),
            content_addressed=True,
        )


def capture_from_document(capture: Mapping[str, Any]) -> TargetCapture:
    """Rebuild a ``TargetCapture`` from an archived document's ``capture``.

    The inverse of the archive's encoding (lists back to sets, frames back to
    captures, the Skyvern ref maps' string keys back to the integers the
    cleaners look up), so a cleaner can run offline on what a run recorded —
    a different cleaner or serializer, or occlusion from the paint order.
    """
    listeners = capture.get("js_click_listener_ids")
    raw_dom = dict(capture.get("raw_dom") or {})
    for key in INTEGER_KEYED_MAPS:
        refs = raw_dom.get(key)
        if isinstance(refs, Mapping):
            raw_dom[key] = {
                int(ref) if isinstance(ref, str) and ref.isdigit() else ref: value
                for ref, value in refs.items()
            }
    return TargetCapture(
        raw_dom=raw_dom,
        snapshot=dict(capture.get("snapshot") or {}),
        ax_tree=dict(capture.get("ax_tree") or {}),
        session_id=capture.get("session_id"),
        target_id=capture.get("target_id"),
        namespace=int(capture.get("namespace") or 0),
        js_click_listener_ids=set(listeners) if listeners is not None else None,
        frames={
            str(frame_id): capture_from_document(child)
            for frame_id, child in (capture.get("frames") or {}).items()
        },
        loader_id=capture.get("loader_id"),
        url=capture.get("url"),
    )


def read_dom_document(data: bytes) -> dict[str, Any]:
    """One archived ``browser.dom_raw`` file (gzip JSON) as a mapping."""
    return json.loads(gzip.decompress(data))
