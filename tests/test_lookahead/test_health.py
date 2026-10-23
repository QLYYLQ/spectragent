"""``health``: a verdict per recording, its reasons, and the exclusions file every consumer reads.

The runs are written through the real WAL writer (``traces.py``): screenshots
with their settle records, and ``browser.network``'s ``index.jsonl`` lines as
the recorder writes them, read back through the browser package's reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookahead.cli import run
from lookahead.health import (
    BLANK_INK_SHARE,
    INK_LEVEL,
    MIN_BLANK_STEPS,
    analyse,
    answer_check,
    waited_or_reloaded,
)
from lookahead.imaging import decode, ink_share
from lookahead.runs import LookaheadInputError, open_runs
from spectragent.core.data_loader.task.run_exclusions import read_run_exclusions
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.restoration import BranchProvenance
from tests._helpers.trace_builder import agent_result
from tests.test_lookahead.conftest import World
from tests.test_lookahead.traces import (
    StepSpec,
    capped,
    click,
    exchange,
    flat,
    gradient,
    ok_check,
    send_keys,
    settled,
    wait,
    with_box,
    write_run,
)

START = "https://shop.example/"
#: A dark page with skeleton placeholders a few levels off its background.
SKELETON = with_box(flat((18, 19, 20)), (16, 16, 112, 40), (28, 28, 30))
EMPTY_WHITE = flat((255, 255, 255))
CONTENT = gradient()
NOT_LOADED = capped(5000, ("content",))


def _answer(text: str) -> AgentResult:
    return agent_result(answer=text)


def _blank_steps(count: int, image: bytes, actions: list, *, images: list[bytes] | None = None):
    return [
        StepSpec(
            step=step,
            image=images[step % len(images)] if images else image,
            url=START,
            settle=NOT_LOADED,
            actions=[actions[step % len(actions)]],
        )
        for step in range(count)
    ]


def _content_steps(count: int, *, network: list[dict] | None = None, url: str = START):
    return [
        StepSpec(
            step=step,
            image=CONTENT if step % 2 == 0 else with_box(CONTENT, (0, 0, 64, 48), (255, 0, 0)),
            url=url if step == 0 else START + f"page-{step}",
            settle=settled(),
            actions=[click(10, 10, "link", f"Item {step}")],
            network=network if step == 0 else None,
        )
        for step in range(count)
    ]


def _requests(ok: int, failed: int = 0, cancelled: int = 0) -> list[dict]:
    """Content requests of one commit: the start document, *ok* data calls, failures, cancellations."""
    return [
        exchange(START, "Document"),
        *(exchange(f"https://api.shop.example/{index}") for index in range(ok)),
        *(
            exchange(
                f"https://api.shop.example/f{index}",
                "Fetch",
                status=None,
                error_text="net::ERR_CONNECTION_CLOSED",
            )
            for index in range(failed)
        ),
        *(
            exchange(f"https://ads.example/c{index}", status=None, error_text="net::ERR_ABORTED")
            for index in range(cancelled)
        ),
        # an image that failed is not a content request
        exchange("https://img.example/a.png", "Image", status=None, error_text="net::ERR_FAILED"),
    ]


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("health") / "traces"
    record = root / "record"
    write_run(
        record,
        "blank",
        task="在商店搜索耳机",
        steps=_blank_steps(MIN_BLANK_STEPS + 1, SKELETON, [wait(), send_keys("Meta+r")]),
        result=_answer("无法完成任务：页面一直停在骨架屏，无法搜索耳机。"),
    )
    write_run(
        record,
        "changing",
        task="在商店搜索耳机",
        steps=_blank_steps(MIN_BLANK_STEPS + 1, SKELETON, [wait()], images=[SKELETON, EMPTY_WHITE]),
        result=_answer("¥99"),
    )
    write_run(
        record,
        "clicking",
        task="在商店搜索耳机",
        steps=_blank_steps(MIN_BLANK_STEPS + 1, SKELETON, [click(10, 10, "link", "Home")]),
        result=_answer("¥99"),
    )
    loading_once = _content_steps(3, network=_requests(ok=40, failed=1, cancelled=10))
    loading_once[1] = StepSpec(
        step=1, image=SKELETON, url=START, settle=NOT_LOADED, actions=[wait()]
    )
    write_run(record, "healthy", task="在商店搜索耳机", steps=loading_once, result=_answer("¥99"))
    write_run(
        record,
        "degraded",
        task="在商店搜索耳机",
        steps=_content_steps(3, network=_requests(ok=20, failed=6, cancelled=4)),
        result=_answer("最便宜的是 ¥99"),
    )
    error_page = _content_steps(2)
    error_page[0].url = "chrome-error://chromewebdata/"
    write_run(record, "error-page", task="在商店搜索耳机", steps=error_page, result=_answer("¥99"))
    not_found = _requests(ok=20)
    not_found[0] = exchange(START, "Document", status=404)
    write_run(
        record,
        "not-found",
        task="在商店搜索耳机",
        steps=_content_steps(2, network=not_found),
        result=_answer("¥99"),
    )
    write_run(
        record,
        "gave-up",
        task="在商店搜索耳机",
        steps=_content_steps(2),
        result=_answer("Unable to complete the task: the search box never appeared."),
    )
    write_run(
        root / "branches",
        "blank-b0",
        task="在商店搜索耳机",
        network_source="archive",
        branch=BranchProvenance(
            source_run_id="blank",
            source_trace="blank",
            checkpoint_step=1,
            branch_index=0,
            branch_group_id="blank/s1",
        ),
        steps=[
            StepSpec(step=0, origin="restored", image=SKELETON, url=START, restoration=ok_check(0)),
            StepSpec(step=1, image=SKELETON, url=START, actions=[wait()], restoration=ok_check(1)),
        ],
    )
    return root


@pytest.fixture(scope="module")
def health(root: Path) -> dict:
    report = analyse(open_runs(root))
    return {item.run.removeprefix("record/"): item for item in report.runs} | {"_report": report}


def _codes(item) -> list[str]:
    return [reason.code for reason in item.reasons]


# ── never rendered ────────────────────────────────────────────────────


def test_a_skeleton_screen_is_blank_and_content_is_not() -> None:
    skeleton, content = decode(SKELETON), decode(CONTENT)
    assert skeleton is not None and content is not None
    assert ink_share(skeleton, INK_LEVEL) < BLANK_INK_SHARE
    assert ink_share(content, INK_LEVEL) > 0.5


def test_a_page_that_never_rendered(health: dict) -> None:
    item = health["blank"]
    assert item.verdict == "unusable"
    assert _codes(item) == ["never_rendered", "answer_states_failure"]
    assert [reason.basis for reason in item.reasons] == ["recording", "answer"]
    [stretch] = item.blank_stretches
    assert (stretch.first_step, stretch.last_step, stretch.steps) == (0, 5, 6)
    assert stretch.waited_or_reloaded == 6 and stretch.content_unseen == 6
    assert stretch.urls == [START]
    assert item.reasons[0].summary.startswith(
        "never rendered: steps 0-5 (6 policy steps) showed a blank, unchanged screen"
    )


@pytest.mark.parametrize(
    ("run_name", "why"),
    [
        ("changing", "a blank screen that changes starts a new stretch"),
        ("clicking", "the agent never waited or reloaded"),
        ("healthy", "a page still loading is blank in one observation"),
    ],
)
def test_blank_screens_that_are_not_a_page_that_never_rendered(
    health: dict, run_name: str, why: str
) -> None:
    item = health[run_name]
    assert "never_rendered" not in _codes(item), why
    assert item.blank_screens >= 1


def test_the_changing_and_the_clicking_runs_measure_what_they_are(health: dict) -> None:
    assert max(stretch.steps for stretch in health["changing"].blank_stretches) == 1
    [stretch] = health["clicking"].blank_stretches
    assert stretch.steps == MIN_BLANK_STEPS + 1 and stretch.waited_or_reloaded == 0


@pytest.mark.parametrize(
    ("keys", "reloads"),
    [("Meta+r", True), ("ctrl+R", True), ("F5", True), ("cmd+shift+r", True), ("Enter", False)],
)
def test_reload_keys(keys: str, reloads: bool) -> None:
    action, _ = send_keys(keys)
    assert waited_or_reloaded([action], START) is reloads


# ── degraded at load ──────────────────────────────────────────────────


def test_content_requests_that_failed_live(health: dict) -> None:
    item = health["degraded"]
    assert item.verdict == "unusable" and _codes(item) == ["degraded_at_load"]
    first = item.load[0]
    assert (first.content_requests, first.failed_live, first.cancelled_by_page) == (31, 6, 4)
    assert first.errors == {"net::ERR_CONNECTION_CLOSED": 6}
    assert first.failed_hosts == {"api.shop.example": 6}
    assert "step 0: 6 of 31 content requests (19%) failed live" in item.reasons[0].summary


def test_the_page_cancelling_requests_and_one_failure_are_no_degradation(health: dict) -> None:
    item = health["healthy"]
    assert item.verdict == "usable"
    assert (item.load[0].failed_live, item.load[0].cancelled_by_page) == (1, 10)


def test_the_start_page_on_chromes_error_page(health: dict) -> None:
    item = health["error-page"]
    assert _codes(item) == ["degraded_at_load"]
    assert item.start_document.error_page_step == 0 and item.start_document.failed
    assert "step 0 showed Chrome's error page" in item.reasons[0].summary


def test_the_start_document_answered_with_an_error_status(health: dict) -> None:
    item = health["not-found"]
    assert _codes(item) == ["degraded_at_load"]
    assert (item.start_document.status, item.start_document.failed) == (404, True)
    assert "the start page's document failed (status 404)" in item.reasons[0].summary


# ── the final answer ──────────────────────────────────────────────────


def test_an_answer_that_says_the_task_was_not_done(health: dict) -> None:
    item = health["gave-up"]
    assert item.verdict == "unusable" and _codes(item) == ["answer_states_failure"]
    assert item.reasons[0].basis == "answer"
    assert item.answer.rule == "en_could_not_complete"


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        ("无法完成：爱奇艺搜索页一直停在骨架屏", "zh_could_not_complete"),
        ("未能完成实时价对比。原因：无法打开东方财富网", "zh_could_not_complete"),
        ("任务失败，页面打不开", "zh_task_failed"),
        ("I was unable to complete the task.", "en_could_not_complete"),
        ("The task cannot be completed in this environment.", "en_task_cannot_be_completed"),
        ("Task is not achievable because the page never loads.", "en_not_achievable"),
        ("总播放量：未能在页面直接读取；评分 9.3 分", None),
        ("该剧尚未完成更新，全 23 集", None),
        ("waited 1.0s", None),
    ],
)
def test_answer_rules(tmp_path: Path, answer: str, rule: str | None) -> None:
    write_run(tmp_path, "run", task="t", steps=_content_steps(1), result=_answer(answer))
    [found] = open_runs(tmp_path).runs
    assert answer_check(found).rule == rule


def test_the_agents_own_success_flag_is_never_read(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "run",
        task="t",
        steps=_content_steps(1),
        result=agent_result(answer="¥99", agent_reported_success=False),
    )
    [found] = open_runs(tmp_path).runs
    assert answer_check(found).rule is None


# ── the command and its exclusions file ───────────────────────────────


def test_only_recordings_are_judged(health: dict) -> None:
    report = health["_report"]
    assert set(report.not_assessed) == {"branches/blank-b0"}
    assert report.counts == {
        "usable": 3,
        "unusable": 5,
        "never_rendered": 1,
        "degraded_at_load": 3,
        "answer_states_failure": 2,
    }


def test_the_command_writes_the_report_and_the_exclusions_file(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "health"
    assert run(["health", "--root", str(root), "--out", str(out)]) == 0
    assert (
        "unusable record/blank (never_rendered; answer_states_failure)" in capsys.readouterr().out
    )
    document = json.loads((out / "health.json").read_text(encoding="utf-8"))
    assert document["format"] == "lookahead-run-health-1"
    assert document["thresholds"]["min_blank_steps"] == MIN_BLANK_STEPS
    excluded = read_run_exclusions(out / "exclusions.json")
    assert set(excluded) == {"blank", "degraded", "error-page", "not-found", "gave-up"}
    assert excluded["blank"].startswith("never rendered: steps 0-5")
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "### `record/degraded`" in report and "| record/healthy |" in report


def test_exclusions_leave_out_the_runs_and_their_branches(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "health"
    assert run(["health", "--root", str(root), "--out", str(out)]) == 0
    runs = open_runs(root, exclude_runs=out / "exclusions.json")
    assert {found.key for found in runs.runs} == {
        "record/changing",
        "record/clicking",
        "record/healthy",
    }
    assert runs.excluded["record/blank"].startswith("the exclusions file lists it: never rendered")
    assert runs.excluded["branches/blank-b0"].startswith(
        "its source run 'blank' is in the exclusions file: never rendered"
    )


def test_a_malformed_exclusions_file_is_refused(root: Path, tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(json.dumps({"format": "run-exclusions-1", "runs": [{"run_id": "blank"}]}))
    with pytest.raises(LookaheadInputError, match="--exclude-runs: exclude_runs file"):
        open_runs(root, exclude_runs=path)
    assert run(["extract", "--root", str(root), "--out", str(tmp_path / "x.jsonl"),
                "--exclude-runs", str(path)]) == 2  # fmt: skip


def _exclusions(tmp_path: Path, run_id: str) -> Path:
    path = tmp_path / "exclusions.json"
    path.write_text(
        json.dumps(
            {"format": "run-exclusions-1", "runs": [{"run_id": run_id, "reason": "never rendered"}]}
        ),
        encoding="utf-8",
    )
    return path


def test_every_queue_builder_lists_what_it_left_out(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exclusions = ["--exclude-runs", str(_exclusions(tmp_path, "rec-1"))]
    common = ["--root", str(world.root), *exclusions]
    assert run(["stability", *common, "--out", str(tmp_path / "s")]) == 0
    report = json.loads((tmp_path / "s" / "stability.json").read_text(encoding="utf-8"))
    assert report["excluded_runs"]["record/rec-1"] == "the exclusions file lists it: never rendered"
    assert {item["run"] for item in report["observations"]} == {"record/rec-2", "replays/replay-1"}
    assert "## Runs left out by the exclusions file" in (tmp_path / "s" / "report.md").read_text(
        encoding="utf-8"
    )
    assert run(["gold-queue", *common, "--out", str(tmp_path / "g")]) == 0
    sampling = json.loads((tmp_path / "g" / "sampling.json").read_text(encoding="utf-8"))
    assert "record/rec-1" in sampling["excluded_runs"] and "record/rec-1" not in sampling["by_run"]
    assert run(["judge-forks", *common, "--out", str(tmp_path / "f"), "--rules-only"]) == 0
    judged = (tmp_path / "f" / "judgments.jsonl").read_text(encoding="utf-8")
    assert judged == ""  # every branch came from rec-1
    report_md = (tmp_path / "f" / "report.md").read_text(encoding="utf-8")
    assert "- `branches/br-same`: its source run 'rec-1' is in the exclusions file" in report_md
    err = capsys.readouterr().err
    assert "excluded record/rec-1: the exclusions file lists it: never rendered" in err
