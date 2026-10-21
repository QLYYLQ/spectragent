"""A directory of synthetic runs: two recordings, an offline replay and four branches.

``record/rec-1`` (a Chinese shopping task on shop.example) carries the frames
archiver's evidence in every shape the pipeline must read: a delayed frame
in its own step's batch, one that was still in flight and landed in the next
step's batch, a superseded one, before-action frames, and a step whose
observation cycle is known only from the delayed frame's request time.
``record/rec-2`` (news.example) ends in a model error, so its last step has no
next observation. ``replays/replay-1`` replays rec-1's network; the four
branches resume rec-1 at step 1: the same click, a typed query, a
task-variant branch and one that diverged while restoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import spectragent.core.capabilities.browser.actions  # noqa: F401 — action effects registered before any test
from spectragent.core.trace.models import TraceStatus
from spectragent.schemas.restoration import (
    BranchProvenance,
    RestorationCheck,
    RestorationEvidence,
)
from tests.test_lookahead.traces import (
    StepSpec,
    add_task_variant,
    capped,
    click,
    done,
    flipped_right_half,
    frame,
    gradient,
    install_frames_reader,
    ok_check,
    playing_video,
    scroll_down,
    settled,
    type_text,
    wait,
    with_box,
    write_run,
)

TASK = "在商店搜索耳机，找到最便宜的一款并记下价格。"
VARIANT_TASK = "在商店找到退货政策允许退货的天数。"
BASE = gradient()
SEARCHED = with_box(BASE, (64, 0, 128, 32), (0, 0, 0))
LATER = flipped_right_half(SEARCHED)
WHITE_LEFT = with_box(BASE, (0, 0, 64, 96), (255, 255, 255))
SPANS = [
    {
        "action": "browser.click_coordinate",
        "started_at": 1001.0,
        "ended_at": 1001.2,
        "outcome": "returned",
    },
    {"action": "browser.wait", "started_at": 1001.2, "ended_at": 1002.2, "outcome": "returned"},
]


@dataclass
class World:
    root: Path
    rec1: Path
    rec2: Path
    replay: Path
    same: Path
    typed: Path
    variant: Path
    ended: Path


def _record_one(base: Path) -> Path:
    return write_run(
        base,
        "rec-1",
        task=TASK,
        steps=[
            StepSpec(
                step=0,
                image=BASE,
                url="https://shop.example/",
                settle=settled(),
                actions=[click(20, 10, "textbox", "Search"), wait()],
                llm_output="Thought: open the search box.",
                dom_cycle=1,
                action_spans=SPANS,
                frames=[
                    frame("after_observation", 1, image=BASE, requested_at=1001.0),
                    frame(
                        "before_action",
                        1,
                        image=BASE,
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1004.0,
                    ),
                    frame(
                        "before_action",
                        1,
                        image=SEARCHED,
                        action_index=1,
                        action="browser.wait",
                        requested_at=1004.2,
                    ),
                ],
            ),
            StepSpec(
                step=1,
                image=SEARCHED,
                url="https://shop.example/search",
                settle=capped(),
                media=playing_video((64, 0, 128, 96)),
                actions=[click(100, 10, "button", "Search")],
                llm_output="Thought: run the search.",
                dom_cycle=2,
                action_spans=[],
                frames=[
                    frame(
                        "before_action",
                        2,
                        image=SEARCHED,
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1015.0,
                    ),
                ],
            ),
            StepSpec(
                step=2,
                image=BASE,
                url="https://shop.example/results",
                settle=settled(),
                actions=[scroll_down()],
                frames=[
                    # step 1's delayed frame, still in flight when step 1 committed
                    frame("after_observation", 2, image=LATER, requested_at=1011.0),
                    frame(
                        "after_observation",
                        3,
                        requested_at=1021.0,
                        outcome="superseded",
                        superseded_by="action",
                    ),
                    frame(
                        "before_action",
                        3,
                        image=WHITE_LEFT,
                        action_index=0,
                        action="browser.scroll",
                        requested_at=1020.8,
                    ),
                ],
            ),
            StepSpec(
                step=3,
                image=BASE,
                url="https://shop.example/results",
                settle=settled(),
                actions=[done("¥99")],
                frames=[frame("after_observation", 4, image=BASE, requested_at=1031.0)],
            ),
            StepSpec(
                step=4,
                origin="terminal",
                image=BASE,
                url="https://shop.example/results",
                dom_cycle=5,
            ),
        ],
    )


def _record_two(base: Path) -> Path:
    return write_run(
        base,
        "rec-2",
        task="Find the headline of the top story.",
        start_url="https://news.example/",
        steps=[
            StepSpec(
                step=0,
                image=BASE,
                url="https://news.example/",
                settle=settled(),
                actions=[click(30, 40, "link", "Top story")],
            ),
            StepSpec(
                step=1,
                image=SEARCHED,
                url="https://news.example/top",
                settle=settled(),
                error_phase="llm",
                error="TimeoutError: the model did not answer",
            ),
        ],
        status=TraceStatus.COMPLETE,
    )


def _branch(base: Path, run_id: str, decision: list, next_url: str | None, *, index: int) -> Path:
    steps = [
        StepSpec(
            step=0,
            origin="restored",
            image=BASE,
            url="https://shop.example/",
            settle=settled(),
            actions=[click(20, 10, "textbox", "Search"), wait()],
            restoration=ok_check(0),
        ),
        StepSpec(
            step=1,
            image=SEARCHED,
            url="https://shop.example/search",
            settle=settled(),
            actions=decision,
            restoration=ok_check(1),
            llm_output="Thought: decide again.",
        ),
    ]
    if next_url is not None:
        steps.append(StepSpec(step=2, origin="terminal", image=BASE, url=next_url))
    return write_run(
        base,
        run_id,
        task=TASK,
        network_source="archive",
        branch=BranchProvenance(
            source_run_id="rec-1",
            source_trace="rec-1",
            source_benchmark="bench",
            source_task_id="rec-1",
            checkpoint_step=1,
            branch_index=index,
            branch_group_id="rec-1/s1",
        ),
        steps=steps,
    )


def build_world(root: Path) -> World:
    record = root / "record"
    rec1 = _record_one(record)
    rec2 = _record_two(record)
    replay = write_run(
        root / "replays",
        "replay-1",
        task=TASK,
        network_source="archive",
        steps=[
            StepSpec(
                step=0,
                image=BASE,
                url="https://shop.example/",
                settle=settled(),
                actions=[click(20, 10, "textbox", "Search")],
            ),
            StepSpec(step=1, image=SEARCHED, url="https://shop.example/search", settle=settled()),
        ],
    )
    branches = root / "branches"
    same = _branch(
        branches,
        "br-same",
        [click(101, 11, "button", "Search")],
        "https://shop.example/results",
        index=0,
    )
    typed = _branch(
        branches, "br-typed", [type_text("耳机")], "https://shop.example/typed", index=1
    )
    variant = _branch(
        branches,
        "br-variant",
        [click(10, 90, "link", "Returns")],
        "https://shop.example/returns",
        index=2,
    )
    add_task_variant(
        variant,
        {
            "variant_id": "1",
            "task_text": VARIANT_TASK,
            "source_task_text": TASK,
            "note": "written for a test",
        },
    )
    ended = write_run(
        branches,
        "br-ended",
        task=TASK,
        network_source="archive",
        branch=BranchProvenance(
            source_run_id="rec-1",
            source_trace="rec-1",
            checkpoint_step=1,
            branch_index=3,
            branch_group_id="rec-1/s1",
        ),
        steps=[
            StepSpec(
                step=0,
                origin="restored",
                image=BASE,
                url="https://shop.example/",
                settle=settled(),
                actions=[click(20, 10, "textbox", "Search")],
                restoration=RestorationCheck(
                    step=0,
                    verdict="diverged",
                    reason="screenshot_distance",
                    reasons=["screenshot_distance"],
                    evidence=[RestorationEvidence(capability_id="browser", screenshot_distance=30)],
                ),
            )
        ],
    )
    return World(root, rec1, rec2, replay, same, typed, variant, ended)


@pytest.fixture(scope="session")
def world_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lookahead") / "traces"
    build_world(root)
    return root


@pytest.fixture
def world(world_root: Path) -> World:
    return World(
        world_root,
        world_root / "record" / "rec-1",
        world_root / "record" / "rec-2",
        world_root / "replays" / "replay-1",
        world_root / "branches" / "br-same",
        world_root / "branches" / "br-typed",
        world_root / "branches" / "br-variant",
        world_root / "branches" / "br-ended",
    )


@pytest.fixture(autouse=True)
def _frames_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    install_frames_reader(monkeypatch)
