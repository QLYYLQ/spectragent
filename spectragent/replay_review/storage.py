"""Independent review commit index, sharing durable I/O and call/image encoding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from spectragent.core.trace.encoding import (
    EncodedLLMCall,
    TraceFile,
    prompt_image_digest,
    prompt_image_text,
    restore_prompt_images,
)
from spectragent.core.trace.fsio import atomic_write, fsync_dir, write_durable
from spectragent.core.trace.models import RecordedLLMCall
from spectragent.replay_review.models import ReviewStatus

MANIFEST = "review-manifest.json"


class ReviewFile(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class ReviewItem(BaseModel):
    index: int
    status: ReviewStatus
    assessment: str
    evidence: str | None = None
    calls: list[str] = Field(default_factory=list)


class ReviewManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["af-replay-review-1"] = "af-replay-review-1"
    job_id: str = Field(min_length=1)
    status: Literal["writing", "complete", "incomplete"] = "writing"
    expected_items: int
    items: list[ReviewItem] = Field(default_factory=list)
    files: dict[str, ReviewFile] = Field(default_factory=dict)
    recording_problems: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_index(self):
        indices = [item.index for item in self.items]
        if len(indices) != len(set(indices)) or any(
            i < 0 or i >= self.expected_items for i in indices
        ):
            raise ValueError("Review item identities are duplicated or outside the job")
        if self.status == "complete" and (
            set(indices) != set(range(self.expected_items)) or self.recording_problems
        ):
            raise ValueError(
                "A complete review manifest must cover its entire job without recording gaps"
            )
        return self


class ReviewReader(Protocol):
    """Committed review evidence; no write operations are exposed to consumers."""

    def manifest(self) -> ReviewManifest: ...
    def read(self, relative: str) -> bytes: ...
    def read_json(self, relative: str) -> Any: ...
    def call(self, relative: str) -> RecordedLLMCall: ...
    def report(self) -> dict[str, Any]: ...


def open_review(root: Path) -> ReviewReader:
    return ReviewStore(root)


def json_file(path: str, value: Any) -> TraceFile:
    return TraceFile(
        path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    )


class ReviewStore:
    """Files first, atomic index last; uncertain installs are reconciled before another write."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, relative: str) -> Path:
        path = self.root / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not path.resolve().is_relative_to(self.root)
        ):
            raise ValueError(f"Review path escapes its job: {relative!r}")
        return path

    def create(self, job_id: str, expected_items: int, config: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        fsync_dir(self.root.parent)
        manifest = ReviewManifest(job_id=job_id, expected_items=expected_items)
        self._commit(manifest, [json_file("config.json", config)])

    def manifest(self) -> ReviewManifest:
        return ReviewManifest.model_validate_json(self.path(MANIFEST).read_bytes())

    def _commit(self, manifest: ReviewManifest, files: list[TraceFile]) -> None:
        touched = set()
        for file in files:
            target = self.path(file.rel_path)
            digest = hashlib.sha256(file.data).hexdigest()
            existing = manifest.files.get(file.rel_path)
            if existing is not None:
                if (
                    existing.sha256 != digest
                    or hashlib.sha256(target.read_bytes()).hexdigest() != digest
                ):
                    raise ValueError(f"Committed review content changed: {file.rel_path}")
                continue
            touched.update(write_durable(target, file.data))
            manifest.files[file.rel_path] = ReviewFile(sha256=digest, size=len(file.data))
            parent = target.parent
            while parent.is_relative_to(self.root):
                touched.add(parent)
                if parent == self.root:
                    break
                parent = parent.parent
        for directory in sorted(touched, key=lambda p: len(p.parts), reverse=True):
            fsync_dir(directory)
        atomic_write(self.path(MANIFEST), manifest.model_dump_json(indent=2).encode())

    def commit_item(
        self, item: ReviewItem, files: list[TraceFile], calls: list[EncodedLLMCall]
    ) -> None:
        manifest = self.manifest()  # disk is authoritative, also after an uncertain prior rename
        if any(old.index == item.index for old in manifest.items):
            raise ValueError(f"Review item {item.index} is already committed")
        contents = list(files)
        for call in calls:
            contents.extend([*call.images, call.file])
        manifest.items.append(item)
        self._commit(manifest, contents)

    def finish(self, problems: list[str], summary: dict[str, Any]) -> None:
        manifest = self.manifest()
        manifest.recording_problems.extend(problems)
        manifest.summary = summary
        manifest.status = (
            "complete"
            if len(manifest.items) == manifest.expected_items and not problems
            else "incomplete"
        )
        self._commit(manifest, [])

    def read(self, relative: str) -> bytes:
        manifest = self.manifest()
        if relative not in manifest.files:
            raise ValueError(f"Uncommitted review file {relative!r}")
        data = self.path(relative).read_bytes()
        entry = manifest.files[relative]
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError(f"Corrupt review file {relative!r}")
        return data

    def read_json(self, relative: str) -> Any:
        return json.loads(self.read(relative))

    def call(self, relative: str) -> RecordedLLMCall:
        record = self.read_json(relative)

        def image_text(ref):
            data = self.read(ref.stored_image)
            if prompt_image_digest(ref.stored_image) != hashlib.sha256(data).hexdigest():
                raise ValueError("Review image content does not match its address")
            return prompt_image_text(ref, data)

        restore_prompt_images(record, image_text)
        call = TypeAdapter(RecordedLLMCall).validate_python(record)
        if (
            call.owner != "replay_review"
            or call.owner_id != self.manifest().job_id
            or call.step is not None
        ):
            raise ValueError("Review call has inconsistent ownership")
        return call

    def report(self) -> dict[str, Any]:
        manifest = self.manifest()
        return {
            "manifest": manifest.model_dump(mode="json"),
            "config": self.read_json("config.json"),
            "items": [
                {
                    "index": item.index,
                    "assessment": self.read_json(item.assessment),
                    "evidence": self.read_json(item.evidence) if item.evidence else None,
                    "calls": [
                        self.call(path).model_dump(mode="json", by_alias=True)
                        for path in item.calls
                    ],
                }
                for item in manifest.items
            ],
        }
