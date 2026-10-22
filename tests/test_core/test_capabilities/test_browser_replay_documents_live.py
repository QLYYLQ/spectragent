"""Two documents a replay once lost, recorded and replayed in a real Chrome (§4.14; no LLM).

Both failed silently in the 2026-10-21/22 rounds (fixture pages modelled on them):

- **A document slower than the recorder's hold** — bilibili's search results
  (gzip, GBK, no ``Content-Length``), whose body took longer than
  ``body_read_timeout_s``. The recording gave its read up and kept no body;
  every replay of that step failed the document. Now the read goes on after
  the response is released and the body is kept whole.
- **A status with no standard reason phrase, recorded without one** —
  ctrip's 432 over HTTP/2. The replay sent no phrase, Chrome refused the
  fulfilment ("Invalid http status code or phrase"), the recorder failed the
  document and the replay log said ``hit``. Now the replay sends a phrase
  Chrome takes; and a response Chrome refuses all the same is a
  ``fulfil_failed`` miss, a divergence for a main-frame document, which
  restoration and the document-miss counts see.

The flow is recorded once through the real runtime with a scripted model —
step 0 clicks the slow document's ``again`` link, which opens it in a new tab
(the focus follows), step 1 clicks its ``status`` link, which opens the 432
page in that tab — a click, so the action succeeds whatever the page then
gets, as bilibili's Enter did — and step 2 finishes. The recording holds a
response well under the fixture's delay. Then it is replayed with the
recording's own completions, and branched at step 2 (steps 0 and 1
restored, each observation verified); both again with the phrase rule as it
was (the negative control), where the 432 page's answer is refused.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.benchmark_runner import TaskOutcome
from spectragent.core.capabilities.browser.replay import responder
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLogEntry,
    document_miss_total,
    iter_replay_log,
)
from spectragent.core.trace.reader import RunTrace
from tests._helpers import branch_driver as branch
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import (
    SLOW_DOCUMENT_TEXT,
    UNUSUAL_STATUS,
    FixtureSite,
    slow_document_point,
)
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
TASK = "Open the results again, then the status page."
#: The recording's hold: well under the fixture's delay, so both slow reads outlive it.
SHORT_HOLD_S = 0.3
#: The step the branches fork at: the 432 page, after steps 0 and 1 are restored.
CHECKPOINT = 2
#: The phrase rule the replay applies, kept before any test patches it.
_REASON_PHRASE = responder.reason_phrase


def _phrase_as_it_was(status: int, recorded: str) -> str:
    """The rule before the fix: a code with no standard phrase got none, which Chrome refuses."""
    phrase = _REASON_PHRASE(status, recorded)
    return "" if phrase == responder.UNKNOWN_STATUS_PHRASE else phrase


@dataclass
class Flow:
    source: driver.Outcome
    replay: driver.Outcome
    branch: RunTrace
    refused_replay: driver.Outcome
    refused_branch: TaskOutcome


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted models are on loopback; the machine's proxy must not see them.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


@pytest.fixture(scope="module")
def flow(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Flow:
    base = tmp_path_factory.mktemp("documents")
    start = site.url("slow_document")
    recording = [
        driver.click_at(*slow_document_point("again")),
        driver.click_at(*slow_document_point("status")),
        driver.done("shown"),
    ]
    with ScriptedLLMServer(recording) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "recorded")
        (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
        writer["archive"] = [
            {**entry, "body_read_timeout_s": SHORT_HOLD_S}
            if entry["archiver_id"] == "browser.network"
            else entry
            for entry in writer["archive"]
        ]
        source = asyncio.run(driver.run_task(raw, task=TASK, start_url=start, timeout=_TIMEOUT_S))
    assert llm.errors == [], llm.errors
    source.require_finished(len(recording), "shown")

    def replay(trace_dir: Path) -> driver.Outcome:
        with ScriptedLLMServer(driver.recorded_completions(source.trace)) as llm:
            replay_raw = driver.to_replay_config(raw, llm=llm, trace_dir=trace_dir)
            outcome = asyncio.run(
                driver.run_task(
                    replay_raw,
                    task=TASK,
                    start_url=start,
                    replay_archive=source.run_dir,
                    timeout=_TIMEOUT_S,
                )
            )
        assert llm.errors == [], llm.errors
        outcome.require_finished(len(recording), "shown")
        return outcome

    def branch_run(trace_dir: Path) -> TaskOutcome:
        with ScriptedLLMServer([driver.done("shown")]) as teacher:
            config = branch.branch_config(
                raw,
                llm=teacher,
                trace_dir=trace_dir,
                sources_root=base / "recorded",
                step=CHECKPOINT,
                continue_steps=0,
            )
            return branch.run_branch_task(config, f"{source.run_dir.name}/s{CHECKPOINT}/b0")

    replayed = replay(base / "replay")
    branched = branch.require_ran(branch_run(base / "branch"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(responder, "reason_phrase", _phrase_as_it_was)
        refused_replay = replay(base / "refused_replay")
        refused_branch = branch_run(base / "refused_branch")
    return Flow(source, replayed, branched, refused_replay, refused_branch)


def _index_lines(run_dir: Path, path: str) -> list[dict[str, Any]]:
    """The recording's ``exchange`` lines for *path* (every tab's, in order)."""
    trace = RunTrace(run_dir)
    lines: list[dict[str, Any]] = []
    for _step, record in trace.iter_archive("browser.network", include_trailing=True):
        if record.name != "index.jsonl":
            continue
        for text in trace.read_extra(record.path).decode().splitlines():
            line = json.loads(text)
            if line["kind"] == "exchange" and path in line["url"]:
                lines.append(line)
    return lines


def _documents(run_dir: Path) -> list[ReplayLogEntry]:
    """The replay log's decisions on main-frame documents, in order."""
    return [
        entry
        for _step, entry in iter_replay_log(open_archive(run_dir))
        if entry.resource_type == "Document"
    ]


def test_the_recording_kept_both_slow_documents_whole(flow: Flow) -> None:
    lines = _index_lines(flow.source.run_dir, "/slow_document/page.html")
    assert len(lines) == 2, "the first tab's document and the new tab's"
    for line in lines:
        assert line["body_status"] == "ok", line["body_note"]
        assert str(line["body_note"]).startswith("read after the response was released")
    (status,) = _index_lines(flow.source.run_dir, "/unusual_status/page.html")
    assert (status["status"], status["status_text"], status["body_status"]) == (
        UNUSUAL_STATUS,
        "",
        "ok",
    )


def test_the_replay_answers_every_document_as_recorded(flow: Flow) -> None:
    documents = _documents(flow.replay.run_dir)
    assert [(d.kind, d.answer, d.status) for d in documents] == [
        ("hit", "fulfilled", 200),
        ("hit", "fulfilled", 200),
        ("hit", "fulfilled", UNUSUAL_STATUS),
    ]
    assert document_miss_total(open_archive(flow.replay.run_dir)) == 0
    assert driver.step_signatures(flow.replay.trace) == driver.step_signatures(flow.source.trace)
    assert SLOW_DOCUMENT_TEXT in branch.page_text(flow.replay.trace, 1), "GBK, decoded right"
    assert f"status {UNUSUAL_STATUS}" in branch.page_text(flow.replay.trace, CHECKPOINT)


def test_the_branch_restores_both_documents(flow: Flow) -> None:
    records = flow.branch.step_records
    for record in records[: CHECKPOINT + 1]:
        check = record.restoration
        assert check is not None and check.verdict == "ok", (record.step, check)
        (evidence,) = check.evidence
        assert evidence.url_match is True and evidence.document_misses == 0


def test_a_refused_document_is_a_divergence_the_replay_counts(flow: Flow) -> None:
    """The negative control: the 432 page's answer goes out without a phrase."""
    *served, refused = _documents(flow.refused_replay.run_dir)
    assert [(d.kind, d.answer) for d in served] == [("hit", "fulfilled"), ("hit", "fulfilled")]
    assert (refused.kind, refused.reason, refused.answer) == ("miss", "fulfil_failed", "failed")
    assert (refused.frame, refused.request_class, refused.diverged) == ("main", "document", True)
    assert refused.record_id is not None
    assert refused.fulfil_error is not None
    assert "Invalid http status code or phrase" in refused.fulfil_error
    assert document_miss_total(open_archive(flow.refused_replay.run_dir)) == 1
    shown = branch.page_text(flow.refused_replay.trace, CHECKPOINT)
    assert f"status {UNUSUAL_STATUS}" not in shown, "Chrome's error page, not the recorded one"


def test_a_refused_document_fails_the_branchs_restoration(flow: Flow) -> None:
    outcome = flow.refused_branch
    assert outcome.trace_dir is not None, outcome.error
    checks = [
        record.restoration
        for record in RunTrace(outcome.trace_dir).step_records
        if record.restoration is not None
    ]
    failing = [check for check in checks if check.verdict != "ok"]
    assert [check.step for check in failing] == [CHECKPOINT], checks
    (check,) = failing
    assert check.verdict == "diverged" and "document_miss" in check.reasons
    (evidence,) = check.evidence
    assert evidence.document_misses == 1
