"""Each image a prompt sent inline is stored once per run, and read back in place.

``docs/architecture_design.md`` §5.3 ("调用记录里的图片只存一份"): a call record
holds the whole prompt, and an agent that keeps every screenshot in its memory
sends all of them on every call. The encoding layer replaces each inline image
— a base64 ``data:`` URL under ``image_url`` — by a ``PromptImageRef`` to a
content-addressed file under ``messages/images/``, written by the first commit
that carries it; ``RunTrace`` puts the text back before it validates the
record. Pinned here:

- a 12-step run whose prompts carry a growing history stores each distinct
  screenshot once, ``messages/`` stays near their size, and every prompt reads
  back byte for byte;
- the round trip is lossless for every way a payload can be written, standard
  base64 or not, and nothing that is not an inline image is touched;
- the commit protocol: an image's bytes are durable before the manifest names
  it, it is written once per run, and a crash before the manifest leaves it
  invisible;
- every way a referenced image can be damaged is a ``TraceSchemaError`` naming
  the file at fault.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_json

from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace import encoding, fsio
from spectragent.core.trace.encoding import (
    IMAGE_REFS_KEY,
    PROMPT_IMAGES_DIR,
    EncodedLLMCall,
    PendingLLMCall,
    TraceFile,
    call_path,
    encode_llm_call,
    prompt_image_path,
    prompt_image_text,
    restore_prompt_images,
    sha256_hex,
    store_prompt_images,
)
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import RecordedLLMCall, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from tests._helpers.fragment_samples import JPEG_BYTES, PNG_BYTES
from tests._helpers.trace_builder import (
    agent_result,
    as_chat,
    data_url,
    extra,
    new_wal,
    read_back,
    screenshot,
    step_record,
    usage,
)


def _image(url: str) -> ContentPartImageParam:
    return ContentPartImageParam(image_url=ImageURL(url=url, detail="high"))


def _call(step: int, call: int, messages: list[Any], *, completed: bool = True) -> EncodedLLMCall:
    pending = PendingLLMCall(messages)
    if completed:
        pending.complete(ChatInvokeCompletion(completion="click(1, 1)", usage=usage()))
    return encode_llm_call(step, call, pending)


def _as_sent(messages: list[Any]) -> bytes:
    """The prompt's serialised form as the call record held it before images were stored apart."""
    return to_json(PendingLLMCall(messages).messages, indent=2)


def _as_read(call: RecordedLLMCall) -> bytes:
    """The serialised form of a prompt read back."""
    return to_json(
        [message.model_dump(mode="json") for message in as_chat(call).messages], indent=2
    )


# ── a growing history of screenshots ──────────────────────────


STEPS = 12
#: Each step's screenshot: a PNG of its own, random bytes after the signature so
#: neither gzip nor a lucky collision makes the numbers look better than they are.
#: About the size of a real one (the smoke run's averaged 390 KB).
SCREENSHOT_SIZE = 128 * 1024


def screenshot_png(step: int) -> bytes:
    return PNG_BYTES + random.Random(step).randbytes(SCREENSHOT_SIZE)


def history_prompt(upto: int) -> list[Any]:
    """The prompt at step *upto* of an agent whose memory keeps every screenshot.

    One user turn per step so far, each with that step's screenshot, and the
    reply in between — the shape UI-TARS sends.
    """
    messages: list[Any] = [SystemMessage(content="You operate a browser.")]
    for step in range(upto + 1):
        messages.append(
            UserMessage(
                content=[
                    ContentPartTextParam(text=f"Step {step}: what next?"),
                    _image(data_url(screenshot_png(step))),
                ]
            )
        )
        if step < upto:
            messages.append(AssistantMessage(content=f"Action: click(point='({step},{step})')"))
    return messages


@dataclass
class HistoryRun:
    run_dir: Path
    #: The prompt each committed step's call sent, and the trailing call's.
    prompts: dict[int, list[Any]] = field(default_factory=dict)
    trailing: list[Any] = field(default_factory=list)
    #: Bytes of every call record as the inline encoding would have written it.
    inline_bytes: int = 0


@pytest.fixture()
def history(tmp_path: Path) -> HistoryRun:
    wal = new_wal(tmp_path)
    run = HistoryRun(wal.run_dir)
    for step in range(STEPS):
        run.prompts[step] = history_prompt(step)
        call = _call(step, 0, run.prompts[step])
        run.inline_bytes += len(call.file.data) + sum(
            len(data_url(image.data)) for image in call.images
        )
        wal.commit_step(
            step,
            step_record(step),
            fragments=[screenshot(step, screenshot_png(step))],
            llm_calls=[call],
        )
    # The step the loop never surrendered sends the whole history once more.
    run.trailing = history_prompt(STEPS - 1) + [UserMessage(content="and now?")]
    wal.commit_trailing(llm_calls=[_call(STEPS, 0, run.trailing, completed=False)])
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return run


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


class TestAGrowingHistoryOfScreenshots:
    def test_each_distinct_screenshot_is_stored_once(self, history: HistoryRun) -> None:
        trace = RunTrace(history.run_dir)
        expected = [
            prompt_image_path(sha256_hex(screenshot_png(step)), ".png") for step in range(STEPS)
        ]
        assert trace.manifest.prompt_images == expected, "each once, in the order first sent"
        on_disk = sorted(path.name for path in (history.run_dir / PROMPT_IMAGES_DIR).iterdir())
        assert on_disk == sorted(Path(rel).name for rel in expected)
        for step in range(STEPS):
            stored = (history.run_dir / expected[step]).read_bytes()
            assert stored == screenshot_png(step), "the decoded image itself"
            # The same digest the step's own screenshot artifact has.
            assert sha256_hex(stored) == sha256_hex(trace.screenshot(step) or b"")

    def test_messages_stays_near_the_size_of_the_distinct_images(self, history: HistoryRun) -> None:
        distinct = sum(len(screenshot_png(step)) for step in range(STEPS))
        messages = _tree_bytes(history.run_dir / "messages")
        # 13 call records referring to 90 images in all — the reference, its position and
        # the message around it come to under a kilobyte an image.
        assert distinct < messages < 1.1 * distinct
        # What the inline encoding wrote: every screenshot again in every call that sent it.
        assert history.inline_bytes > 6 * messages

    def test_every_prompt_reads_back_byte_for_byte(self, history: HistoryRun) -> None:
        trace = RunTrace(history.run_dir)
        for step in range(STEPS):
            (call,) = trace.llm_messages(step)
            assert _as_read(call) == _as_sent(history.prompts[step]), f"step {step}"
            assert as_chat(call).messages == history.prompts[step]
        (trailing,) = trace.trailing_calls
        assert _as_read(trailing) == _as_sent(history.trailing)

    def test_the_call_record_holds_references_where_the_images_stood(
        self, history: HistoryRun
    ) -> None:
        record = json.loads((history.run_dir / call_path(3, 0)).read_bytes())
        assert record[IMAGE_REFS_KEY] == [
            [1 + 2 * step, "content", 1, "image_url", "url"] for step in range(4)
        ]
        first = record["messages"][1]["content"][1]["image_url"]
        assert first == {
            "url": {
                "stored_image": prompt_image_path(sha256_hex(screenshot_png(0)), ".png"),
                "prefix": "data:image/png;base64,",
            },
            "detail": "high",
            "media_type": "image/png",
        }
        # Validated as it lies on disk, the record refuses: no prompt with images missing.
        with pytest.raises(ValidationError):
            TypeAdapter(RecordedLLMCall).validate_python(record)

    def test_the_storage_measure_files_them_apart(self, history: HistoryRun) -> None:
        from spectragent.flywheel.metrics import storage_category

        trace = RunTrace(history.run_dir)
        images = [file for file in trace.committed_files if file.kind == "prompt_image"]
        assert len(images) == STEPS
        assert {storage_category(file) for file in images} == {"messages:images"}
        assert all(trace.read_committed(file.path) for file in images)


# ── lossless, whatever the payload ────────────────────────────


def _standard(data: bytes, header: str = "data:image/png;base64,") -> str:
    return header + base64.b64encode(data).decode()


def _wrapped(data: bytes) -> str:
    """MIME-style base64: line breaks every 76 characters."""
    return "data:image/png;base64," + base64.encodebytes(data).decode()


#: name → (the URL as the prompt sent it, the stored file's suffix).
URLS: dict[str, tuple[str, str]] = {
    "a PNG": (data_url(PNG_BYTES), ".png"),
    "a JPEG": (data_url(JPEG_BYTES), ".jpg"),
    "a JPEG labelled PNG": (_standard(JPEG_BYTES), ".jpg"),
    "bytes no header names": (_standard(b"not an image at all"), ".bin"),
    "upper-case scheme": (_standard(PNG_BYTES, "DATA:IMAGE/PNG;BASE64,"), ".png"),
    "a parameter before base64": (
        _standard(PNG_BYTES, "data:image/png;name=a.png;base64,"),
        ".png",
    ),
    "line breaks": (_wrapped(PNG_BYTES * 8), ".txt"),
    "missing padding": ("data:image/png;base64,QUI", ".txt"),
    "the URL-safe alphabet": (
        "data:image/png;base64," + base64.urlsafe_b64encode(b"\xfb\xff\xbf").decode(),
        ".txt",
    ),
    "set trailing bits": ("data:image/png;base64,QR==", ".txt"),
    "excess padding": ("data:image/png;base64,QUJD====", ".txt"),
    "an empty payload": ("data:image/png;base64,", ".txt"),
    "a character outside ASCII": ("data:image/png;base64,QUJD€", ".txt"),
}


def _one_image_prompt(url: str) -> list[Any]:
    return [
        SystemMessage(content="sys"),
        UserMessage(content=[ContentPartTextParam(text="look"), _image(url)]),
    ]


class TestTheRoundTripIsLossless:
    @pytest.mark.parametrize("name", list(URLS))
    def test_each_payload(self, name: str) -> None:
        url, suffix = URLS[name]
        messages = _one_image_prompt(url)
        encoded = _call(0, 0, messages)

        (image,) = encoded.images
        assert image.content_addressed and image.rel_path.endswith(suffix)
        assert image.rel_path == prompt_image_path(sha256_hex(image.data), suffix)
        ref = json.loads(encoded.file.data)["messages"][1]["content"][1]["image_url"]["url"]
        if suffix == ".txt":
            assert ref["prefix"] is None and image.data == url.encode()
        else:
            assert url == ref["prefix"] + base64.b64encode(image.data).decode()
        assert url not in encoded.file.data.decode()
        call = read_back(encoded)
        assert _as_read(call) == _as_sent(messages)

    def test_through_the_wal_and_the_reader(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        names = list(URLS)
        for step, name in enumerate(names):
            wal.commit_step(
                step,
                step_record(step),
                llm_calls=[_call(step, 0, _one_image_prompt(URLS[name][0]))],
            )
        trace = RunTrace(wal.run_dir)
        for step, name in enumerate(names):
            (call,) = trace.llm_messages(step)
            assert _as_read(call) == _as_sent(_one_image_prompt(URLS[name][0])), name

    def test_a_url_that_is_not_even_utf8_stays_inline(self) -> None:
        messages = [{"image_url": {"url": "data:image/png;base64,\ud800"}}]
        stored = store_prompt_images(messages)
        assert (stored.messages, stored.image_refs, stored.images) == (messages, [], ())

    def test_one_image_sent_twice_in_one_prompt_is_one_file(self) -> None:
        url = data_url(PNG_BYTES)
        messages = [
            UserMessage(content=[_image(url), _image(url)]),
            ToolMessage(tool_call_id="c1", content=[_image(url)]),
        ]
        encoded = _call(0, 0, messages)
        assert len(encoded.images) == 1
        record = json.loads(encoded.file.data)
        assert len(record[IMAGE_REFS_KEY]) == 3, "a user and a tool message alike"
        assert _as_read(read_back(encoded)) == _as_sent(messages)

    def test_the_prompt_handed_over_is_not_modified(self) -> None:
        pending = PendingLLMCall(_one_image_prompt(data_url(PNG_BYTES)))
        before = to_json(pending.messages)
        encode_llm_call(0, 0, pending)
        encode_llm_call(0, 0, pending)  # a failed commit's calls are encoded again
        assert to_json(pending.messages) == before


class TestWhatIsLeftAlone:
    #: Strings that look like images but are not inline images where af puts one.
    UNTOUCHED: dict[str, Any] = {
        "an image sent by URL": [UserMessage(content=[_image("https://img.test/a.png")])],
        "a data URL that is not base64": [UserMessage(content=[_image("data:text/plain,hello")])],
        "a data URL inside a text part": [
            UserMessage(content=[ContentPartTextParam(text=data_url(PNG_BYTES))])
        ],
        "a url outside image_url": [{"role": "user", "url": data_url(PNG_BYTES)}],
        "image_url holding a string": [{"image_url": data_url(PNG_BYTES)}],
        "a prompt that is not messages": [{"m": 1}, "<opaque>"],
        "a text prompt": [SystemMessage(content="sys"), UserMessage(content="go")],
    }

    @pytest.mark.parametrize("name", list(UNTOUCHED))
    def test_untouched(self, name: str) -> None:
        messages = self.UNTOUCHED[name]
        encoded = _call(0, 0, messages)
        record = json.loads(encoded.file.data)
        assert encoded.images == ()
        assert IMAGE_REFS_KEY not in record, "written only when the prompt sent an image"
        assert to_json(record["messages"], indent=2) == _as_sent(messages)

    def test_a_prompt_the_writer_cannot_walk_is_recorded_inline(self, monkeypatch, caplog) -> None:
        """The record must never be what fails a run: it falls back to the images inline."""

        def broken(url: str) -> Any:
            raise RuntimeError("a bug in the image store")

        monkeypatch.setattr(encoding, "_stored_image", broken)
        messages = _one_image_prompt(data_url(PNG_BYTES))
        with caplog.at_level(logging.WARNING, logger="spectragent.core.trace.encoding"):
            encoded = _call(0, 0, messages)
        assert encoded.images == ()
        assert TypeAdapter(RecordedLLMCall).validate_json(encoded.file.data).messages == messages
        assert "recorded inline" in caplog.text


# ── the commit protocol ───────────────────────────────────────


@dataclass
class DurableLog:
    """Every data ``fsync`` (with the file's size then) and every rename, in order."""

    run_dir: Path
    events: list[tuple[str, Any]] = field(default_factory=list)

    def renamed(self, rel: str) -> int:
        return self.events.index(("rename", rel))


@pytest.fixture()
def durable_log(monkeypatch, tmp_path: Path) -> DurableLog:
    log = DurableLog(tmp_path / "run-test")
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        status = os.fstat(fd)
        if not stat.S_ISDIR(status.st_mode):
            log.events.append(("fsync", status.st_size))
        real_fsync(fd)

    def replace(src: Any, dst: Any) -> None:
        real_replace(src, dst)
        target = Path(dst)
        if target.is_relative_to(log.run_dir):
            log.events.append(("rename", target.relative_to(log.run_dir).as_posix()))

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return log


class TestTheCommitProtocol:
    def test_an_image_is_durable_before_the_manifest_names_it(
        self, tmp_path: Path, durable_log: DurableLog
    ) -> None:
        wal = new_wal(tmp_path)
        durable_log.events.clear()
        call = _call(0, 0, history_prompt(1))
        wal.commit_step(0, step_record(0), llm_calls=[call])

        manifest = durable_log.renamed("manifest.json")
        for image in call.images:
            at = durable_log.renamed(image.rel_path)
            # Its bytes were fsynced, whole, right before the rename put it in place...
            assert durable_log.events[at - 1] == ("fsync", len(image.data))
            # ...and both before the rename that commits it.
            assert at < manifest
        assert durable_log.renamed(call.file.rel_path) < manifest
        assert RunTrace(wal.run_dir).manifest.prompt_images == [
            image.rel_path for image in call.images
        ]

    def test_an_image_is_written_once_per_run(self, tmp_path: Path, monkeypatch) -> None:
        wal = new_wal(tmp_path)
        written: list[str] = []
        real = fsio.write_durable

        def write_durable(path: Path, data: bytes) -> set[Path]:
            written.append(path.relative_to(wal.run_dir).as_posix())
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        first, second = _call(0, 0, history_prompt(0)), _call(1, 0, history_prompt(1))
        wal.commit_step(0, step_record(0), llm_calls=[first])
        wal.commit_step(1, step_record(1), llm_calls=[second])
        wal.commit_trailing(llm_calls=[_call(2, 0, history_prompt(1), completed=False)])

        (shared,) = first.images
        assert [image.rel_path for image in second.images] == [
            shared.rel_path,
            prompt_image_path(sha256_hex(screenshot_png(1)), ".png"),
        ]
        stored = [rel for rel in written if rel.startswith(f"{PROMPT_IMAGES_DIR}/")]
        assert stored == [image.rel_path for image in second.images], "each written once"
        assert RunTrace(wal.run_dir).manifest.prompt_images == stored

    def test_a_crash_before_the_manifest_leaves_the_images_invisible(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), llm_calls=[_call(0, 0, history_prompt(0))])
        real = fsio.write_durable

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json":
                raise OSError(5, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        late = _call(1, 0, history_prompt(1))
        new_image = late.images[-1].rel_path
        with pytest.raises(OSError, match="power lost"):
            wal.commit_step(1, step_record(1), llm_calls=[late])

        assert (wal.run_dir / new_image).is_file(), "the crash window is populated"
        trace = RunTrace(wal.run_dir)
        assert new_image not in trace.manifest.prompt_images
        assert trace.llm_messages(1) == []
        with pytest.raises(KeyError):
            trace.read_committed(new_image)

        # Nothing of the failed attempt was claimed: the retry commits it, the image again.
        monkeypatch.setattr(fsio, "write_durable", real)
        wal.commit_step(1, step_record(1), llm_calls=[late])
        trace = RunTrace(wal.run_dir)
        assert trace.manifest.prompt_images[-1] == new_image
        assert _as_read(trace.llm_messages(1)[0]) == _as_sent(history_prompt(1))


class TestTheWALRefusesAMisplacedImage:
    @staticmethod
    def _with(image: TraceFile) -> EncodedLLMCall:
        call = _call(0, 0, _one_image_prompt(data_url(PNG_BYTES)))
        return EncodedLLMCall(call.file, images=(image,))

    @pytest.mark.parametrize(
        ("image", "message"),
        [
            (
                TraceFile(prompt_image_path(sha256_hex(PNG_BYTES), ".png"), PNG_BYTES),
                "content-addressed",
            ),
            (
                TraceFile(
                    f"{PROMPT_IMAGES_DIR}/{sha256_hex(PNG_BYTES)[:2]}/{sha256_hex(PNG_BYTES)[2:]}",
                    PNG_BYTES,
                    content_addressed=True,
                ),
                "directly under messages/images/",
            ),
            (
                TraceFile(f"env/{sha256_hex(PNG_BYTES)}.png", PNG_BYTES, content_addressed=True),
                "directly under messages/images/",
            ),
            (
                TraceFile(prompt_image_path("0" * 64, ".png"), PNG_BYTES, content_addressed=True),
                "does not name its data's sha256",
            ),
        ],
        ids=["not content-addressed", "sharded", "elsewhere", "wrong digest"],
    )
    def test_refused_before_a_byte_is_written(
        self, tmp_path: Path, image: TraceFile, message: str
    ) -> None:
        wal = new_wal(tmp_path)
        before = (wal.run_dir / "manifest.json").read_bytes()
        with pytest.raises(ValueError, match=message):
            wal.commit_step(0, step_record(0), llm_calls=[self._with(image)])
        assert (wal.run_dir / "manifest.json").read_bytes() == before
        assert not (wal.run_dir / PROMPT_IMAGES_DIR).exists()

    def test_a_call_record_cannot_live_among_the_images(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        call = _call(0, 0, _one_image_prompt(data_url(PNG_BYTES)))
        stray = EncodedLLMCall(TraceFile(f"{PROMPT_IMAGES_DIR}/000.json", call.file.data))
        with pytest.raises(ValueError, match="among stored prompt images"):
            wal.commit_step(0, step_record(0), llm_calls=[stray])

    def test_no_file_takes_the_directory_s_name(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        call = _call(0, 0, _one_image_prompt(data_url(PNG_BYTES)))
        stray = EncodedLLMCall(TraceFile(PROMPT_IMAGES_DIR, call.file.data))
        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(0, step_record(0), llm_calls=[stray])

    def test_an_extra_file_cannot_go_there(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        with pytest.raises(ValueError, match="a name the WAL owns"):
            wal.commit_step(
                0, step_record(0), extra_files=[extra(f"{PROMPT_IMAGES_DIR}/x.png", PNG_BYTES)]
            )


# ── damage ────────────────────────────────────────────────────


@dataclass
class ImageRun:
    wal: WALWriter
    #: Step 0's call sends PNG and JPEG; the trailing call sends PNG again.
    call: EncodedLLMCall

    @property
    def run_dir(self) -> Path:
        return self.wal.run_dir

    @property
    def record(self) -> Path:
        return self.run_dir / self.call.file.rel_path

    def image(self, index: int) -> Path:
        return self.run_dir / self.call.images[index].rel_path

    def edit_record(self, edit: Any) -> None:
        record = json.loads(self.record.read_bytes())
        edit(record)
        self.record.write_text(json.dumps(record))


@pytest.fixture()
def image_run(tmp_path: Path) -> ImageRun:
    wal = new_wal(tmp_path)
    messages = [
        SystemMessage(content="sys"),
        UserMessage(content=[_image(data_url(PNG_BYTES)), _image(data_url(JPEG_BYTES))]),
    ]
    call = _call(0, 0, messages)
    wal.commit_step(0, step_record(0), llm_calls=[call])
    wal.commit_trailing(
        llm_calls=[_call(1, 0, [UserMessage(content=[_image(data_url(PNG_BYTES))])])]
    )
    return ImageRun(wal, call)


#: Reads that put a call record's images back, by what they read.
READS = {
    "llm_messages": lambda trace: trace.llm_messages(0),
    "trailing_calls": lambda trace: trace.trailing_calls,
}


class TestADamagedImageNamesItself:
    @pytest.mark.parametrize("read", list(READS))
    @pytest.mark.parametrize("damage", ["missing", "other bytes", "empty"])
    def test_an_image_file(self, image_run: ImageRun, read: str, damage: str) -> None:
        path = image_run.image(0)  # the PNG: both calls send it
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"" if damage == "empty" else PNG_BYTES + b"tampered")
        message = "missing on disk" if damage == "missing" else "content-addressed by"
        with pytest.raises(TraceSchemaError, match=message) as exc_info:
            READS[read](RunTrace(image_run.run_dir))
        assert exc_info.value.path == path
        with pytest.raises(TraceSchemaError) as exc_info:
            RunTrace(image_run.run_dir).read_committed(image_run.call.images[0].rel_path)
        assert exc_info.value.path == path

    def test_checked_on_every_read(self, image_run: ImageRun) -> None:
        trace = RunTrace(image_run.run_dir)
        assert trace.llm_messages(0)
        image_run.image(1).write_bytes(JPEG_BYTES + b"changed later")
        with pytest.raises(TraceSchemaError, match="content-addressed by"):
            trace.llm_messages(0)

    def test_a_stored_text_that_is_not_utf8(self, tmp_path: Path) -> None:
        """Named by its own digest, so only the decode can refuse it — a producer's bug."""
        wal = new_wal(tmp_path)
        data = b"data:image/png;base64,\xff\xfe"
        rel = prompt_image_path(sha256_hex(data), ".txt")
        record = json.loads(_call(0, 0, _one_image_prompt("data:image/png;base64,QR==")).file.data)
        record["messages"][1]["content"][1]["image_url"]["url"] = {
            "stored_image": rel,
            "prefix": None,
        }
        encoded = EncodedLLMCall(
            TraceFile(call_path(0, 0), json.dumps(record).encode()),
            images=(TraceFile(rel, data, content_addressed=True),),
        )
        wal.commit_step(0, step_record(0), llm_calls=[encoded])
        with pytest.raises(TraceSchemaError, match="does not read back as the text") as exc_info:
            RunTrace(wal.run_dir).llm_messages(0)
        assert exc_info.value.path == wal.run_dir / rel


class TestABadReferenceNamesTheCallRecord:
    def _refused(self, image_run: ImageRun, match: str) -> None:
        with pytest.raises(TraceSchemaError, match=match) as exc_info:
            RunTrace(image_run.run_dir).llm_messages(0)
        assert exc_info.value.path == image_run.record

    def test_an_image_the_manifest_does_not_list(self, image_run: ImageRun) -> None:
        planted = PNG_BYTES + b"planted"
        rel = prompt_image_path(sha256_hex(planted), ".png")
        (image_run.run_dir / rel).write_bytes(planted)

        def edit(record: dict[str, Any]) -> None:
            record["messages"][1]["content"][0]["image_url"]["url"]["stored_image"] = rel

        image_run.edit_record(edit)
        self._refused(image_run, "never committed")

    @pytest.mark.parametrize(
        "positions",
        [
            "not a list",
            [[]],
            [[1, "content", 7, "image_url", "url"]],
            [[1, "content", "0", "image_url", "url"]],
            [[True, "content", 0, "image_url", "url"]],
            [[1, "content", 0, "image_url"]],
            [[1, "content", 0, "image_url", "url"], [1, "content", 0, "image_url", "url"]],
        ],
        ids=[
            "not a list",
            "empty",
            "leads nowhere",
            "an index spelled as a key",
            "a boolean index",
            "not at the reference",
            "listed twice",
        ],
    )
    def test_a_position(self, image_run: ImageRun, positions: Any) -> None:
        image_run.edit_record(lambda record: record.update({IMAGE_REFS_KEY: positions}))
        self._refused(image_run, "cannot be put back")

    def test_a_reference_that_is_not_one(self, image_run: ImageRun) -> None:
        def edit(record: dict[str, Any]) -> None:
            record["messages"][1]["content"][0]["image_url"]["url"]["extra"] = "field"

        image_run.edit_record(edit)
        self._refused(image_run, "cannot be put back")

    def test_a_reference_the_record_does_not_list(self, image_run: ImageRun) -> None:
        image_run.edit_record(lambda record: record[IMAGE_REFS_KEY].pop())
        self._refused(image_run, "does not parse as a RecordedLLMCall")

    def test_a_manifest_that_lists_a_name_no_digest_makes(self, image_run: ImageRun) -> None:
        rel = f"{PROMPT_IMAGES_DIR}/not-a-digest.png"
        manifest = image_run.run_dir / "manifest.json"
        document = json.loads(manifest.read_bytes())
        document["prompt_images"][0] = rel
        manifest.write_text(json.dumps(document))

        def edit(record: dict[str, Any]) -> None:
            record["messages"][1]["content"][0]["image_url"]["url"]["stored_image"] = rel

        image_run.edit_record(edit)
        with pytest.raises(TraceSchemaError, match="no sha256 names it") as exc_info:
            RunTrace(image_run.run_dir).llm_messages(0)
        assert exc_info.value.path == manifest


class TestTheEncodingPair:
    """``restore_prompt_images`` is ``store_prompt_images``' inverse on a parsed record."""

    def test_restore_puts_every_text_back_and_drops_the_positions(self) -> None:
        sent = PendingLLMCall(history_prompt(2)).messages
        stored = store_prompt_images(sent)
        files = {image.rel_path: image.data for image in stored.images}
        record = json.loads(
            to_json({"messages": stored.messages, IMAGE_REFS_KEY: stored.image_refs})
        )
        restore_prompt_images(record, lambda ref: prompt_image_text(ref, files[ref.stored_image]))
        assert record == {"messages": sent}

    def test_a_record_without_positions_is_left_alone(self) -> None:
        record = {"messages": [{"role": "user", "content": "go"}]}
        restore_prompt_images(record, lambda ref: pytest.fail("nothing to put back"))
        assert record == {"messages": [{"role": "user", "content": "go"}]}
