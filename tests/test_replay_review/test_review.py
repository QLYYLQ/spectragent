from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.aop.weaver import Weaver
from spectragent.core.capabilities.browser.archive import DomRawArchiver
from spectragent.core.capabilities.browser.archive.dom_raw import read_dom_document
from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.archive.redaction import redact_raw_capture
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
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
    ReviewCase,
    ReviewEvidenceError,
    ReviewObservation,
    ReviewPair,
    ReviewPolicy,
)
from spectragent.replay_review.runner import ReplayReviewRunner, ReviewCallRecorder
from spectragent.replay_review.storage import ReviewItem, ReviewStore, json_file
from spectragent.schemas.action import Action
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from spectragent.schemas.step_record import StepRecord
from tests._helpers.browser_archive import archiving_capability
from tests._helpers.dom_payloads import reference_page
from tests._helpers.trace_builder import agent_result, new_wal, step_record
from tests.test_core.test_llm.test_decision_provider import image_asset, request_dict, response_dict


def result(choice="acceptable", confidence=0.97, equivalent=0.95):
    return DecisionResult.model_validate(
        {
            "requested_model": "jev-1.14.0",
            "model": "jev-1.14.0",
            "answers": {
                "verdict": {
                    "type": "choice",
                    "choice": choice,
                    "confidence": confidence,
                    "probabilities": {
                        key: 0.98 if key == choice else 0.01
                        for key in ("acceptable", "material_difference", "insufficient_evidence")
                    },
                },
                "task_equivalent": {"type": "noul", "noul": equivalent},
            },
        }
    )


def case():
    obs = ReviewObservation(
        url="https://example.test", text="[1] Search results", scope={"full": True}
    )
    ref = EvidenceRef(
        run_id="source", step=0, trace="source", manifest_sha256="a" * 64, config_hash="b" * 64
    )
    return ReviewCase(
        task="Read the results",
        next_actions=[Action(name="browser.done")],
        reference=obs,
        replay=obs.model_copy(deep=True),
        source_ref=ref,
        replay_ref=ref.model_copy(update={"run_id": "replay"}),
    )


@pytest.mark.parametrize(
    ("choice", "confidence", "equivalent", "route", "reason"),
    [
        ("acceptable", 0.97, 0.95, "candidate_acceptable", None),
        ("material_difference", 0.98, 0.02, "investigate_difference", None),
        ("acceptable", 0.6, 0.95, "further_review", "low_choice_confidence"),
        ("acceptable", 0.97, 0.2, "further_review", "question_disagreement"),
        ("material_difference", 0.98, 0.8, "further_review", "question_disagreement"),
        ("acceptable", 0.97, 0.6, "further_review", "equivalence_not_established"),
        ("insufficient_evidence", 1.0, 0.1, "further_review", "insufficient_evidence"),
    ],
)
def test_policy_routes_distinct_facts_and_recomputes(choice, confidence, equivalent, route, reason):
    raw = result(choice, confidence, equivalent)
    policy = ReviewPolicy()
    assessment = route_result(raw, policy)
    assert assessment.route == route and assessment.status == "assessed"
    assert assessment.verdict == choice
    if reason:
        assert reason in assessment.reasons
    assert (
        route_result(
            DecisionResult.model_validate_json(raw.model_dump_json()),
            ReviewPolicy.model_validate_json(policy.model_dump_json()),
        )
        == assessment
    )


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
        "replay_reviewer": [{"reviewer_id": "jev", "policy": {"confidence_min": 0.93}}],
    }
    # A required single receptor must be inline, even when a top-level
    # instance exists. Collection receptors adopt only their own axis.
    with pytest.raises(ValidationError, match="decision_model"):
        composer.compose(root)
    root["replay_reviewer"][0]["decision_model"] = root["decision_model"].pop()
    run = composer.compose(root)
    reviewer = run.single("replay_reviewer")
    assert reviewer.decision_model is run.single("decision_model")
    assert reviewer.decision_model is not run.single("llm")
    assert reviewer.decision_model.timeout == 17 and reviewer.policy.confidence_min == 0.93
    del root["replay_reviewer"][0]["decision_model"]
    with pytest.raises(Exception, match="decision_model"):
        composer.compose(root)


async def test_direct_python_protocol_check_and_missing_visuals():
    reviewer = JevReplayReviewer()
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
    reviewer = JevReplayReviewer()
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
        reviewer = JevReplayReviewer()
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
        [json_file("items/0.json", {"verdict": "acceptable"})],
        [],
    )
    store.path("items/0.json").write_text("{}")
    with pytest.raises(ValueError, match="Corrupt"):
        store.read("items/0.json")


async def _recorded_pair(tmp_path):
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
        wal = WALWriter(tmp_path, name)
        wal.init_run(
            TraceConfig(
                task="Read the visible results",
                agent_id="browser_use",
                loop_id="react",
                resolved=redact_config(composed.top_level_cfg),
                negotiation=TraceNegotiation(active_archivers=["browser.dom_raw"]),
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
        record = StepRecord(
            step=0,
            actions=[Action(name="browser.done")],
            origin="restored" if name == "replay" else "policy",
            restoration=RestorationCheck(step=0, verdict="ok") if name == "replay" else None,
        )
        wal.commit_step(
            0,
            record,
            fragments=[f for f in fragments if f is not None],
            extra_files=[encode_archive_file(raw, step=0)],
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
    prepared = await prepare_case(pair, JevReplayReviewer())
    assert prepared.reference.text and prepared.reference.text == prepared.replay.text
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
        await prepare_case(pair, JevReplayReviewer())
    assert error.value.status == "missing_evidence"


async def test_missing_scope_corruption_and_source_are_distinct(tmp_path):
    pair = await _recorded_pair(tmp_path)
    config_path = Path(pair.source) / "config.json"
    config = json.loads(config_path.read_text())
    config["recording"]["trajectory"] = False
    config_path.write_text(json.dumps(config))
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, JevReplayReviewer())
    assert failure.value.status == "scope_unrecorded"
    config_path.write_text("{broken")
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, JevReplayReviewer())
    assert failure.value.status == "corrupt_evidence"
    pair.source = str(tmp_path / "absent")
    with pytest.raises(ReviewEvidenceError) as failure:
        await prepare_case(pair, JevReplayReviewer())
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
        reviewer = JevReplayReviewer()
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
    prepared = await prepare_case(pair, JevReplayReviewer())
    assert prepared.reference.url.endswith("#History")
    assert not prepared.reference.scope["dom_capture_url"].endswith("#History")
    path = tmp_path / "source" / meta_file
    meta = json.loads(path.read_bytes())
    meta["url"] = meta["url"].replace("#History", "?changed=1")
    path.write_text(json.dumps(meta))
    with pytest.raises(ReviewEvidenceError) as error:
        await prepare_case(pair, JevReplayReviewer())
    assert error.value.status == "alignment_error"
