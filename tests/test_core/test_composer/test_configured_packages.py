"""A run imports only the packages its configuration names (``docs/architecture_design.md`` §4.2.1).

``ConfigComposer.for_config`` imports the framework's own packages, the
capability package each ``capability_id`` names and the agent family each
``agent_id`` names. Each case composes in a subprocess: the test interpreter
has long since imported every package, so only a fresh one shows what
composing imports. The throwaway capability package is written to a
temporary directory and joined to ``spectragent.core.capabilities`` inside the
subprocess, so the source tree gains nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import pytest
from conscribe import WiringResolutionError

import spectragent.core.composer as composer_module
from spectragent import REPO_ROOT
from spectragent.agents._registrar import AgentBase
from spectragent.core.capabilities.browser.declarations import STRATEGY_AXES as BROWSER_AXES
from spectragent.core.composer import ComposeError, ConfigComposer, UnknownPackageError
from spectragent.core.registries import capability_ids
from spectragent.scripts import generate_all

#: A second capability, complete enough to compose and prepare a UI-TARS run:
#: its declarations, a screenshot that reports geometry, a platform fragment
#: whose closed Literal says what it reports, two of the actions UI-TARS
#: declares (none of its optional ones), the capability and its own
#: single-capability environment.
_PROBE_PACKAGE = {
    "__init__.py": "",
    "declarations.py": """
        from spectragent.schemas.task import SetupBase

        class ProbeSetup(SetupBase):
            __registry_key__ = "probe"
            __env_types__ = ("probe",)

        STRATEGY_AXES = {}
        TASK_SETUP = ProbeSetup
        OPTIONAL_EXTRA_MODULES = ()
    """,
    "observations.py": """
        from typing import Literal

        from spectragent.schemas.observation._registrar import FragmentBase

        class ProbeScreenshot(FragmentBase):
            __registry_key__ = "probe.screenshot"
            image_width: int = 0
            image_height: int = 0
            viewport_width: int = 0
            viewport_height: int = 0

        class ProbePlatform(FragmentBase):
            __registry_key__ = "probe.platform"
            operating_system: Literal["linux"] = "linux"
    """,
    "actions.py": """
        from dataclasses import dataclass
        from typing import Annotated, Any

        from pydantic import BaseModel, ConfigDict

        from spectragent.core.actions._registrar import ActionBase
        from spectragent.core.actions.params import PointAxis
        from spectragent.schemas.action import ActionDescriptor, ActionResult

        @dataclass
        class ProbeClickCoordinate(ActionBase):
            __registry_key__ = "probe.click_coordinate"

            class Params(BaseModel):
                model_config = ConfigDict(extra="forbid")
                coordinate_x: Annotated[int, PointAxis("coordinate", "x")]
                coordinate_y: Annotated[int, PointAxis("coordinate", "y")]

            def descriptor(self, **context: Any) -> ActionDescriptor:
                return self.describe("Click a point.", **context)

            async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
                return ActionResult(success=True)

        @dataclass
        class ProbeTypeCoordinate(ActionBase):
            __registry_key__ = "probe.type_coordinate"

            # The relative name's params, as the browser's: the point is optional.
            class Params(BaseModel):
                model_config = ConfigDict(extra="forbid")
                coordinate_x: Annotated[int | None, PointAxis("coordinate", "x")] = None
                coordinate_y: Annotated[int | None, PointAxis("coordinate", "y")] = None
                text: str

            def descriptor(self, **context: Any) -> ActionDescriptor:
                return self.describe("Type, clicking a point first when given one.", **context)

            async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
                return ActionResult(success=True)
    """,
    "capability.py": """
        from typing import Any

        from spectragent.core.capabilities._registrar import CapabilityBase
        from spectragent.core.capabilities.probe import observations  # noqa: F401
        from spectragent.core.capabilities.probe.actions import (
            ProbeClickCoordinate,
            ProbeTypeCoordinate,
        )
        from spectragent.schemas.action import ActionResult

        class ProbeCapability(CapabilityBase):
            __registry_key__ = "probe"
            __wiring__ = {
                "observation": {
                    "registry": "observation",
                    "keys": ["probe.screenshot", "probe.platform"],
                    "declare_only": True,
                },
                "action": {
                    "registry": "action",
                    "keys": ["probe.click_coordinate", "probe.type_coordinate"],
                    "declare_only": True,
                },
            }

            def get_action_descriptors(self, **context: Any) -> list[Any]:
                actions = (ProbeClickCoordinate(), ProbeTypeCoordinate())
                return [action.descriptor(**context) for action in actions]

            async def execute_action(self, action: Any) -> ActionResult:
                return ActionResult(success=True)
    """,
    "environment.py": """
        from spectragent.core.environments._registrar import EnvironmentBase

        class ProbeEnvironment(EnvironmentBase):
            __registry_key__ = "probe"
            __wiring__ = {"capabilities": ("capability", ["probe"])}
    """,
    # A class of a framework layer (hook) that holds the inline-only restorer:
    # it holds it only in a run that names the probe, never in one an earlier
    # import of the probe happened to precede.
    "hooks.py": """
        from spectragent.core.hooks._registrar import HookBase
        from spectragent.core.loops.restore import Restorer

        class ProbeRestorerHook(HookBase):
            __registry_key__ = "probe_restorer"
            __wiring__ = {"restorer": ("restorer", ["prefix_replay"])}

            restorer: Restorer
    """,
}

#: A family whose one agent narrows its loop to ``react``, so nothing it names
#: can write a ``branch`` loop block, the restorer's only holder.
_REACT_ONLY_FAMILY = {
    "__init__.py": "",
    "agent.py": """
        from dataclasses import dataclass

        from spectragent.agents._registrar import AgentBase

        @dataclass
        class ReactOnlyProbe(AgentBase):
            __registry_key__ = "reactonlyprobe"
            __wiring__ = {"agent_loop": ("agent_loop", ["react"])}
    """,
}

#: Every module of the browser capability package starts with this.
_BROWSER = "spectragent.core.capabilities.browser"

#: UI-TARS on the probe capability, which provides two of its required actions
#: and none of its optional ones.
_UI_TARS_ON_PROBE = {
    "agent": [
        {
            "agent_id": "ui_tars",
            "id": "main",
            "agent_loop": {"loop_id": "react"},
            "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
            "memory": {"memory_id": "simple"},
        }
    ],
    "environment": [{"env_type": "probe", "capabilities": [{"capability_id": "probe"}]}],
}

_PREPARE = """
import json
import sys

import spectragent.core.capabilities as capabilities

capabilities.__path__.append({root!r})

from spectragent.core.composer import ConfigComposer
from spectragent.core.runtime import AgentRuntime

raw = {raw}
runtime = AgentRuntime.for_agent(ConfigComposer.for_config(raw).compose(raw), "main")
runtime.prepare()
print(json.dumps({{
    "actions": sorted(runtime.action_space.allowed_actions),
    "browser_modules": sorted(m for m in sys.modules if m.startswith({browser!r})),
    "agent_cdp": "agent_cdp" in sys.modules,
}}))
"""

_COMPOSE = """
import json
import sys
from pathlib import Path

import yaml

import spectragent.core.capabilities as capabilities

capabilities.__path__.append({root!r})

from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import agent_families

raw = {raw}
composer = ConfigComposer.for_config(raw)
run = composer.compose(raw)
print(json.dumps({{
    "families": [f for f in agent_families() if f"spectragent.agents.{{f}}" in sys.modules],
    "probe": any(m.split(".")[:4] == ["spectragent", "core", "capabilities", "probe"]
                 for m in sys.modules),
    "browser_modules": sorted(m for m in sys.modules if m.startswith({browser!r})),
    "agent_cdp": "agent_cdp" in sys.modules,
    "layers": sorted(composer.registrars),
    "environments": [type(env).__name__ for env in run.instances("environment")],
}}))
"""


class _Composed(TypedDict):
    """What composing one configuration imported and built, as ``_COMPOSE`` prints it."""

    families: list[str]
    probe: bool
    browser_modules: list[str]
    agent_cdp: bool
    layers: list[str]
    environments: list[str]


class _Prepared(TypedDict):
    """What ``_PREPARE`` reports: the run's actions and what it imported."""

    actions: list[str]
    browser_modules: list[str]
    agent_cdp: bool


class _Generated(TypedDict):
    """What the whole-catalog discovery registered, against the packages that exist."""

    families: list[str]
    packages: list[str]
    capabilities: list[str]
    capability_packages: list[str]
    environments: list[str]


def _run(code: str) -> object:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def react_only_root(tmp_path: Path) -> Path:
    """A directory holding the throwaway ``reactonlyprobe`` agent family."""
    package = tmp_path / "reactonlyprobe"
    package.mkdir()
    for name, source in _REACT_ONLY_FAMILY.items():
        (package / name).write_text(textwrap.dedent(source))
    return tmp_path


@pytest.fixture
def probe_root(tmp_path: Path) -> Path:
    """A directory holding the throwaway ``probe`` capability package."""
    package = tmp_path / "probe"
    package.mkdir()
    for name, source in _PROBE_PACKAGE.items():
        (package / name).write_text(textwrap.dedent(source))
    return tmp_path


def _compose(root: Path, raw: str) -> _Composed:
    return cast(_Composed, _run(_COMPOSE.format(root=str(root), raw=raw, browser=_BROWSER)))


def _shipped(name: str) -> str:
    return f"yaml.safe_load(Path('configs/{name}').read_text())"


@pytest.mark.parametrize(
    ("config", "family"),
    [
        ("browser_use_local.yaml", "browser_use"),
        ("skyvern_agent_local.yaml", "skyvern"),
        ("ui_tars_local.yaml", "ui_tars"),
    ],
)
def test_a_browser_run_imports_only_the_agent_family_it_names(
    probe_root: Path, config: str, family: str
) -> None:
    assert _compose(probe_root, _shipped(config))["families"] == [family]


def test_a_browser_run_does_not_import_a_capability_package_it_does_not_name(
    probe_root: Path,
) -> None:
    facts = _compose(probe_root, _shipped("browser_use_local.yaml"))
    assert facts["probe"] is False, "not even the probe's declarations"
    assert facts["environments"] == ["BrowserEnvironment"]


def test_a_run_of_another_capability_imports_neither_the_browser_capability_nor_an_agent(
    probe_root: Path,
) -> None:
    raw = {"environment": [{"env_type": "probe", "capabilities": [{"capability_id": "probe"}]}]}
    facts = _compose(probe_root, repr(raw))
    assert facts["probe"] is True, "the probe is a capability package a run can name"
    assert facts["browser_modules"] == [] and facts["agent_cdp"] is False
    assert facts["families"] == []
    assert facts["environments"] == ["ProbeEnvironment"]


def test_a_judge_configuration_imports_no_browser_module(probe_root: Path) -> None:
    # An LLM-only configuration names no capability: no browser module comes in,
    # not even through the framework's export and replay review modules.
    facts = _compose(probe_root, _shipped("lookahead/judge_deepseek.yaml"))
    assert not {"agent", "capability", "environment"} & set(facts["layers"])
    assert not set(BROWSER_AXES) & set(facts["layers"]), "no axis of a capability it never named"
    assert facts["browser_modules"] == [] and facts["agent_cdp"] is False
    assert facts["families"] == []


def test_a_run_imports_only_the_benchmark_it_names() -> None:
    """A benchmark is a package the run root names (``benchmark_id``), like an agent family."""
    code = textwrap.dedent(
        """
        import json, sys
        from spectragent.benchmarks.selection import read_benchmark, run_root
        from spectragent.core.composer import ConfigComposer
        from spectragent.core.registries import benchmark_ids

        raw = run_root({}, read_benchmark("online_mind2web"))
        ConfigComposer.for_config(raw).validate(raw)
        imported = [b for b in benchmark_ids() if f"spectragent.benchmarks.{b}" in sys.modules]
        print(json.dumps(imported))
        """
    )
    assert _run(code) == ["online_mind2web"]


def test_an_agent_composes_with_a_capability_lacking_some_of_its_actions(
    probe_root: Path,
) -> None:
    # A run's schema is conscribe's partial build: UI-TARS's optional actions
    # the probe does not provide are left out, and negotiation in prepare()
    # decides what the run uses.
    code = _PREPARE.format(root=str(probe_root), raw=repr(_UI_TARS_ON_PROBE), browser=_BROWSER)
    prepared = cast(_Prepared, _run(code))
    assert prepared["actions"] == ["loop.done", "probe.click_coordinate", "probe.type_coordinate"]
    assert prepared["browser_modules"] == [] and prepared["agent_cdp"] is False


_EXPLAIN = """
import json
from pathlib import Path

import yaml

import spectragent.core.capabilities as capabilities

capabilities.__path__.append({root!r})

from spectragent.cli.configuration import explain_config

path = Path({root!r}) / "run.yaml"
path.write_text(yaml.safe_dump({raw}))
provenance = explain_config(path)["provenance"]
print(json.dumps({{
    "declared": sorted(provenance["declarations"]),
    "unresolved": [
        [item["owner_key"], item["name"], item["missing_optional_keys"]]
        for item in provenance["unresolved_wiring"]
    ],
}}))
"""


_TRACE_WRITER = """
import json

from spectragent.core.composer import ComposeError, ConfigComposer

llm = [{{"model_type": "openai", "model": "m", "provider": {{"name": "direct"}}}}]
plain = {{"llm": llm, "hook": [{{"hook_id": "trace_writer"}}]}}
archive = ConfigComposer.for_config(plain).compose(plain).single("hook").archive
named = {{"llm": llm, "hook": [{{"hook_id": "trace_writer", "archive": [{{"archiver_id": "browser.network"}}]}}]}}
try:
    ConfigComposer.for_config(named).compose(named)
except ComposeError as exc:
    print(json.dumps({{"archive": archive, "code": exc.issue.code, "path": exc.issue.path}}))
"""


def test_a_trace_writer_composes_in_a_run_with_no_archiver() -> None:
    # No named capability provides an archiver, so the archiver layer is left
    # out: archive left out keeps its default, and an archiver named anyway is
    # refused in plain words rather than as a wiring or validation error.
    refused = cast(dict[str, object], _run(_TRACE_WRITER.format()))
    assert refused == {"archive": [], "code": "no_implementation", "path": "hook[0].archive"}


_REFUSAL = """
import json

from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.registries import discover_every_implementation

raw = {raw}


def refusal():
    try:
        ConfigComposer.for_config(raw).compose(raw)
    except ComposeError as exc:
        return [exc.issue.code, exc.issue.path, exc.issue.remedy, str(exc)]
    return None


fresh = refusal()
discover_every_implementation()
print(json.dumps({{"fresh": fresh, "imported": refusal()}}))
"""

_LLM = [{"model_type": "openai", "model": "m", "provider": {"name": "direct"}}]


def _refusals(raw: dict[str, object]) -> dict[str, list[str]]:
    """How a fresh interpreter refuses *raw*, then again after importing every package.

    In a fresh interpreter: the test session's own fakes (an archiver some test
    module registers) would keep layers in that a run naming no capability
    leaves out.
    """
    return cast(dict[str, list[str]], _run(_REFUSAL.format(raw=repr(raw))))


def test_an_archiver_an_earlier_import_registered_is_still_refused() -> None:
    # Once the browser package is imported its archivers stay registered, and
    # trace_writer's archive takes their bare keys. A run naming no capability
    # holds no archiver layer, so naming one is refused as in a fresh process.
    archive = [{"hook_id": "trace_writer", "archive": ["browser.network"]}]
    refusals = _refusals({"llm": _LLM, "hook": archive})
    assert refusals["imported"] == refusals["fresh"]
    assert refusals["fresh"][:2] == ["no_implementation", "hook[0].archive"]


def test_explain_says_what_a_run_cannot_provide(probe_root: Path) -> None:
    # Explained with the run's partial build: the complete one would refuse
    # UI-TARS's declarations in this run, where no capability offers scroll or
    # a capture state.
    code = _EXPLAIN.format(root=str(probe_root), raw=repr(_UI_TARS_ON_PROBE))
    explained = cast(dict[str, list[object]], _run(code))
    assert "agent/ui_tars" in explained["declared"]
    assert explained["unresolved"] == [
        ["ui_tars", "observation", ["capture_state"]],
        [
            "ui_tars",
            "action",
            ["scroll", "send_keys", "drag", "wait", "long_press", "open_app", "press_key"],
        ],
    ]


_DIGESTS = """
import hashlib
import json
from pathlib import Path

import yaml

import spectragent.core.capabilities as capabilities

capabilities.__path__.append({root!r})

from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import discover_every_implementation
from spectragent.core.trace.redaction import redact_config

configs = {{name: yaml.safe_load(Path(name).read_text()) for name in {shipped!r}}}
configs["ui_tars_on_probe"] = {raw}


def digest(raw):
    run = ConfigComposer.for_config(raw).compose(raw)
    dump = redact_config(run.top_level_cfg, record_secrets=True)
    text = json.dumps(dump, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


before = {{name: digest(raw) for name, raw in configs.items()}}
discover_every_implementation()
after = {{name: digest(raw) for name, raw in configs.items()}}
print(json.dumps({{"before": before, "after": after}}))
"""

#: Shipped configurations that name no agent and no capability.
_AGENT_LESS = (
    "configs/flywheel/export_sft.yaml",
    "configs/lookahead/judge_deepseek.yaml",
    "configs/replay_review/jev_direct.yaml",
    "configs/replay_review/jev_dmx.yaml",
    "configs/replay_review/jev_openrouter.yaml",
)


def test_a_runs_configuration_hash_does_not_follow_import_history(probe_root: Path) -> None:
    # Which layers a run holds follows the packages it names. Implementations an
    # earlier import left registered (the browser's archivers, other families)
    # must not add an empty section to the resolved config its hash covers, or
    # one experiment becomes two groups in the viewer.
    code = _DIGESTS.format(root=str(probe_root), shipped=_AGENT_LESS, raw=repr(_UI_TARS_ON_PROBE))
    digests = cast(dict[str, dict[str, str]], _run(code))
    assert set(digests["before"]) == {*_AGENT_LESS, "ui_tars_on_probe"}
    assert digests["before"] == digests["after"]


_REACT_ONLY = """
import hashlib
import json

import spectragent.agents as agents

agents.__path__.append({root!r})

from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.redaction import redact_config

raw = {raw}


def built():
    composer = ConfigComposer.for_config(raw)
    dump = redact_config(composer.compose(raw).top_level_cfg, record_secrets=True)
    text = json.dumps(dump, sort_keys=True, separators=(",", ":"))
    return {{
        "restorer": "restorer" in composer.registrars,
        "digest": hashlib.sha256(text.encode()).hexdigest(),
    }}


fresh = built()
import spectragent.agents.ui_tars  # an earlier run's family, whose agent may branch
print(json.dumps({{"fresh": fresh, "after": built()}}))
"""

_REACT_ONLY_RUN = {
    "capability": [
        {
            "capability_id": "browser",
            "backend": {"provider_id": "local_chrome"},
            "dom_cleaner": {"cleaner_type": "browser_use"},
            "dom_serializer": {"serializer_type": "text"},
        }
    ],
    "agent": [
        {
            "agent_id": "reactonlyprobe",
            "agent_loop": {"loop_id": "react"},
            "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
            "memory": {"memory_id": "simple"},
        }
    ],
}


def test_a_restorer_no_named_block_can_hold_is_left_out(react_only_root: Path) -> None:
    # A path from the top level must reach a block of an inline-only layer, and
    # only a branch loop holds the restorer. The named family's agent runs on
    # react alone, so the run leaves the restorer out: fresh, and the same after
    # an earlier import of a family whose agent may branch.
    code = _REACT_ONLY.format(root=str(react_only_root), raw=repr(_REACT_ONLY_RUN))
    built = cast(dict[str, dict[str, object]], _run(code))
    assert built["fresh"]["restorer"] is False
    assert built["fresh"] == built["after"]


_EXPORT = """
import json
import sys
from pathlib import Path

import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import agent_families
from spectragent.export.render import StudentRenderer
from spectragent.export.runner import ExportRunner

raw = yaml.safe_load(Path("configs/flywheel/export_sft.yaml").read_text())
exporter = ExportRunner.from_run(ConfigComposer.for_config(raw).compose(raw), origin="test").exporter
renderer = StudentRenderer.from_config(
    Path(exporter.student_run_config),
    untranslatable=exporter.translator.untranslatable,
    thought_source=exporter.thought_source,
    thought_index_refs=exporter.thought_index_refs,
)
print(json.dumps({
    "loop": renderer.loop_id,
    "families": [f for f in agent_families() if f"spectragent.agents.{f}" in sys.modules],
}))
"""


def test_the_export_composes_its_document_and_then_its_student() -> None:
    # The export document names no package; the student's configuration names
    # its own (UI-TARS, the browser), composed by the renderer itself.
    composed = cast(dict[str, object], _run(_EXPORT))
    assert composed == {"loop": "react", "families": ["ui_tars"]}


_REPLAY_REVIEW = """
import json
import sys
from pathlib import Path

from spectragent.core.registries import agent_families
from spectragent.replay_review.evidence import _source_browser

capability, configured = _source_browser(Path("configs/flywheel/record_ui_tars_pinned.yaml"))
print(json.dumps({
    "capability": type(capability).__name__,
    "cleaner": configured["dom_cleaner"]["cleaner_type"],
    "families": [f for f in agent_families() if f"spectragent.agents.{f}" in sys.modules],
}))
"""


def test_replay_review_composes_the_source_runs_browser() -> None:
    # As replay review builds the recorded recipe: the source configuration's
    # own packages, and only its browser block instantiated.
    composed = cast(dict[str, object], _run(_REPLAY_REVIEW))
    assert composed == {
        "capability": "BrowserCapability",
        "cleaner": "browser_use",
        "families": ["ui_tars"],
    }


def test_a_misspelled_declared_key_fails_the_complete_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A run's partial build leaves a declared key with no implementation out,
    # so generation and the whole-catalog Composer are what catch a typo.
    @dataclass
    class _Misspelled(AgentBase):
        __registry_key__ = "misspelled_probe"
        __wiring__ = {
            "action": {
                "registry": "action",
                "keys": ["click"],
                "optional_keys": ["scrol"],
                "declare_only": True,
            }
        }

    with pytest.raises(WiringResolutionError, match="scrol"):
        ConfigComposer.for_every_package()
    monkeypatch.setattr(generate_all, "GENERATED_DIR", tmp_path)
    with pytest.raises(WiringResolutionError, match="scrol"):
        generate_all.main([])
    assert list(tmp_path.iterdir()) == []


def _no_discovery(*packages: str, **names: object) -> None:
    pytest.fail(f"discovery ran before the refusal: {packages} {names}")


@pytest.mark.parametrize(
    ("raw", "code", "path"),
    [
        (
            {"capability": [{"capability_id": "nowhere"}]},
            "unknown_capability_package",
            "capability[0].capability_id",
        ),
        (
            {"environment": [{"env_type": "browser", "capabilities": [{"capability_id": "x"}]}]},
            "unknown_capability_package",
            "environment[0].capabilities[0].capability_id",
        ),
        ({"agent": [{"agent_id": "nowhere.dom"}]}, "unknown_agent_family", "agent[0].agent_id"),
        # The memory axis lives under spectragent/agents/, and is no family.
        ({"agent": [{"agent_id": "memory.simple"}]}, "unknown_agent_family", "agent[0].agent_id"),
    ],
)
def test_a_name_with_no_package_is_refused_before_anything_is_imported(
    monkeypatch: pytest.MonkeyPatch, raw: dict[str, object], code: str, path: str
) -> None:
    monkeypatch.setattr(composer_module, "discover_implementations", _no_discovery)
    with pytest.raises(UnknownPackageError) as refused:
        ConfigComposer.for_config(raw)
    issue = refused.value.issue
    assert issue is not None and (issue.code, issue.path) == (code, path)
    assert path in str(refused.value)


def test_an_agent_without_a_capability_is_refused_in_plain_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its declarations name the capability's actions, which conscribe would
    # report in its own terms ("registered: ['loop.done']").
    monkeypatch.setattr(composer_module, "discover_implementations", _no_discovery)
    raw = {"agent": [{"agent_id": "browser_use.dom", "agent_loop": {"loop_id": "react"}}]}
    with pytest.raises(ComposeError, match="add an environment block") as refused:
        ConfigComposer.for_config(raw)
    issue = refused.value.issue
    assert issue is not None
    assert (issue.code, issue.path) == ("agent_without_capability", "agent[0].agent_id")
    assert f"capability packages: {', '.join(capability_ids())}" in str(refused.value)


def test_a_free_form_field_names_no_package() -> None:
    # Only capability and agent blocks name packages: the same key inside a
    # hook's parameters or a provider's extra_body is the user's data.
    free_form = {"capability_id": "nowhere", "agent_id": "nowhere.dom"}
    raw = {
        "llm": [
            {
                "model_type": "openai",
                "model": "m",
                "provider": {"name": "openrouter", "extra_body": free_form},
            }
        ],
        "hook": [
            {
                "hook_id": "prompt_contribution",
                "function": "tests.test_core.test_hooks.test_prompt_contribution:plain_contribution",
                "parameters": free_form,
            }
        ],
    }
    run = ConfigComposer.for_config(raw).compose(raw)
    assert run.single("hook").parameters == free_form
    assert run.single("llm").extra_body == free_form


def test_an_environment_without_a_capability_says_what_is_missing() -> None:
    # No capability is named, so no environment is imported: the refusal says
    # so instead of failing validation with "Extra inputs are not permitted".
    refused = cast(
        dict[str, str],
        _run(
            """
import json

from spectragent.core.composer import ComposeError, ConfigComposer

raw = {"environment": [{"env_type": "browser"}]}
try:
    ConfigComposer.for_config(raw).compose(raw)
except ComposeError as exc:
    print(json.dumps({"code": exc.issue.code, "path": exc.issue.path, "message": str(exc)}))
"""
        ),
    )
    assert (refused["code"], refused["path"]) == ("no_implementation", "environment")
    assert f"capability packages: {', '.join(capability_ids())}" in refused["message"]


def test_an_archiver_section_says_where_archivers_are_written() -> None:
    # Naming the capability that provides archivers would only meet the
    # inline_only_layer refusal, so this refusal says where they go instead.
    refusals = _refusals({"llm": _LLM, "archiver": []})
    assert refusals["imported"] == refusals["fresh"]
    code, path, remedy, message = refusals["fresh"]
    assert (code, path) == ("no_implementation", "archiver")
    assert "written under hook[trace_writer].archive" in message
    assert remedy.startswith("Leave it out, or write its instances under hook[trace_writer]")


_GENERATE = """
import json

from spectragent.agents._registrar import AgentRegistrar
from spectragent.agents.prompts import agent_family
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.environments._registrar import EnvironmentRegistrar
from spectragent.core.registries import agent_families, capability_ids, discover_every_implementation

discover_every_implementation()
print(json.dumps({
    "families": sorted({agent_family(key) for key in AgentRegistrar.keys()}),
    "packages": list(agent_families()),
    "capabilities": sorted(CapabilityRegistrar.keys()),
    "capability_packages": list(capability_ids()),
    "environments": sorted(EnvironmentRegistrar.keys()),
}))
"""


def test_generation_imports_every_agent_family_and_capability() -> None:
    facts = cast(_Generated, _run(_GENERATE))
    assert facts["packages"] and facts["families"] == facts["packages"]
    assert facts["capability_packages"] and facts["capabilities"] == facts["capability_packages"]
    assert "browser" in facts["environments"]
