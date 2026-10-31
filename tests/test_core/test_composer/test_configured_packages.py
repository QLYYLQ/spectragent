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
from pathlib import Path
from typing import TypedDict, cast

import pytest

import spectragent.core.composer as composer_module
from spectragent import REPO_ROOT
from spectragent.core.capabilities.browser.declarations import STRATEGY_AXES as BROWSER_AXES
from spectragent.core.composer import ConfigComposer, UnknownPackageError

#: A second capability, complete enough to compose: its declarations, its
#: capability and its own single-capability environment.
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
    "capability.py": """
        from spectragent.core.capabilities._registrar import CapabilityBase

        class ProbeCapability(CapabilityBase):
            __registry_key__ = "probe"
    """,
    "environment.py": """
        from spectragent.core.environments._registrar import EnvironmentBase

        class ProbeEnvironment(EnvironmentBase):
            __registry_key__ = "probe"
            __wiring__ = {"capabilities": ("capability", ["probe"])}
    """,
}

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
    "browser_capability": "spectragent.core.capabilities.browser.capability" in sys.modules,
    "layers": sorted(composer.registrars),
    "environments": [type(env).__name__ for env in run.instances("environment")],
}}))
"""


class _Composed(TypedDict):
    """What composing one configuration imported and built, as ``_COMPOSE`` prints it."""

    families: list[str]
    probe: bool
    browser_capability: bool
    layers: list[str]
    environments: list[str]


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
def probe_root(tmp_path: Path) -> Path:
    """A directory holding the throwaway ``probe`` capability package."""
    package = tmp_path / "probe"
    package.mkdir()
    for name, source in _PROBE_PACKAGE.items():
        (package / name).write_text(textwrap.dedent(source))
    return tmp_path


def _compose(root: Path, raw: str) -> _Composed:
    return cast(_Composed, _run(_COMPOSE.format(root=str(root), raw=raw)))


def _shipped(name: str) -> str:
    return f"yaml.safe_load(Path('configs/{name}').read_text())"


@pytest.mark.parametrize(
    ("config", "family"),
    [("browser_use_local.yaml", "browser_use"), ("ui_tars_local.yaml", "ui_tars")],
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
    assert facts["browser_capability"] is False
    assert facts["families"] == []
    assert facts["environments"] == ["ProbeEnvironment"]


def test_a_configuration_naming_no_capability_composes_without_their_layers(
    probe_root: Path,
) -> None:
    facts = _compose(probe_root, _shipped("lookahead/judge_deepseek.yaml"))
    assert not {"agent", "capability", "environment"} & set(facts["layers"])
    assert not set(BROWSER_AXES) & set(facts["layers"]), "no axis of a capability it never named"
    assert facts["browser_capability"] is False and facts["families"] == []


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
    def discovered(*packages: str, **names: object) -> None:
        pytest.fail(f"discovery ran before the refusal: {packages} {names}")

    monkeypatch.setattr(composer_module, "discover_implementations", discovered)
    with pytest.raises(UnknownPackageError) as refused:
        ConfigComposer.for_config(raw)
    issue = refused.value.issue
    assert issue is not None and (issue.code, issue.path) == (code, path)
    assert path in str(refused.value)


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
    assert "capability packages: browser" in refused["message"]


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
