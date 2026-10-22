"""Building review queues with the viewer's own models (``spectragent.annotation``).

A queue is written with :func:`spectragent.annotation.write_queue` to
``<annotations>/queues/<queue_id>.json``; the viewer started with
``--annotations <annotations>`` reads it. The models are the one definition
of the format, and the viewer's loader is strict, so every queue is built
only from them. Run keys are relative to the root the viewer is started
with; boxes are ``[x0, y0, x1, y1]`` in the referenced image's own pixels.

Everything a queue shows a person is Simplified Chinese (the annotators are
Chinese); stored values (choice values, region kinds, question keys) stay
English identifiers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit

from spectragent.annotation import (
    ArchiveImage,
    MarkSource,
    ObservationImage,
    QueueImage,
    Region,
)
from spectragent.core.capabilities.browser.archive.frames import FRAMES_ARCHIVER_ID

#: Sites by registrable domain, as an annotator knows them.
SITE_NAMES = {
    "bilibili.com": "B 站",
    "v.qq.com": "腾讯视频",
    "iqiyi.com": "爱奇艺",
    "mgtv.com": "芒果 TV",
    "10jqka.com.cn": "同花顺",
    "ign.com": "IGN",
    "finance.sina.com.cn": "新浪财经",
    "sina.com.cn": "新浪",
    "eastmoney.com": "东方财富",
    "douban.com": "豆瓣",
    "youku.com": "优酷",
    "zhihu.com": "知乎",
    "baidu.com": "百度",
    "taobao.com": "淘宝",
    "jd.com": "京东",
}


def default_queue_id(prefix: str) -> str:
    """``<prefix>-<YYYYMMDD>`` for today."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}"


def observation_image(
    role: str, text: str, run: str, step: int, *, marks_from: list[MarkSource] | None = None
) -> QueueImage:
    """A step's committed screenshot; *marks_from* ``None`` draws that step's actions on it."""
    return QueueImage(
        role=role,
        text=text,
        source=ObservationImage(kind="observation", run=run, step=step),
        marks_from=marks_from,
    )


def frame_image(
    role: str,
    text: str,
    run: str,
    step: int | None,
    name: str,
    *,
    marks_from: list[MarkSource] | None = None,
) -> QueueImage:
    """A ``browser.frames`` image, by the step whose commit took it (``None``: trailing).

    *marks_from* ``None``: a before-action frame shows the action it was taken before.
    """
    return QueueImage(
        role=role,
        text=text,
        source=ArchiveImage(
            kind="archive", run=run, step=step, archiver=FRAMES_ARCHIVER_ID, name=name
        ),
        marks_from=marks_from,
    )


def site_name(url: str | None) -> str | None:
    """The site a page belongs to, as a person names it; ``None`` when the URL names no site."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme == "chrome-error":
        return "浏览器错误页"
    if parts.scheme == "about":
        return "空白页"
    host = (parts.hostname or "").lower()
    if not host:
        return None
    for domain in sorted(SITE_NAMES, key=len, reverse=True):
        if host == domain or host.endswith("." + domain):
            return SITE_NAMES[domain]
    return host.removeprefix("www.")


def item_title(page_url: str | None, start_url: str | None, task_id: str | None, where: str) -> str:
    """``腾讯视频 · 任务 263 · 第 1 步``: the site of the page shown, the task and *where*.

    The site is the page's own; when the task started on another site, that
    one follows in brackets.
    """
    page, start = site_name(page_url), site_name(start_url)
    site = page or start or "未知网站"
    if page and start and page != start:
        site += f"（从{start}开始）"
    parts = [site]
    if task_id:
        parts.append(f"任务 {task_id}")
    parts.append(where)
    return " · ".join(parts)


def region(image: str, kind: str, box: Sequence[float]) -> Region:
    """A box on the item image *image*: ``[x0, y0, x1, y1]`` in its own pixels."""
    return Region(image=image, kind=kind, box=[*box])
