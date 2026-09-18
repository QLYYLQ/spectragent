"""Observation Fragment system — M02.

Breaks the monolithic Observation into independently registered Fragments
with a 3-party negotiation engine (Capability provides / Agent requires /
Loop requires). The negotiation engine itself lives in
:mod:`spectragent.core.negotiation`.

See docs/architecture_design.md §4.4 and docs/execution_dag.md M02 for design details.
"""

from spectragent.schemas.observation._registrar import (
    FragmentBase,
    ObservationRegistrar,
)
from spectragent.schemas.observation.base import ObservationFragment
from spectragent.schemas.observation.dynamic_model import (
    build_observation_model,
    build_observation_model_from_result,
    iter_fragments,
    summarize_observation,
)
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.geometry import (
    CoordinateSpace,
    PointGrid,
    ScreenshotGeometry,
    point_grid,
)
from spectragent.schemas.observation.image import (
    ImageMediaType,
    image_media_type,
    image_size,
    image_suffix,
)

__all__ = [
    "CoordinateSpace",
    "FragmentBase",
    "FragmentFailure",
    "ImageMediaType",
    "ObservationFragment",
    "ObservationRegistrar",
    "PointGrid",
    "ScreenshotGeometry",
    "build_observation_model",
    "build_observation_model_from_result",
    "image_media_type",
    "image_size",
    "image_suffix",
    "iter_fragments",
    "point_grid",
    "summarize_observation",
]
