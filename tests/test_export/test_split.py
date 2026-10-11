"""Split by site or task — never by step — and decontamination (arch §4.15 "切分与去污染")."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.models import TaskSetupRecord
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.errors import ExportConfigError, ExportEmptyError
from spectragent.export.filters import TaskIdentity
from spectragent.export.settings import BenchmarkRef, Decontamination, SplitSettings
from spectragent.export.split import Decontaminator, split_key, split_of
from tests._helpers.export_runs import exporter_entry, read_jsonl, run_export, write_benchmark
from tests._helpers.export_traces import click_at, ui_tars_step, write_ui_tars_run
from tests._helpers.trace_builder import eval_result

PASSED = {"judge": eval_result(evaluator_id="online_mind2web", predicted_label=1)}


def _identity(**fields: str) -> TaskIdentity:
    values = {
        "benchmark": "b",
        "subset": "s",
        "task_id": "t",
        "task": "Find the kettle",
        "start_url": "https://shop.example.test/",
    }
    values.update(fields)
    return TaskIdentity(**values)  # type: ignore[arg-type]


def _setup(url: str) -> TaskSetupRecord:
    return TaskSetupRecord(context={"start_url": url}, redactions=RedactionLedger())


class TestTheSplit:
    def test_a_salted_hash_of_the_key(self) -> None:
        settings = SplitSettings()
        keys = [f"site-{i}.test" for i in range(400)]
        assignment = [split_of(key, settings) for key in keys]
        assert assignment == [split_of(key, settings) for key in keys], "deterministic"
        assert 20 < assignment.count("validation") < 60  # ~10% of 400
        assert {split_of(k, SplitSettings(validation_fraction=0.0)) for k in keys} == {"train"}
        assert {split_of(k, SplitSettings(validation_fraction=1.0)) for k in keys} == {"validation"}
        resalted = [split_of(key, SplitSettings(salt="other")) for key in keys]
        assert resalted != assignment

    def test_the_keys(self) -> None:
        identity = _identity()
        assert split_key(identity, SplitSettings(by="site")) == "shop.example.test"
        assert split_key(identity, SplitSettings(by="task")) == "b/s/t"
        assert split_key(_identity(start_url=""), SplitSettings(by="site")) is None

    def test_never_by_step(self, composer: ConfigComposer, tmp_path: Path) -> None:
        """Every sample of a site lands in that site's split, whichever run and step it is."""
        sources = tmp_path / "sources"
        for i in range(8):
            write_ui_tars_run(
                sources,
                f"run-{i}",
                [ui_tars_step(click_at(10 * j + 1, 5)) for j in range(3)],
                task_id=f"t-{i}",
                task_setup=_setup(f"https://site{i % 4}.example.test/"),
            )
        out = tmp_path / "out"
        report = run_export(
            composer,
            exporter_entry(sources, out, split={"by": "site", "validation_fraction": 0.5}),
        )
        keys = report.manifest["split"]["keys"]
        assert sorted(keys) == [f"site{i}.example.test" for i in range(4)]
        assert set(keys.values()) == {"train", "validation"}, "the fixture exercises both"
        seen: dict[str, set[str]] = {}
        for split in ("train", "validation"):
            for line in read_jsonl(out / f"{split}.samples.jsonl"):
                assert line["render"]["split"] == split
                run = line["sample"]["run_id"]
                site = f"site{int(run.split('-')[1]) % 4}.example.test"
                assert keys[site] == split
                seen.setdefault(run, set()).add(split)
        assert all(len(splits) == 1 for splits in seen.values())
        assert sum(report.samples.values()) == 24


class TestDecontamination:
    def test_the_default_is_the_evaluated_benchmarks(self) -> None:
        loaders = BenchmarkLoaders()
        decon = Decontaminator.configure(
            Decontamination(), evaluated=["b2", "b1", "b2", ""], loaders=loaders
        )
        assert decon.configured
        assert decon.against == (BenchmarkRef(benchmark="b1"), BenchmarkRef(benchmark="b2"))
        unconfigured = Decontaminator.configure(Decontamination(), evaluated=None, loaders=loaders)
        assert not unconfigured.configured and unconfigured.against == ()
        nothing_named = Decontaminator.configure(Decontamination(), evaluated=[""], loaders=loaders)
        assert not nothing_named.configured
        explicit_none = Decontaminator.configure(
            Decontamination(against=()), evaluated=["b1"], loaders=loaders
        )
        assert explicit_none.configured and explicit_none.against == ()

    def test_decontamination_default_follows_require_eval(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``against: null`` is the benchmark the sources were evaluated on — not the evaluator.

        The grader here is ``exact_match``, a registered evaluator that is no
        benchmark's loader key: the default must not look ``exact_match`` up
        as a benchmark, it checks the sources' own benchmark.
        """
        data = tmp_path / "data"
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(data))
        write_benchmark(
            data,
            "online_mind2web",
            [
                {"task_id": "t-eval", "task_text": "Buy a kettle", "start_url": "https://a.test/"},
                {
                    "task_id": "t-other",
                    "task_text": "Find   the BLUE kettle",
                    "start_url": "https://shop.example.test/x",
                },
            ],
            subset="All",  # the loader's default subset: what `against: null` loads
        )
        write_benchmark(  # the subset the sources were recorded (and graded) on
            data,
            "online_mind2web",
            [
                {"task_id": task_id, "task_text": "x", "expected_answer": "a reference answer"}
                for task_id in ("t-eval", "t-new", "t-train")
            ],
            subset="test",
        )
        sources = tmp_path / "sources"
        steps = [ui_tars_step(click_at(1, 1))]
        graded = {"judge": eval_result(evaluator_id="exact_match", predicted_label=1)}
        for run_id, task_id, task in (
            ("eval-task", "t-eval", "Buy a kettle"),
            ("same-site-and-instruction", "t-new", "find the blue kettle"),
            ("train-task", "t-train", "Put the blue kettle in the cart"),
        ):
            write_ui_tars_run(
                sources,
                run_id,
                steps,
                benchmark="online_mind2web",
                task_id=task_id,
                task=task,
                verdicts=graded,
            )
        entry = exporter_entry(
            sources,
            tmp_path / "out",
            filters={
                "require_eval": {"evaluator_id": "exact_match"},
                "max_document_misses": None,
            },
            decontamination={"against": None, "match": "either"},
        )
        report = run_export(composer, entry)
        filtered = report.manifest["sources"]["filtered"]
        assert filtered == {
            "eval-task": "decontaminated: task 't-eval' is an evaluation task of online_mind2web",
            "same-site-and-instruction": (
                "decontaminated: site shop.example.test and instruction match an evaluation "
                "task of online_mind2web"
            ),
        }
        assert [s["path"] for s in report.manifest["sources"]["exported"]] == ["train-task"]
        assert report.manifest["decontamination"] == {
            "configured": True,
            "against": [{"benchmark": "online_mind2web", "subset": None}],
            "match": "either",
            "excluded": 2,
        }
        assert report.manifest["leak_scan"] == {"checked": 1, "unchecked": {}}

        only_ids = run_export(
            composer,
            {**entry, "decontamination": {"match": "task_id"}, "output_dir": str(tmp_path / "o2")},
        )
        assert list(only_ids.manifest["sources"]["filtered"]) == ["eval-task"]

    def test_the_documents_loaders_are_the_ones_read(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Benchmark data the export document points at with ``data_dir`` is where it is read.

        The environment's data root is empty, so a loader built from the
        registry with its defaults finds nothing: decontamination and the
        leak scan both read the document's ``eval_loader`` / ``task_loader``
        blocks — knobs and all — and a reference naming a ``subset`` reads
        only the configured loaders of that subset.
        """
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "empty"))
        data = tmp_path / "data"
        write_benchmark(
            data,
            "online_mind2web",
            [{"task_id": "t-eval", "task_text": "Buy a kettle", "start_url": "https://a.test/"}],
            subset="Hard",  # what the student is evaluated on
        )
        directory = write_benchmark(
            data,
            "online_mind2web",
            [
                {"task_id": task_id, "task_text": "x", "expected_answer": "a reference answer"}
                for task_id in ("t-eval", "t-train")
            ],
            subset="All",  # what the sources were recorded on
        )
        sources = tmp_path / "sources"
        for run_id, task_id in (("eval-task", "t-eval"), ("train-task", "t-train")):
            write_ui_tars_run(
                sources,
                run_id,
                [ui_tars_step(click_at(1, 1))],
                benchmark="online_mind2web",
                subset="All",
                task_id=task_id,
            )
        loaders = {
            layer: [
                {"benchmark_name": "online_mind2web", "subset": subset, "data_dir": str(directory)}
                for subset in subsets
            ]
            for layer, subsets in (("eval_loader", ("Hard", "All")), ("task_loader", ("Hard",)))
        }
        against = {"against": [{"benchmark": "online_mind2web", "subset": "Hard"}]}
        entry = exporter_entry(sources, tmp_path / "out", decontamination=against)
        report = run_export(composer, entry, **loaders)
        assert report.manifest["sources"]["filtered"] == {
            "eval-task": (
                "decontaminated: task 't-eval' is an evaluation task of online_mind2web/Hard"
            )
        }
        assert report.manifest["leak_scan"] == {"checked": 1, "unchecked": {}}

        only_all = {"eval_loader": loaders["eval_loader"][1:], "task_loader": []}
        entry = exporter_entry(sources, tmp_path / "o2", decontamination=against)
        with pytest.raises(ExportConfigError, match=r"subset\(s\) All only"):
            run_export(composer, entry, **only_all)

    def test_recording_on_the_eval_benchmark_itself_empties_the_export(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented fail-safe of the default: explained, not silent."""
        data = tmp_path / "data"
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(data))
        write_benchmark(
            data, "online_mind2web", [{"task_id": "t-1", "task_text": "x"}], subset="All"
        )
        sources = tmp_path / "sources"
        write_ui_tars_run(
            sources,
            "run",
            [ui_tars_step(click_at(1, 1))],
            benchmark="online_mind2web",
            task_id="t-1",
            verdicts=PASSED,
        )
        entry = exporter_entry(
            sources,
            tmp_path / "out",
            filters={
                "require_eval": {"evaluator_id": "online_mind2web"},
                "max_document_misses": None,
            },
            decontamination={"against": None},
        )
        with pytest.raises(ExportEmptyError) as excinfo:
            run_export(composer, entry)
        assert excinfo.value.counts == {"sources decontaminated": 1}
        assert not (tmp_path / "out").exists() and not (tmp_path / "out.partial").exists()

    def test_unconfigured_is_warned_and_recorded(
        self, composer: ConfigComposer, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sources = tmp_path / "sources"
        write_ui_tars_run(sources, "run", [ui_tars_step(click_at(1, 1))])
        entry = exporter_entry(sources, tmp_path / "out", decontamination={"against": None})
        with caplog.at_level(logging.WARNING, logger="spectragent.export.runner"):
            report = run_export(composer, entry)
        assert "decontamination is not configured" in caplog.text
        assert report.manifest["decontamination"]["configured"] is False

    def test_a_benchmark_that_cannot_load_fails_the_export(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "empty"))
        sources = tmp_path / "sources"
        write_ui_tars_run(sources, "run", [ui_tars_step(click_at(1, 1))])
        for against in ([{"benchmark": "online_mind2web"}], [{"benchmark": "no_such_benchmark"}]):
            entry = exporter_entry(sources, tmp_path / "out", decontamination={"against": against})
            with pytest.raises(ExportConfigError, match="decontamination against"):
                run_export(composer, entry)
        assert not (tmp_path / "out").exists()
