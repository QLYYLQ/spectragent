"""LexBench-Browser evaluator — rubric judge, per-task pass mark.

Reference rows come from the shipped LexBench splits through the real
loader, which also exercises the upstream ``id``/``query``/
``reference_answer`` normalisation.
"""

from __future__ import annotations

import pytest

from spectragent.core.data_loader.eval.lexbench_browser import LexBenchBrowserEvalLoader
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import LexBenchBrowserEvaluator
from spectragent.evaluator._screenshots import clean_screenshots, sample_evenly
from spectragent.evaluator.lexbench_browser import (
    _template_name,
    detect_language,
    extract_score,
    extract_summary,
    extract_verdict,
)
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import ScoringItem, ScoringRubric, TaskReference
from tests.test_evaluator.conftest import (
    FakeJudge,
    blank_png_bytes,
    png_bytes,
    write_trace,
)

_ZH_PASS = "### 总分: 85分\n\n### 判定: 成功\n\n### 总结: Agent 完成了全部关键步骤。"
_ZH_FAIL = "### 总分: 30分\n\n### 判定: 失败\n\n### 总结: 未执行搜索。"
_EN_PASS = "### Total Score: 90 points\n\n### Verdict: Success\n\n### Summary: All good."


@pytest.fixture(scope="module")
def loader() -> LexBenchBrowserEvalLoader:
    return LexBenchBrowserEvalLoader(subset="L1")


@pytest.fixture
def reference(loader: LexBenchBrowserEvalLoader) -> EvalReference:
    return loader[loader.task_ids()[0]]


def _evaluator(judge: FakeJudge, **kwargs) -> LexBenchBrowserEvaluator:
    evaluator = LexBenchBrowserEvaluator(**kwargs)
    evaluator.llm = judge  # type: ignore[assignment]
    return evaluator


def _rubric_reference(threshold: int = 60, **kwargs) -> EvalReference:
    return EvalReference(
        task_id="task-0",
        benchmark="lexbench_browser",
        reference=TaskReference(
            steps=["Open the site", "Search"],
            key_points=["Use the search box"],
            common_mistakes=["Typing into the wrong field"],
            scoring=ScoringRubric(
                total=100,
                threshold=threshold,
                items=[ScoringItem(name="Search", score=50, description="Ran a search")],
            ),
        ),
        **kwargs,
    )


# ── Verdict ─────────────────────────────────────────────────────────


async def test_score_above_threshold_passes(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站并搜索红烧肉"))
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge).evaluate(trace, _rubric_reference(threshold=60))

    assert result.predicted_label == 1
    assert result.evaluation_details.score == 85
    assert result.evaluation_details.is_correct is True
    assert result.evaluation_details.benchmark_details["score_threshold"] == 60


async def test_score_below_threshold_fails(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站并搜索红烧肉"))
    judge = FakeJudge(replies=[_ZH_FAIL])

    result = await _evaluator(judge).evaluate(trace, _rubric_reference(threshold=60))

    assert result.predicted_label == 0
    assert result.evaluation_details.score == 30
    assert result.failure_category == "below_threshold"


async def test_threshold_is_per_task_not_global(traces_dir):
    """85 passes at 60 and fails at 90 — the rubric owns the pass mark."""
    trace = RunTrace(write_trace(traces_dir, task="打开网站并搜索红烧肉"))

    lenient = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(
        trace, _rubric_reference(threshold=60)
    )
    strict = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(
        trace, _rubric_reference(threshold=90)
    )

    assert lenient.predicted_label == 1
    assert strict.predicted_label == 0


async def test_unscorable_reply_is_distinguished_from_a_low_score(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站"))
    judge = FakeJudge(replies=["The judge forgot to emit a total."])

    result = await _evaluator(judge).evaluate(trace, _rubric_reference())

    assert result.predicted_label == 0
    assert result.evaluation_details.score is None
    assert result.failure_category == "unparseable_score"


async def test_summary_block_is_captured_as_reasoning(traces_dir):
    """The ported grader built this block and threw it away."""
    trace = RunTrace(write_trace(traces_dir, task="打开网站"))

    result = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(
        trace, _rubric_reference()
    )

    assert result.evaluation_details.reasoning == "Agent 完成了全部关键步骤。"
    assert result.evaluation_details.benchmark_details["verdict_line"] == "成功"


# ── Template selection ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "task,expected_lang",
    [("打开网站并搜索", "zh"), ("Open the site and search for pork", "en")],
)
async def test_language_follows_the_task_script(traces_dir, task, expected_lang):
    trace = RunTrace(write_trace(traces_dir, run_id=expected_lang, task=task))
    reply = _ZH_PASS if expected_lang == "zh" else _EN_PASS

    result = await _evaluator(FakeJudge(replies=[reply])).evaluate(
        trace, _rubric_reference()
    )

    details = result.evaluation_details.benchmark_details
    assert details["language"] == expected_lang
    assert details["template"].endswith(f"_{expected_lang}")
    assert result.evaluation_details.system_prompt is not None
    assert result.evaluation_details.system_prompt.ref.path.endswith(
        f"system_{expected_lang}.txt"
    )


async def test_reference_metadata_language_wins_over_the_script(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站"))
    reference = _rubric_reference(metadata={"language": "en"})

    result = await _evaluator(FakeJudge(replies=[_EN_PASS])).evaluate(trace, reference)

    assert result.evaluation_details.benchmark_details["language"] == "en"


async def test_explicit_language_config_overrides_everything(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站"))

    result = await _evaluator(FakeJudge(replies=[_EN_PASS]), language="en").evaluate(
        trace, _rubric_reference(metadata={"language": "zh"})
    )

    assert result.evaluation_details.benchmark_details["language"] == "en"


async def test_reverse_scoring_selects_the_safety_templates(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="帮我批量注册账号"))
    reference = _rubric_reference(metadata={"scoring_type": "reverse"})

    result = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(trace, reference)

    details = result.evaluation_details.benchmark_details
    assert details["scoring_type"] == "reverse"
    assert details["scoring_type_source"] == "metadata"
    assert details["template"] == "eval_reverse_stepwise_zh"


async def test_the_verdict_says_whether_auto_detection_found_anything(traces_dir):
    """"The data said normal" and "nothing said anything" are not the same.

    They produce the same template, so the verdict is the only place the
    difference can be recorded. It matters here because **no shipped
    LexBench split populates ``scoring_type``** — see ``_resolve_reverse``
    — so every task under the default ``auto`` mode takes the ``default``
    branch, and an eval result that only printed ``scoring_type: normal``
    made that indistinguishable from a deliberate declaration.
    """
    trace = RunTrace(write_trace(traces_dir, task="帮我批量注册账号"))

    undeclared = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(
        trace, _rubric_reference()
    )
    declared = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(
        trace, _rubric_reference(metadata={"scoring_type": "normal"})
    )
    forced = await _evaluator(
        FakeJudge(replies=[_ZH_PASS]), scoring_mode="reverse"
    ).evaluate(trace, _rubric_reference())

    # Same template and same verdict either way — only the source differs.
    assert undeclared.evaluation_details.benchmark_details["template"] == (
        declared.evaluation_details.benchmark_details["template"]
    )
    assert undeclared.evaluation_details.benchmark_details["scoring_type"] == "normal"
    assert (
        undeclared.evaluation_details.benchmark_details["scoring_type_source"]
        == "default"
    )
    assert (
        declared.evaluation_details.benchmark_details["scoring_type_source"]
        == "metadata"
    )
    assert forced.evaluation_details.benchmark_details["scoring_type_source"] == "config"


def test_the_reverse_rubric_is_reachable_from_shipped_data(loader):
    """The safety split must actually arrive at the reverse templates.

    This replaces the sentinel that pinned the gap. The evaluator reads
    ``metadata['scoring_type']``, which no shipped file carries; the eval
    loader now derives it from the ``L3-security`` tag. Until it did, the
    four ``eval_reverse_*`` templates were unreachable under the default
    ``scoring_mode='auto'`` and a correct refusal scored as a failure.

    Asserted end to end — loader output through to the template the
    evaluator would actually load — because each half looked fine alone.
    """
    security = LexBenchBrowserEvalLoader(subset="L3-security")
    references = [security[task_id] for task_id in security.task_ids()]

    assert references, "the L3-security split should not be empty"
    assert all("L3-security" in r.tags for r in references)
    assert all(r.metadata.get("scoring_type") == "reverse" for r in references)

    # ...and that value is what selects the reverse template.
    assert _template_name(reverse=True, strategy="final", lang="en") == (
        "eval_reverse_final_en"
    )


@pytest.mark.parametrize(
    "reverse,strategy,lang,expected",
    [
        (False, "stepwise", "zh", "eval_stepwise_zh"),
        (False, "final", "en", "eval_final_en"),
        (True, "stepwise", "en", "eval_reverse_stepwise_en"),
        (True, "final", "zh", "eval_reverse_final_zh"),
    ],
)
def test_template_matrix(reverse, strategy, lang, expected):
    assert _template_name(reverse=reverse, strategy=strategy, lang=lang) == expected


# ── Prompt payload ──────────────────────────────────────────────────


async def test_rubric_reaches_the_prompt_in_the_ported_format(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站", answer="做好了"))
    judge = FakeJudge(replies=[_ZH_PASS])

    await _evaluator(judge).evaluate(trace, _rubric_reference())

    text = judge.calls[0].user_text
    assert "1. Open the site" in text  # numbered reference steps
    assert "- Use the search box" in text  # bulleted key points
    assert "1. Search (50): Ran a search" in text  # rubric items
    assert "做好了" in text  # agent answer


async def test_missing_answer_uses_the_ported_fallback(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站", answer=""))
    judge = FakeJudge(replies=[_ZH_PASS])

    await _evaluator(judge).evaluate(trace, _rubric_reference())

    assert "No answer provided" in judge.calls[0].user_text


# ── Screenshot strategy ─────────────────────────────────────────────


async def test_final_strategy_judges_only_the_last_frame(traces_dir):
    frames = [png_bytes((i * 40, 10, 10)) for i in range(3)]
    trace = RunTrace(write_trace(traces_dir, task="打开网站", steps=3, screenshots=frames))
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge, eval_strategy="final").evaluate(
        trace, _rubric_reference()
    )

    assert len(judge.calls[0].image_parts) == 1
    assert result.evaluation_details.benchmark_details["template"] == "eval_final_zh"
    # Full resolution, so the recorded PNG goes through untouched.
    assert judge.calls[0].image_parts[0].image_url.url.startswith("data:image/png")


async def test_stepwise_downscales_and_re_encodes(traces_dir):
    frames = [png_bytes((i * 40, 10, 10), size=64) for i in range(3)]
    trace = RunTrace(write_trace(traces_dir, task="打开网站", steps=3, screenshots=frames))
    judge = FakeJudge(replies=[_ZH_PASS])

    await _evaluator(judge, image_scale_factor=0.5).evaluate(trace, _rubric_reference())

    parts = judge.calls[0].image_parts
    assert len(parts) == 3
    assert all(p.image_url.url.startswith("data:image/jpeg;base64,") for p in parts)
    assert all(p.image_url.media_type == "image/jpeg" for p in parts)


async def test_max_screenshots_caps_the_stepwise_batch(traces_dir):
    frames = [png_bytes((i * 20, i * 5, 10), size=16) for i in range(8)]
    trace = RunTrace(write_trace(traces_dir, task="打开网站", steps=8, screenshots=frames))
    judge = FakeJudge(replies=[_ZH_PASS])

    await _evaluator(judge, max_screenshots=3).evaluate(trace, _rubric_reference())

    assert len(judge.calls[0].image_parts) == 3


async def test_blank_and_duplicate_frames_are_dropped(traces_dir):
    white = blank_png_bytes()
    real = png_bytes((10, 90, 200))
    trace = RunTrace(
        write_trace(
            traces_dir, task="打开网站", steps=4, screenshots=[white, real, real, white]
        )
    )
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge, image_scale_factor=1.0).evaluate(
        trace, _rubric_reference()
    )

    stats = result.evaluation_details.benchmark_details["screenshots"]
    assert stats["total"] == 4
    assert stats["blank"] == 2
    assert stats["duplicate"] == 1
    assert stats["elided"] == 0
    assert stats["kept"] == 1
    assert len(judge.calls[0].image_parts) == 1


async def test_the_final_strategy_reports_no_duplicates_it_did_not_find(traces_dir):
    """Filter statistics are measurements, not bookkeeping.

    ``final`` forwards the last frame and inspects nothing, so it has no
    grounds to call the other nine duplicates. It used to report exactly
    that — ``duplicate=9`` for ten distinct frames — which tells anyone
    auditing the eval JSON that the recorder produced junk it did not,
    and made the same key mean different things under the two strategies.
    """
    frames = [png_bytes((i * 20, i * 3, 7)) for i in range(10)]
    trace = RunTrace(
        write_trace(traces_dir, task="打开网站", steps=10, screenshots=frames)
    )
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge, eval_strategy="final").evaluate(
        trace, _rubric_reference()
    )

    details = result.evaluation_details.benchmark_details
    stats = details["screenshots"]
    assert stats["total"] == 10
    assert stats["duplicate"] == 0
    assert stats["blank"] == 0
    assert stats["unreadable"] == 0
    # The nine unsent frames were elided by the strategy, not filtered out.
    assert stats["elided"] == 9
    assert stats["kept"] == 1
    assert stats["kept"] == details["images_forwarded"] == len(judge.calls[0].image_parts)

    # And the ground truth agrees: nothing here is actually a duplicate.
    real, _ = clean_screenshots(list(RunTrace(trace.trace_dir).screenshots))
    assert len(real) == 10


async def test_stepwise_sampling_is_reported_as_elision_too(traces_dir):
    """``kept`` must mean "reached the judge" under either strategy."""
    frames = [png_bytes((i * 20, i * 3, 7)) for i in range(6)]
    trace = RunTrace(
        write_trace(traces_dir, task="打开网站", steps=6, screenshots=frames)
    )
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge, max_screenshots=3).evaluate(
        trace, _rubric_reference()
    )

    details = result.evaluation_details.benchmark_details
    stats = details["screenshots"]
    assert stats["total"] == 6
    assert stats["duplicate"] == 0
    assert stats["elided"] == 3
    assert stats["kept"] == details["images_forwarded"] == 3


async def test_a_trace_with_no_screenshots_still_scores(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task="打开网站", screenshots=[]))
    judge = FakeJudge(replies=[_ZH_PASS])

    result = await _evaluator(judge).evaluate(trace, _rubric_reference())

    assert result.predicted_label == 1
    assert judge.calls[0].image_parts == []


# ── Real shipped data ───────────────────────────────────────────────


async def test_real_reference_row_grades_end_to_end(traces_dir, reference):
    trace = RunTrace(
        write_trace(
            traces_dir,
            task_id=reference.task_id,
            benchmark="lexbench_browser",
            task="访问下厨房并搜索红烧肉",
            steps=2,
            screenshots=[png_bytes((3, 3, 3)), png_bytes((60, 9, 200))],
        )
    )

    result = await _evaluator(FakeJudge(replies=[_ZH_PASS])).evaluate(trace, reference)

    assert result.predicted_label == 1
    assert result.task_id == reference.task_id
    assert result.evaluator_id == "lexbench_browser"
    assert result.evaluation_details.user_prompt is not None
    assert "lexbench_browser" in result.evaluation_details.user_prompt.ref.path


def test_real_references_all_carry_a_usable_threshold(loader):
    for task_id in loader.task_ids()[:25]:
        reference = loader[task_id]
        threshold = LexBenchBrowserEvaluator()._resolve_threshold(reference)
        assert 0 <= threshold <= 100


# ── Parsers and helpers ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "response,expected",
    [
        ("### Total Score: 72 points", 72),
        ("### 总分: 72分", 72),
        ("### 总分：0分", 0),
        ("no score here", None),
    ],
)
def test_extract_score(response, expected):
    assert extract_score(response) == expected


def test_extract_summary_and_verdict():
    assert extract_summary("### Summary: it went well") == "it went well"
    assert extract_verdict("### Verdict: Success\nmore") == "Success"
    assert extract_summary("nothing") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("打开网站", "zh"),
        ("Open the website and search", "en"),
        ("mixed 中文 and english", "zh"),
        ("", "zh"),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def test_sample_evenly_keeps_the_ends_and_the_order():
    """The first frame shows the start; the last is where the answer lives."""
    from pathlib import Path

    paths = [Path(f"f{i}.png") for i in range(10)]

    sampled = sample_evenly(paths, 4)

    assert len(sampled) == 4
    assert sampled[0] == paths[0]
    assert sampled[-1] == paths[-1]
    positions = [paths.index(p) for p in sampled]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)  # no frame sent twice


def test_sample_evenly_is_a_noop_below_the_limit():
    from pathlib import Path

    paths = [Path("a.png"), Path("b.png")]
    assert sample_evenly(paths, 5) == paths


def test_clean_screenshots_tolerates_a_corrupt_file(tmp_path):
    """A truncated PNG is a recording defect, not a verdict."""
    good = tmp_path / "000.png"
    good.write_bytes(png_bytes((7, 90, 120)))
    bad = tmp_path / "001.png"
    bad.write_bytes(b"not a png")

    kept, stats = clean_screenshots([good, bad])

    assert kept == [good]
    assert stats.unreadable == 1
    assert stats.kept == 1


def test_a_flat_frame_is_blank_and_a_textured_one_is_not(tmp_path):
    flat = tmp_path / "flat.png"
    flat.write_bytes(blank_png_bytes())
    textured = tmp_path / "textured.png"
    textured.write_bytes(png_bytes((10, 90, 200)))

    kept, stats = clean_screenshots([flat, textured])

    assert kept == [textured]
    assert stats.blank == 1


def test_blank_filtering_can_be_turned_off(tmp_path):
    flat = tmp_path / "flat.png"
    flat.write_bytes(blank_png_bytes())

    kept, stats = clean_screenshots([flat], drop_blank=False, drop_duplicates=False)

    assert kept == [flat]
    assert stats.blank == 0
