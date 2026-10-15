"""``browser.storage`` — cookies and Web Storage at the step boundary (§4.14).

Read over the recorder connection at collection time; cookie and storage
values stored as they are (the user's 2026-10-13 ruling, §5.6), with an empty
ledger naming the policy; a state written only when it changed.
"""

from __future__ import annotations

import json
from typing import Any

from spectragent.core.capabilities.browser.archive import StorageArchiver
from spectragent.core.capabilities.browser.archive.redaction import BROWSER_REDACTION_POLICY
from spectragent.core.capabilities.browser.archive.storage import STORAGE_NAME, StorageSession
from tests._helpers.archive_recorder import ScriptedRecorder, open_session

COOKIE = {
    "name": "sid",
    "value": "planted-cookie-value",
    "domain": "shop.test",
    "path": "/",
    "expires": -1,
    "httpOnly": True,
    "secure": False,
    "session": True,
    "partitionKey": {"topLevelSite": "http://shop.test", "hasCrossSiteAncestor": False},
    "sourcePort": 80,
}


class _Recorder(ScriptedRecorder):
    """Answers the three reads a storage collection makes."""

    cookies: list[dict[str, Any]]
    local: dict[str, dict[str, str]]
    session: dict[str, dict[str, str]]
    origins: list[str]

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:  # noqa: E501
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.cookies]}
        if method == "Page.getFrameTree":
            root, *children = self.origins
            prefix = session_id or ""
            return {
                "frameTree": {
                    "frame": {"id": prefix + "F0", "securityOrigin": root},
                    "childFrames": [
                        {"frame": {"id": prefix + f"F{i}", "securityOrigin": o}}
                        for i, o in enumerate(children, 1)
                    ],
                }
            }
        if method == "Storage.getStorageKey":
            index = int((params or {})["frameId"].split("F")[-1])
            return {"storageKey": self.origins[index] + "/"}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 1}
        if method == "Runtime.evaluate":
            return {"result": {"value": []}}
        if method == "DOMStorage.getDOMStorageItems":
            storage_id = (params or {})["storageId"]
            store = self.local if storage_id["isLocalStorage"] else self.session
            return {
                "entries": [
                    [k, v] for k, v in store.get(storage_id["storageKey"].rstrip("/"), {}).items()
                ]
            }
        return {}


def _session(**knobs: Any) -> tuple[StorageSession, _Recorder]:
    recorder = _Recorder()
    recorder.cookies = [COOKIE]
    recorder.local = {"http://shop.test": {"cart": "2 items", "auth_token": "planted-token"}}
    recorder.session = {"http://shop.test": {"step": "1"}}
    recorder.origins = ["http://shop.test", "http://ads.test", "about:blank"]
    recorder.add_target("page-s", "PAGE")
    recorder.add_target("page2-s", "PAGE2")
    session, _ = open_session(StorageArchiver(**knobs), recorder)
    assert isinstance(session, StorageSession)
    return session, recorder


def _document(files: list[Any]) -> dict[str, Any]:
    (item,) = files
    assert item.name == STORAGE_NAME and item.scope == "step"
    return json.loads(item.data)


async def test_cookie_values_are_stored_as_they_are() -> None:
    session, _ = _session()
    document = _document(await session.collect())
    (cookie,) = document["cookies"]
    assert cookie["value"] == "planted-cookie-value"
    assert {k: cookie[k] for k in ("name", "domain", "path", "httpOnly")} == {
        "name": "sid",
        "domain": "shop.test",
        "path": "/",
        "httpOnly": True,
    }
    assert document["redaction"] == {"policy": BROWSER_REDACTION_POLICY, "entries": []}


async def test_allow_session_state_does_not_change_what_is_stored() -> None:
    """The knob only accepts a non-fresh profile (§5.6); the values are the same either way."""
    plain, _ = _session()
    allowed, _ = _session(allow_session_state=True)
    stored = [_document(await s.collect()) for s in (plain, allowed)]
    for document in stored:
        del document["captured_at"]
    assert stored[0] == stored[1]


async def test_storage_items_are_stored_as_they_are() -> None:
    session, _ = _session()
    document = _document(await session.collect())
    (local,) = [entry for entry in document["local"] if entry["origin"] == "http://shop.test"]
    assert local["items"] == {"cart": "2 items", "auth_token": "planted-token"}
    assert document["redaction"]["entries"] == []


async def test_local_storage_once_per_origin_session_storage_per_tab() -> None:
    session, recorder = _session()
    document = _document(await session.collect())
    assert [entry["origin"] for entry in document["local"]] == [
        "http://ads.test",
        "http://shop.test",
    ]
    assert {(e["tab"], e["origin"]) for e in document["session"]} == {
        ("tab-0", "http://shop.test"),
        ("tab-1", "http://shop.test"),
        ("tab-0", "http://ads.test"),
        ("tab-1", "http://ads.test"),
    }
    local_reads = [
        p
        for m, p, _ in recorder.sent
        if m == "DOMStorage.getDOMStorageItems" and p["storageId"]["isLocalStorage"]
    ]
    assert len(local_reads) == 2, "each origin's localStorage is read once, not once per tab"


async def test_written_only_when_the_state_changed() -> None:
    session, recorder = _session()
    assert await session.collect()
    assert await session.collect() == [], "an unchanged state is not handed over again"
    recorder.cookies = [{**COOKIE, "value": "rotated"}]
    assert await session.collect()


async def test_a_tab_that_went_away_is_listed_not_fatal() -> None:
    session, recorder = _session()

    async def send(
        method: str, params: Any = None, session_id: Any = None, *, timeout: Any = None
    ) -> Any:
        if method == "Page.getFrameTree" and session_id == "page2-s":
            raise RuntimeError("No target with given id")
        return await _Recorder.send(recorder, method, params, session_id, timeout=timeout)

    recorder.send = send  # type: ignore[method-assign]
    document = _document(await session.collect())
    assert document["unavailable"] == [
        {
            "target": "PAGE2",
            "frame": None,
            "operation": "frames",
            "error": "No target with given id",
        }
    ]


def test_declarations() -> None:
    archiver = StorageArchiver()
    assert archiver.archiver_id == "browser.storage"
    assert archiver.needs == frozenset()
    # Cookie and Web Storage values are stored as they are, whatever the knob.
    assert "contains_session_state" in archiver.privacy_markers
    assert "contains_session_state" in StorageArchiver(allow_session_state=True).privacy_markers
    # Nothing per instance is withheld, so nothing extra enters the fingerprint.
    assert archiver.redaction_facts() == {}


async def test_a_token_inside_a_json_value_is_kept_so_session_state_is_declared() -> None:
    """No credential-shaped key to catch it: the value is stored, and the marker says so."""
    session, recorder = _session()
    token = "eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOAD.sig"
    cached = json.dumps({"body": {"access_token": token}})
    recorder.local = {"http://shop.test": {"@@auth0spajs@@::cid::api::openid": cached}}
    document = _document(await session.collect())
    (shop,) = [x for x in document["local"] if x["origin"] == "http://shop.test"]
    assert shop["items"]["@@auth0spajs@@::cid::api::openid"] == cached
    assert "contains_session_state" in session.archiver.privacy_markers


async def test_failed_storage_is_explicit() -> None:
    session, recorder = _session()
    original = _Recorder.send

    async def send(
        method: str, params: Any = None, session_id: Any = None, *, timeout: Any = None
    ) -> Any:
        if (params or {}).get("storageId", {}).get("storageKey") == "http://ads.test/":
            raise RuntimeError("Frame not found for the given storage id")
        return await original(recorder, method, params, session_id, timeout=timeout)

    recorder.send = send
    document = _document(await session.collect())
    assert len(document["unavailable"]) == 2
    assert {f["error"] for f in document["unavailable"]} == {
        "Frame not found for the given storage id"
    }
    assert [x["origin"] for x in document["local"]] == ["http://shop.test"]


async def test_a_local_read_another_tab_completes_is_not_listed_as_failed() -> None:
    session, recorder = _session()
    recorder.local["http://ads.test"] = {"seen": "1"}
    original = _Recorder.send

    async def send(
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        storage_id = (params or {}).get("storageId") or {}
        if (
            str(storage_id.get("storageKey", "")).rstrip("/") == "http://ads.test"
            and session_id == "page-s"
        ):
            raise RuntimeError("busy")
        return await original(recorder, method, params, session_id, timeout=timeout)

    recorder.send = send  # type: ignore[method-assign]
    document = _document(await session.collect())
    assert {x["origin"]: x["items"] for x in document["local"]}["http://ads.test"] == {"seen": "1"}
    assert document["unavailable"][0]["target"] == "PAGE"
