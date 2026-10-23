"""A replay's rule version survives source edits and only exists after WAL commit."""

import hashlib
import json
from pathlib import Path

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.replay.archive_source import ArchiveNetworkSource
from spectragent.core.capabilities.browser.replay.matcher import LiveRequest, MatcherConfig
from spectragent.core.capabilities.browser.replay.rule_snapshot import recorded_site_rules
from spectragent.core.capabilities.browser.replay.server import ReplayContext
from spectragent.core.capabilities.browser.replay.site_rules import SiteRuleError
from tests._helpers.replay_archive import RecordingScript
from tests._helpers.trace_builder import archive_file, new_wal

RULE_TEXT = """# The fixture has a polling endpoint and a stateful endpoint.
rules:
  - id: other-recorded-resources
    scope: {}
    priority: -100
    exhaustion: miss
    unavailable: miss
  - id: polling-status
    scope: {host: shop.test, path: /poll, method: GET, port: 80}
    priority: 10
    exhaustion: repeat_last
    unavailable: miss
    ignore_query: [tick]
"""


async def test_frozen_rule_body_round_trips_via_committed_run_record(tmp_path: Path) -> None:
    script = RecordingScript(tmp_path / "recording", run_id="source")
    await script.exchange("http://shop.test/poll?tick=1", body=b"first")
    await script.exchange("http://shop.test/poll?tick=2", body=b"second")
    await script.exchange("http://shop.test/order", method="POST", body=b"ordered")
    await script.commit(0)
    recorded_dir = script.finish()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rule_path = rules_dir / "fixture.yaml"
    rule_path.write_text(RULE_TEXT)
    source = ArchiveNetworkSource(matcher=MatcherConfig(site_rules=str(rules_dir)))
    source.check_config()
    recorded = source.open_replay(open_archive(recorded_dir))
    server = source.serve(recorded, ReplayContext(recorder=None, cycle=lambda: 0))  # type: ignore[arg-type]
    rule_path.write_text(RULE_TEXT.replace("repeat_last", "miss"))
    rule_path.unlink()
    rules_dir.rmdir()

    request = LiveRequest("GET", "http://shop.test/poll?tick=3")
    selected = [server.matcher.match(request) for _ in range(3)]
    assert [item.record.record_id for item in selected if item.record] == [
        recorded.index.records[0].record_id,
        recorded.index.records[1].record_id,
        recorded.index.records[1].record_id,
    ]
    assert selected[-1].exhausted and selected[-1].reason is None
    order = LiveRequest("POST", "http://shop.test/order")
    assert server.matcher.match(order).servable
    assert server.matcher.match(order).reason == "sequence_exhausted"

    block = server.fingerprint()
    file = block["site_rules_snapshot"]["files"][0]
    assert file["text"] == RULE_TEXT
    assert file["sha256"] == hashlib.sha256(RULE_TEXT.encode()).hexdigest()
    wal = new_wal(tmp_path / "replays", "replayed")
    encoded = archive_file(
        "browser",
        "fingerprint.json",
        json.dumps({"network_source": block}).encode(),
        scope="run",
        run_version=0,
    )
    # Merely having a file on disk cannot make it part of the archive.
    orphan = wal.run_dir / "env" / "uncommitted.json"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(json.dumps({"network_source": block}))
    with pytest.raises(SiteRuleError, match="no browser rule snapshot"):
        recorded_site_rules(open_archive(wal.run_dir))
    wal.commit_trailing(extra_files=[encoded])
    restored = recorded_site_rules(open_archive(wal.run_dir))
    assert restored.snapshot() == recorded.rules.snapshot()
    assert restored.require_for_request("GET", request.url).exhaustion == "repeat_last"


async def test_uncovered_recorded_request_is_refused_before_serving(tmp_path: Path) -> None:
    script = RecordingScript(tmp_path / "recording", run_id="source")
    await script.exchange("http://shop.test/order", body=b"content")
    await script.commit(0)
    source_dir = script.finish()
    with pytest.raises(SiteRuleError, match="no replay rule declares exhaustion.*GET.*order"):
        ArchiveNetworkSource().open_replay(open_archive(source_dir))


def test_rule_levels_are_checked_before_opening_a_run(tmp_path: Path) -> None:
    (tmp_path / "rule.yaml").write_text(
        "rules:\n  - id: over-broad\n    exhaustion: miss\n    unavailable: miss\n    levels: [normalized]\n"
    )
    source = ArchiveNetworkSource(
        matcher=MatcherConfig(levels=("exact",), site_rules=str(tmp_path))
    )
    with pytest.raises(SiteRuleError, match="over-broad.*normalized.*exact"):
        source.check_config()


PAGE_URL_TEXT = """# The fixture's search page writes vt into its own address.
page_urls:
  - id: search-vt
    scope: {host: shop.test, path: /search}
    ignore_query: [vt]
"""


async def test_page_url_rules_are_frozen_with_the_replay_and_read_back_from_its_record(
    tmp_path: Path,
) -> None:
    """The server answers restoration from the rules it loaded; the committed run keeps them."""
    script = RecordingScript(tmp_path / "recording", run_id="source")
    await script.exchange("http://shop.test/poll?tick=1", body=b"first")
    await script.commit(0)
    recorded_dir = script.finish()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "fixture.yaml").write_text(RULE_TEXT)
    (rules_dir / "page_urls.yaml").write_text(PAGE_URL_TEXT)
    source = ArchiveNetworkSource(matcher=MatcherConfig(site_rules=str(rules_dir)))
    source.check_config()
    recorded = source.open_replay(open_archive(recorded_dir))
    server = source.serve(recorded, ReplayContext(recorder=None, cycle=lambda: 0))  # type: ignore[arg-type]
    (rules_dir / "page_urls.yaml").unlink()

    assert [rule.id for rule in server.page_urls] == ["search-vt"]
    block = server.fingerprint()
    assert {f["name"] for f in block["site_rules_snapshot"]["files"]} == {
        "fixture.yaml",
        "page_urls.yaml",
    }
    wal = new_wal(tmp_path / "replays", "replayed")
    wal.commit_trailing(
        extra_files=[
            archive_file(
                "browser",
                "fingerprint.json",
                json.dumps({"network_source": block}).encode(),
                scope="run",
                run_version=0,
            )
        ]
    )
    assert recorded_site_rules(open_archive(wal.run_dir)).page_urls == server.page_urls
