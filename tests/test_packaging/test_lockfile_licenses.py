"""The locked dependency set carries no AGPL chain, and ported code is noticed.

warcio (Apache-2.0) is a core dependency of the network archiver. The WACZ /
CDXJ tooling around it is not: ``py-wacz`` requires ``cdxj-indexer``, which
requires ``surt`` — AGPL-3.0 (``ref/flywheel/MANIFEST.md``). The PRD's license
trap is kept out by this test, not by memory: none of the three may appear in
``uv.lock``, under any extra.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("surt", "wacz", "cdxj-indexer")


def _locked_packages() -> set[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    return {str(package["name"]).lower() for package in lock.get("package", [])}


def test_no_agpl_chain() -> None:
    locked = _locked_packages()
    assert locked, "uv.lock lists no package — did it move?"
    found = sorted(name for name in locked if name in FORBIDDEN or "wacz" in name)
    assert found == [], f"AGPL chain in the lock file: {found} (see ref/flywheel/MANIFEST.md)"


def test_warcio_is_a_core_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert any(re.match(r"warcio\b", dep) for dep in project["dependencies"])
    assert "warcio" in _locked_packages()


def test_every_ported_source_is_noticed() -> None:
    """THIRD_PARTY_NOTICES.md names each upstream the browser package ports from."""
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text("utf-8")
    for upstream, revision, port in (
        ("Web Page Replay", "6a31bb0937dccd01bedae8bd6cdeb8c113937cfb", "replay/shims.py"),
        ("Playwright", "d9f2fd3e2232ace8e317a84eda6cb86e25bcf49c", "archive/network_model.py"),
        # Offline replay (M30): the canonicaliser and the matcher.
        ("cdxj-indexer", "a65917b12fbef23e58ee3e66878b852923811fb6", "replay/canonical.py"),
        ("harBackend.ts", "d9f2fd3e2232ace8e317a84eda6cb86e25bcf49c", "replay/matcher.py"),
        ("archive.go", "6a31bb0937dccd01bedae8bd6cdeb8c113937cfb", "replay/matcher.py"),
        ("mitmproxy", "5253dcbd1d8f0522de097bfe56918fe12a0f267a", "replay/matcher.py"),
        # The responder's date handling (M30 serving half).
        ("proxy.go", "6a31bb0937dccd01bedae8bd6cdeb8c113937cfb", "replay/responder.py"),
        ("Response.refresh", "5253dcbd1d8f0522de097bfe56918fe12a0f267a", "replay/responder.py"),
    ):
        assert upstream in notices and revision in notices and port in notices, upstream
    for ported in ("archive/network_model.py", "replay/canonical.py"):
        header = (ROOT / "spectragent/core/capabilities/browser" / ported).read_text("utf-8")
        assert "Licensed under the Apache License, Version 2.0" in header, ported
        assert "Modified by af" in header, ported
