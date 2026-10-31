"""Without the optional numpy, the Composer still builds and a browser config composes.

numpy is the ``learned-mouse`` extra: only the ``learned`` mouse trajectory
needs it, and only once it is constructed. Discovery skips the inference
module that imports it at top level (the browser's ``OPTIONAL_EXTRA_MODULES``,
``docs/architecture_design.md`` §4.2.1, §4.13), so a machine without the extra
composes every run that does not select ``learned``, and a run that does is
refused at composition, before it starts, naming the extra.

Each case runs in a subprocess with numpy blocked: the test interpreter has
numpy (the dev extra installs it) and has long since imported the module.
"""

from __future__ import annotations

import subprocess
import sys

from spectragent import REPO_ROOT

_PRELUDE = """
import sys
sys.modules["numpy"] = None  # from here on, importing numpy raises ModuleNotFoundError
from pathlib import Path
import yaml
from spectragent.core.composer import ConfigComposer
raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
composer = ConfigComposer.for_config(raw)
"""


def _without_numpy(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PRELUDE + body],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_composer_builds_and_a_browser_config_composes() -> None:
    result = _without_numpy(
        """
from spectragent.core.capabilities.browser.input.trajectory import MouseTrajectoryRegistrar
assert "learned" in MouseTrajectoryRegistrar.keys()
assert composer.compose(raw).ids("agent") == ["main"]
"""
    )
    assert result.returncode == 0, result.stderr


def test_selecting_learned_is_refused_naming_the_extra() -> None:
    result = _without_numpy(
        """
from spectragent.core.composer import InstantiationError
capability = raw["environment"][0]["capabilities"][0]
capability["mouse_trajectory"] = {"trajectory_type": "learned", "weights_dir": "weights"}
try:
    composer.compose(raw)
except InstantiationError as exc:
    print(exc)
else:
    raise SystemExit("composed a learned trajectory without numpy")
"""
    )
    assert result.returncode == 0, result.stderr
    assert "uv sync --extra learned-mouse" in result.stdout
