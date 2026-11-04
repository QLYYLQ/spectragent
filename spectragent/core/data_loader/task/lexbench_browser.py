"""LexBench-Browser benchmark task loader."""

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from spectragent.core.data_loader._lexbench_legacy import normalize_legacy_record
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.schemas.task import TaskDefinition

#: A start page as LexBench writes it: a bare host — dotted labels of ASCII
#: letters, digits and hyphens — with an optional port, then optionally a path,
#: query or fragment. No scheme, and no whitespace anywhere.
_BARE_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?::[0-9]{1,5})?"
    r"(?:[/?#]\S*)?"
)


def complete_start_page(start_url: str) -> str:
    """*start_url* as one ``https://`` URL when LexBench wrote it as a bare host.

    LexBench-Browser writes its start pages without a scheme
    (``www.bilibili.com``, ``s.weibo.com/top/summary``), and a URL without one
    is not something the browser opens. Only a bare host (``_BARE_HOST``),
    ASCII throughout, is completed; anything else is returned unchanged — a
    URL that already has a scheme, and text or several sites
    (``www.xiachufang.com或其他美食网站``, ``jd.com + tmall.com``), which the
    browser's setup check then refuses by name at ``prepare()``
    (``docs/architecture_design.md`` §4.10, "起始页").
    """
    if start_url.isascii() and _BARE_HOST.fullmatch(start_url):
        return f"https://{start_url}"
    return start_url


@dataclass
class LexBenchBrowserTaskLoader(TaskLoaderBase):
    """Loads LexBench-Browser benchmark tasks: the public dataset's, as its fetch writes them.

    Its start pages are completed here (:func:`complete_start_page`), in the
    loader rather than in the browser, so the run records a complete URL: the
    flywheel report and the export recognise a run's site by its recorded
    start page's host, and a bare host has none. The other benchmarks' data
    already writes complete URLs, which is why this is not the shared base's
    business.
    """

    __registry_key__ = "lexbench_browser"

    subset: Annotated[
        Literal["All", "global", "lexmount", "sample50"],
        Field(
            "All",
            description=(
                "A subset of the public LexBench-Browser dataset: All (210 tasks), global and "
                "lexmount (its en and zh website regions), sample50 (50 of All)."
            ),
        ),
    ] = "All"

    def normalize_raw(self, raw: dict[str, Any]) -> TaskDefinition:
        """Accept both the dataset's own record shape and the normalized one; complete the start page.

        The public dataset's records key on an integer ``id`` and write
        ``query``, ``target_website`` and flat login flags
        (``_lexbench_legacy.py``). Either shape's start page goes through
        :func:`complete_start_page`.
        """
        # Imported here, not at the top: every run imports this loader with the
        # framework, and a run that names no browser imports no browser module.
        from spectragent.core.capabilities.browser.task_setup import BrowserSetup

        task = super().normalize_raw(normalize_legacy_record(raw))
        setup = task.setup
        if isinstance(setup, BrowserSetup) and setup.start_url:
            completed = complete_start_page(setup.start_url)
            if completed != setup.start_url:
                setup = setup.model_copy(update={"start_url": completed})
                task = task.model_copy(update={"setup": setup})
        return task
