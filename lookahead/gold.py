"""``gold-queue``: a review queue for human gold labels of the information the agent used next.

User ruling: a gold label marks the information the agent **actually used**
for its next step. Each item shows the screen the agent saw (O_t) with the
actions it issued there, the next screen (O_{t+1}) with the actions the agent
issued next drawn on it, the agent's next reply, and, when the frames
archiver took one, the frame right before the first action. The annotator
picks the kind of information the next step needed and boxes where it is on
the next screen. Items carry no suggestion: the viewer seeds a label with a
suggestion and grades the label against it, and a pixel difference is no
answer.

Items are policy steps whose two screens were both recorded and whose next
step is a policy step that acted; a step that finished the task (``done``) is
left out, since nothing follows it. They are sampled in turn across (site,
action type) groups, preferring runs used least so far.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from lookahead.extract import step_rows
from lookahead.forks import FILLER, action_type
from lookahead.output import write_json
from lookahead.queues import frame_image, item_title, observation_image
from lookahead.rows import StepRow
from lookahead.runs import Run, RunSet
from spectragent.annotation import (
    QUEUE_FORMAT,
    GuideSection,
    ItemField,
    ItemLink,
    LabelChoice,
    LabelSpec,
    MarkSource,
    QueueImage,
    QueueItem,
    RegionKind,
    RegionSpec,
    ReviewQueue,
    write_queue,
)
from spectragent.inspection.marks import action_lines

NO_SITE = "(no site)"
CHOICES = [
    LabelChoice(
        value="result_list",
        text="结果列表",
        help="下一步在几个相似的项目里挑选或阅读：搜索结果、视频列表、排行榜等。"
        "即使智能体接着点开了其中一项，也选这一项。",
        regions="required",
    ),
    LabelChoice(
        value="confirmation",
        text="确认/反馈",
        help="继续之前，智能体先确认上一步生效了：输入的文字出现了、筛选或排序已选中、"
        "下拉框或联想词打开了、页面出现了提示消息。",
        regions="required",
    ),
    LabelChoice(
        value="detail_fields",
        text="详情字段",
        help="要读取或记录的具体数值：播放量、评分、价格、日期、作者等，"
        "包括智能体直接写进答案里的数值。",
        regions="required",
    ),
    LabelChoice(
        value="navigation_target",
        text="操作入口",
        help="下一步要操作的某一个具体控件：按钮、标签页、链接、输入框、翻页等，"
        "而不是在几个相似项目里挑一个。",
        regions="required",
    ),
    LabelChoice(
        value="nothing_new",
        text="不需要新信息",
        help="下一步不依赖这一屏上的新内容：按原计划继续、等待、重试。",
        regions="forbidden",
    ),
    LabelChoice(
        value="other",
        text="其他",
        help="以上都不是；请在备注里写清楚。",
        regions="optional",
    ),
]
EVIDENCE = RegionKind(
    value="evidence",
    text="信息所在位置",
    help="智能体下一步用到的信息在“下一屏”上的位置。",
)
TITLE = "下一步用到的信息在哪里"
INSTRUCTIONS = (
    "本条标注的是下一步的决定：先读“下一步的回复”和“下一步的动作”，再在最右侧的下一次观察上，"
    "框出这次决定实际用到的信息，并选择信息类型。左侧的辅助图和档案帮助你理解经过，不能画标注框。"
    "如果下一屏是空白的或还没加载出来，请按“跳过”，并在备注里写明原因。"
)
GUIDE = [
    GuideSection(
        title="要做什么",
        text=(
            "- 智能体在“当前屏幕”上做了一些动作，页面变成了“下一屏”，然后它又做了下一步。\n"
            "- 你要判断：智能体做下一步时，用到了“下一屏”上的哪些信息？\n"
            "- 选出信息的类型，并在“下一屏”上把这些信息框出来。"
        ),
    ),
    GuideSection(
        title="三张图分别是什么",
        text=(
            "- 当前屏幕：智能体做这一步决定时看到的画面；上面的编号标记是它在这一屏上做的动作。\n"
            "- 第一个动作之前：当前步的智能体想完、刚要执行第一个动作时的画面；这是辅助证据，模型没有看到。\n"
            "- 下一屏：当前步动作执行后的下一次观察；标记是基于这张新观察作出的下一步动作，并非这些动作执行后的结果。只在这张图上画框。\n"
            "- 三张图按发生顺序排列：当前观察 → 思考 → 第一个动作前 → 执行动作 → 下一次观察。\n"
            "- 当前观察下的档案可悬停预览、点击固定，查看此前和之后已记录的观察、回复、思考和动作结果。\n"
            "- 标记的编号和右边动作列表的编号一致；点“动作标记”可以隐藏或显示。"
        ),
    ),
    GuideSection(
        title="例子",
        text=(
            "- 下一步在搜索结果里点开了第 2 个视频：选“结果列表”，框出它比较过的那几条结果。\n"
            "- 上一步点了“最多播放”，下一屏上“最多播放”已经高亮，智能体接着往下看：选“确认/反馈”，"
            "框出高亮的“最多播放”。\n"
            "- 下一步直接在回答里写了“播放量 120 万”：选“详情字段”，框出这个数字。\n"
            "- 下一步点击了“更多筛选”按钮：选“操作入口”，框出这个按钮。\n"
            "- 下一步只是等待、重试，或按原计划继续往下滚动：选“不需要新信息”，不画框。"
        ),
    ),
    GuideSection(
        title="框怎么画",
        text=(
            "- 框画在“下一屏”上，框住信息本身；宁可稍大一点，也不要漏掉。\n"
            "- 用到几处信息就画几个框。\n"
            "- 选“不需要新信息”时不能画框；选“其他”时可画可不画；其余类型至少画一个框。"
        ),
    ),
    GuideSection(
        title="什么时候跳过",
        text=(
            "- 下一屏是空白的、没加载出来，或看不清：按“跳过”，在备注里写原因。\n"
            "- 看不懂智能体下一步在做什么：也可以跳过，并在备注里说明。"
        ),
    ),
]


def main_action(row: StepRow) -> str:
    """The step's action type for sampling: its first action other than waiting."""
    for action in row.actions:
        kind = action_type(action.name)
        if kind not in FILLER:
            return kind
    return "wait" if row.actions else "none"


def is_done(row: StepRow) -> bool:
    """Whether the step finished the task (it issued ``done``): nothing follows it."""
    return any(action_type(action.name) == "done" for action in row.actions)


def candidates(runs: RunSet) -> list[tuple[Run, StepRow]]:
    """Policy steps with both screens recorded and a next policy step that acted.

    A step that finished the task is left out; a run whose evidence does not
    read is skipped.
    """

    def acted_next(run: Run, row: StepRow) -> bool:
        record = run.records.get(row.next.step) if row.next.step is not None else None
        return record is not None and record.origin == "policy" and bool(record.actions)

    def of_run(run: Run) -> list[StepRow]:
        return [
            row
            for row in step_rows(run, runs)
            if row.origin == "policy"
            and row.observation.screenshot
            and row.next.exists
            and row.next.screenshot
            and not is_done(row)
            and acted_next(run, row)
        ]

    return [(run, row) for run, rows in runs.each(of_run) for row in rows]


def sample(pool: list[tuple[Run, StepRow]], count: int, seed: int) -> list[tuple[Run, StepRow]]:
    """Up to *count* items: in turn from each (site, action type) group, the least-used run first."""
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[tuple[Run, StepRow]]] = {}
    for run, row in pool:
        groups.setdefault((row.site or NO_SITE, main_action(row)), []).append((run, row))
    for members in groups.values():
        rng.shuffle(members)
    used: Counter[str] = Counter()
    chosen: list[tuple[Run, StepRow]] = []
    order = sorted(groups)
    rng.shuffle(order)
    while len(chosen) < count and any(groups[key] for key in order):
        for key in order:
            members = groups[key]
            if not members or len(chosen) >= count:
                continue
            pick = min(range(len(members)), key=lambda index: used[members[index][0].key])
            run, row = members.pop(pick)
            used[run.key] += 1
            chosen.append((run, row))
    return chosen


def gold_item(run: Run, row: StepRow) -> QueueItem:
    assert row.next.step is not None
    following = run.records[row.next.step]
    current = run.records[row.step]
    images: list[QueueImage] = [
        observation_image(
            "current",
            f"当前屏幕 · 第 {row.step} 步的观察",
            run.key,
            row.step,
            marks_from=[MarkSource(run=run.key, step=row.step, label="当前步的动作")],
        ),
    ]
    before = row.frame("before_action")
    if before is not None and before.captured:
        assert before.name is not None
        images.append(
            frame_image(
                "before_action",
                f"第 {row.step} 步 · 第一个动作执行前（思考结束）",
                run.key,
                before.batch_step,
                before.name,
            )
        )
    images.append(
        observation_image(
            "next",
            f"下一屏 · 第 {row.next.step} 步的观察与动作（在这里标注）",
            run.key,
            row.next.step,
            marks_from=[MarkSource(run=run.key, step=row.next.step, label="本次标注的下一步动作")],
        )
    )
    fields = [
        ItemField(text="任务", value=row.task),
        ItemField(text="这一步的动作（画在“当前屏幕”上）", value=action_lines(current)),
        ItemField(text="下一步的回复（完整记录）", value=following.llm_output or "（没有回复）"),
        ItemField(text="下一步的动作（画在“下一屏”上）", value=action_lines(following)),
        ItemField(text="当前步的回复（完整记录）", value=row.llm_output, technical=True),
        ItemField(text="当前网址", value=row.observation.url or "未记录", technical=True),
        ItemField(text="下一屏网址", value=row.next.url or "未记录", technical=True),
        ItemField(text="运行", value=run.key, technical=True),
        ItemField(
            text="步号（当前 / 下一步）", value=f"{row.step} / {row.next.step}", technical=True
        ),
        ItemField(
            text="这一步的动作参数",
            value=json.dumps(
                [a.params | {"name": a.name} for a in row.actions], ensure_ascii=False
            ),
            technical=True,
        ),
    ]
    if before is None or not before.captured:
        missing = {
            "timeout": "拍摄超时",
            "failed": "拍摄失败",
            "no_tab": "页面不可用",
            "superseded": "拍摄已取消",
        }
        fields.append(
            ItemField(
                text="第一个动作前截图",
                value="未记录可定位的截图"
                if before is None
                else missing.get(before.outcome, "没有拍到截图"),
            )
        )
    return QueueItem(
        item_id=f"{run.key}@{row.step}",
        title=item_title(row.observation.url, run.start_url, run.task_id, f"第 {row.step} 步"),
        run=run.key,
        step=row.step,
        images=images,
        trajectory=True,
        fields=fields,
        links=[ItemLink(text="打开下一步", run=run.key, step=row.next.step)],
    )


def gold_queue(
    runs: RunSet, out: Path, *, annotations: Path, queue_id: str, count: int, seed: int
) -> tuple[dict[str, Any], Path]:
    """Write the queue and a ``sampling.json`` summary in *out*; the summary and the queue file."""
    if count < 1:
        raise ValueError("--n must be at least 1")
    pool = candidates(runs)
    chosen = sample(pool, count, seed)
    queue = ReviewQueue(
        format=QUEUE_FORMAT,
        queue_id=queue_id,
        title=TITLE,
        instructions=INSTRUCTIONS,
        guide=GUIDE,
        label=LabelSpec(
            choices=CHOICES,
            multi=False,
            regions=RegionSpec(kinds=[EVIDENCE], image_roles=["next"]),
            note=True,
        ),
        items=[gold_item(run, row) for run, row in chosen],
    )
    path = write_queue(annotations, queue)
    summary = {
        "queue": str(path),
        "candidates": len(pool),
        "items": len(chosen),
        "seed": seed,
        "by_site": dict(Counter(row.site or NO_SITE for _, row in chosen)),
        "by_action": dict(Counter(main_action(row) for _, row in chosen)),
        "by_run": dict(Counter(run.key for run, _ in chosen)),
        "skipped_runs": dict(runs.skipped),
        "excluded_runs": dict(runs.excluded),
    }
    write_json(out / "sampling.json", summary)
    return summary, path
