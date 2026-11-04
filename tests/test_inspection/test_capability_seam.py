"""The read side names no capability: each one states its own wording, elements and evidence.

``docs/architecture_design.md`` §4.17 "动作标记", §4.2.1: adding a capability
never edits a framework file, the viewer's read side included. A capability
gives the viewer its wording and marks by implementing the Protocols of
``core/presentation.py`` on the classes it already registers.
"""

from __future__ import annotations

import ast
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

import spectragent.core.capabilities as capabilities_package
import spectragent.inspection as inspection_package
from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement, PointAxis
from spectragent.core.presentation import (
    ActionPresentation,
    DescribesElements,
    ElementDescription,
    Point,
    PresentsAction,
    number,
)
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.graph import run_graph
from spectragent.inspection.marks import action_verbs, observation_marks, presenter
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import fragment

INSPECTION = Path(inspection_package.__file__).parent
#: Every capability package: its name is its capability id.
CAPABILITY_IDS = sorted(
    module.name
    for module in pkgutil.iter_modules(capabilities_package.__path__)
    if module.ispkg and not module.name.startswith("_")
)


def test_the_read_side_names_no_capability_and_imports_none() -> None:
    assert {"android", "browser"} <= set(CAPABILITY_IDS)
    named = re.compile(rf"\b({'|'.join(CAPABILITY_IDS)})\b", re.IGNORECASE)
    for path in sorted(INSPECTION.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        found = sorted({match.group(0) for match in named.finditer(source)})
        assert not found, f"{path.name} names the capability {found}"
        for node in ast.walk(ast.parse(source)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for module in modules:
                parts = module.split(".")
                assert not (
                    parts[:3] == ["spectragent", "core", "capabilities"]
                    and len(parts) > 3
                    and not parts[3].startswith("_")
                ), f"{path.name} imports the capability module {module}"


# ── a capability the framework has never heard of ─────────────────────


def test_a_new_capability_gets_wording_and_marks_without_a_framework_edit(tmp_path: Path) -> None:
    """Its action words itself and its element fragment describes elements: nothing else is needed."""

    class ScreenObs(FragmentBase):
        """A screenshot fragment with the four sizes, like any capability's."""

        __registry_key__ = "dial.screenshot"
        __source__: ClassVar[Literal["env", "loop"]] = "env"
        image_width: int = 0
        image_height: int = 0
        viewport_width: int = 0
        viewport_height: int = 0

    class KnobsObs(FragmentBase):
        """The dial's knobs, each with its bounds."""

        __registry_key__ = "dial.knobs"
        __source__: ClassVar[Literal["env", "loop"]] = "env"
        labels: list[str] = []

        def describe_element(self, index: int) -> ElementDescription | None:
            if not 0 <= index < len(self.labels):
                return None
            return ElementDescription(
                f"旋钮“{self.labels[index]}”", (index * 100, 0, index * 100 + 80, 40)
            )

    @dataclass
    class TurnAction(ActionBase):
        __registry_key__ = "dial.turn"
        person_verb: ClassVar[str] = "转动"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")
            knob: Annotated[int, AddressesElement("dial.knobs")]
            to_x: Annotated[int | None, PointAxis("to", "x")] = None
            to_y: Annotated[int | None, PointAxis("to", "y")] = None

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Turn a knob.", **context)

        async def execute(self, params: dict[str, Any], **_: Any) -> ActionResult:
            return ActionResult(success=True)

        @classmethod
        def present(
            cls,
            action: Action,
            result: ActionResult | None,
            *,
            element: ElementDescription | None,
            pointer: Point | None,
        ) -> ActionPresentation:
            target = element.text if element is not None else "?"
            x, y = number(action.params.get("to_x")), number(action.params.get("to_y"))
            point = (x, y) if x is not None and y is not None else None
            return ActionPresentation(
                "click",
                f"{cls.person_verb}{target}",
                point=point,
                box=element.bounds if element else None,
            )

    assert isinstance(TurnAction, PresentsAction) and issubclass(KnobsObs, DescribesElements)
    wal = WALWriter(tmp_path, "dial")
    wal.init_run(TraceConfig())
    record = StepRecord(
        step=0,
        actions=[Action(name="dial.turn", params={"knob": 1, "to_x": 150, "to_y": 20})],
        action_results=[ActionResult(success=True)],
    )
    shot = ScreenObs(image_width=200, image_height=40, viewport_width=400, viewport_height=80)
    knobs = KnobsObs(labels=["音量", "音调"])
    wal.commit_step(0, record, fragments=[fragment(0, shot), fragment(0, knobs)])
    trace = RunTrace(wal.run_dir)

    assert presenter("dial.turn") is TurnAction
    assert action_verbs(["dial.turn"]) == {"dial.turn": "转动"}
    assert run_graph(trace)["steps"][0]["actions"][0]["text"] == "转动旋钮“音调”"
    (mark,) = observation_marks(trace, "dial", 0).sets[0].marks
    # its viewport is twice the image: the framework places the point and the box
    assert (mark.kind, mark.x, mark.y, mark.box) == ("click", 75, 10, (50, 0, 90, 20))
    assert mark.text == "转动旋钮“音调”"
