"""Native decision inputs and outputs; probabilities are evidence, not booleans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from spectragent.core.llm.views import ModelInvokeUsage

DecisionKind = Literal["choice", "score", "noul", "box"]
StructuredValue = str | dict[str, JsonValue] | list[JsonValue]
Description = StructuredValue | None
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


def validate_structure(value: object, *, depth: int = 0) -> None:
    """Native JSON restrictions apply inside structured descriptions too."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Decision JSON numbers must be finite")
    if isinstance(value, dict | list):
        if depth >= 64:
            raise ValueError("Decision JSON exceeds 64 nested containers")
        for item in value.values() if isinstance(value, dict) else value:
            validate_structure(item, depth=depth + 1)


class DecisionValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChoiceQuestion(DecisionValue):
    type: Literal["choice"] = "choice"
    instructions: Description
    criteria: Annotated[dict[str, Description], Field(min_length=1, max_length=255)]


class ScoreQuestion(DecisionValue):
    type: Literal["score"] = "score"
    instructions: Description
    criteria: Annotated[list[Description], Field(min_length=2, max_length=10)]


class NoulCriteria(DecisionValue):
    # Field aliases retain the native JSON keys, including false and true.
    positive: Description = Field(default=None, alias="true")
    negative: Description = Field(default=None, alias="false")


class NoulQuestion(DecisionValue):
    type: Literal["noul"] = "noul"
    instructions: Description
    criteria: NoulCriteria | None = None


class BoxQuestion(DecisionValue):
    type: Literal["box"] = "box"
    instructions: StructuredValue


DecisionQuestion = Annotated[
    ChoiceQuestion | ScoreQuestion | NoulQuestion | BoxQuestion, Field(discriminator="type")
]


class ImageProvenance(DecisionValue):
    """Caller-declared origin and transformations, never forwarded to the model."""

    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_width: Annotated[int, Field(ge=1)]
    source_height: Annotated[int, Field(ge=1)]
    transformations: list[dict[str, JsonValue]] = Field(default_factory=list)


class DecisionImage(DecisionValue):
    type: Literal["image"] = "image"
    format: Literal["png", "jpg", "jpeg"]
    data_base64: Annotated[str, Field(min_length=1)]
    provenance: ImageProvenance | None = None


def string_values(value: object) -> list[str]:
    """Asset references appear in values, never in object keys."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in string_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in string_values(item)]
    return []


class DecisionRequest(DecisionValue):
    state: StructuredValue
    questions: Annotated[dict[str, DecisionQuestion], Field(min_length=1)]
    assets: dict[str, DecisionImage] = Field(default_factory=dict)

    def logical_dump(self) -> dict[str, Any]:
        """Keep discriminators while distinguishing omitted criteria from explicit nulls."""
        data = self.model_dump(mode="json", by_alias=True)
        data["questions"] = {
            key: {
                **question.model_dump(mode="json", by_alias=True, exclude_unset=True),
                "type": question.type,
            }
            for key, question in self.questions.items()
        }
        return data

    @model_validator(mode="after")
    def check_input(self) -> Self:
        validate_structure(self.state)
        descriptions: list[object] = [self.state]
        for key, question in self.questions.items():
            if not key.strip():
                raise ValueError("Decision question IDs must be nonempty")
            data = question.model_dump(mode="json", by_alias=True, exclude_unset=True)
            for field in ("instructions", "criteria"):
                validate_structure(data.get(field))
                descriptions.append(data.get(field))
        strings = string_values(descriptions)
        if sum(len(key.encode()) for key in self.assets) > 65536:
            raise ValueError("Decision asset references exceed 65536 UTF-8 bytes")
        for key in self.assets:
            if not key.strip() or len(key.encode()) > 1024:
                raise ValueError("Decision asset reference must contain 1–1024 UTF-8 bytes")
            if not any(key in text for text in strings):
                raise ValueError(f"Decision asset {key!r} is not referenced in an input value")
        if any(q.type == "box" for q in self.questions.values()) and not self.assets:
            raise ValueError("Box questions require an image asset")
        return self


class ChoiceAnswer(DecisionValue):
    type: Literal["choice"]
    choice: str
    probabilities: Annotated[dict[str, Probability], Field(min_length=1)]
    confidence: Probability


class ScoreAnswer(DecisionValue):
    type: Literal["score"]
    score: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
    legend: dict[str, Description]
    probabilities: Annotated[dict[str, Probability], Field(min_length=2)]
    confidence: Probability


class NoulAnswer(DecisionValue):
    type: Literal["noul"]
    noul: Probability


class NormalizedBox(DecisionValue):
    top: Probability
    bottom: Probability
    left: Probability
    right: Probability

    @model_validator(mode="after")
    def check_order(self) -> Self:
        if self.top >= self.bottom or self.left >= self.right:
            raise ValueError("Box must have top < bottom and left < right")
        return self


class BoxAnswer(DecisionValue):
    type: Literal["box"]
    box: NormalizedBox
    confidence: Probability


DecisionAnswer = Annotated[
    ChoiceAnswer | ScoreAnswer | NoulAnswer | BoxAnswer, Field(discriminator="type")
]


class DecisionResult(DecisionValue):
    requested_model: str
    model: Annotated[str, Field(min_length=1)]
    answers: dict[str, DecisionAnswer]
    usage: ModelInvokeUsage | None = None
    native_usage: dict[str, JsonValue] | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    def check_answers(
        self, request: DecisionRequest, *, score_legends: dict[str, list[Description]] | None = None
    ) -> None:
        """Validate the response against this request without normalizing native values.

        The 0.02 distribution tolerance permits the route's rounded probabilities;
        score tolerance includes rounding of each level's probability and the score.
        OpenRouter returns image parts in legends, so its serializer supplies those
        exact wire descriptions for comparison instead of the logical asset keys.
        """
        if self.answers.keys() != request.questions.keys():
            raise ValueError("Decision answer IDs differ from the requested question IDs")
        for key, question in request.questions.items():
            answer = self.answers[key]
            if answer.type != question.type:
                raise ValueError(
                    f"Decision answer {key!r} has type {answer.type}, expected {question.type}"
                )
            if (
                isinstance(answer, ChoiceAnswer | ScoreAnswer)
                and abs(sum(answer.probabilities.values()) - 1) > 0.02000001
            ):
                raise ValueError(f"Decision answer {key!r} probabilities do not sum to one")
            if isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
                if answer.probabilities.keys() != question.criteria.keys():
                    raise ValueError(f"Decision answer {key!r} changed the choice options")
                if answer.choice not in answer.probabilities or (
                    answer.probabilities[answer.choice] < max(answer.probabilities.values())
                ):
                    raise ValueError(f"Decision answer {key!r} choice is not an argmax")
            if isinstance(question, ScoreQuestion) and isinstance(answer, ScoreAnswer):
                criteria = (score_legends or {}).get(key, question.criteria)
                legend = {str(i): level for i, level in enumerate(criteria)}
                if answer.legend != legend or answer.probabilities.keys() != legend.keys():
                    raise ValueError(f"Decision answer {key!r} changed the score legend")
                if answer.score > len(criteria) - 1:
                    raise ValueError(f"Decision answer {key!r} score is outside its levels")
                expectation = sum(int(i) * p for i, p in answer.probabilities.items())
                if abs(answer.score - expectation) > 0.02 * len(criteria):
                    raise ValueError(
                        f"Decision answer {key!r} score disagrees with its distribution"
                    )


@dataclass(frozen=True)
class DecisionCapabilities:
    """The intersection of the route and selected model's declared capabilities."""

    question_types: frozenset[DecisionKind]
    images: bool
    min_image_side: int = 280
    max_image_side: int = 1568
    min_image_pixels: int = 78400
    max_image_bytes: int = 16 * 1024 * 1024
    max_request_bytes: int = 32 * 1024 * 1024
