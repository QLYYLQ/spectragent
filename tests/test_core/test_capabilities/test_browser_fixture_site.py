"""Self-test of ``tests/_helpers/fixture_site.py`` — no Chrome needed.

The fixture is the completeness oracle of every recorder / archive / replay
test, so what it serves and what it logs must be right on its own. Driven
with plain HTTP and WebSocket clients that never consult the environment's
proxy settings.
"""

from __future__ import annotations

import hashlib
import re
import socket
import time

import httpx
import pytest
from websockets.sync.client import connect as ws_connect

from tests._helpers.fixture_site import (
    FLYWHEEL_BOXES,
    FLYWHEEL_CHALLENGE_HEADERS,
    FLYWHEEL_PASSWORD,
    FLYWHEEL_PASSWORD_PARTS,
    FLYWHEEL_SCENARIOS,
    FLYWHEEL_TOKEN,
    FORM_SCENARIOS,
    LARGE_BODY_BYTES,
    SCENARIOS,
    VIDEO_BYTES,
    FixtureSite,
    flywheel_point,
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


class TestFormPages:
    """M33's ``form_submit`` pages: the forms, and a result page echoing both methods."""

    def test_entry_and_expected_paths(self, site: FixtureSite) -> None:
        assert FORM_SCENARIOS == ("form_submit",)
        assert site.url("form_submit").endswith("/form_submit/get.html")
        assert FixtureSite.expected("form_submit") == [
            "/form_submit/get.html",
            "/form_submit/result.html",
        ]

    def test_the_forms(self, site: FixtureSite) -> None:
        get = _get(site, "/form_submit/get.html").text
        post = _get(site, "/form_submit/post.html").text
        enter = _get(site, "/form_submit/enter.html").text
        for page, method in ((get, "GET"), (post, "POST")):
            assert f'<form method="{method}" action="/form_submit/result.html">' in page
            assert 'id="item" name="item" type="text"' in page and 'value="kettle"' in page
            assert '<button id="add" type="submit">' in page
        # One text field and no button: Enter submits it (implicit submission).
        assert enter.count("<input") == 1 and "<button" not in enter
        assert 'id="query" name="query" type="text"' in enter

    def test_the_result_page_echoes_either_method(self, site: FixtureSite) -> None:
        got = _get(site, "/form_submit/result.html?query=blue+kettle").text
        assert '<span id="received">query=blue kettle</span>' in got and "by GET" in got
        with httpx.Client(trust_env=False) as client:
            posted = client.post(
                f"http://127.0.0.1:{site.main_port}/form_submit/result.html",
                content=b"item=kettle&note=%3Cb%3E",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).text
        assert '<span id="received">item=kettle, note=&lt;b&gt;</span>' in posted
        assert "by POST" in posted
        assert [(r.method, r.scenario) for r in site.requests_for("form_submit")] == [
            ("GET", "form_submit"),
            ("POST", "form_submit"),
        ]


class TestFlywheelPages:
    """M33's rehearsal sites: positioned controls, the time-origin URLs, the planted credentials."""

    def test_three_sites_on_their_own_hosts(self) -> None:
        with FixtureSite(mapped=True) as mapped:
            assert [mapped.url(s) for s in FLYWHEEL_SCENARIOS] == [
                "http://shop.example.test/flywheel_shop/start.html",
                "http://drift.example.test/flywheel_drift/start.html",
                "http://blocked.example.test/flywheel_blocked/start.html",
            ]
            for path in ("/flywheel_drift/start.html", "/flywheel_blocked/start.html"):
                assert _get(mapped, path).status_code in (200, 403)
            assert {r.scenario for r in mapped.requests} == {"flywheel_drift", "flywheel_blocked"}

    def test_the_controls_are_where_the_boxes_say(self, site: FixtureSite) -> None:
        start = _get(site, "/flywheel_shop/start.html").text
        results = _get(site, "/flywheel_shop/search.html?q=kettle").text
        for page, name in ((start, "query"), (start, "search"), (results, "product")):
            left, top, width, height = FLYWHEEL_BOXES[name]
            assert f'id="{name}"' in page
            assert f"left:{left}px;top:{top}px;width:{width}px;height:{height}px" in page
        assert flywheel_point("search") == (410, 116)
        assert "Results for kettle" in results
        assert '<form method="POST" action="/flywheel_shop/cart">' in results

    def test_time_origin_urls(self, site: FixtureSite) -> None:
        product = _get(site, "/flywheel_shop/product.html?id=7").text
        drift = _get(site, "/flywheel_drift/start.html").text
        assert "recommend.json?session=' + Math.floor(performance.timeOrigin)" in product
        assert "next.html?visit=' + Math.floor(performance.timeOrigin)" in drift

    def test_the_blocked_site_answers_a_challenge(self, site: FixtureSite) -> None:
        response = _get(site, "/flywheel_blocked/start.html")
        assert response.status_code == 403
        for name, value in FLYWHEEL_CHALLENGE_HEADERS:
            assert response.headers.get_list(name) == [value], name

    def test_no_response_holds_a_planted_credential(self, site: FixtureSite) -> None:
        login = _get(site, "/flywheel_shop/login.html").text
        assert all(f"'{part}'" in login for part in FLYWHEEL_PASSWORD_PARTS)
        for path in (
            "/flywheel_shop/login.html",
            "/flywheel_shop/api/me",
            "/flywheel_shop/site.css",
            "/flywheel_shop/start.html",
        ):
            body = _get(site, path).content
            assert FLYWHEEL_PASSWORD.encode() not in body and FLYWHEEL_TOKEN.encode() not in body
        with httpx.Client(trust_env=False) as client:
            reply = client.post(
                f"http://127.0.0.1:{site.main_port}/flywheel_shop/api/login",
                content=f"user=ann&password={FLYWHEEL_PASSWORD}".encode(),
            )
        assert FLYWHEEL_PASSWORD.encode() not in reply.content

    def test_the_cart_takes_a_post_only(self, site: FixtureSite) -> None:
        assert _get(site, "/flywheel_shop/cart").status_code == 405
        with httpx.Client(trust_env=False) as client:
            added = client.post(
                f"http://127.0.0.1:{site.main_port}/flywheel_shop/cart", content=b"item=7"
            )
        assert "Added item 7" in added.text


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

    def test_a_websocket_connection_counts_without_a_handshake(self, site: FixtureSite) -> None:
        """The ws server counts TCP accepts too: a socket that never sends a request is egress."""
        before = site.accepts()["ws"]
        socket.create_connection(("127.0.0.1", site.ws_port), timeout=2).close()
        deadline = time.monotonic() + 5
        while site.accepts()["ws"] == before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert site.accepts()["ws"] == before + 1
        assert site.requests_for("websocket") == [], "no handshake, nothing logged"
        with ws_connect(f"ws://127.0.0.1:{site.ws_port}/websocket/echo", proxy=None):
            pass
        assert site.accepts()["ws"] == before + 2, "a handshake is counted once"

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
                f"MAP ws.example.test 127.0.0.1:{mapped.ws_port}, "
                f"MAP drift.example.test 127.0.0.1:{mapped.main_port}, "
                f"MAP blocked.example.test 127.0.0.1:{mapped.main_port}"
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

    def test_the_egress_page_needs_no_mapping(self) -> None:
        """Served and linked by IP literal only; the hermetic recording lets loopback through."""
        with FixtureSite(mapped=True) as mapped:
            assert mapped.url("egress") == f"http://127.0.0.1:{mapped.main_port}/egress/page.html"
            page = _get(mapped, "/egress/page.html").text
            hosts = re.findall(r"(?:https?|wss?)://([^/'\"]+)", page)
            assert sorted(hosts) == sorted(
                f"127.0.0.1:{port}"
                for port in (mapped.cross_port, mapped.main_port, mapped.ws_port)
            )
            rules = mapped.chrome_args(hermetic=True)[0]
            assert rules.endswith("MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1")

    def test_the_localhost_page_is_served_and_linked_on_localhost_only(self) -> None:
        """Every route of the page goes by exact ``localhost``; a hermetic recording reaches them."""
        with FixtureSite(mapped=True) as mapped:
            assert mapped.url("localhost") == (
                f"http://localhost:{mapped.main_port}/localhost/page.html"
            )
            page = _get(mapped, "/localhost/page.html").text
            hosts = re.findall(r"(?:https?|wss?)://([^/'\"]+)", page)
            assert sorted(hosts) == sorted(
                f"localhost:{port}"
                for port in (mapped.cross_port, mapped.main_port, mapped.ws_port)
            )
            assert _get(mapped, "/localhost/local.js").status_code == 200
            assert "EXCLUDE localhost" in mapped.chrome_args(hermetic=True)[0]
