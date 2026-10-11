"""``openai_messages``: one OpenAI chat-format record per sample (arch §4.15 "格式化").

A record is ``{"id": <sample_id>, "messages": [...]}``: the student's context
and the target turn, serialised by the framework's own
:class:`~spectragent.core.llm.serializers.openai_format.OpenAIMessageSerializer`
(so the record is what the student's provider would have been sent), with a
``weight`` on every assistant message — 0 on the earlier turns, the sample's
``loss_weight`` (1) on the target. Each image part's ``data:`` URL is
replaced by the path the image store filed it under
(``images/<sha256><suffix>``), so one screenshot is stored once however many
records show it.

OpenAI's self-serve fine-tuning is shrinking; this is an exchange format.
``llamafactory``, ``ms_swift`` and ``trl`` are further exporters — new
classes here, the pipeline unchanged.

No knob of its own, so no ``__init__``: conscribe reads :class:`ExporterBase`'s.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from spectragent.core.llm.messages import AssistantMessage
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.export._registrar import ExporterBase, ExportRecord, ImageStore
from spectragent.export.render import RenderedSample

_DATA_URL_PREFIX = "data:"


class OpenAIMessagesExporter(ExporterBase):
    """``{"id", "messages"}`` records; assistant turns weighted, images stored as files."""

    __registry_key__ = "openai_messages"

    def format(self, rendered: RenderedSample, images: ImageStore) -> ExportRecord:
        messages: list[Any] = [
            dict(message)
            for message in OpenAIMessageSerializer.serialize_messages(
                [*rendered.context, AssistantMessage(content=rendered.target)]
            )
        ]
        target_weight = rendered.sample.labels.loss_weight
        last = len(messages) - 1
        stored: list[str] = []
        for position, message in enumerate(messages):
            if message.get("role") == "assistant":
                message["weight"] = _weight(target_weight if position == last else 0.0)
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    _stored_part(part, images, stored, rendered.sample.sample_id)
                    for part in content
                ]
        return ExportRecord(
            sample_id=rendered.sample.sample_id,
            record={"id": rendered.sample.sample_id, "messages": messages},
            images=tuple(stored),
        )


def _weight(value: float) -> int | float:
    """``0`` / ``1`` as integers (what trainers expect); any other weight as written."""
    return int(value) if float(value).is_integer() else value


def _stored_part(part: Any, images: ImageStore, stored: list[str], sample_id: str) -> Any:
    """*part* with an image's ``data:`` URL replaced by its stored path."""
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return part
    url = part["image_url"]["url"]
    media_type, data = _decode_data_url(url, sample_id)
    path = images.put(data, media_type)
    stored.append(path)
    return {**part, "image_url": {**part["image_url"], "url": path}}


def _decode_data_url(url: str, sample_id: str) -> tuple[str, bytes]:
    """``(media type, bytes)`` of a base64 ``data:`` URL.

    Raises:
        ValueError: The image is not inline (a remote URL would make the
            dataset depend on a server) or does not decode.
    """
    header, sep, payload = url.partition(",")
    if not url.startswith(_DATA_URL_PREFIX) or not sep or not header.endswith(";base64"):
        raise ValueError(
            f"{sample_id}: an image in the student's context is not an inline base64 data "
            f"URL ({url[:40]!r}…); the dataset stores images, it does not link them"
        )
    media_type = header[len(_DATA_URL_PREFIX) : -len(";base64")]
    try:
        return media_type, base64.b64decode(payload, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"{sample_id}: an image data URL does not decode: {exc}") from exc


__all__ = ["OpenAIMessagesExporter"]
