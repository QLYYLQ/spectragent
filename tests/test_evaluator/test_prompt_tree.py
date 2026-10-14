"""One prompt tree, keyed by registry key, resolved without an alias table.

The repository used to carry two byte-identical evaluator prompt trees:
``spectragent/prompts/browser/evaluators/`` (git-tracked, zero Python
consumers, and the one a wheel shipped) and ``benchmarks/prompts/``
(gitignored, the one the real graders read). Neither directory name
matched a registry key. These tests pin the merged result.
"""

from __future__ import annotations

import re

import pytest

from spectragent.evaluator import EvaluatorRegistrar
from spectragent.evaluator.prompts import EVALUATOR_PROMPT_ROOT, load_prompt
from spectragent.prompts import PROMPT_PATH, PromptNotFoundError

#: Every prompt each evaluator loads, and the placeholders it substitutes.
_EXPECTED: dict[str, dict[str, set[str]]] = {
    "browsecomp": {
        "grader_user": {"question", "correct_answer", "response"},
    },
    "online_mind2web": {
        "identify_key_points_system": set(),
        "identify_key_points_user": {"task"},
        "judge_image_system": set(),
        "judge_image_user": {"task", "key_points"},
        "webjudge_system": set(),
        "webjudge_user": {"task", "key_points", "last_actions", "thoughts"},
        "webjudge_user_no_images": {"task", "key_points", "last_actions"},
    },
    # LexBench's ten templates differ from each other by design (rubric
    # items only in normal mode, screenshot_count only in stepwise), so
    # they are checked against what the evaluator supplies rather than
    # against a hand-copied literal that would rot.
    "lexbench_browser": {
        "system_en": set(),
        "system_zh": set(),
        "recorded_evidence_context": {"recorded_at", "task_dates"},
    },
}

_LEXBENCH_TEMPLATES = [
    "eval_stepwise_en",
    "eval_stepwise_zh",
    "eval_final_en",
    "eval_final_zh",
    "eval_reverse_stepwise_en",
    "eval_reverse_stepwise_zh",
    "eval_reverse_final_en",
    "eval_reverse_final_zh",
]


def test_the_dead_duplicate_tree_is_gone():
    """The whole ``browser/`` parent is gone now that agents left it too."""
    assert not (PROMPT_PATH / "browser").exists()


def test_scoring_prompts_are_not_under_browser():
    """A judge prompt is not a browser concern; SWE-bench will share this tree."""
    assert EVALUATOR_PROMPT_ROOT == PROMPT_PATH / "evaluators"
    assert EVALUATOR_PROMPT_ROOT.is_dir()


def test_every_prompt_directory_is_exactly_a_registry_key():
    on_disk = {
        child.name
        for child in EVALUATOR_PROMPT_ROOT.iterdir()
        if child.is_dir() and not child.name.startswith("__")
    }
    assert on_disk <= set(EvaluatorRegistrar.keys()), (
        f"Prompt directories {sorted(on_disk - set(EvaluatorRegistrar.keys()))} "
        f"match no registry key, so an evaluator would need an alias table."
    )
    assert on_disk == set(_EXPECTED)


@pytest.mark.parametrize(
    "evaluator_id,name",
    [(k, n) for k, names in _EXPECTED.items() for n in names],
)
def test_each_prompt_loads_and_hashes(evaluator_id, name):
    text, ref = load_prompt(evaluator_id, name)
    assert text.strip()
    assert ref.path == f"evaluators/{evaluator_id}/{name}.txt"
    assert len(ref.content_hash) == 64


@pytest.mark.parametrize(
    "evaluator_id,name,expected",
    [(k, n, p) for k, names in _EXPECTED.items() for n, p in names.items()],
)
def test_placeholders_match_what_the_evaluator_supplies(evaluator_id, name, expected):
    """A template placeholder with no supplied value is a KeyError at grade time."""
    text, _ = load_prompt(evaluator_id, name)
    found = set(re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", text))
    assert found == expected


def test_missing_prompt_names_the_directory_and_alternatives():
    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("browsecomp", "no_such_prompt")
    message = str(excinfo.value)
    assert "no_such_prompt" in message
    assert "grader_user" in message


@pytest.mark.parametrize("name", _LEXBENCH_TEMPLATES)
def test_lexbench_templates_only_use_supplied_placeholders(name, traces_dir):
    """Every ``{placeholder}`` must be something ``_build_params`` supplies.

    One that is not is a ``KeyError`` at grade time — after the agent
    already burned a full trajectory.
    """
    from spectragent.core.trace.reader import RunTrace
    from spectragent.evaluator import LexBenchBrowserEvaluator, TraceView
    from spectragent.schemas.eval_reference import EvalReference
    from tests.test_evaluator.conftest import write_trace

    evaluator = LexBenchBrowserEvaluator()
    view = TraceView(RunTrace(write_trace(traces_dir, benchmark="lexbench_browser")))
    supplied = set(
        evaluator._build_params(
            view=view,
            reference=EvalReference(task_id="t", benchmark="lexbench_browser"),
            task_text="t",
            screenshot_count=0,
        )
    )

    text, _ = load_prompt("lexbench_browser", name)
    found = set(re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", text))
    assert found <= supplied, f"{name} needs unsupplied {sorted(found - supplied)}"
    assert found, f"{name} substitutes nothing — it cannot be task-specific"


def test_lexbench_templates_cover_the_whole_selection_matrix():
    """Four modes x two languages; a gap would surface only in production."""
    from spectragent.evaluator.lexbench_browser import _template_name

    for reverse in (True, False):
        for strategy in ("stepwise", "final"):
            for lang in ("en", "zh"):
                name = _template_name(reverse=reverse, strategy=strategy, lang=lang)
                assert (EVALUATOR_PROMPT_ROOT / "lexbench_browser" / f"{name}.txt").is_file()
