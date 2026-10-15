"""``TaskSetup`` is a real discriminated union, not a one-member stub.

Each variant owns its ``to_context()``, so nothing downstream needs an
``isinstance(..., BrowserSetup)`` branch.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spectragent.schemas.task import (
    TASK_SETUP_VARIANTS,
    BrowserSetup,
    DockerSetup,
    SetupBase,
    TaskDefinition,
    VMSetup,
    setup_class_for_env,
)


class TestUnionMembership:
    def test_all_variants_registered(self) -> None:
        assert set(TASK_SETUP_VARIANTS) == {BrowserSetup, DockerSetup, VMSetup}

    def test_every_variant_subclasses_the_base(self) -> None:
        assert all(issubclass(v, SetupBase) for v in TASK_SETUP_VARIANTS)

    def test_setup_type_values_are_unique(self) -> None:
        values = [v.setup_type_value() for v in TASK_SETUP_VARIANTS]
        assert sorted(values) == ["browser", "docker", "vm"]

    def test_env_type_claims_do_not_overlap(self) -> None:
        claims = [e for v in TASK_SETUP_VARIANTS for e in v.__env_types__]
        assert len(claims) == len(set(claims))

    @pytest.mark.parametrize("variant", TASK_SETUP_VARIANTS)
    def test_every_variant_implements_to_context(self, variant: type[SetupBase]) -> None:
        assert isinstance(variant().to_context(), dict)


class TestDiscriminatorRouting:
    @pytest.mark.parametrize(
        ("setup_type", "expected"),
        [
            ("browser", BrowserSetup),
            ("docker", DockerSetup),
            ("vm", VMSetup),
        ],
    )
    def test_task_definition_routes_by_setup_type(
        self, setup_type: str, expected: type[SetupBase]
    ) -> None:
        task = TaskDefinition.model_validate(
            {"task_id": "t1", "benchmark": "b", "setup": {"setup_type": setup_type}}
        )
        assert isinstance(task.setup, expected)

    def test_unknown_setup_type_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskDefinition.model_validate(
                {
                    "task_id": "t1",
                    "benchmark": "b",
                    "setup": {"setup_type": "unknown_env"},
                }
            )


class TestSetupClassForEnv:
    @pytest.mark.parametrize(
        ("env_type", "expected"),
        [
            ("browser", BrowserSetup),
            ("docker", DockerSetup),
            ("docker_terminal", DockerSetup),
            ("container", DockerSetup),
            ("vm", VMSetup),
            ("desktop", VMSetup),
            ("desktop_vm", VMSetup),
        ],
    )
    def test_known_env_types(self, env_type: str, expected: type[SetupBase]) -> None:
        assert setup_class_for_env(env_type) is expected

    def test_unknown_env_type_raises_with_the_known_list(self) -> None:
        with pytest.raises(ValueError) as exc:
            setup_class_for_env("mobile")
        message = str(exc.value)
        assert "mobile" in message
        assert "browser" in message and "docker" in message and "desktop" in message


class TestToContext:
    def test_browser_empty_when_nothing_to_say(self) -> None:
        assert BrowserSetup().to_context() == {}

    def test_browser_surfaces_start_url_and_login(self) -> None:
        ctx = BrowserSetup(
            start_url="https://example.com",
            login_required=True,
            login_type="sso",
        ).to_context()
        assert ctx == {
            "start_url": "https://example.com",
        }

    def test_docker_surfaces_image_commit_and_test_ids(self) -> None:
        ctx = DockerSetup(
            image="swebench/sweb.eval.x86_64.django_1776_django-11333",
            repo="django/django",
            base_commit="55b68de",
            fail_to_pass=["test_a"],
            pass_to_pass=["test_b"],
        ).to_context()
        assert ctx["image"].startswith("swebench/")
        assert ctx["repo"] == "django/django"
        assert ctx["base_commit"] == "55b68de"
        assert ctx["fail_to_pass"] == ["test_a"]
        assert ctx["pass_to_pass"] == ["test_b"]

    def test_vm_surfaces_snapshot_and_config(self) -> None:
        ctx = VMSetup(
            snapshot="chrome",
            os_type="Ubuntu",
            config=[{"type": "launch"}],
            related_apps=["chrome"],
        ).to_context()
        assert ctx == {
            "snapshot": "chrome",
            "os_type": "Ubuntu",
            "config": [{"type": "launch"}],
            "related_apps": ["chrome"],
        }

    def test_context_omits_unset_fields(self) -> None:
        assert DockerSetup(repo="a/b").to_context() == {"repo": "a/b"}
        assert VMSetup().to_context() == {}


class TestDockerTestListDecoding:
    def test_accepts_json_encoded_strings(self) -> None:
        setup = DockerSetup.model_validate({"FAIL_TO_PASS": '["a", "b"]', "PASS_TO_PASS": '["c"]'})
        assert setup.fail_to_pass == ["a", "b"]
        assert setup.pass_to_pass == ["c"]

    def test_accepts_snake_case_names(self) -> None:
        setup = DockerSetup.model_validate({"fail_to_pass": ["a"]})
        assert setup.fail_to_pass == ["a"]


class TestSetupPreservesUnknownKeys:
    def test_extra_setup_keys_are_not_dropped(self) -> None:
        setup = BrowserSetup.model_validate({"setup_type": "browser", "future_key": 42})
        assert (setup.model_extra or {})["future_key"] == 42
