"""Self-test of ``tests/_helpers/fixture_site.py`` — no Chrome needed.

The fixture is the completeness oracle of every recorder / archive / replay
test, so what it serves and what it logs must be right on its own. Driven
with plain HTTP and WebSocket clients that never consult the environment's
proxy settings.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from websockets.sync.client import connect as ws_connect

from tests._helpers.fixture_site import (
    LARGE_BODY_BYTES,
    SCENARIOS,
    VIDEO_BYTES,
    FixtureSite,
)


@pytest.fixture
def site():
    with FixtureSite() as fixture:
        yield fixture


def _get(site: FixtureSite, path: str, *, server: str = "main", **kwargs) -> httpx.Response:
    port = site.main_port if server == "main" else site.cross_port
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        return client.get(f"http://127.0.0.1:{port}{path}", **kwargs)


class TestContent:
    def test_every_scenario_has_an_entry_url_and_expected_paths(self, site: FixtureSite) -> None:
        for scenario in SCENARIOS:
            assert site.url(scenario).startswith("http://")
            expected = site.expected(scenario)
            assert expected and all(p.startswith(f"/{scenario}/") for p in expected)

    def test_unknown_scenario_is_a_key_error(self, site: FixtureSite) -> None:
        with pytest.raises(KeyError, match="unknown fixture scenario"):
            site.url("nope")
        with pytest.raises(KeyError):
            FixtureSite.expected("nope")

    def test_every_expected_http_path_is_served(self, site: FixtureSite) -> None:
        for scenario in SCENARIOS:
            for path in site.expected(scenario):
                if path == "/websocket/echo":
                    continue
                server = (
                    "cross"
                    if scenario == "service_worker" or path.startswith("/oopif/frame")
                    else "main"
                )
                response = _get(site, path, server=server)
                assert response.status_code in (200, 204, 206, 301, 302), (path, response)

    def test_redirect_chain_is_302_then_301(self, site: FixtureSite) -> None:
        first = _get(site, "/redirect/hop1")
        second = _get(site, "/redirect/hop2")
        assert (first.status_code, first.headers["location"]) == (302, "/redirect/hop2")
        assert (second.status_code, second.headers["location"]) == (
            301,
            "/redirect/landing.html",
        )

    def test_pages_are_deterministic_and_request_no_favicon(self, site: FixtureSite) -> None:
        one = _get(site, "/oopif/page.html").content
        two = _get(site, "/oopif/page.html").content
        assert one == two
        assert b'rel="icon" href="data:,"' in one
        assert b"autofocus" not in one

    def test_large_bodies_exceed_the_inline_tier(self, site: FixtureSite) -> None:
        assert len(_get(site, "/large/static.js").content) > 5_000_000
        assert len(_get(site, "/large/data.bin").content) == LARGE_BODY_BYTES

    def test_range_is_answered_with_a_hand_written_206(self, site: FixtureSite) -> None:
        partial = _get(site, "/range_video/clip.webm", headers={"Range": "bytes=10-19"})
        assert partial.status_code == 206
        assert partial.headers["content-range"] == f"bytes 10-19/{VIDEO_BYTES}"
        assert len(partial.content) == 10
        open_ended = _get(site, "/range_video/clip.webm", headers={"Range": "bytes=0-"})
        assert open_ended.status_code == 206
        assert len(open_ended.content) == VIDEO_BYTES
        assert _get(site, "/range_video/clip.webm").status_code == 200

    def test_websocket_echoes(self, site: FixtureSite) -> None:
        with ws_connect(
            f"ws://127.0.0.1:{site.ws_port}/websocket/echo", proxy=None, compression=None
        ) as ws:
            ws.send("ping")
            assert ws.recv(timeout=2) == "ping"


class TestLogs:
    def test_request_log_records_method_host_headers_and_body_digest(
        self, site: FixtureSite
    ) -> None:
        body = b'{"query":"shoes"}'
        with httpx.Client(trust_env=False) as client:
            client.post(
                f"http://127.0.0.1:{site.main_port}/json_post/api",
                content=body,
                headers={"Content-Type": "application/json", "X-Probe": "1"},
            )
        (entry,) = site.requests_for("json_post")
        assert entry.method == "POST"
        assert entry.server == "main"
        assert entry.host == f"127.0.0.1:{site.main_port}"
        assert entry.body_sha256 == hashlib.sha256(body).hexdigest()
        assert entry.body_length == len(body)
        assert "x-probe" in entry.header_names and "content-type" in entry.header_names

    def test_websocket_handshake_is_logged(self, site: FixtureSite) -> None:
        with ws_connect(f"ws://127.0.0.1:{site.ws_port}/websocket/echo", proxy=None):
            pass
        (entry,) = site.requests_for("websocket")
        assert (entry.server, entry.path) == ("ws", "/websocket/echo")

    def test_tcp_accepts_are_counted_per_server(self, site: FixtureSite) -> None:
        before = site.accepts()
        _get(site, "/beacon/page.html")
        _get(site, "/oopif/frame.html", server="cross")
        after = site.accepts()
        assert after["main"] == before["main"] + 1
        assert after["cross"] == before["cross"] + 1

    def test_scenario_is_derived_from_the_path(self, site: FixtureSite) -> None:
        _get(site, "/beacon/page.html?x=1")
        _get(site, "/elsewhere")
        scenarios = [r.scenario for r in site.requests]
        assert scenarios == ["beacon", None]


class TestHostModes:
    def test_loopback_mode_uses_two_sites_and_needs_no_switches(self, site: FixtureSite) -> None:
        assert site.main_origin == f"http://127.0.0.1:{site.main_port}"
        assert site.cross_origin == f"http://localhost:{site.cross_port}"
        assert site.chrome_args() == []
        assert site.url("service_worker").startswith(f"http://localhost:{site.cross_port}/")

    def test_mapped_mode_names_hosts_and_maps_each_to_its_server(self) -> None:
        with FixtureSite(mapped=True) as mapped:
            assert mapped.main_origin == "http://shop.example.test"
            assert mapped.cross_origin == "http://ads.thirdparty.test"
            assert mapped.ws_origin == "ws://ws.example.test"
            args = mapped.chrome_args()
            assert args[-1] == "--no-proxy-server"
            assert args[0] == (
                "--host-resolver-rules="
                f"MAP shop.example.test 127.0.0.1:{mapped.main_port}, "
                f"MAP ads.thirdparty.test 127.0.0.1:{mapped.cross_port}, "
                f"MAP ws.example.test 127.0.0.1:{mapped.ws_port}"
            )
            # The service-worker page must stay a secure context.
            assert mapped.url("service_worker").startswith("http://localhost:")
            # Pages link the mapped names, so the browser asks for them.
            page = _get(mapped, "/oopif/page.html").content
            assert b"http://ads.thirdparty.test/oopif/frame.html" in page
            # A request under the mapped Host header is served and logged as such.
            response = _get(mapped, "/beacon/page.html", headers={"Host": "shop.example.test"})
            assert response.status_code == 200
            assert mapped.requests[-1].host == "shop.example.test"
