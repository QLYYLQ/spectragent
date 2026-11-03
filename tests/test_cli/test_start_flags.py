"""The single-task start flags every capability declares (``docs/architecture_design.md`` §4.9 "起始参数").

``spectragent run`` and ``config check`` / ``config explain`` offer the static
union of every capability package's ``START_FLAGS``, listed by capability in
``--help`` and in tab completion; the framework's CLI names none of them, and
a flag of a capability the configuration does not name is refused before
anything starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from spectragent import REPO_ROOT
from spectragent.cli import main
from spectragent.core.registries import start_flags

BROWSER_CONFIG = REPO_ROOT / "configs" / "browser_use_local.yaml"


def _complete(words: str) -> str:
    """What ``spectragent``'s zsh completion offers for the command line *words*."""
    env = {
        **os.environ,
        "_SPECTRAGENT_COMPLETE": "complete_zsh",
        "_TYPER_COMPLETE_ARGS": words,
    }
    script = "import sys; from spectragent.cli import main; sys.exit(main())"
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    return done.stdout


@pytest.mark.parametrize("command", ["spectragent run", "spectragent config check"])
def test_tab_completion_offers_every_start_flag_with_its_capability(command: str) -> None:
    offered = _complete(f"{command} --")
    for capability_id, flags in start_flags().items():
        for flag in flags:
            assert f'"{flag.flag}":"({capability_id}) ' in offered, (command, flag.flag)


def test_the_config_commands_list_the_start_flags_by_capability(capsys) -> None:
    assert main(["config", "check", "--help"]) == 0
    text = capsys.readouterr().out
    assert "browser start flags" in text
    for flag in start_flags()["browser"]:
        assert flag.flag in text


def test_config_check_prepares_with_a_start_flag(capsys) -> None:
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

    argv = ["config", "check", str(BROWSER_CONFIG), "--json"]
    assert main([*argv, "--start-url", "https://shop.example"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == "configuration_and_prepare"
    assert main([*argv, "--replay-archive", "/runs/src"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["stage"], error["type"]) == ("prepare", BrowserCooperationError.__name__)


def test_config_check_refuses_a_flag_of_a_capability_the_configuration_lacks(
    tmp_path: Path, capsys
) -> None:
    config = tmp_path / "android.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "environment": [
                    {"env_type": "android", "capabilities": [{"capability_id": "android"}]}
                ]
            }
        )
    )
    code = main(["config", "check", str(config), "--json", "--start-url", "https://a.example"])
    assert code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["stage"] == "validation"
    assert "names no browser capability" in error["message"]


def test_start_app_is_the_android_capabilitys_and_refused_for_a_run_without_one(capsys) -> None:
    assert [flag.flag for flag in start_flags()["android"]] == ["--start-app"]
    assert main(["config", "check", "--help"]) == 0
    text = capsys.readouterr().out
    assert "android start flags" in text and "--start-app" in text

    argv = ["config", "check", str(BROWSER_CONFIG), "--json", "--start-app", "Settings"]
    assert main(argv) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["stage"] == "validation"
    assert "--start-app is a start flag of the android capability" in error["message"]
