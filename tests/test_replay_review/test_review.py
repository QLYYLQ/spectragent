from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.aop.weaver import Weaver
from spectragent.core.capabilities.browser.archive import DomRawArchiver
from spectragent.core.capabilities.browser.archive.dom_raw import read_dom_document
from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.archive.redaction import redact_raw_capture
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    BrowserMetaObs,
    ScreenshotObs,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.decision import DecisionRequest, DecisionResult
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.typesafe import TypeSafeDirect
from spectragent.core.registries import collect_registrars
from spectragent.core.trace.encoding import (
    PendingLLMCall,
    encode_archive_file,
    encode_fragment,
    encode_llm_call,
)
from spectragent.core.trace.models import TraceConfig, TraceNegotiation, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import redact_config
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.repository import open_repository
from spectragent.replay_review.evidence import prepare_case
from spectragent.replay_review.jev import JevReplayReviewer, route_result
from spectragent.replay_review.models import (
    EvidenceRef,
    MachineFacts,
    ReviewAssessment,
    ReviewCase,
    ReviewEvidenceError,
    ReviewEvidenceOptions,
    ReviewObservation,
    ReviewPair,
    ReviewPolicy,
    ReviewQuestionSet,
    ReviewSettings,
    TaskIdentity,
    ViewportImageReadings,
)
from spectragent.replay_review.runner import ReplayReviewRunner, ReviewCallRecorder
from spectragent.replay_review.storage import ReviewItem, ReviewStore, json_file
from spectragent.schemas.action import Action
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from spectragent.schemas.step_record import StepRecord
from tests._helpers.browser_archive import archiving_capability
from tests._helpers.dom_payloads import reference_page
from tests._helpers.trace_builder import agent_result, archive_file, new_wal, step_record
from tests.test_core.test_llm.test_decision_provider import image_asset, request_dict, response_dict

SHIPPED_DMX = Path("configs/replay_review/jev_dmx.yaml")
QUESTION_IDS = ("targets_equivalent", "information_equivalent", "evidence_sufficient")


def reviewer_block() -> dict:
    """The shipped reviewer's questions, policy and evidence options, without its model."""
    block = yaml.safe_load(SHIPPED_DMX.read_text())["replay_reviewer"][0]
    return {key: value for key, value in block.items() if key != "decision_model"}


def refit(block: dict) -> dict:
    """Write a new fit entry for the block's current questions and evidence options."""
    questions = ReviewQuestionSet.model_validate(block["questions"]).sha256()
    evidence = ReviewEvidenceOptions.model_validate(block["evidence"])
    policy = block["policy"]
    for thresholds in [policy["thresholds"], *policy["overrides"]]:
        thresholds["fit"].update(
            questions_sha256=questions,
            evidence=evidence.model_dump(mode="json"),
            evidence_sha256=evidence.sha256(),
        )
    return block


def make_reviewer(**changes) -> JevReplayReviewer:
    block = refit({**reviewer_block(), **changes})
    return JevReplayReviewer(
        questions=ReviewQuestionSet.model_validate(block["questions"]),
        policy=ReviewPolicy.model_validate(block["policy"]),
        evidence=ReviewEvidenceOptions.model_validate(block["evidence"]),
    )


def settings(policy: dict | None = None) -> ReviewSettings:
    block = reviewer_block()
    return ReviewSettings.of(
        ReviewQuestionSet.model_validate(block["questions"]),
        ReviewPolicy.model_validate(policy or block["policy"]),
        ReviewEvidenceOptions.model_validate(block["evidence"]),
    )


def result(information=0.9, targets=0.8, sufficient=0.3):
    return DecisionResult.model_validate(
        {
            "requested_model": "jev-1.14.0",
            "model": "jev-1.14.0",
            "answers": {
                "targets_equivalent": {"type": "noul", "noul": targets},
                "information_equivalent": {"type": "noul", "noul": information},
                "evidence_sufficient": {"type": "noul", "noul": sufficient},
            },
        }
    )


def facts(**changes) -> MachineFacts:
    """Facts whose image fact comes from the replay log alone unless sources are given."""
    values = {
        "site_host": "www.example.test",
        "task": TaskIdentity(benchmark="bench", subset="L1", task_id="7"),
        "restoration_warning": False,
        "viewport_image_not_served": False,
        **changes,
    }
    values.setdefault(
        "viewport_image_sources",
        {"replay_log": values["viewport_image_not_served"], "capture_state": None},
    )
    return MachineFacts.model_validate(values)


def case():
    obs = ReviewObservation(
        url="https://example.test", text="[1] Search results", scope={"full": True}
    )
    ref = EvidenceRef(
        run_id="source", step=0, trace="source", manifest_sha256="a" * 64, config_hash="b" * 64
    )
    return ReviewCase(
        task="Read the results",
        next_actions=[Action(name="browser.done", params={"extracted_content": "four eggs"})],
        reference=obs,
        replay=obs.model_copy(deep=True),
        source_ref=ref,
        replay_ref=ref.model_copy(update={"run_id": "replay"}),
        facts=facts(),
    )


def fit(dataset: str = "override fixture") -> dict:
    """A fit for the shipped questions and evidence options, on another data set."""
    return {
        **reviewer_block()["policy"]["thresholds"]["fit"],
        "dataset": dataset,
        "dataset_sha256": "c" * 64,
        "fitted_on": "2026-10-21",
    }


@pytest.mark.parametrize(
    ("information", "changes", "route", "reasons"),
    [
        (0.9, {}, "candidate_acceptable", []),
        (0.45, {}, "candidate_acceptable", []),
        (0.449, {}, "further_review", ["information_between_thresholds"]),
        (0.26, {}, "further_review", ["information_between_thresholds"]),
        (0.25, {}, "investigate_difference", ["information_at_or_below_investigate_max"]),
        (0.02, {}, "investigate_difference", ["information_at_or_below_investigate_max"]),
        # The deterministic image gate comes before the thresholds.
        (
            0.97,
            {"viewport_image_not_served": True},
            "investigate_difference",
            ["viewport_image_not_served"],
        ),
        (
            0.1,
            {"viewport_image_not_served": True},
            "investigate_difference",
            ["viewport_image_not_served", "information_at_or_below_investigate_max"],
        ),
        # A warning or an unmeasured image fact only blocks acceptance.
        (0.9, {"restoration_warning": True}, "further_review", ["restoration_warning"]),
        (
            0.2,
            {"restoration_warning": True},
            "investigate_difference",
            ["information_at_or_below_investigate_max"],
        ),
        (
            0.9,
            {"viewport_image_not_served": None},
            "further_review",
            ["viewport_images_not_measurable"],
        ),
        (
            0.9,
            {"restoration_warning": True, "viewport_image_not_served": None},
            "further_review",
            ["restoration_warning", "viewport_images_not_measurable"],
        ),
    ],
)
def test_information_question_routes_and_recomputes(information, changes, route, reasons):
    raw = result(information=information, targets=0.05, sufficient=0.05)
    recorded = settings()
    machine = facts(**changes)
    assessment = route_result(raw, recorded, machine)
    assert assessment.status == "assessed" and assessment.route == route
    assert assessment.reasons == reasons and assessment.information == information
    assert assessment.thresholds is not None and assessment.thresholds.scope == "default"
    assert assessment.thresholds.fit.policy_version == "replay-triage-2"
    # Only the information question routes; the recorded questions change nothing.
    assert route_result(result(information, 0.99, 0.99), recorded, machine).route == route
    stored = ReviewAssessment.model_validate_json(
        assessment.model_dump_json(exclude={"request", "result"})
    )
    assert stored.settings is not None and stored.facts is not None
    assert route_result(raw, stored.settings, stored.facts) == assessment


@pytest.mark.parametrize(
    ("replay_log", "capture_state", "combined"),
    [
        (True, None, True),
        (None, True, True),
        (True, False, True),
        (False, True, True),
        (True, True, True),
        (False, None, False),
        (None, False, False),
        (False, False, False),
        (None, None, None),
    ],
)
def test_the_viewport_image_fact_combines_its_two_sources(replay_log, capture_state, combined):
    """Either source true makes it true; both unmeasured leave it unmeasured."""
    readings = ViewportImageReadings(replay_log=replay_log, capture_state=capture_state)
    assert readings.combined() is combined
    machine = facts(viewport_image_not_served=combined, viewport_image_sources=readings)
    stored = MachineFacts.model_validate_json(machine.model_dump_json())
    assert stored.viewport_image_sources == readings
    route = route_result(result(0.9), settings(), stored).route
    assert (
        route
        == {
            True: "investigate_difference",
            False: "candidate_acceptable",
            None: "further_review",
        }[combined]
    )


@pytest.mark.parametrize(
    ("fact", "replay_log", "capture_state"),
    [(False, False, True), (None, None, False), (True, None, None), (None, True, None)],
)
def test_facts_refuse_an_image_fact_that_does_not_combine_its_sources(
    fact, replay_log, capture_state
):
    with pytest.raises(ValidationError, match="must combine its sources"):
        facts(
            viewport_image_not_served=fact,
            viewport_image_sources={"replay_log": replay_log, "capture_state": capture_state},
        )


def site(domain: str, accept: float, investigate: float, dataset: str) -> dict:
    return {
        "scope": "site",
        "domain": domain,
        "accept_information_min": accept,
        "investigate_information_max": investigate,
        "fit": fit(dataset),
    }


def test_overrides_by_site_and_task_record_their_fit():
    policy = reviewer_block()["policy"]
    policy["overrides"] = [
        site("example.test", 0.8, 0.1, "site pairs"),
        {
            "scope": "task",
            "benchmark": "bench",
            "subset": "L1",
            "task_id": "7",
            "accept_information_min": 0.6,
            "investigate_information_max": 0.3,
            "fit": fit("task pairs"),
        },
    ]
    recorded = settings(policy)
    other_task = TaskIdentity(benchmark="bench", subset="L1", task_id="8")
    by_site = route_result(result(0.7), recorded, facts(task=other_task))
    assert by_site.thresholds is not None and by_site.thresholds.scope == "site"
    assert by_site.thresholds.domain == "example.test"
    assert by_site.thresholds.fit.dataset == "site pairs"
    assert by_site.route == "further_review"  # 0.7 would pass the default 0.45
    by_task = route_result(result(0.7), recorded, facts())
    assert by_task.thresholds is not None and by_task.thresholds.scope == "task"
    assert by_task.thresholds.task == TaskIdentity(benchmark="bench", subset="L1", task_id="7")
    assert by_task.thresholds.fit.dataset == "task pairs"
    assert by_task.route == "candidate_acceptable"
    assert route_result(result(0.28), recorded, facts()).route == "investigate_difference"
    default = route_result(result(0.7), recorded, facts(site_host="other.test", task=None))
    assert default.thresholds is not None and default.thresholds.scope == "default"
    assert default.route == "candidate_acceptable"
    persisted = json.loads(by_task.model_dump_json())
    assert persisted["thresholds"]["fit"]["dataset"] == "task pairs"
    assert persisted["thresholds"]["fit"]["evidence"]["max_text_chars"] == 6000
    assert persisted["settings"]["policy"]["overrides"][1]["task_id"] == "7"
    # Every override changes the recorded policy digest.
    assert recorded.policy_sha256 != settings().policy_sha256


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("bilibili.com", "bilibili.com"),
        ("www.bilibili.com", "bilibili.com"),
        ("account.bilibili.com", "bilibili.com"),
        # The longest matching domain wins.
        ("space.bilibili.com", "space.bilibili.com"),
        ("a.space.bilibili.com", "space.bilibili.com"),
        # A suffix that is not a whole domain label does not match.
        ("notbilibili.com", None),
        ("bilibili.com.evil.test", None),
        (None, None),
    ],
)
def test_site_scope_matches_the_domain_and_its_subdomains(host, expected):
    policy = reviewer_block()["policy"]
    policy["overrides"] = [
        site("space.bilibili.com", 0.7, 0.2, "space pages"),
        site("bilibili.com", 0.6, 0.3, "bilibili pages"),
    ]
    applied = ReviewPolicy.model_validate(policy).select(facts(site_host=host, task=None))
    assert applied.domain == expected
    assert applied.scope == ("default" if expected is None else "site")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(version="replay-triage-1"),
        lambda p: p.pop("overrides"),
        lambda p: p.pop("information_question"),
        lambda p: p["thresholds"].pop("fit"),
        lambda p: p["thresholds"]["fit"].pop("dataset_sha256"),
        lambda p: p["thresholds"].update(investigate_information_max=0.45),
        lambda p: p["thresholds"].update(accept_information_min=1.2),
        lambda p: p.update(
            overrides=[site("a.test", 0.5, 0.2, "x"), site("a.test", 0.6, 0.1, "y")]
        ),
        lambda p: p.update(overrides=[site("https://A.test", 0.5, 0.2, "x")]),
        lambda p: p.update(overrides=[site(".a.test", 0.5, 0.2, "x")]),
        lambda p: p.update(overrides=[{**site("a.test", 0.5, 0.2, "x"), "scope": "host"}]),
        lambda p: p["thresholds"]["fit"].pop("questions_sha256"),
        lambda p: p["thresholds"]["fit"].pop("evidence"),
        # The recorded evidence digest must be the digest of the recorded evidence options.
        lambda p: p["thresholds"]["fit"]["evidence"].update(max_text_chars=12000),
    ],
)
def test_policy_shape_is_refused_when_incomplete_or_inconsistent(mutate):
    policy = reviewer_block()["policy"]
    mutate(policy)
    with pytest.raises(ValidationError):
        ReviewPolicy.model_validate(policy)


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["questions"]["items"]["information_equivalent"]["instructions"].update(
            question="Is the needed information the same?"
        ),
        lambda b: b["evidence"].update(max_text_chars=12000),
        lambda b: b["evidence"].update(done_answer="keep"),
    ],
)
def test_thresholds_fitted_on_other_questions_or_evidence_are_refused(change):
    composer = ConfigComposer(registrars=collect_registrars())
    root = yaml.safe_load(SHIPPED_DMX.read_text())
    block = root["replay_reviewer"][0]
    fitted = block["policy"]["thresholds"]["fit"]
    change(block)
    questions = ReviewQuestionSet.model_validate(block["questions"]).sha256()
    evidence = ReviewEvidenceOptions.model_validate(block["evidence"]).sha256()
    with pytest.raises(Exception) as refused:
        composer.compose(root)
    message = str(refused.value)
    for digest in (fitted["questions_sha256"], fitted["evidence_sha256"], questions, evidence):
        assert digest in message
    assert "Re-fit" in message
    # A new fit entry for the changed questions and evidence options is accepted.
    refit(block)
    assert composer.compose(root).single("replay_reviewer").settings().evidence_sha256 == evidence


def test_a_site_override_fitted_elsewhere_is_refused_by_name():
    policy = reviewer_block()["policy"]
    stale = site("bilibili.com", 0.6, 0.3, "older questions")
    stale["fit"]["questions_sha256"] = "d" * 64
    policy["overrides"] = [stale]
    with pytest.raises(ValueError, match="site bilibili.com thresholds were fitted on"):
        JevReplayReviewer(
            questions=ReviewQuestionSet.model_validate(reviewer_block()["questions"]),
            policy=ReviewPolicy.model_validate(policy),
            evidence=ReviewEvidenceOptions.model_validate(reviewer_block()["evidence"]),
        )


@pytest.mark.parametrize(
    ("image", "route", "reasons"),
    [
        (True, "investigate_difference", ["input_limit", "viewport_image_not_served"]),
        (False, "further_review", ["input_limit"]),
        (None, "further_review", ["input_limit"]),
    ],
)
async def test_a_failed_model_call_still_follows_the_image_fact(image, route, reasons):
    body = {"detail": {"error_type": "max_tokens_exceeded"}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body))
    ) as client:
        reviewer = make_reviewer()
        reviewer.decision_model = TypeSafeDirect(api_key="test", http_client=client)
        subject = case()
        subject.facts = facts(viewport_image_not_served=image)
        assessed = await reviewer.review(subject)
    assert assessed.status == "input_limit" and assessed.route == route
    assert assessed.reasons == reasons and assessed.information is None
    assert assessed.request is not None and assessed.facts == subject.facts


def test_unanswered_reviews_follow_the_image_fact_without_a_model():
    missing = route_result(
        DecisionResult.model_validate(
            {"requested_model": "jev-1.14.0", "model": "jev-1.14.0", "answers": {}}
        ),
        settings(),
        facts(viewport_image_not_served=True),
    )
    assert missing.status == "invalid_response" and missing.route == "investigate_difference"
    assert missing.reasons == ["review_answer_contract", "viewport_image_not_served"]


async def test_missing_visuals_with_an_image_miss_go_to_investigation():
    reviewer = make_reviewer()
    reviewer.decision_model = TypeSafeDirect()
    subject = case()
    subject.visual_required = True
    subject.facts = facts(viewport_image_not_served=True)
    assessed = await reviewer.review(subject)
    assert assessed.status == "missing_evidence" and assessed.result is None
    assert assessed.route == "investigate_difference"
    assert assessed.reasons == ["visual_evidence_required", "viewport_image_not_served"]


@pytest.mark.parametrize("field", ["questions", "policy", "evidence"])
def test_required_yaml_fields_have_no_code_defaults(field):
    composer = ConfigComposer(registrars=collect_registrars())
    root = yaml.safe_load(SHIPPED_DMX.read_text())
    del root["replay_reviewer"][0][field]
    with pytest.raises(ValidationError, match=field):
        composer.compose(root)
    block = reviewer_block()
    typed = {
        "questions": ReviewQuestionSet.model_validate(block["questions"]),
        "policy": ReviewPolicy.model_validate(block["policy"]),
        "evidence": ReviewEvidenceOptions.model_validate(block["evidence"]),
    }
    del typed[field]
    with pytest.raises(TypeError, match=f"missing 1 required .*'{field}'"):
        JevReplayReviewer(**typed)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("question", "message"),
    [("missing_question", "information_question"), ("verdict", "information_question")],
)
def test_routing_question_must_be_a_configured_noul(question, message):
    composer = ConfigComposer(registrars=collect_registrars())
    root = yaml.safe_load(SHIPPED_DMX.read_text())
    block = root["replay_reviewer"][0]
    block["questions"]["items"]["verdict"] = {
        "type": "choice",
        "instructions": "Pick one",
        "criteria": {"same": "Same", "different": "Different"},
    }
    block["policy"]["information_question"] = question
    with pytest.raises(Exception, match=message):
        composer.compose(root)


def test_old_question_and_policy_versions_are_gone():
    for root in (Path("spectragent"), Path("configs")):
        for path in root.rglob("*"):
            if path.suffix in {".py", ".yaml", ".json"} and "generated" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="replace")
                assert "replay-relevance-1" not in text and "replay-triage-1" not in text, path


def test_request_asks_three_independent_noul_questions_and_withholds_done_answers():
    subject = case()
    subject.machine_evidence["restoration"] = {
        "actions": [
            {
                "recorded": {"name": "browser.done", "params": {"extracted_content": "four eggs"}},
                "executed": {"name": "browser.done", "params": {"extracted_content": "four eggs"}},
                "result": {"success": True, "is_done": True, "extracted_content": "four eggs"},
            }
        ]
    }
    withheld = make_reviewer().build_request(subject)
    assert tuple(withheld.questions) == QUESTION_IDS
    assert {q.type for q in withheld.questions.values()} == {"noul"}
    state = json.dumps(withheld.state, ensure_ascii=False)
    assert "four eggs" not in state and "browser.done" in state
    assert '"is_done": true' in state
    kept = make_reviewer(
        evidence={**reviewer_block()["evidence"], "done_answer": "keep"}
    ).build_request(subject)
    assert json.dumps(kept.state, ensure_ascii=False).count("four eggs") == 4
    assert kept.questions == withheld.questions
    # Withholding works on a copy; the case keeps the source answer.
    assert subject.next_actions[0].params["extracted_content"] == "four eggs"
    shipped = yaml.safe_load(SHIPPED_DMX.read_text())["replay_reviewer"][0]
    assert shipped["evidence"] == {
        "done_answer": "withhold",
        "max_text_chars": 6000,
        "max_links": 20,
        "include_screenshots": True,
    }
    assert shipped["policy"]["thresholds"]["fit"]["evidence"] == shipped["evidence"]
    assert withheld.logical_dump()["questions"] == {
        key: {**value, "type": "noul"} for key, value in shipped["questions"]["items"].items()
    }


@pytest.mark.parametrize("path", sorted(Path("configs/replay_review").glob("*.yaml")))
def test_shipped_reviewer_config_and_wrong_chat_model(path):
    composer = ConfigComposer(registrars=collect_registrars())
    root = yaml.safe_load(path.read_text())
    composed = composer.compose(root)
    reviewer = composed.single("replay_reviewer")
    assert reviewer.decision_model is composed.single("decision_model")
    assert reviewer.decision_model.timeout == 35
    root["replay_reviewer"][0]["decision_model"] = {
        "model_type": "openai",
        "provider": {"name": "direct"},
        "model": "test",
    }
    with pytest.raises(ValidationError):
        composer.compose(root)


def test_top_level_adoption_is_per_axis_and_missing_receptor_is_refused():
    composer = ConfigComposer(registrars=collect_registrars())
    root = {
        "llm": [{"model_type": "openai", "provider": {"name": "direct"}, "model": "chat"}],
        "decision_model": [
            {
                "model_type": "typesafe",
                "provider": {"name": "direct"},
                "model": "jev-1.14.0",
                "timeout": 17,
            }
        ],
        "replay_reviewer": [{"reviewer_id": "jev", **reviewer_block()}],
    }
    root["replay_reviewer"][0]["policy"]["thresholds"]["accept_information_min"] = 0.93
    # A required single receptor must be inline, even when a top-level
    # instance exists. Collection receptors adopt only their own axis.
    with pytest.raises(ValidationError, match="decision_model"):
        composer.compose(root)
    root["replay_reviewer"][0]["decision_model"] = root["decision_model"].pop()
    run = composer.compose(root)
    reviewer = run.single("replay_reviewer")
    assert reviewer.decision_model is run.single("decision_model")
    assert reviewer.decision_model is not run.single("llm")
    assert reviewer.decision_model.timeout == 17
    assert reviewer.policy.thresholds.accept_information_min == 0.93
    del root["replay_reviewer"][0]["decision_model"]
    with pytest.raises(Exception, match="decision_model"):
        composer.compose(root)


async def test_direct_python_protocol_check_and_missing_visuals():
    reviewer = make_reviewer()
    reviewer.decision_model = ChatOpenAI(model="chat")  # type: ignore[assignment]
    with pytest.raises(TypeError, match="decision"):
        await reviewer.review(case())
    reviewer.decision_model = TypeSafeDirect()
    subject = case()
    subject.visual_required = True
    assessed = await reviewer.review(subject)
    assert assessed.status == "missing_evidence" and assessed.result is None
    assert "visual_evidence_required" in assessed.reasons


def test_task_and_next_action_are_inputs_and_labels_have_no_slot():
    reviewer = make_reviewer()
    first = case()
    second = case()
    second.task = "Select the first search-history suggestion"
    second.next_actions = [Action(name="browser.click", params={"index": 1})]
    assert reviewer.build_request(first).state != reviewer.build_request(second).state
    bad = first.model_dump()
    bad["expected"] = "acceptable"
    with pytest.raises(ValidationError):
        ReviewCase.model_validate(bad)


async def test_job_roundtrip_owns_calls_and_viewer_report(tmp_path, monkeypatch):
    async def prepare(pair, reviewer):
        return case()

    monkeypatch.setattr("spectragent.replay_review.runner.prepare_case", prepare)
    raw = result().model_dump()
    wire = {key: raw[key] for key in ("model", "answers")}
    wire["usage"] = {"input_tokens": 30, "output_tokens": 5}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wire))
    ) as client:
        reviewer = make_reviewer()
        model = TypeSafeDirect(api_key="private-test-key", http_client=client)
        reviewer.decision_model = model
        pair = ReviewPair(
            source="source",
            replay="replay",
            source_step=0,
            replay_step=0,
            source_config="source.yaml",
        )
        out = tmp_path / "review"
        report = await ReplayReviewRunner(reviewer).run([pair, pair], out=out)
        assert report["summary"]["calls"] == 2 and report["summary"]["tokens_reported"] == 70
        assert report["summary"]["assessed"] == 2 and not report["recording_problems"]
        store = ReviewStore(out)
        manifest = store.manifest()
        assert manifest.status == "complete"
        call = store.call(manifest.items[0].calls[0])
        assert call.kind == "decision" and call.owner == "replay_review" and call.step is None
        assert call.owner_id == report["job_id"] and call.purpose == "replay_review"
        assert reviewer.decision_model is model and not client.is_closed
        snapshot = store.read_json("config.json")
        assert snapshot["questions"] == reviewer.questions.content()
        recorded = snapshot["settings"]
        assert recorded["questions"] == {
            "version": "replay-relevance-2",
            "sha256": reviewer.questions.sha256(),
        }
        assert recorded["policy_sha256"] == reviewer.policy.sha256()
        assert recorded["evidence"] == {
            "done_answer": "withhold",
            "max_text_chars": 6000,
            "max_links": 20,
            "include_screenshots": True,
        }
        assert recorded["evidence_sha256"] == reviewer.evidence.sha256()
        assert "question_version" not in snapshot
        item = store.read_json(manifest.items[0].assessment)
        assert item["settings"] == recorded and item["route"] == "candidate_acceptable"
        assert item["information"] == 0.9 and item["thresholds"]["scope"] == "default"
        assert item["facts"] == facts().model_dump(mode="json")
        call_answers = call.model_dump(mode="json")["result"]["answers"]
        assert set(call_answers) == set(QUESTION_IDS)
        assert report["summary"]["routes"] == {"candidate_acceptable": 2}
        assert report["summary"]["threshold_scopes"] == {"default": 2}
        repo = open_repository(tmp_path)
        listed = repo.reports()
        assert len(listed) == 1 and listed[0]["kind"] == "replay_review"
        assert repo.report(listed[0]["key"])["data"]["items"][0]["calls"][0]["kind"] == "decision"
        with pytest.raises(FileExistsError):
            await ReplayReviewRunner(reviewer).run([pair], out=out)


def test_decision_images_in_runtime_and_standalone_records(tmp_path):
    request = DecisionRequest.model_validate(request_dict())
    pending = PendingLLMCall(None, kind="decision", request=request, owner_id="run-test")
    pending.complete(
        DecisionResult.model_validate(
            {
                "requested_model": "jev-1.14.0",
                **{
                    key: value
                    for key, value in response_dict().items()
                    if key in {"model", "answers"}
                },
            }
        )
    )
    encoded = encode_llm_call(0, 0, pending)
    assert len(encoded.images) == 1
    disk = json.loads(encoded.file.data)
    assert isinstance(disk["request"]["assets"]["reference-image"]["data_base64"], dict)
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0), llm_calls=[encoded])
    trace = RunTrace(wal.run_dir)
    call = trace.llm_messages(0)[0]
    assert call.kind == "decision" and call.request == request
    image = wal.run_dir / encoded.images[0].rel_path
    image.write_bytes(b"corruption")
    with pytest.raises(Exception, match="sha256|hash|digest"):
        RunTrace(wal.run_dir).llm_messages(0)


def test_metrics_only_never_snapshots_decision_input():
    class Untouchable:
        def model_dump(self, *a, **kw):
            raise AssertionError("copied")

        def __str__(self):
            raise AssertionError("read")

    pending = PendingLLMCall(None, kind="decision", request=Untouchable(), capture_trajectory=False)
    assert pending.request is None
    pending.complete(result())
    assert pending.result is None and pending.completion is None


async def test_aop_cancellation_settles_once_without_false_result():
    entered = asyncio.Event()

    class Model:
        model = "decision-test"

        async def ainvoke(self, request):
            entered.set()
            await asyncio.Event().wait()

    recorder = ReviewCallRecorder("job")
    wrapped = Weaver([recorder]).wrap(Model(), role="llm")
    task = asyncio.create_task(wrapped.ainvoke(DecisionRequest.model_validate(request_dict())))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(recorder.calls) == 1 and recorder.calls[0].status == "cancelled"
    recorder.calls[0].complete(result())
    assert recorder.calls[0].status == "cancelled" and recorder.calls[0].result is None


def test_commit_index_ignores_orphans_and_refuses_escape_and_tampering(tmp_path):
    store = ReviewStore(tmp_path / "job")
    store.create("job", 1, {})
    store.path("orphan.json").write_text("{}")
    with pytest.raises(ValueError, match="Uncommitted"):
        store.read("orphan.json")
    with pytest.raises(ValueError, match="escapes"):
        store.path("../secret")
    store.commit_item(
        ReviewItem(index=0, status="assessed", assessment="items/0.json"),
        [json_file("items/0.json", {"route": "candidate_acceptable"})],
        [],
    )
    store.path("items/0.json").write_text("{}")
    with pytest.raises(ValueError, match="Corrupt"):
        store.read("items/0.json")


def _capture_state(failed: FailedVisibleResources | None) -> BrowserCaptureStateObs | None:
    if failed is None:
        return None
    return BrowserCaptureStateObs(
        pointer=PointerState(status="unknown"), failed_visible_resources=failed
    )


async def _recorded_pair(
    tmp_path,
    replay_log: list[dict] | None = None,
    *,
    capture_state: FailedVisibleResources | None = None,
    source_capture_state: FailedVisibleResources | None = None,
):
    """A source and its replay at step 0; capture_state is negotiated where one is given."""
    cap, driver = archiving_capability(archivers=(DomRawArchiver(),))
    await driver.start()
    try:
        await driver.observe()
        batch = await cap.collect_archive()
    finally:
        await cap.teardown()
    raw = next(f for f in batch.files if f.archiver_id == "browser.dom_raw")
    document = read_dom_document(raw.data)
    document["capture"] = redact_raw_capture(reference_page())[0]
    raw = replace(raw, data=gzip_json(document))
    url = document["url"]
    config = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    composed = ConfigComposer(registrars=collect_registrars()).compose(config)
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text(yaml.safe_dump(config))
    provenance = BranchProvenance(
        source_run_id="source",
        source_trace="source",
        checkpoint_step=0,
        branch_index=0,
        branch_group_id="group",
    )
    for name in ("source", "replay"):
        captured = _capture_state(capture_state if name == "replay" else source_capture_state)
        wal = WALWriter(tmp_path, name)
        wal.init_run(
            TraceConfig(
                task="Read the visible results",
                agent_id="browser_use",
                loop_id="react",
                resolved=redact_config(composed.top_level_cfg),
                negotiation=TraceNegotiation(
                    active_fragments=["browser.browser_meta", "browser.screenshot"]
                    + (["browser.capture_state"] if captured is not None else []),
                    active_archivers=["browser.dom_raw"]
                    + (
                        ["browser.replay_log"]
                        if name == "replay" and replay_log is not None
                        else []
                    ),
                ),
                branch=provenance if name == "replay" else None,
            )
        )
        png = base64.b64decode(image_asset()["data_base64"])
        fragments = [
            encode_fragment(0, BrowserMetaObs(url=url)),
            encode_fragment(
                0,
                ScreenshotObs(
                    image_bytes=png, source_url=url, viewport_width=1280, viewport_height=720
                ),
            ),
        ]
        if captured is not None:
            fragments.append(encode_fragment(0, captured))
        record = StepRecord(
            step=0,
            actions=[Action(name="browser.done")],
            origin="restored" if name == "replay" else "policy",
            restoration=RestorationCheck(step=0, verdict="ok") if name == "replay" else None,
        )
        extra = [encode_archive_file(raw, step=0)]
        if name == "replay" and replay_log:
            extra.append(
                archive_file(
                    "browser.replay_log",
                    "replay.jsonl",
                    "\n".join(json.dumps(entry) for entry in replay_log).encode(),
                    step=0,
                )
            )
        wal.commit_step(
            0,
            record,
            fragments=[f for f in fragments if f is not None],
            extra_files=extra,
        )
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
    return ReviewPair(
        source=str(tmp_path / "source"),
        replay=str(tmp_path / "replay"),
        source_step=0,
        replay_step=0,
        source_config=str(source_yaml),
    )


async def test_committed_alignment_and_configured_dom_recipe(tmp_path):
    pair = await _recorded_pair(tmp_path)
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    prepared = await prepare_case(pair, make_reviewer())
    assert prepared.reference.text and prepared.reference.text == prepared.replay.text
    assert prepared.facts.restoration_warning is False
    assert prepared.facts.site_host == urlsplit(prepared.reference.url).hostname
    # This replay did not bind the replay log: unknown is not "no missing image".
    assert prepared.facts.viewport_image_not_served is None
    assert prepared.facts.viewport_image_sources == ViewportImageReadings(
        replay_log=None, capture_state=None
    )
    viewport_images = prepared.machine_evidence["viewport_images"]
    assert viewport_images["replay_log"]["measurable"] is False
    assert viewport_images["capture_state"] == {
        "measurable": False,
        "reason": "the run did not negotiate capture_state",
    }
    assert prepared.reference.image is not None and prepared.source_ref.files
    assert prepared.machine_evidence["restoration"]["verdict"] == "ok"
    after = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before
    pair.source_step = 1
    with pytest.raises(ReviewEvidenceError) as error:
        await prepare_case(pair, make_reviewer())
    assert error.value.status == "missing_evidence"


async def test_replay_image_facts_reach_the_case_through_the_browser_rule(tmp_path):
    miss = {
        "kind": "miss",
        "captured_at": 0.0,
        "cycle": 1,
        "method": "GET",
        "url": "https://img.example.test/not-on-this-page.webp",
        "resource_type": "Image",
        "reason": "no_match",
        "answer": "fulfilled",
        "status": 404,
    }
    pair = await _recorded_pair(tmp_path, replay_log=[miss])
    prepared = await prepare_case(pair, make_reviewer())
    images = prepared.machine_evidence["viewport_images"]["replay_log"]
    assert images["measurable"] is True and images["images_not_served"] == 1
    assert images["referenced_in_main_document"] == 0 and images["in_viewport"] == 0
    assert images["observed_at_source"] == "replay_screenshot"
    assert prepared.facts.viewport_image_not_served is False
    assert route_result(result(0.9), settings(), prepared.facts).route == "candidate_acceptable"
    (tmp_path / "empty").mkdir()
    empty = await _recorded_pair(tmp_path / "empty", replay_log=[])
    assert (await prepare_case(empty, make_reviewer())).facts.viewport_image_not_served is False


IMAGE_MISS_OFF_PAGE = {
    "kind": "miss",
    "captured_at": 0.0,
    "cycle": 1,
    "method": "GET",
    "url": "https://img.example.test/not-on-this-page.webp",
    "resource_type": "Image",
    "reason": "no_match",
    "answer": "fulfilled",
    "status": 404,
}


def failed_images(count: int) -> FailedVisibleResources:
    names = tuple(f"image-{index}.webp" for index in range(min(count, 2)))
    return FailedVisibleResources(status="measured", count=count, identifiers=names)


@pytest.mark.parametrize(
    ("replay_log", "captured", "readings", "route"),
    [
        # Without a replay log, the capture's own failed image decides.
        (None, failed_images(1), (None, True), "investigate_difference"),
        (None, failed_images(0), (None, False), "candidate_acceptable"),
        # The sources disagree: either true makes the fact true.
        ([IMAGE_MISS_OFF_PAGE], failed_images(2), (False, True), "investigate_difference"),
        # An unknown capture reading leaves the replay log's measurement.
        (
            [IMAGE_MISS_OFF_PAGE],
            FailedVisibleResources(status="unknown", detail="the probe got no answer within 1s"),
            (False, None),
            "candidate_acceptable",
        ),
        (
            None,
            FailedVisibleResources(status="unavailable"),
            (None, None),
            "further_review",
        ),
    ],
)
async def test_capture_state_failed_images_are_the_second_source_of_the_image_fact(
    tmp_path, replay_log, captured, readings, route
):
    pair = await _recorded_pair(
        tmp_path,
        replay_log=replay_log,
        capture_state=captured,
        source_capture_state=failed_images(1),
    )
    prepared = await prepare_case(pair, make_reviewer())
    expected = ViewportImageReadings(replay_log=readings[0], capture_state=readings[1])
    assert prepared.facts.viewport_image_sources == expected
    assert prepared.facts.viewport_image_not_served is expected.combined()
    assert route_result(result(0.9), settings(), prepared.facts).route == route
    evidence = prepared.machine_evidence["viewport_images"]
    assert evidence["replay_log"]["measurable"] is (replay_log is not None)
    reading = evidence["capture_state"]
    assert reading["measurable"] is (captured.status == "measured")
    if captured.status == "measured":
        assert reading["failed_visible_images"] == captured.count
        assert reading["identifiers"] == list(captured.identifiers)
        assert reading["not_covered"] == [
            "CSS background images",
            "images inside subframe documents",
        ]
    else:
        assert reading["reason"].startswith(f"failed visible images are {captured.status}")
    # The source's own reading is kept for comparison and never enters the fact.
    assert evidence["source_capture_state"]["failed_visible_images"] == 1


async def test_missing_scope_corruption_and_source_are_distinct(tmp_path):
    pair = await _recorded_pair(tmp_path)
    config_path = Path(pair.source) / "config.json"
    config = json.loads(config_path.read_text())
    config["recording"]["trajectory"] = False
    config_path.write_text(json.dumps(config))
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, make_reviewer())
    assert failure.value.status == "scope_unrecorded"
    config_path.write_text("{broken")
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, make_reviewer())
    assert failure.value.status == "corrupt_evidence"
    pair.source = str(tmp_path / "absent")
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, make_reviewer())
    assert failure.value.status == "missing_source"


def test_chat_list_rejects_decision_and_empty_list_retains_its_meaning():
    composer = ConfigComposer(registrars=collect_registrars())
    root = {
        "hook": [
            {
                "hook_id": "prompt_contribution",
                "function": "tests.test_core.test_hooks.test_prompt_contribution:plain_contribution",
                "helper_llms": [],
            }
        ],
        "decision_model": [{"model_type": "typesafe", "provider": {"name": "direct"}}],
    }
    assert composer.compose(root).single("hook").helper_llms == []
    root["hook"][0]["helper_llms"] = root["decision_model"]
    with pytest.raises(ValidationError):
        composer.compose(root)


def test_unknown_kind_and_absent_discriminator_are_rejected(tmp_path):
    from spectragent.core.llm.base import require_inference_protocol

    class Unknown:
        async def ainvoke(self, request):
            return None

    with pytest.raises(TypeError, match="protocol"):
        require_inference_protocol(Unknown(), "decision", consumer="test")
    wal = new_wal(tmp_path)
    call = PendingLLMCall([], owner_id="run-test")
    call.complete("reply")
    encoded = encode_llm_call(0, 0, call)
    wal.commit_step(0, step_record(0), llm_calls=[encoded])
    path = wal.run_dir / encoded.file.rel_path
    record = json.loads(path.read_bytes())
    record.pop("kind")
    path.write_text(json.dumps(record))
    with pytest.raises(Exception, match="kind"):
        RunTrace(wal.run_dir).llm_messages(0)


async def test_model_result_survives_recording_failure_and_is_not_task_failure(
    tmp_path, monkeypatch
):
    async def prepare(pair, reviewer):
        return case()

    monkeypatch.setattr("spectragent.replay_review.runner.prepare_case", prepare)
    monkeypatch.setattr(
        ReviewStore,
        "commit_item",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    raw = result().model_dump()
    wire = {key: raw[key] for key in ("model", "answers")}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wire))
    ) as client:
        reviewer = make_reviewer()
        reviewer.decision_model = TypeSafeDirect(api_key="test", http_client=client)
        pair = ReviewPair(
            source="source", replay="replay", source_step=0, replay_step=0, source_config="config"
        )
        report = await ReplayReviewRunner(reviewer).run([pair], out=tmp_path / "review")
        assert report["summary"]["assessed"] == 1 and report["recording_problems"]
        assert ReviewStore(tmp_path / "review").manifest().status == "incomplete"
        assert report["summary"]["missing_usage_calls"] == 1


def test_uncertain_commit_is_reconciled_before_later_item(tmp_path, monkeypatch):
    import spectragent.replay_review.storage as storage

    store = ReviewStore(tmp_path / "job")
    store.create("job", 2, {})
    original = storage.atomic_write

    def installed_then_failed(path, data):
        original(path, data)
        raise OSError("directory sync failed after replace")

    monkeypatch.setattr(storage, "atomic_write", installed_then_failed)
    with pytest.raises(OSError):
        store.commit_item(
            ReviewItem(index=0, status="assessed", assessment="items/0.json"),
            [json_file("items/0.json", {})],
            [],
        )
    monkeypatch.setattr(storage, "atomic_write", original)
    store.commit_item(
        ReviewItem(index=1, status="assessed", assessment="items/1.json"),
        [json_file("items/1.json", {})],
        [],
    )
    assert [item.index for item in store.manifest().items] == [0, 1]


@pytest.mark.parametrize("purpose", ["prompt_helper", "agent_decision"])
def test_history_and_export_separate_decision_helpers_from_main_calls(tmp_path, purpose):
    from spectragent.core.loops.restore.source import SourceRun
    from spectragent.core.loops.validation import IncompatibleCheckpointError
    from spectragent.core.trace.errors import IncompatibleCallKindError
    from spectragent.export.canonical import canonicalise
    from spectragent.schemas.action import ActionResult
    from spectragent.schemas.restoration import Checkpoint
    from tests._helpers.trace_builder import completed_call

    wal = new_wal(tmp_path)
    pending = PendingLLMCall(
        None,
        kind="decision",
        request=DecisionRequest.model_validate(request_dict()),
        purpose=purpose,
        owner_id="run-test",
    )
    pending.complete(
        DecisionResult.model_validate(
            {
                "requested_model": "jev-1.14.0",
                **{k: v for k, v in response_dict().items() if k in {"model", "answers"}},
            }
        )
    )
    decision = encode_llm_call(0, 1, pending)
    step = StepRecord(
        step=0,
        actions=[Action(name="browser.click", params={"index": 1})],
        action_results=[ActionResult(success=True)],
    )
    wal.commit_step(0, step, llm_calls=[completed_call(0, 0, "go"), decision])
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    source = SourceRun.open(Checkpoint(trace_dir=str(wal.run_dir.resolve()), step=0))
    if purpose == "agent_decision":
        with pytest.raises(IncompatibleCheckpointError, match="non-Chat"):
            source.calls(0)
        with pytest.raises(IncompatibleCallKindError, match="training export"):
            canonicalise(RunTrace(wal.run_dir), rel_path="source")
    else:
        assert len(source.calls(0)) == 1 and source.calls(0)[0].kind == "chat"
        samples = canonicalise(RunTrace(wal.run_dir), rel_path="source")
        assert len(samples) == 1 and samples[0].thought is None


async def test_document_anchor_alignment_requires_same_url_and_loader(tmp_path):
    pair = await _recorded_pair(tmp_path)
    source = RunTrace(Path(pair.source))
    entry = source.archive_files(0, "browser.dom_raw")[0]
    loader = read_dom_document(source.read_extra(entry.path))["loader_id"]
    for name in ("source", "replay"):
        trace = RunTrace(tmp_path / name)
        meta_file = (
            next(e for e in trace.manifest.steps if e.step == 0)
            .fragments["browser.browser_meta"]
            .file
        )
        path = tmp_path / name / meta_file
        meta = json.loads(path.read_bytes())
        meta["url"] += "#History"
        meta["loader_id"] = loader
        path.write_text(json.dumps(meta))
    prepared = await prepare_case(pair, make_reviewer())
    assert prepared.reference.url.endswith("#History")
    assert not prepared.reference.scope["dom_capture_url"].endswith("#History")
    path = tmp_path / "source" / meta_file
    meta = json.loads(path.read_bytes())
    meta["url"] = meta["url"].replace("#History", "?changed=1")
    path.write_text(json.dumps(meta))
    with pytest.raises(ReviewEvidenceError) as error:
        await prepare_case(pair, make_reviewer())
    assert error.value.status == "alignment_error"
