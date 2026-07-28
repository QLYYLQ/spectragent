"""Safety-refusal tasks must reach the reverse rubric.

LexBench ships four ``eval_reverse_*`` templates whose first line reads
"a security protection test to evaluate whether the Agent can identify
and reject malicious requests" — refusing *is* the correct outcome. The
evaluator selects them from ``reference.metadata["scoring_type"]``, but
no shipped data file carries that key, so the templates were unreachable
and a correct refusal was graded as a failure.

The reachable signal is the ``L3-security`` tag, which sits on exactly
the 25 safety records.
"""

from __future__ import annotations

from spectragent.core.data_loader.eval.lexbench_browser import LexBenchBrowserEvalLoader


class TestSecurityTasksAreScoredInReverse:
    def test_the_security_split_is_marked_reverse(self) -> None:
        loader = LexBenchBrowserEvalLoader(subset="L3-security")
        refs = [loader[task_id] for task_id in loader.task_ids()]
        assert refs, "L3-security split loaded nothing"
        assert all("L3-security" in r.tags for r in refs)
        assert all(r.metadata.get("scoring_type") == "reverse" for r in refs)

    def test_only_the_security_tasks_are_marked(self) -> None:
        """A normal task must keep the normal rubric."""
        loader = LexBenchBrowserEvalLoader(subset="All")
        marked, unmarked = [], []
        for task_id in loader.task_ids():
            ref = loader[task_id]
            (marked if ref.metadata.get("scoring_type") == "reverse" else unmarked).append(ref)

        assert marked, "no task reached the reverse rubric"
        assert unmarked, "every task was flipped to reverse"
        assert all("L3-security" in r.tags for r in marked)
        assert not any("L3-security" in r.tags for r in unmarked)
        # The tag sits on exactly 25 records in the shipped data.
        assert len(marked) == 25

    def test_an_explicit_scoring_type_wins(self) -> None:
        """Derivation must not overwrite a value the data actually stated."""
        loader = LexBenchBrowserEvalLoader(subset="L3-security")
        ref = loader._normalize_raw(
            {
                "task_id": "explicit",
                "tags": ["L3-security"],
                "metadata": {"scoring_type": "normal"},
            }
        )
        assert ref.metadata["scoring_type"] == "normal"
