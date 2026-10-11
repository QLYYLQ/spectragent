"""One recorded step of the ``sticky`` page, read back as the translators read it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, DOMObs
from spectragent.core.trace.reader import RunTrace
from spectragent.export import canonicalise
from spectragent.schemas.action import Action
from spectragent.schemas.sample import CanonicalStepSample
from tests._helpers.export_traces import ExportStep, screenshot, write_run
from tests._helpers.occlusion_pages import LOADER_ID, sticky_state
from tests._helpers.trace_builder import archive_file


class Recorder:
    """Commits one step and returns its canonical sample and the trace."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs = 0

    def __call__(
        self,
        actions: Sequence[Action],
        *,
        state: DOMState | None = None,
        dpr: float = 1,
        documents: Sequence[dict[str, Any]] = (),
        loader_id: str | None = LOADER_ID,
        dom: bool = True,
    ) -> tuple[CanonicalStepSample, RunTrace]:
        self.runs += 1
        run_id = f"run-{self.runs}"
        state = state if state is not None else sticky_state()
        fragments: list[Any] = [screenshot(dpr=dpr)]
        if dom:
            fragments.append(DOMObs(element_tree=state, selector_map=dict(state.selector_map)))
        if loader_id is not None:
            fragments.append(BrowserMetaObs(url="http://shop.example.test/", loader_id=loader_id))
        extra = [
            archive_file(
                "browser.dom_raw",
                f"dom-0-tab{i}.json.gz",
                gzip_json(document),
                step=0,
                media_type="application/gzip",
                content_addressed=True,
                ids={"tab": "tab-1"},
            )
            for i, document in enumerate(documents)
        ]
        run = write_run(
            self.root,
            run_id,
            [ExportStep(actions=list(actions), fragments=fragments, extra_files=extra)],
        )
        trace = RunTrace(run)
        (sample,) = canonicalise(trace, rel_path=run_id)
        return sample, trace


@pytest.fixture
def recorded(tmp_path: Path) -> Recorder:
    return Recorder(tmp_path)
