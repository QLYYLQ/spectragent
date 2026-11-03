"""Task setups are classes that capability packages register, not a closed union.

``schemas/task.py`` defines the contract and ``TaskSetupRegistrar``; each
capability package registers its own setup and names it ``TASK_SETUP`` in its
``declarations.py`` (``docs/architecture_design.md`` §4.10).
``TaskDefinition.setup`` dispatches on ``setup_type`` among whatever is
registered, so a capability added later needs no edit here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from conscribe.testing import isolated_registries
from pydantic import ValidationError

from spectragent.core.capabilities.browser.task_setup import BrowserSetup
from spectragent.core.registries import capability_declarations
from spectragent.schemas.task import SetupBase, TaskDefinition, TaskSetup, TaskSetupRegistrar

#: A setup a second capability would register, captured here and activated
#: only by the tests that need it.
FAKES = isolated_registries()

with FAKES:

    class ContainerSetup(SetupBase):
        """Stands in for the setup of a capability that does not exist yet."""

        __registry_key__ = "second_capability_for_tests"
        __env_types__: ClassVar[tuple[str, ...]] = ("second_capability_for_tests",)
        __raw_keys__: ClassVar[tuple[str, ...]] = ("image",)

        image: str | None = None
        workdir: str | None = None

        def to_context(self) -> dict[str, Any]:
            return {"image": self.image} if self.image else {}


@pytest.fixture
def container_setup() -> Iterator[type[SetupBase]]:
    with FAKES.activate():
        yield ContainerSetup


class TestRegistration:
    def test_each_capability_packages_setup_is_registered(self) -> None:
        for capability in capability_declarations():
            setup = capability.task_setup
            assert TaskSetupRegistrar.get(setup.__registry_key__) is setup

    def test_the_browser_registers_its_setup(self) -> None:
        assert TaskSetupRegistrar.get("browser") is BrowserSetup
        assert BrowserSetup.__env_types__ == ("browser",)
        assert BrowserSetup.__raw_keys__ == ("start_url", "storage_state")

    def test_a_registered_setup_satisfies_the_protocol(self) -> None:
        assert isinstance(BrowserSetup(), TaskSetup)

    def test_the_setup_type_is_the_registry_key(self) -> None:
        assert BrowserSetup().setup_type == "browser"
        assert BrowserSetup.model_validate({"start_url": "https://e.com"}).setup_type == "browser"

    def test_another_setup_type_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="does not name BrowserSetup"):
            BrowserSetup.model_validate({"setup_type": "second_capability_for_tests"})


class TestTaskDefinitionDispatch:
    def test_dispatches_on_setup_type(self) -> None:
        task = TaskDefinition.model_validate(
            {"task_id": "t1", "benchmark": "b", "setup": {"setup_type": "browser"}}
        )
        assert isinstance(task.setup, BrowserSetup)

    def test_a_setup_registered_later_is_dispatched_to(
        self, container_setup: type[SetupBase]
    ) -> None:
        task = TaskDefinition.model_validate(
            {
                "task_id": "t1",
                "benchmark": "b",
                "setup": {"setup_type": "second_capability_for_tests", "image": "img"},
            }
        )
        assert isinstance(task.setup, container_setup)
        assert task.setup.to_context() == {"image": "img"}

    def test_an_unregistered_setup_type_is_refused_naming_the_registered(self) -> None:
        with pytest.raises(
            ValidationError,
            match=r"'unknown_env' names no registered.*browser.*capability_declarations\(\)",
        ):
            TaskDefinition.model_validate(
                {"task_id": "t1", "benchmark": "b", "setup": {"setup_type": "unknown_env"}}
            )

    def test_a_setup_without_setup_type_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="setup_type None names no registered"):
            TaskDefinition.model_validate(
                {"task_id": "t1", "benchmark": "b", "setup": {"start_url": "https://e.com"}}
            )

    def test_a_task_without_setup_has_none(self) -> None:
        assert TaskDefinition(task_id="t1", benchmark="b").setup is None

    def test_a_dump_keeps_the_subclass_fields_and_validates_back(
        self, container_setup: type[SetupBase]
    ) -> None:
        task = TaskDefinition(task_id="t1", benchmark="b", setup=container_setup(image="img"))
        dumped = task.model_dump(mode="json")
        assert dumped["setup"] == {
            "setup_type": "second_capability_for_tests",
            "image": "img",
            "workdir": None,
        }
        assert TaskDefinition.model_validate(dumped) == task

    def test_unknown_setup_keys_are_preserved(self) -> None:
        setup = BrowserSetup.model_validate({"setup_type": "browser", "future_key": 42})
        assert (setup.model_extra or {})["future_key"] == 42


class TestBrowserContext:
    def test_empty_when_nothing_to_say(self) -> None:
        assert BrowserSetup().to_context() == {}

    def test_surfaces_start_url_not_login_metadata(self) -> None:
        context = BrowserSetup(
            start_url="https://example.com",
            login_required=True,
            login_type="sso",
        ).to_context()
        assert context == {"start_url": "https://example.com"}


class TestTheStartPoint:
    """Where a task starts is its capability's to say (``docs/architecture_design.md`` §4.10)."""

    def test_the_browser_says_its_start_urls_site(self) -> None:
        assert BrowserSetup.start_point({"start_url": "https://Shop.Example.test/a?b=1"}) == (
            "shop.example.test"
        )
        assert BrowserSetup.start_point({"storage_state": "state.json"}) is None
        # A start page that is not a URL names no site (setup refuses it anyway).
        assert BrowserSetup.start_point({"start_url": "www.xiachufang.com或其他美食网站"}) is None

    def test_a_setup_that_states_none_has_none(self) -> None:
        from spectragent.core.capabilities.android.task_setup import AndroidSetup

        assert AndroidSetup.start_point({"oracle_task": {"task": "SimpleSmsSend"}}) is None

    def test_the_framework_asks_the_setup_of_the_environment_type(self) -> None:
        from spectragent.core.trace.sources import start_point

        context = {"start_url": "https://shop.example.test/"}
        assert start_point("browser", context) == "shop.example.test"
        assert start_point("android", context) is None
        assert start_point("no_such_environment", context) is None
