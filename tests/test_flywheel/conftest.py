"""The known-answer chain root the flywheel's unit tests read (``tests/_helpers/flywheel_traces.py``)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from spectragent.flywheel.metrics import ChainLayout, ChainRuns, open_chain
from spectragent.flywheel.report import FlywheelReport, build_report
from tests._helpers.flywheel_traces import KnownChain, known_chain


@pytest.fixture(scope="package")
def known(tmp_path_factory: pytest.TempPathFactory) -> KnownChain:
    """Written once: every reader here is read-only."""
    return asyncio.run(known_chain(tmp_path_factory.mktemp("known") / "chain"))


@pytest.fixture(scope="package")
def layout(known: KnownChain) -> ChainLayout:
    return ChainLayout(known.root)


@pytest.fixture(scope="package")
def chain(layout: ChainLayout) -> ChainRuns:
    return open_chain(layout)


@pytest.fixture(scope="package")
def report(layout: ChainLayout) -> FlywheelReport:
    return build_report(layout, evidence="rehearsal", name="known")


def empty_layout(tmp_path: Path) -> ChainLayout:
    """A chain root with nothing in it."""
    root = tmp_path / "empty"
    root.mkdir()
    return ChainLayout(root)
