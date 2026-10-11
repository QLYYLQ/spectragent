"""Where an export reads benchmark data (arch §4.15 "基准数据").

The export document's own ``eval_loader:`` / ``task_loader:`` blocks, by
``benchmark_name`` and in document order; the registry's loader, built with
its defaults, only for a benchmark the document configures none for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.composer import ConfigComposer
from spectragent.export.benchmarks import BenchmarkLoaders
from tests._helpers.export_runs import write_benchmark


def test_the_documents_loaders_by_benchmark_in_document_order(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    directory = write_benchmark(tmp_path, "online_mind2web", [{"task_id": "t"}], subset="Hard")
    write_benchmark(tmp_path, "online_mind2web", [{"task_id": "u"}], subset="All")
    block = {"benchmark_name": "online_mind2web", "data_dir": str(directory)}
    run = composer.compose(
        {
            "eval_loader": [{**block, "subset": "Hard"}, {**block, "subset": "All"}],
            "task_loader": [{**block, "subset": "All"}],
        }
    )
    loaders = BenchmarkLoaders.from_run(run)
    assert [loader.subset for loader in loaders.configured("eval", "online_mind2web")] == [
        "Hard",
        "All",
    ]
    assert [loader.task_ids() for loader in loaders.configured("task", "online_mind2web")] == [
        ["u"]
    ]
    assert loaders.configured("eval", "browsecomp") == ()


def test_a_registered_default_is_built_once_and_never_beside_a_configured_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))
    write_benchmark(tmp_path, "online_mind2web", [{"task_id": "t"}], subset="test")
    loaders = BenchmarkLoaders()
    built = loaders.registered_default("eval", "online_mind2web", "test")
    assert built is loaders.registered_default("eval", "online_mind2web", "test")
    assert (built.subset, built.task_ids()) == ("test", ["t"])
    assert loaders.registered_default("eval", "no_such_benchmark", None) is None
    assert loaders.registered_default("task", "", None) is None

    configured = BenchmarkLoaders(eval_loaders=[built])
    with pytest.raises(ValueError, match="eval_loader blocks configure 'online_mind2web'"):
        configured.registered_default("eval", "online_mind2web", "test")
