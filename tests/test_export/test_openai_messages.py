"""``openai_messages``: one ``{"id", "messages"}`` record per sample (arch §4.15 "格式化")."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import ContentPartImageParam, ImageURL, UserMessage
from spectragent.core.trace.reader import RunTrace
from spectragent.export import OpenAIMessagesExporter, canonicalise
from spectragent.export.render import RenderedSample, StudentRenderer, TrajectoryStep
from spectragent.export.translate.identity import IdentityTranslator
from tests._helpers.export_runs import student_root
from tests._helpers.export_traces import click_at, finished, ui_tars_step, write_ui_tars_run


class MemoryStore:
    """An image store that keeps what it is given."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, data: bytes, media_type: str) -> str:
        self.puts.append((media_type, data))
        return f"images/{hashlib.sha256(data).hexdigest()}.png"


async def _rendered(composer: ConfigComposer, tmp_path: Path) -> list[RenderedSample]:
    trace = RunTrace(
        write_ui_tars_run(
            tmp_path,
            "run-1",
            [
                ui_tars_step(click_at(640, 360), thought="open", color=10),
                ui_tars_step(finished("ok"), thought="done", color=10),  # the same screenshot
            ],
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
    return await renderer.render(
        [
            TrajectoryStep(trace, "run-1", record, translator.translate(s, trace).applied(s))
            for record, s in zip(trace.step_records, samples, strict=True)
        ]
    )


async def test_a_record_is_the_context_and_the_weighted_target(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    _first, second = await _rendered(composer, tmp_path)
    store = MemoryStore()
    record = OpenAIMessagesExporter(student_run_config="s", output_dir="o").format(second, store)
    assert record.sample_id == "run-1/s1"
    assert set(record.record) == {"id", "messages"} and record.record["id"] == "run-1/s1"
    roles = [(m["role"], m.get("weight")) for m in record.record["messages"]]
    assert roles == [
        ("system", None),
        ("user", None),
        ("assistant", 0),
        ("user", None),
        ("assistant", 1),
    ]
    assert record.record["messages"][-1]["content"] == second.target
    assert record.record["messages"][2]["content"] == _first.target


async def test_images_become_stored_paths(composer: ConfigComposer, tmp_path: Path) -> None:
    _first, second = await _rendered(composer, tmp_path)
    store = MemoryStore()
    record = OpenAIMessagesExporter(student_run_config="s", output_dir="o").format(second, store)
    parts = [
        part
        for message in record.record["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part["type"] == "image_url"
    ]
    assert len(parts) == 2 == len(record.images)
    assert [part["image_url"]["url"] for part in parts] == list(record.images)
    assert all(path.startswith("images/") and path.endswith(".png") for path in record.images)
    # Both turns show the same bytes: the store is asked twice, the record names one file.
    assert len({data for _media, data in store.puts}) == 1
    assert len(set(record.images)) == 1
    assert "base64" not in str(record.record)


async def test_an_image_that_is_not_inline_is_refused(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    first, _second = await _rendered(composer, tmp_path)
    linked = UserMessage(
        content=[ContentPartImageParam(image_url=ImageURL(url="https://cdn.example.test/a.png"))]
    )
    item = replace(first, context=(*first.context, linked))
    with pytest.raises(ValueError, match="not an inline base64 data URL"):
        OpenAIMessagesExporter(student_run_config="s", output_dir="o").format(item, MemoryStore())
    garbled = UserMessage(
        content=[ContentPartImageParam(image_url=ImageURL(url="data:image/png;base64,@@@"))]
    )
    with pytest.raises(ValueError, match="does not decode"):
        OpenAIMessagesExporter(student_run_config="s", output_dir="o").format(
            replace(first, context=(*first.context, garbled)), MemoryStore()
        )
