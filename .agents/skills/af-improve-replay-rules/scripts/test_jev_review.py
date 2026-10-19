"""Routing tests for the offline helper, especially disagreement and unavailable evidence."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jev_review import DEFAULT_MODEL, assess_response, build_request, prepare_case


def envelope(verdict="acceptable", confidence: Any = 0.97, equivalent=0.85):
    return {
        "status": 200,
        "response": {
            "model": DEFAULT_MODEL,
            "answers": {
                "verdict": {
                    "type": "choice",
                    "choice": verdict,
                    "confidence": confidence,
                    "probabilities": {
                        key: float(key == verdict)
                        for key in ("acceptable", "material_difference", "insufficient_evidence")
                    },
                },
                "task_equivalent": {"type": "noul", "noul": equivalent},
            },
        },
    }


class RoutingTests(unittest.TestCase):
    def test_agreement_routes_to_candidate_without_changing_raw_outputs(self):
        value = envelope()
        before = copy.deepcopy(value)
        result = assess_response(value, model=DEFAULT_MODEL)
        self.assertEqual(result["route"], "candidate_acceptable")
        self.assertEqual(result["confidence"], 0.97)
        self.assertEqual(result["task_equivalent"], 0.85)
        self.assertEqual(value, before)

    def test_recorded_kind_of_conflict_is_escalated(self):
        result = assess_response(envelope(confidence=0.61, equivalent=0.26), model=DEFAULT_MODEL)
        self.assertEqual(result["route"], "further_review")
        self.assertEqual(
            set(result["reasons"]), {"low_choice_confidence", "choice_and_yes_no_disagree"}
        )

    def test_high_confidence_does_not_override_a_conflicting_yes_no_answer(self):
        for choice, p in (("acceptable", 0.1), ("material_difference", 0.9)):
            with self.subTest(choice=choice):
                result = assess_response(envelope(choice, 1.0, p), model=DEFAULT_MODEL)
                self.assertEqual(result["route"], "further_review")

    def test_material_difference_routes_to_investigation(self):
        result = assess_response(envelope("material_difference", 0.95, 0.07), model=DEFAULT_MODEL)
        self.assertEqual(result["route"], "investigate_difference")

    def test_absent_evidence_and_visual_work_never_take_text_fast_path(self):
        for options in ({"missing_observation": True}, {"visual_review_required": True}):
            with self.subTest(options=options):
                result = assess_response(envelope(), model=DEFAULT_MODEL, **options)
                self.assertEqual(result["route"], "further_review")
        result = assess_response(envelope("insufficient_evidence", 0.99, 0.02), model=DEFAULT_MODEL)
        self.assertEqual(result["route"], "further_review")

    def test_context_error_is_unavailable_not_a_negative_verdict(self):
        result = assess_response(
            {"status": 400, "response": {"detail": {"error_type": "max_tokens_exceeded"}}},
            model=DEFAULT_MODEL,
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("verdict", result)
        self.assertIn("reduce_irrelevant_input", result["reasons"])

    def test_wrong_model_and_invalid_probabilities_are_refused(self):
        value = envelope()
        value["response"]["model"] = "unexpected-model"
        self.assertEqual(assess_response(value, model=DEFAULT_MODEL)["status"], "invalid_response")
        for bad in (float("nan"), 1.1, True, "0.95", None):
            with self.subTest(bad=bad):
                value = envelope(confidence=bad)
                self.assertEqual(
                    assess_response(value, model=DEFAULT_MODEL)["status"], "invalid_response"
                )

    def test_case_files_resolve_relative_to_case_and_metadata_stays_out_of_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reference.txt").write_text("Recorded results", encoding="utf-8")
            case = {
                "task": "Read the results",
                "next_action": "Read titles",
                "reference_observation": {"text_file": "reference.txt"},
                "replay_observation": {"available": False},
                "evidence_refs": {"note": "not model evidence"},
                "visual_review_required": True,
            }
            path = root / "case.json"
            path.write_text(json.dumps(case))
            state, metadata = prepare_case(path)
            self.assertEqual(state["reference_observation"]["text"], "Recorded results")
            self.assertTrue(metadata["missing_observation"])
            request = build_request(state, DEFAULT_MODEL)
            self.assertNotIn("not model evidence", request["state"])
            case["expected"] = "acceptable"
            path.write_text(json.dumps(case))
            with self.assertRaises(ValueError):
                prepare_case(path)


if __name__ == "__main__":
    unittest.main()
