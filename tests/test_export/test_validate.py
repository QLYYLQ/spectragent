"""Dataset checks before anything is written (arch §4.15 "校验").

Each check is exercised on a real rendered sample (a WAL-built UI-TARS run
rendered by the shipped student), then broken in exactly one way.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.composer import ConfigComposer
from spectragent.core.data_loader.eval.online_mind2web import OnlineMind2WebEvalLoader
from spectragent.core.llm.messages import SystemMessage
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.secret_patterns import SECRET_PATTERNS
from spectragent.export import ExportRecord, canonicalise
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.errors import (
    CredentialLeakError,
    ExportValidationError,
    ReferenceLeakError,
    SampleTooLongError,
)
from spectragent.export.render import RenderedSample, StudentRenderer, TrajectoryStep
from spectragent.export.settings import LengthBudget
from spectragent.export.translate.identity import IdentityTranslator
from spectragent.export.validate import (
    ApproxTokenEstimator,
    LengthEstimator,
    ReferenceScanner,
    check_coordinates,
    check_credentials,
    check_images,
    check_length,
    image_parts,
)
from spectragent.schemas.action import Action
from tests._helpers.export_runs import REPO, student_root, write_benchmark
from tests._helpers.export_traces import click_at, ui_tars_step, write_ui_tars_run

ANSWER = "The Trader Joe's on Hollywood Blvd opens at 8 am"


@pytest.fixture
async def item(composer: ConfigComposer, tmp_path: Path) -> RenderedSample:
    trace = RunTrace(
        write_ui_tars_run(
            tmp_path,
            "run-1",
            [ui_tars_step(click_at(100, 50)), ui_tars_step(click_at(640, 360))],
            benchmark="leakbench",
            task_id="t-1",
        )
    )
    renderer = StudentRenderer(
        composer.compose(student_root()),
        untranslatable="drop_step",
        thought_source="teacher",
        thought_index_refs="drop",
    )
    samples = canonicalise(
        trace, rel_path="run-1", thought_source="teacher", teacher_cls=UITarsAgent
    )
    translator = IdentityTranslator()
    rendered = await renderer.render(
        [
            TrajectoryStep(trace, "run-1", record, translator.translate(s, trace).applied(s))
            for record, s in zip(trace.step_records, samples, strict=True)
        ]
    )
    return rendered[-1]


def _record(item: RenderedSample, images: tuple[str, ...]) -> ExportRecord:
    return ExportRecord(sample_id=item.sample.sample_id, record={}, images=images)


class TestImages:
    def test_image_count(self, item: RenderedSample, tmp_path: Path) -> None:
        assert len(image_parts(item.context)) == 2
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(b"x")
        check_images(_record(item, ("images/a.png", "images/a.png")), item, tmp_path)
        with pytest.raises(ExportValidationError, match="references 1 image"):
            check_images(_record(item, ("images/a.png",)), item, tmp_path)

    def test_a_referenced_file_must_exist(self, item: RenderedSample, tmp_path: Path) -> None:
        with pytest.raises(ExportValidationError, match="images/b.png is referenced but was not"):
            check_images(_record(item, ("images/b.png", "images/b.png")), item, tmp_path)


class TestCoordinates:
    def test_coordinates_in_bounds(self, item: RenderedSample) -> None:
        check_coordinates(item, "normalized_1000")
        off = item.sample.model_copy(update={"actions": [click_at(1280, 10)]})
        with pytest.raises(ExportValidationError, match=r"\(1280, 10\) is outside the 1280x720"):
            check_coordinates(replace(item, sample=off), "normalized_1000")

    def test_the_executed_point_lies_in_the_image(self, item: RenderedSample) -> None:
        executed = (click_at(10, 720),)
        with pytest.raises(
            ExportValidationError, match="executes at .* outside the 1280x720 image"
        ):
            check_coordinates(replace(item, target_actions=executed), "normalized_1000")

    def test_a_point_needs_an_image(self, item: RenderedSample) -> None:
        blind = item.sample.model_copy(update={"image": None})
        with pytest.raises(ExportValidationError, match="no recorded image"):
            check_coordinates(replace(item, sample=blind), "normalized_1000")

    def test_actions_without_points_are_not_checked(self, item: RenderedSample) -> None:
        done = Action(name="browser.done", params={"extracted_content": "x"})
        plain = item.sample.model_copy(update={"actions": [done], "image": None})
        check_coordinates(replace(item, sample=plain, target_actions=(done,)), "normalized_1000")


class TestReferenceLeaks:
    def _scan(
        self, item: RenderedSample, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> ReferenceScanner:
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(data_root))
        scanner = ReferenceScanner(BenchmarkLoaders())
        scanner.check(item, benchmark="online_mind2web", subset="test", task_id="t-1")
        return scanner

    def test_reference_leak_named(
        self, item: RenderedSample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_benchmark(
            tmp_path, "online_mind2web", [{"task_id": "t-1", "expected_answer": ANSWER}]
        )
        scanner = self._scan(item, tmp_path, monkeypatch)
        assert (scanner.compared, scanner.no_eval_loader, scanner.no_reference_answer) == (
            1,
            {},
            {},
        )

        seeded = SystemMessage(
            content=f"{item.context[0].text}\nHint: the  trader joe's on hollywood BLVD opens at 8 am"
        )
        with pytest.raises(ReferenceLeakError) as excinfo:
            self._scan(replace(item, context=(seeded, *item.context[1:])), tmp_path, monkeypatch)
        assert (excinfo.value.sample_id, excinfo.value.field) == ("run-1/s1", "seed")

        told = item.sample.model_copy(update={"task": f"Answer: {ANSWER}"})
        with pytest.raises(ReferenceLeakError) as excinfo:
            self._scan(replace(item, sample=told), tmp_path, monkeypatch)
        assert excinfo.value.field == "task"

    def test_an_observation_showing_the_answer_is_the_world(
        self, item: RenderedSample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_benchmark(
            tmp_path, "online_mind2web", [{"task_id": "t-1", "expected_answer": ANSWER}]
        )
        from spectragent.core.llm.messages import UserMessage

        page = UserMessage(content=f"the page says: {ANSWER}")
        self._scan(replace(item, context=(*item.context, page)), tmp_path, monkeypatch)

    @pytest.mark.parametrize("answer", ["Yes", None])
    def test_a_short_or_missing_answer_is_not_scanned_for_nor_counted_as_compared(
        self,
        item: RenderedSample,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answer: str | None,
    ) -> None:
        """Nothing was compared, so nothing is counted as compared."""
        write_benchmark(
            tmp_path, "online_mind2web", [{"task_id": "t-1", "expected_answer": answer}]
        )
        told = item.sample.model_copy(update={"task": "Is it open? Yes"})
        scanner = self._scan(replace(item, sample=told), tmp_path, monkeypatch)
        assert scanner.compared == 0
        assert scanner.no_reference_answer == {"online_mind2web": 1}
        assert scanner.no_eval_loader == {}

    def test_no_eval_loader_is_unchecked_a_missing_reference_is_an_error(
        self, item: RenderedSample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = ReferenceScanner(BenchmarkLoaders())
        scanner.check(item, benchmark="no_such_benchmark", subset="", task_id="t-1")
        assert (scanner.compared, scanner.no_eval_loader) == (0, {"no_such_benchmark": 1})
        write_benchmark(
            tmp_path, "online_mind2web", [{"task_id": "other", "expected_answer": ANSWER}]
        )
        with pytest.raises(ExportValidationError, match="no reference for task 't-1'"):
            self._scan(item, tmp_path, monkeypatch)

    def test_the_documents_eval_loader_is_read(
        self, item: RenderedSample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured eval loader — its own ``data_dir`` and ``subset`` — is where references are.

        The environment's data root is empty: a loader built from the registry
        with the source run's subset would not load, so a passing scan proves
        the configured one was read (and no default-built one beside it).
        """
        monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "empty"))
        directory = write_benchmark(
            tmp_path / "data",
            "online_mind2web",
            [{"task_id": "t-1", "expected_answer": ANSWER}],
            subset="Hard",
        )
        loader = OnlineMind2WebEvalLoader(subset="Hard", data_dir=str(directory))
        scanner = ReferenceScanner(BenchmarkLoaders(eval_loaders=[loader]))
        scanner.check(item, benchmark="online_mind2web", subset="test", task_id="t-1")
        assert (scanner.compared, scanner.no_eval_loader) == (1, {})
        told = item.sample.model_copy(update={"task": f"Answer: {ANSWER}"})
        with pytest.raises(ReferenceLeakError):
            scanner.check(
                replace(item, sample=told),
                benchmark="online_mind2web",
                subset="test",
                task_id="t-1",
            )
        with pytest.raises(ExportValidationError, match=r"\(subset Hard\) has no reference"):
            scanner.check(item, benchmark="online_mind2web", subset="test", task_id="other")


class TestCredentials:
    @pytest.mark.parametrize("label", [label for label, _pattern in SECRET_PATTERNS])
    def test_every_shape_fails_closed_naming_file_and_sample(self, label: str) -> None:
        planted = {
            "openai-style api key": "sk-proj-9fQ2zLk4TgUvXr7NmPaB6HdEwYcJ",
            "anthropic-style api key": "sk-ant-api03-9fQ2zLk4TgUvXr7NmPaB",
            "bearer token": "Bearer abcdefghijklmnopqrstuvwxyz",
            "url with credentials": "http://bot:Hunter2Hunter2@proxy.internal:8080",
            "url carrying a token": "https://shop.example/x?token=Zk3Lm9QpRt7V",
            "websocket devtools token": "ws://127.0.0.1:9222/devtools/browser/0a1b2c3d-4e5f",
        }[label]
        clean = b'{"id":"run-1/s0","messages":[]}\n'
        dirty = ('{"id":"run-1/s1","messages":[{"content":"' + planted + '"}]}\n').encode()
        check_credentials({"train.jsonl": clean})
        with pytest.raises(CredentialLeakError) as excinfo:
            check_credentials({"manifest.json": b"{}", "train.jsonl": clean + dirty})
        error = excinfo.value
        assert (error.file, error.sample_id, error.check) == (
            "train.jsonl",
            "run-1/s1",
            "credentials",
        )
        assert planted not in str(error), "the value never travels further"


class TestLength:
    def test_over_length_named_never_truncated(self, item: RenderedSample) -> None:
        estimator = ApproxTokenEstimator.from_budget(LengthBudget(max_tokens=10))
        estimated = estimator.estimate(item)
        check_length(item, estimator, estimated)
        with pytest.raises(SampleTooLongError) as excinfo:
            check_length(item, estimator, estimated - 1)
        assert (excinfo.value.sample_id, excinfo.value.estimated) == ("run-1/s1", estimated)
        assert "never truncated" in str(excinfo.value)

    def test_the_qwen_vl_count_is_per_image_pixels(self, item: RenderedSample) -> None:
        text_only = ApproxTokenEstimator(image_tokens="fixed", fixed_image_tokens=0).estimate(item)
        qwen = ApproxTokenEstimator().estimate(item)
        per_image = 46 * 26 + 2  # ceil(1280/28) x ceil(720/28) + 2
        assert qwen - text_only == 2 * per_image
        fixed = ApproxTokenEstimator(image_tokens="fixed", fixed_image_tokens=100).estimate(item)
        assert fixed - text_only == 200

    async def test_the_shipped_budget_holds_a_full_segment_of_the_shipped_student(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """``export_sft.yaml``'s ``length`` fits the longest record the shipped student renders.

        The student keeps every turn's screenshot (``memory: simple``), so the
        last sample of an unbroken ``max_steps``-long segment carries the most
        images; exporting a recording that used the whole step budget must not
        fail on length. The teacher's thoughts here are long (~800 characters
        a step), to leave room for real ones.
        """
        shipped = yaml.safe_load((REPO / "configs/flywheel/export_sft.yaml").read_text())
        budget = composer.compose(shipped).single("exporter").length
        assert budget is not None
        student = composer.compose(student_root())
        max_steps = student.single("agent").agent_loop.max_steps
        thought = " ".join(
            ["The blue kettle is listed under Appliances; the cart icon is at the top right."] * 10
        )
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path,
                "full-length",
                [
                    ui_tars_step(click_at(10 + i, 20 + i), thought=thought, color=i)
                    for i in range(max_steps)
                ],
            )
        )
        samples = canonicalise(
            trace, rel_path="full-length", thought_source="teacher", teacher_cls=UITarsAgent
        )
        translator = IdentityTranslator()
        renderer = StudentRenderer(
            student, untranslatable="drop_step", thought_source="teacher", thought_index_refs="drop"
        )
        rendered = await renderer.render(
            [
                TrajectoryStep(
                    trace, "full-length", record, translator.translate(s, trace).applied(s)
                )
                for record, s in zip(trace.step_records, samples, strict=True)
            ]
        )
        assert [item.segment for item in rendered] == [0] * max_steps, "one unbroken segment"
        estimator = ApproxTokenEstimator.from_budget(budget)
        longest = max(estimator.estimate(item) for item in rendered)
        assert len(image_parts(rendered[-1].context)) == max_steps
        assert longest <= budget.max_tokens, (longest, budget.max_tokens)

    def test_estimator_is_offline(
        self, item: RenderedSample, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_network(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the length estimator touched the network")

        monkeypatch.setattr(socket, "socket", no_network)
        monkeypatch.setattr(socket, "create_connection", no_network)
        estimator = ApproxTokenEstimator()
        assert isinstance(estimator, LengthEstimator)
        assert estimator.estimate(item) > 0
