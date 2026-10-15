"""``browser.storage`` — cookies and Web Storage, read at each collection (§4.14).

At collection time — the step boundary, after the step's action — the
session reads, over the recorder connection:

- ``Storage.getCookies`` (browser level; ``Network.getAllCookies`` is
  deprecated);
- ``DOMStorage.getDOMStorageItems`` for every origin in the frame tree of
  every tab the recorder prepared — ``localStorage`` once per origin (it is
  shared by the origin's tabs), ``sessionStorage`` per tab and origin.

Nothing is left out silently: a tab gone before its read is listed in
``unreadable_tabs``, an origin whose read failed in ``unreadable_origins``,
and every out-of-process frame — a target of its own, absent from the tab's
frame tree, whose storage is partitioned by the top-level site — in
``unread_frames`` (not read in v1).

That the read happens at collection rather than at every observation cycle
is deliberate (``docs/architecture_design.md`` §4.14, ``browser.storage``): the collection point is
the step boundary already, and a per-cycle read would record the same state
again. ``storage.json`` is written only when the state differs from the last
one written (its capture time aside), so a reader takes the latest
``storage.json`` at or before a step as the state of that step.

Privacy (§5.6): values are stored as they are — every cookie (name, value,
domain, path, expiry, flags) and every local / session storage item. The
user's ruling of 2026-10-13 makes the trace and ``env/`` private to the
machine that recorded them; the gates are the prompt sent to a model
provider and dataset export, and ``env/`` is never exported. Session tokens
are therefore in this file (and in the WARC's ``Set-Cookie`` headers), so
the archiver always declares ``contains_session_state``. The file still
carries the browser ledger, empty: it names the policy it was written under.
"""

import logging
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import MARKER_SESSION_STATE
from spectragent.core.capabilities.browser.archive.base import (
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
)
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.archive.redaction import (
    MARKER_BROWSER_REDACTED,
    browser_ledger,
)

logger = logging.getLogger(__name__)

STORAGE_NAME = "storage.json"

#: Cookie fields stored as CDP reports them (the value is always stored, as a string).
_COOKIE_FIELDS = (
    "name",
    "domain",
    "path",
    "expires",
    "size",
    "httpOnly",
    "secure",
    "session",
    "sameSite",
    "priority",
    "sourceScheme",
    "sourcePort",
    "partitionKey",
)


class StorageArchiver(BrowserArchiverBase):
    """Cookies and local / session storage at every step boundary."""

    __registry_key__ = "browser.storage"
    # No capture channel: everything is read at collection.
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset()

    def __init__(
        self,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept recording a browser that did not start from a fresh profile "
                    "(logged-in pages). Cookie and Web Storage values are stored as they "
                    "are either way."
                ),
            ),
        ] = False,
        command_timeout_s: Annotated[
            float,
            Field(gt=0, description="Seconds each storage read on the recorder may take."),
        ] = 1.0,
    ) -> None:
        self.allow_session_state = allow_session_state
        self.command_timeout_s = command_timeout_s

    @property
    def privacy_markers(self) -> frozenset[str]:
        # Cookie and Web Storage values are stored as they are: the evidence
        # carries session state.
        return frozenset({MARKER_BROWSER_REDACTED, MARKER_SESSION_STATE})

    def open(self, ctx: BrowserArchiveContext) -> "StorageSession":
        return StorageSession(self, ctx)


def _origins(frame_tree: Any) -> list[str]:
    """The distinct http(s) security origins of a ``Page.getFrameTree`` result, root first."""
    origins: list[str] = []
    stack = [(frame_tree or {}).get("frameTree")]
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        origin = str((node.get("frame") or {}).get("securityOrigin") or "")
        if origin.startswith(("http://", "https://")) and origin not in origins:
            origins.append(origin)
        stack.extend(node.get("childFrames") or [])
    return origins


class StorageSession(BrowserArchiveSession):
    """Reads the storage state at each collection and writes it when it changed."""

    archiver: StorageArchiver

    def __init__(self, archiver: StorageArchiver, ctx: BrowserArchiveContext) -> None:
        super().__init__(archiver, ctx)
        #: Canonical bytes of the last state written (capture time excluded).
        self._last: bytes | None = None

    async def collect(self) -> list[ArchiveFile]:
        captured_at = self.ctx.clock()
        state = await self._read()
        key = canonical_json(state)
        if key == self._last:
            return []
        self._last = key
        document = {
            "captured_at": captured_at,
            **state,
            "redaction": browser_ledger([]).model_dump(mode="json"),
        }
        return [
            ArchiveFile(
                archiver_id=self.archiver.archiver_id,
                name=STORAGE_NAME,
                media_type="application/json",
                data=canonical_json(document),
                meta=ArchiveProvenance(captured_at=captured_at),
            )
        ]

    async def _read(self) -> dict[str, Any]:
        timeout = self.archiver.command_timeout_s
        send = self.ctx.recorder.send
        reply = await send("Storage.getCookies", {}, None, timeout=timeout)
        cookies: list[dict[str, Any]] = []
        for raw in (reply or {}).get("cookies") or []:
            if not isinstance(raw, dict):
                continue
            cookie = {field: raw[field] for field in _COOKIE_FIELDS if field in raw}
            cookie["value"] = str(raw.get("value", ""))
            cookies.append(cookie)

        local: dict[str, dict[str, Any]] = {}
        session: list[dict[str, Any]] = []
        unreadable: list[dict[str, str]] = []
        #: Origins whose storage read failed: listed, never silently dropped.
        failed: list[dict[str, str]] = []
        for tab in self.ctx.tabs():
            try:
                tree = await send("Page.getFrameTree", {}, tab.session_id, timeout=timeout)
            except Exception as exc:  # the tab went away between listing and reading
                unreadable.append({"tab": tab.target_id, "error": str(exc)})
                continue
            for origin in _origins(tree):
                if origin not in local:
                    items, error = await self._items(tab.session_id, origin, local=True)
                    if items is not None:
                        local[origin] = items
                    else:
                        failed.append(
                            {
                                "tab": tab.target_id,
                                "origin": origin,
                                "storage": "local",
                                "error": error,
                            }
                        )
                items, error = await self._items(tab.session_id, origin, local=False)
                if items:
                    session.append({"tab": tab.target_id, "origin": origin, "items": items})
                elif items is None:
                    failed.append(
                        {
                            "tab": tab.target_id,
                            "origin": origin,
                            "storage": "session",
                            "error": error,
                        }
                    )
        # A local read that failed in one tab and succeeded in another was read.
        failed = [f for f in failed if not (f["storage"] == "local" and f["origin"] in local)]
        # Out-of-process frames are targets of their own: their origins are not
        # in the tab's frame tree, and their storage is partitioned by the top-level
        # site, which this document has no key for yet. Listed, not read.
        unread_frames = sorted(
            {
                (target.target_id, target.url)
                for target in self.ctx.recorder.targets().values()
                if target.type == "iframe"
            }
        )

        return {
            "cookies": cookies,
            "local": [{"origin": origin, "items": local[origin]} for origin in sorted(local)],
            "session": session,
            "unreadable_tabs": unreadable,
            "unreadable_origins": failed,
            "unread_frames": [{"target": target, "url": url} for target, url in unread_frames],
        }

    async def _items(
        self, session_id: str, origin: str, *, local: bool
    ) -> tuple[dict[str, str] | None, str]:
        """The storage items, or ``None`` and why they could not be read."""
        try:
            reply = await self.ctx.recorder.send(
                "DOMStorage.getDOMStorageItems",
                {"storageId": {"securityOrigin": origin, "isLocalStorage": local}},
                session_id,
                timeout=self.archiver.command_timeout_s,
            )
        except Exception as exc:
            logger.debug(
                "browser.storage: %s storage of %s unreadable: %s", origin, session_id, exc
            )
            return None, str(exc) or type(exc).__name__
        items: dict[str, str] = {}
        for entry in (reply or {}).get("entries") or []:
            if isinstance(entry, list) and len(entry) == 2:
                items[str(entry[0])] = str(entry[1])
        return items, ""
