"""``browser.storage`` — cookies and Web Storage at the step boundary (§4.14).

Read over the recorder connection at collection time; cookie values stored as
their digest (with a ledger entry each) unless session state is allowed;
storage items through the shared value rule; a state written only when it
changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from spectragent.core.archive.privacy import REDACTED
from spectragent.core.capabilities.browser.archive import StorageArchiver
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
            return {
                "frameTree": {
                    "frame": {"id": "F0", "securityOrigin": root},
                    "childFrames": [
                        {"frame": {"id": f"F{i}", "securityOrigin": o}}
                        for i, o in enumerate(children, 1)
                    ],
                }
            }
        if method == "DOMStorage.getDOMStorageItems":
            storage_id = (params or {})["storageId"]
            store = self.local if storage_id["isLocalStorage"] else self.session
            return {
                "entries": [[k, v] for k, v in store.get(storage_id["securityOrigin"], {}).items()]
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


async def test_cookie_values_are_hashed_with_a_ledger_entry_each() -> None:
    session, _ = _session()
    document = _document(await session.collect())
    (cookie,) = document["cookies"]
    assert cookie["value"] == "sha256:" + hashlib.sha256(b"planted-cookie-value").hexdigest()
    assert {k: cookie[k] for k in ("name", "domain", "path", "httpOnly")} == {
        "name": "sid",
        "domain": "shop.test",
        "path": "/",
        "httpOnly": True,
    }
    assert {"location": "/cookies/0/value", "field": "sid", "rule": "value_hash"} in document[
        "redaction"
    ]["entries"]
    assert document["redaction"]["policy"] == "af-privacy-1+browser-1"


async def test_allow_session_state_keeps_raw_values() -> None:
    session, _ = _session(allow_session_state=True)
    document = _document(await session.collect())
    assert document["cookies"][0]["value"] == "planted-cookie-value"
    assert not [e for e in document["redaction"]["entries"] if e["rule"] == "value_hash"]


async def test_storage_items_pass_the_shared_value_rule() -> None:
    session, _ = _session()
    document = _document(await session.collect())
    (local,) = [entry for entry in document["local"] if entry["origin"] == "http://shop.test"]
    assert local["items"] == {"cart": "2 items", "auth_token": REDACTED}
    assert any(
        e["field"] == "auth_token" and e["rule"] == "field_name"
        for e in document["redaction"]["entries"]
    )
    assert "planted-token" not in json.dumps(document)


async def test_local_storage_once_per_origin_session_storage_per_tab() -> None:
    session, recorder = _session()
    document = _document(await session.collect())
    assert [entry["origin"] for entry in document["local"]] == [
        "http://ads.test",
        "http://shop.test",
    ]
    assert {(e["tab"], e["origin"]) for e in document["session"]} == {
        ("PAGE", "http://shop.test"),
        ("PAGE2", "http://shop.test"),
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
    assert document["unreadable_tabs"] == [{"tab": "PAGE2", "error": "No target with given id"}]


def test_declarations() -> None:
    archiver = StorageArchiver()
    assert archiver.archiver_id == "browser.storage"
    assert archiver.needs == frozenset()
    # Web Storage values are kept whatever the cookie setting (see below).
    assert "contains_session_state" in archiver.privacy_markers
    assert "contains_session_state" in StorageArchiver(allow_session_state=True).privacy_markers
    assert archiver.redaction_facts() == {"cookie_values": "sha256"}


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


async def test_nothing_is_left_out_silently() -> None:
    """A failed origin read and an out-of-process frame are listed, never just absent."""
    session, recorder = _session()
    frame = recorder.add_target("frame-s", "FRAME1", "iframe")
    original = _Recorder.send

    async def send(
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        storage_id = (params or {}).get("storageId") or {}
        if storage_id.get("securityOrigin") == "http://ads.test":
            raise RuntimeError("Frame not found for the given storage id")
        return await original(recorder, method, params, session_id, timeout=timeout)

    recorder.send = send  # type: ignore[method-assign]
    document = _document(await session.collect())
    failed = {(f["tab"], f["origin"], f["storage"]) for f in document["unreadable_origins"]}
    assert failed == {
        ("PAGE", "http://ads.test", "local"),
        ("PAGE", "http://ads.test", "session"),
        ("PAGE2", "http://ads.test", "local"),  # tried again until one tab reads it
        ("PAGE2", "http://ads.test", "session"),
    }
    assert {f["error"] for f in document["unreadable_origins"]} == {
        "Frame not found for the given storage id"
    }
    assert [x["origin"] for x in document["local"]] == ["http://shop.test"]
    assert document["unread_frames"] == [{"target": frame.target_id, "url": frame.url}]


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
        if storage_id.get("securityOrigin") == "http://ads.test" and session_id == "page-s":
            raise RuntimeError("busy")
        return await original(recorder, method, params, session_id, timeout=timeout)

    recorder.send = send  # type: ignore[method-assign]
    document = _document(await session.collect())
    assert {x["origin"]: x["items"] for x in document["local"]}["http://ads.test"] == {"seen": "1"}
    assert [(f["tab"], f["storage"]) for f in document["unreadable_origins"]] == [
        ("PAGE", "session")
    ]
