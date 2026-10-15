"""Freeze and hydrate the browser's initial cookie and Web Storage state.

Hydration uses temporary empty documents, fulfilled locally through Fetch. They
establish Chrome's real storage partitions without executing application code or
issuing a network request. No init script remains installed after hydration.
"""

import asyncio
import base64
import contextlib
import hashlib
import html
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.commands import BrowserConnection
from spectragent.schemas.browser_storage import BrowserStorageState, StorageArea

INITIAL_STATE_NAME = "initial_state.json"


@dataclass(frozen=True)
class StorageSeed:
    data: bytes
    origin_coverage: Literal["fresh", "seed", "observed_frames"] = "seed"

    @classmethod
    def freeze(cls, source: BrowserStorageState | str) -> "StorageSeed":
        state = (
            BrowserStorageState.model_validate_json(Path(source).expanduser().read_bytes())
            if isinstance(source, str)
            else source
        )
        if state.unavailable:
            raise ValueError("storage_state contains unavailable storage; it cannot seed a run")
        if any(cookie.get("partitionKeyOpaque") for cookie in state.cookies):
            raise ValueError("opaque cookie partitions cannot seed a reproducible browser context")
        return cls(canonical_json(state.model_dump(mode="json")))

    @property
    def state(self) -> BrowserStorageState:
        return BrowserStorageState.model_validate_json(self.data)

    def fingerprint(self) -> dict[str, Any]:
        state = self.state
        return {
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "coverage": state.coverage,
            "nonempty": bool(state.cookies or state.local or state.session or state.tabs),
            "origin_coverage": self.origin_coverage,
        }


def read_initial_seed(reader: ArchiveReader) -> StorageSeed:
    """Read the immutable pre-navigation seed, never a post-action snapshot."""
    run = reader.archive_run_file("browser")
    if run is None:
        raise ValueError("replay requires the browser fingerprint and initial storage seed")
    fingerprint = json.loads(reader.read_extra(run.path)).get("initial_storage")
    if not fingerprint or fingerprint.get("origin_coverage") == "observed_frames":
        raise ValueError(
            "replay requires complete initial storage coverage; persistent-profile origins are unknown"
        )
    for _, entry in reader.iter_archive("browser.storage", include_trailing=True):
        if entry.name == INITIAL_STATE_NAME:
            seed = StorageSeed.freeze(
                BrowserStorageState.model_validate_json(reader.read_extra(entry.path))
            )
            seed = StorageSeed(seed.data, fingerprint["origin_coverage"])
            if seed.fingerprint() != fingerprint:
                raise ValueError("initial storage seed disagrees with browser fingerprint")
            return seed
    # Fresh profiles may omit the storage archiver only when their seed is empty.
    empty = StorageSeed(canonical_json(BrowserStorageState().model_dump(mode="json")), "fresh")
    if empty.fingerprint() == fingerprint:
        return empty
    raise ValueError("replay archive does not contain its initial_state.json")


class _Hydration:
    def __init__(self, cdp: BrowserConnection) -> None:
        self.cdp = cdp
        self.sessions: set[str] = set()
        self.roots: set[str] = set()
        self.frame_sessions: dict[str, str] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.documents: dict[str, str] = {}
        self.errors: list[BaseException] = []

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.done)

    def done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.errors.append(error)

    def attached(self, params: dict[str, Any], parent: str | None = None) -> None:
        if parent in self.sessions:
            self.frame_sessions[params["targetInfo"]["targetId"]] = params["sessionId"]
            self.spawn(self.prepare(params["sessionId"], resume=True))

    def paused(self, params: dict[str, Any], sid: str | None = None) -> None:
        if sid in self.sessions:
            self.spawn(self.fulfill(params, sid))

    async def fulfill(self, params: dict[str, Any], sid: str) -> None:
        document = self.documents.get(params["request"]["url"])
        if document is None:
            await self.cdp.send(
                "Fetch.failRequest",
                {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                sid,
            )
            return  # favicon and speculative requests are blocked, never continued
        await self.cdp.send(
            "Fetch.fulfillRequest",
            {
                "requestId": params["requestId"],
                "responseCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                "body": base64.b64encode(document.encode()).decode(),
            },
            sid,
        )

    async def prepare(self, sid: str, *, resume: bool = False) -> None:
        self.sessions.add(sid)
        if not resume:
            self.roots.add(sid)
        # Queue all commands before releasing a waiting OOPIF.
        replies = []
        try:
            for method, params in (
                ("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}),
                (
                    "Target.setAutoAttach",
                    {"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": True},
                ),
            ):
                replies.append(await self.cdp.post(method, params, sid))
        finally:
            if resume:
                replies.append(await self.cdp.post("Runtime.runIfWaitingForDebugger", {}, sid))
        try:
            for reply in replies:
                await reply.wait(10)
        finally:
            for reply in replies:
                reply.abandon()

    async def frame(self, sid: str, area: StorageArea, expected_url: str) -> tuple[str, str]:
        """Find the live frame carrying the requested *actual* storage key."""
        for _ in range(200):
            if self.errors:
                raise self.errors[0]
            for session in sorted(self.sessions, key=lambda item: item == sid):
                try:
                    tree = await self.cdp.send("Page.getFrameTree", {}, session, timeout=1)
                    nodes = [tree["frameTree"]]
                    while nodes:
                        node = nodes.pop()
                        nodes.extend(node.get("childFrames") or [])
                        frame = node["frame"]
                        if (
                            frame.get("securityOrigin") != area.origin
                            or frame.get("url") != expected_url
                        ):
                            continue
                        # A parent tree may list an OOPIF, but its sessionStorage
                        # namespace belongs to that OOPIF's own target session.
                        owner = self.frame_sessions.get(frame["id"])
                        if owner is not None and owner != session:
                            continue
                        if frame["id"] != tree["frameTree"]["frame"]["id"]:
                            try:
                                info = await self.cdp.send(
                                    "Target.getTargetInfo", {"targetId": frame["id"]}
                                )
                                if info["targetInfo"]["type"] == "iframe":
                                    continue
                            except RuntimeError:
                                pass  # an in-process child has no target
                        result = await self.cdp.send(
                            "Storage.getStorageKey", {"frameId": frame["id"]}, session, timeout=1
                        )
                        if result["storageKey"] == area.storage_key:
                            return session, frame["id"]
                except RuntimeError:
                    continue  # an OOPIF belongs to its own session
            await asyncio.sleep(0.01)
        raise ValueError(f"could not establish storage partition {area.storage_key!r}")

    async def area(self, sid: str, area: StorageArea, *, local: bool) -> None:
        origins = [*reversed(area.ancestor_origins), area.origin]
        nonce = uuid.uuid4().hex
        urls = [
            f"{origin}/__af_storage_seed__/{nonce}/{index}" for index, origin in enumerate(origins)
        ]
        self.documents = {
            url: (
                f'<iframe src="{html.escape(urls[i + 1], quote=True)}"></iframe>'
                if i + 1 < len(urls)
                else "<!doctype html>"
            )
            for i, url in enumerate(urls)
        }
        await self.cdp.send("Page.navigate", {"url": urls[0]}, sid, timeout=10)
        owner, _ = await self.frame(sid, area, urls[-1])
        storage_id = {"storageKey": area.storage_key, "isLocalStorage": local}
        await self.cdp.send("DOMStorage.clear", {"storageId": storage_id}, owner, timeout=10)
        for key, value in area.items.items():
            await self.cdp.send(
                "DOMStorage.setDOMStorageItem",
                {"storageId": storage_id, "key": key, "value": value},
                owner,
                timeout=10,
            )
        await self.cdp.send("Page.navigate", {"url": "about:blank"}, sid, timeout=10)
        for _ in range(100):
            tree = await self.cdp.send("Page.getFrameTree", {}, sid)
            if tree["frameTree"]["frame"].get("url") == "about:blank":
                break
            await asyncio.sleep(0.01)
        else:
            raise ValueError("storage hydration did not retire its synthetic document")

    async def close(self) -> None:
        # Drain already admitted pauses before Fetch.disable could release them.
        if self.tasks:
            await asyncio.wait(list(self.tasks), timeout=1)
        self.cdp.off_event("Fetch.requestPaused", self.paused)
        self.cdp.off_event("Target.attachedToTarget", self.attached)
        failures: list[Exception] = []
        for sid in list(self.sessions):
            for method, params in (
                ("Fetch.disable", {}),
                (
                    "Target.setAutoAttach",
                    {"autoAttach": False, "flatten": True, "waitForDebuggerOnStart": False},
                ),
            ):
                try:
                    await self.cdp.send(method, params, sid, timeout=1)
                except Exception as exc:
                    if sid in self.roots:
                        failures.append(exc)
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for sid in list(self.sessions):
            try:
                await self.cdp.send("Target.detachFromTarget", {"sessionId": sid}, timeout=1)
            except Exception as exc:
                if sid in self.roots:
                    failures.append(exc)
        if failures:
            raise ExceptionGroup("storage hydration cleanup could not be confirmed", failures)


async def _hydrate_storage(
    cdp: BrowserConnection, seed: StorageSeed, browser_context_id: str | None
) -> dict[str, str]:
    """Return logical-tab → target ids; retain only successful about:blank tabs."""
    state = seed.state
    tabs: dict[str, str] = {}
    hydration = _Hydration(cdp)
    cdp.on_event("Fetch.requestPaused", hydration.paused)
    cdp.on_event("Target.attachedToTarget", hydration.attached)
    success = False
    failure: BaseException | None = None
    try:
        cookies = []
        writable = {
            "name",
            "value",
            "url",
            "domain",
            "path",
            "secure",
            "httpOnly",
            "sameSite",
            "expires",
            "priority",
            "sameParty",
            "sourceScheme",
            "sourcePort",
            "partitionKey",
        }
        for cookie in state.cookies:
            cookies.append(
                {
                    k: v
                    for k, v in cookie.items()
                    if k in writable and not (k == "expires" and v <= 0)
                }
            )
        if cookies:
            await cdp.send(
                "Storage.setCookies",
                {
                    "cookies": cookies,
                    **({"browserContextId": browser_context_id} if browser_context_id else {}),
                },
            )
        declared = [tab.tab for tab in state.tabs] or ["tab-0"]
        for logical in declared:
            result = await cdp.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    **({"browserContextId": browser_context_id} if browser_context_id else {}),
                },
            )
            tabs[logical] = result["targetId"]
            attached = await cdp.send(
                "Target.attachToTarget", {"targetId": tabs[logical], "flatten": True}
            )
            sid = attached["sessionId"]
            await hydration.prepare(sid)
            if logical == declared[0]:
                for area in state.local:
                    await hydration.area(sid, area, local=True)
            for area in state.session:
                if area.tab == logical:
                    await hydration.area(sid, area, local=False)
        success = True
        return tabs
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await hydration.close()
        except BaseException as exc:
            success = False
            if failure is None:
                raise
            failure.add_note(f"storage hydration cleanup also failed: {exc!r}")
        finally:
            if not success:
                for target in tabs.values():
                    with contextlib.suppress(Exception):
                        await cdp.send("Target.closeTarget", {"targetId": target}, timeout=1)


async def capture_profile_initial(
    cdp: BrowserConnection, browser_context_id: str | None
) -> StorageSeed:
    """Known frame state of a persistent profile; explicitly incomplete origin coverage."""
    from spectragent.core.capabilities.browser.recorder import RecorderTarget
    from spectragent.core.capabilities.browser.storage_capture import BrowserStorageCapture

    targets: dict[str, RecorderTarget] = {}
    try:
        reply = await cdp.send("Target.getTargets", {})
        for info in reply.get("targetInfos", []):
            if info.get("type") != "page" or info.get("browserContextId") != browser_context_id:
                continue
            attached = await cdp.send(
                "Target.attachToTarget", {"targetId": info["targetId"], "flatten": True}
            )
            sid = attached["sessionId"]
            targets[sid] = RecorderTarget(
                session_id=sid, target_id=info["targetId"], type="page", url=info.get("url", "")
            )
        capture = BrowserStorageCapture(cdp.send, lambda: targets, browser_context_id)
        state = await capture.read()
        return StorageSeed(canonical_json(state.model_dump(mode="json")), "observed_frames")
    finally:
        for sid in targets:
            with contextlib.suppress(Exception):
                await cdp.send("Target.detachFromTarget", {"sessionId": sid}, timeout=1)


async def hydrate_storage(
    cdp: BrowserConnection, seed: StorageSeed, browser_context_id: str | None
) -> dict[str, str]:
    """Bound initial hydration to 30 seconds, including cancellation-safe cleanup."""
    async with asyncio.timeout(30):
        return await _hydrate_storage(cdp, seed, browser_context_id)
