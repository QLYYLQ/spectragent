"""``index_to_coordinate``: element indices become points on the recorded screenshot (arch §4.15).

For a vision student learning from a DOM teacher. An action that addresses
an element — a param marked ``RequiresFragment`` with a value
(``core/actions/params.py::param_fragments``) — becomes its coordinate
counterpart at the point ``core/capabilities/browser/index_points.py``
locates on the step's recorded screenshot; every other action passes
unchanged. The browser knowledge (geometry, occlusion evidence) lives in
``index_points``; this module fetches what it needs from the trace and
organises the calls.

.. note::
   No ``from __future__ import annotations``: conscribe reads the knobs'
   ``Annotated`` metadata from the ``__init__`` signature.
"""

from functools import cached_property
from typing import Annotated, Any, Literal

from pydantic import Field

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.params import param_fragments
from spectragent.core.capabilities.browser import index_points
from spectragent.core.capabilities.browser.archive.dom_raw import read_dom_document
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, DOMObs
from spectragent.core.trace.reader import RunTrace
from spectragent.export.errors import OcclusionEvidenceMissingError
from spectragent.export.translate._registrar import ActionTranslatorBase, Translation
from spectragent.schemas.action import Action
from spectragent.schemas.sample import CanonicalStepSample, TranslationRecord


class _StepEvidence:
    """One step's recorded DOM, document stamp and raw DOM documents, read once, on demand."""

    def __init__(self, trace: RunTrace, step: int) -> None:
        self._trace = trace
        self._step = step

    @cached_property
    def dom(self) -> DOMObs | None:
        fragment = self._trace.observation_fragment(self._step, index_points.DOM_FRAGMENT)
        if isinstance(fragment, DOMObs) and isinstance(fragment.element_tree, DOMState):
            return fragment
        return None

    @cached_property
    def loader_id(self) -> str | None:
        meta = self._trace.observation_fragment(self._step, index_points.META_FRAGMENT)
        return meta.loader_id if isinstance(meta, BrowserMetaObs) else None

    @cached_property
    def documents(self) -> list[dict[str, Any]]:
        return [
            read_dom_document(self._trace.read_extra(record.path))
            for record in self._trace.archive_files(self._step, index_points.DOM_RAW_ARCHIVER)
        ]


class IndexToCoordinateTranslator(ActionTranslatorBase):
    """``browser.click`` / ``browser.type`` by index → ``*_coordinate`` at a checked point."""

    __registry_key__ = "index_to_coordinate"

    # ── the knobs, as attributes (assigned in __init__) ──
    occlusion: Literal["none", "check"]
    occlusion_unknown: Literal["raise", "untranslatable"]

    def __init__(
        self,
        *,
        untranslatable: Annotated[
            Literal["drop_step", "truncate", "keep_as_text", "fail"],
            Field(
                description=(
                    "What the export does with a step that has no target in the student's "
                    "action space: drop_step, truncate, keep_as_text or fail."
                )
            ),
        ] = "drop_step",
        occlusion: Annotated[
            Literal["none", "check"],
            Field(
                description=(
                    "check (the default): a point covered by another element is moved to "
                    "an uncovered point of the element, or the step is untranslatable "
                    "(occluded). none: the point is not checked."
                )
            ),
        ] = "check",
        occlusion_unknown: Annotated[
            Literal["raise", "untranslatable"],
            Field(
                description=(
                    "A step with no occlusion evidence (no hit test, no browser.dom_raw "
                    "paint order, no traced paint order): raise names it; untranslatable "
                    "treats it as reason occlusion_unknown."
                )
            ),
        ] = "raise",
    ) -> None:
        super().__init__(untranslatable=untranslatable)
        self.occlusion = occlusion
        self.occlusion_unknown = occlusion_unknown

    def translate(self, sample: CanonicalStepSample, trace: RunTrace) -> Translation:
        evidence = _StepEvidence(trace, sample.step)
        viewport = (
            (sample.image.viewport_width, sample.image.viewport_height)
            if sample.image is not None
            else None
        )
        actions: list[Action] = []
        records: list[TranslationRecord] = []
        for position, action in enumerate(sample.teacher_actions):
            cls = ActionRegistrar.get_or_none(action.name)
            if cls is None:
                return Translation.refused(
                    "no_counterpart", f"{action.name} is not a registered action", records
                )
            addressed = sorted(
                name for name in param_fragments(cls.Params) if action.params.get(name) is not None
            )
            if not addressed:
                actions.append(self.validated(sample.sample_id, action))
                continue
            if action.name not in index_points.COORDINATE_COUNTERPARTS:
                return Translation.refused(
                    "no_counterpart",
                    f"{action.name} addresses an element ({', '.join(addressed)}) and has no "
                    f"coordinate counterpart",
                    records,
                )
            dom = evidence.dom
            if dom is None:
                return Translation.refused(
                    "no_counterpart",
                    f"step {sample.step} recorded no {index_points.DOM_FRAGMENT} to place "
                    f"{action.name} with",
                    records,
                )
            element_index = int(action.params[index_points.INDEX_PARAM])
            located = index_points.locate_element(
                element_index,
                dom.element_tree,
                dom.selector_map,
                viewport=viewport,
                occlusion=self.occlusion,
                documents=evidence.documents if self.occlusion == "check" else (),
                loader_id=evidence.loader_id,
            )
            record = TranslationRecord(
                translator_id=self.translator_id,
                action_index=position,
                payload=located.provenance,
            )
            if located.point is None:
                assert located.failure is not None  # ElementPoint: no point means a failure
                if located.failure == "occlusion_unknown" and self.occlusion_unknown == "raise":
                    raise OcclusionEvidenceMissingError(sample.sample_id, element_index)
                return Translation.refused(located.failure, located.detail, [*records, record])
            name, params = index_points.coordinate_action(action.name, action.params, located.point)
            actions.append(self.validated(sample.sample_id, Action(name=name, params=params)))
            records.append(record)
        return Translation(actions=actions, records=records)


__all__ = ["IndexToCoordinateTranslator"]
