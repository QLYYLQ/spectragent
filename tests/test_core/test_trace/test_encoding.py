"""The one on-disk encoding rule: fragments, their failure marker, LLM call records.

``core/trace/encoding.py`` decides every file name and every byte a trace
holds, for ``TraceWriter``, ``RunTrace`` and the fixtures alike
(``docs/architecture_design.md`` §5.2–§5.3). The fragment classes' own
``from_trace`` contract is pinned in
``tests/test_schemas/test_observation/test_fragment_from_trace.py``; this
file pins the layer above it — where a fragment lands, how a failure is
told apart, and what a call record says.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    Function,
    ImageURL,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.encoding import (
    PendingLLMCall,
    decode_fragment,
    encode_fragment,
    encode_llm_call,
    encode_observation,
    encode_output_format,
)
from spectragent.core.trace.models import RecordedLLMCall
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.observation._registrar import ObservationRegistrar, fragment_field_name
from spectragent.schemas.observation.failure import FragmentFailure
from tests._helpers.fragment_samples import SAMPLES
from tests._helpers.trace_builder import Reply, new_wal, prompt, step_record, usage

CASES = [(key, variant) for key, variants in SAMPLES.items() for variant in variants]


def _observation(key: str, value: Any) -> BaseModel:
    """A ``NegotiatedObservation`` carrying *value* in *key*'s slot."""
    model = build_observation_model(frozenset({key}), failable=frozenset({key}))
    return model(**{fragment_field_name(key): value})


class TestFragments:
    @pytest.mark.parametrize(("key", "variant"), CASES, ids=[f"{k}:{v}" for k, v in CASES])
    def test_every_fragment_round_trips_through_the_index(self, key: str, variant: str) -> None:
        fragment = SAMPLES[key][variant]()
        (encoded,) = encode_observation(7, _observation(key, fragment))

        assert encoded.key == key
        assert encoded.failed is False
        assert encoded.file.rel_path == f"observations/007_{key}.json"
        binary = fragment.trace_binary()
        if binary is None:
            assert encoded.artifact is None
        else:
            assert encoded.artifact is not None
            assert encoded.artifact.rel_path == f"observations/007_{key}{binary[0]}"
            assert encoded.artifact.data == binary[1]

        cls = ObservationRegistrar.get(key)
        decoded = decode_fragment(
            key,
            cls,
            json.loads(encoded.file.data),
            encoded.artifact.data if encoded.artifact else None,
        )
        assert type(decoded) is cls
        again = encode_fragment(7, decoded)
        assert again is not None
        assert json.loads(again.file.data) == json.loads(encoded.file.data)
        assert (again.artifact.data if again.artifact else None) == (
            encoded.artifact.data if encoded.artifact else None
        )

    def test_the_json_is_the_fragments_own_and_nothing_else(self) -> None:
        shot = ScreenshotObs(image_bytes=b"\x89PNG\r\n\x1a\nxx", viewport_width=1280)
        encoded = encode_fragment(0, shot)
        assert encoded is not None

        payload = json.loads(encoded.file.data)
        assert payload == shot.model_dump(mode="json", exclude=shot.trace_json_exclude())
        assert "artifact_file" not in payload

    def test_a_payload_filed_under_another_key_is_refused(self) -> None:
        encoded = encode_fragment(0, BrowserMetaObs(url="https://a"))
        assert encoded is not None
        with pytest.raises(ValueError, match="filed under 'browser.dom'"):
            decode_fragment("browser.dom", DOMObs, json.loads(encoded.file.data), None)

    def test_an_observation_that_is_not_a_fragment_model_records_nothing(self) -> None:
        class NotAnObservation(BaseModel):
            note: str = "free text"

        assert encode_observation(0, None) == []
        assert encode_observation(0, {"dom": "<div/>"}) == []
        assert encode_observation(0, NotAnObservation()) == []


class TestAFailureNeverDecodesAsAnEmptyFragment:
    """The regression the failure marker exists for.

    ``FragmentFailure``'s JSON carries no discriminator, and every field of
    these three classes has a default — so its bytes validate silently as any
    of them. Read back by key, a failed capture would be an empty fragment.
    """

    FAILABLE = [ScreenshotObs, DOMObs, BrowserMetaObs]

    @pytest.mark.parametrize("cls", FAILABLE, ids=lambda c: c.__registry_key__)
    def test_the_json_alone_is_ambiguous(self, cls) -> None:
        key = cls.__registry_key__
        failure = FragmentFailure(fragment_id=key, capability="browser", error="TimeoutError")
        encoded = encode_fragment(0, failure)
        assert encoded is not None

        # What a reader without the marker would have done — and it works,
        # which is the problem.
        impostor = cls.from_trace(json.loads(encoded.file.data), None)
        assert isinstance(impostor, cls)

    @pytest.mark.parametrize("cls", FAILABLE, ids=lambda c: c.__registry_key__)
    def test_the_index_marks_it_and_the_reader_honours_the_mark(self, tmp_path, cls) -> None:
        key = cls.__registry_key__
        failure = FragmentFailure(
            fragment_id=key, capability="browser", error="TimeoutError", detail="CDP stalled"
        )
        (encoded,) = encode_observation(0, _observation(key, failure))
        assert encoded.failed is True
        assert encoded.artifact is None

        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), fragments=[encoded])
        read = RunTrace(wal.run_dir).observation_fragment(0, key)

        assert isinstance(read, FragmentFailure)
        assert not isinstance(read, cls)
        assert (read.error, read.detail, read.capability) == (
            "TimeoutError",
            "CDP stalled",
            "browser",
        )


def _round_trip(pending: PendingLLMCall, step: int = 3, call: int = 1) -> RecordedLLMCall:
    encoded = encode_llm_call(step, call, pending)
    assert encoded.file.rel_path == f"messages/{step:03d}_call_{call:02d}.json"
    return RecordedLLMCall.model_validate_json(encoded.file.data)


def _raw(pending: PendingLLMCall) -> dict[str, Any]:
    return json.loads(encode_llm_call(0, 0, pending).file.data)


class TestLLMCallRecords:
    def test_a_completed_text_call(self) -> None:
        tool_call = ToolCall(id="c1", function=Function(name="search", arguments='{"q": "x"}'))
        pending = PendingLLMCall(prompt("what now?"))
        pending.complete(
            ChatInvokeCompletion(
                completion="click(3)",
                thinking="the button",
                usage=usage(),
                stop_reason="end_turn",
                tool_calls=[tool_call],
            )
        )
        record = _round_trip(pending)

        assert (record.step, record.call, record.status) == (3, 1, "completed")
        assert record.failure is None
        assert record.output_format is None
        assert record.completion is not None
        assert record.completion.completion == "click(3)"
        assert record.completion.thinking == "the button"
        assert record.completion.usage == usage()
        assert record.completion.stop_reason == "end_turn"
        assert record.completion.tool_calls == [tool_call]
        assert record.messages == prompt("what now?")

    def test_a_structured_reply_is_recorded_as_its_json(self) -> None:
        pending = PendingLLMCall(prompt("go"), encode_output_format(Reply))
        pending.complete(
            ChatInvokeCompletion(completion=Reply(thought="t", action="click"), usage=usage())
        )
        record = _round_trip(pending)

        assert record.completion is not None
        assert record.completion.completion == {"thought": "t", "action": "click"}
        assert record.output_format is not None
        assert record.output_format.name == "Reply"
        assert record.output_format.source == "requested"

    def test_a_refused_structured_reply_keeps_what_the_validator_saw(self) -> None:
        pending = PendingLLMCall(prompt("go"))
        pending.fail(
            StructuredOutputValidationError("mismatch", raw_output='{"a": 1}', usage=usage())
        )
        record = _round_trip(pending)

        assert record.status == "failed"
        assert record.completion is None
        assert record.failure is not None
        assert record.failure.error == "StructuredOutputValidationError"
        assert record.failure.detail == "mismatch"
        assert record.failure.raw_output == '{"a": 1}'
        assert record.failure.usage == usage()
        assert record.failure.partial_output is None

    def test_a_truncation_keeps_what_was_generated(self) -> None:
        pending = PendingLLMCall(prompt("go"))
        pending.fail(
            ModelOutputTruncatedError(
                stop_reason="max_tokens", ceiling=64, usage=usage(), partial_output='{"thou'
            )
        )
        record = _round_trip(pending)

        assert record.failure is not None
        assert record.failure.error == "ModelOutputTruncatedError"
        assert record.failure.partial_output == '{"thou'
        assert record.failure.stop_reason == "max_tokens"
        assert record.failure.usage == usage()
        assert record.failure.raw_output is None

    def test_a_dead_socket_records_error_and_detail_only(self) -> None:
        pending = PendingLLMCall(prompt("go"))
        pending.fail(TimeoutError("gateway gone"))

        assert _raw(pending)["failure"] == {"error": "TimeoutError", "detail": "gateway gone"}

    def test_a_cancelled_call_keeps_its_prompt(self) -> None:
        pending = PendingLLMCall(prompt("never answered"))
        pending.cancel(asyncio.CancelledError())
        record = _round_trip(pending)

        assert record.status == "cancelled"
        assert record.completion is None
        assert record.failure is not None
        assert record.failure.error == "CancelledError"
        assert record.messages == prompt("never answered")

    def test_a_call_still_in_flight_is_recorded_as_cancelled_without_a_failure(self) -> None:
        pending = PendingLLMCall(prompt("still waiting"))
        record = _round_trip(pending)

        assert record.status == "cancelled"
        assert record.failure is None
        assert record.completion is None
        assert record.latency_ms >= 0

    def test_the_first_outcome_wins(self) -> None:
        pending = PendingLLMCall(prompt("go"))
        pending.complete(ChatInvokeCompletion(completion="ok", usage=None))
        pending.cancel(asyncio.CancelledError())

        record = _round_trip(pending)
        assert record.status == "completed"
        assert record.failure is None

    def test_timing(self) -> None:
        before = time.time()
        pending = PendingLLMCall(prompt("go"))
        pending.complete(ChatInvokeCompletion(completion="ok", usage=None))
        record = _round_trip(pending)

        assert before <= record.started_at <= time.time()
        assert record.latency_ms >= 0

    def test_every_message_type_round_trips(self) -> None:
        messages = [
            SystemMessage(content="sys"),
            UserMessage(
                content=[
                    ContentPartTextParam(text="look"),
                    ContentPartImageParam(image_url=ImageURL(url="data:image/png;base64,AAAA")),
                ]
            ),
            AssistantMessage(
                content=None,
                tool_calls=[ToolCall(id="c1", function=Function(name="f", arguments="{}"))],
            ),
            ToolMessage(tool_call_id="c1", content="result", is_error=True),
        ]
        pending = PendingLLMCall(messages)
        pending.complete(ChatInvokeCompletion(completion="ok", usage=None))

        assert _round_trip(pending).messages == messages

    def test_the_prompt_is_the_one_registered(self) -> None:
        """A message mutated after the call went out does not rewrite the record."""
        message = UserMessage(content="as asked")
        pending = PendingLLMCall([message])
        message.content = "rewritten later"
        message.cache = True

        recorded = _round_trip(pending).messages[0]
        assert isinstance(recorded, UserMessage)
        assert (recorded.content, recorded.cache) == ("as asked", False)


class TestTheWriterNeverValidates:
    """The record is taken as it came; the reader is where it must fit."""

    def test_a_prompt_that_is_not_messages_is_still_recorded(self) -> None:
        class Opaque:
            def __str__(self) -> str:
                return "<opaque>"

        pending = PendingLLMCall([{"m": 1}, Opaque()])
        pending.complete("a bare string reply")

        raw = _raw(pending)
        assert raw["messages"] == [{"m": 1}, "<opaque>"]
        assert raw["completion"] == {"completion": "a bare string reply"}
        with pytest.raises(ValidationError):
            RecordedLLMCall.model_validate_json(encode_llm_call(0, 0, pending).file.data)

    def test_a_completion_shaped_object_is_read_by_attribute(self) -> None:
        class PlainUsage:
            prompt_tokens = 7
            completion_tokens = 3
            total_tokens = 10

        class PlainCompletion:
            completion = "done()"
            usage = PlainUsage()

        pending = PendingLLMCall(prompt("go"))
        pending.complete(PlainCompletion())
        record = _round_trip(pending)

        assert record.completion is not None
        assert record.completion.completion == "done()"
        assert record.completion.usage is not None
        assert record.completion.usage.total_tokens == 10
        assert record.completion.usage.prompt_cached_tokens is None  # not reported
        assert record.completion.tool_calls == []

    def test_a_property_that_raises_does_not_fail_the_call(self) -> None:
        class Hostile:
            completion = "ok"

            @property
            def usage(self) -> Any:
                raise RuntimeError("provider bug")

        pending = PendingLLMCall(prompt("go"))
        pending.complete(Hostile())
        record = _round_trip(pending)
        assert record.completion is not None
        assert record.completion.usage is None


class TestOutputSchemas:
    def test_the_requested_schema_is_canonical_json_at_its_digest(self) -> None:
        encoded = encode_output_format(Reply)
        digest = hashlib.sha256(encoded.file.data).hexdigest()

        assert encoded.ref.sha256 == digest
        assert encoded.ref.name == "Reply"
        assert encoded.file.rel_path == f"messages/output_formats/{digest}.json"
        assert encoded.file.content_addressed is True
        assert (
            encoded.file.data
            == json.dumps(
                Reply.model_json_schema(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )

    def test_one_schema_one_digest(self) -> None:
        assert encode_output_format(Reply) == encode_output_format(Reply)

    def test_a_call_carries_its_schema_file(self) -> None:
        schema = encode_output_format(Reply)
        encoded = encode_llm_call(0, 0, PendingLLMCall(prompt("go"), schema))

        assert encoded.output_format == schema.file
        assert json.loads(encoded.file.data)["output_format"] == {
            "name": "Reply",
            "sha256": schema.ref.sha256,
            "source": "requested",
        }

    def test_only_a_model_class_has_a_schema(self) -> None:
        with pytest.raises(TypeError, match="not a pydantic model class"):
            encode_output_format({"type": "object"})
