"""Tests for ObservationFragment registration via conscribe bridge."""


from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    TerminalObs,
    ToolResultObs,
)


class TestFragmentRegistration:
    """All 6 first-batch fragments should be auto-registered."""

    def test_dom_registered(self) -> None:
        assert ObservationRegistrar.get("browser.dom") is DOMObs

    def test_screenshot_registered(self) -> None:
        assert ObservationRegistrar.get("browser.screenshot") is ScreenshotObs

    def test_browser_meta_registered(self) -> None:
        assert ObservationRegistrar.get("browser.browser_meta") is BrowserMetaObs

    def test_action_result_registered(self) -> None:
        assert ObservationRegistrar.get("loop.action_result") is ActionResultObs

    def test_terminal_registered(self) -> None:
        assert ObservationRegistrar.get("docker.terminal") is TerminalObs

    def test_tool_result_registered(self) -> None:
        assert ObservationRegistrar.get("tool_sandbox.tool_result") is ToolResultObs

    def test_all_six_keys_present(self) -> None:
        keys = set(ObservationRegistrar.keys())
        expected = {
            "browser.dom",
            "browser.screenshot",
            "browser.browser_meta",
            "loop.action_result",
            "docker.terminal",
            "tool_sandbox.tool_result",
        }
        assert expected.issubset(keys)


class TestFragmentSource:
    """__source__ correctly distinguishes env vs loop sourced fragments."""

    def test_env_sourced(self) -> None:
        for cls in [DOMObs, ScreenshotObs, BrowserMetaObs, TerminalObs, ToolResultObs]:
            assert cls.__source__ == "env", f"{cls.__name__} should be env-sourced"

    def test_loop_sourced(self) -> None:
        assert ActionResultObs.__source__ == "loop"


class TestFragmentIsPydanticModel:
    """Fragments should be valid Pydantic models (bridge worked)."""

    def test_terminal_instantiation(self) -> None:
        obs = TerminalObs(stdout="hello world", cwd="/tmp")
        assert obs.stdout == "hello world"
        assert obs.stderr == ""
        assert obs.exit_code == 0
        assert obs.cwd == "/tmp"

    def test_terminal_json_roundtrip(self) -> None:
        obs = TerminalObs(stdout="hi", stderr="err", exit_code=1, cwd="/")
        data = obs.model_dump()
        restored = TerminalObs(**data)
        assert restored.stdout == "hi"
        assert restored.exit_code == 1

    def test_action_result_instantiation(self) -> None:
        obs = ActionResultObs(last_action="click", success=True)
        assert obs.last_action == "click"
        assert obs.success is True
        assert obs.error is None

    def test_screenshot_default_construction(self) -> None:
        # M13: image_path and image_bytes are both optional. Consumers
        # must populate at least one in practice, but the schema allows
        # default construction so the lazy-batch resolver can build the
        # fragment incrementally.
        obs = ScreenshotObs()
        assert obs.image_path is None
        assert obs.image_bytes is None

    def test_screenshot_with_path(self) -> None:
        obs = ScreenshotObs(
            image_path="/tmp/s.png", viewport_width=1920, viewport_height=1080
        )
        assert obs.image_path == "/tmp/s.png"

    def test_screenshot_with_bytes(self) -> None:
        obs = ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            viewport_width=1280,
            viewport_height=720,
        )
        assert obs.image_bytes is not None
        assert obs.image_bytes.startswith(b"\x89PNG")
        assert obs.image_path is None

    def test_fragment_id_defaults_from_the_registry_key(self) -> None:
        obs = TerminalObs(stdout="x", cwd="/")
        assert obs.fragment_id == "docker.terminal"

    def test_fragment_id_survives_model_dump(self) -> None:
        """It used to be a ``@property``, so it was not in the data.

        Every fragment therefore reached the trace anonymous, and an
        evaluator reading a ``RunTrace`` could not tell which capability
        produced one — which "Record Once, Evaluate Many" does not
        survive.
        """
        dumped = TerminalObs(stdout="x", cwd="/").model_dump()
        assert dumped["fragment_id"] == "docker.terminal"
        assert TerminalObs.model_validate(dumped).fragment_id == "docker.terminal"

    def test_an_explicit_fragment_id_is_not_overwritten(self) -> None:
        """Reading a trace back must return what was written, not a re-derivation."""
        obs = TerminalObs(fragment_id="docker.terminal", stdout="x", cwd="/")
        assert obs.fragment_id == "docker.terminal"


class TestFragmentBaseAbstract:
    """FragmentBase itself should NOT be registered."""

    def test_base_not_in_registry(self) -> None:
        assert ObservationRegistrar.get_or_none("fragment_base") is None
