"""VerifierRegistrar shape — judging is a registry like every other axis."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from spectragent.benchmarks.browsecomp import BrowseCompVerifier
from spectragent.benchmarks.lexbench_browser import LexBenchBrowserVerifier
from spectragent.benchmarks.online_mind2web import OnlineMind2WebVerifier
from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
from spectragent.verifiers import (
    LLMJudgeVerifier,
    OfflineVerifier,
    Verifier,
    VerifierBase,
    VerifierRegistrar,
    attachment_of,
)
from spectragent.verifiers.exact_match import ExactMatchVerifier
from spectragent.verifiers.prompts import VERIFIER_PROMPT_ROOT

#: Each benchmark's own judge, in that benchmark's package.
_BENCHMARK_VERIFIERS = {
    "browsecomp": BrowseCompVerifier,
    "online_mind2web": OnlineMind2WebVerifier,
    "lexbench_browser": LexBenchBrowserVerifier,
}


def test_registrar_is_keyed_on_verifier_id_and_written_only_inline():
    assert VerifierRegistrar.discriminator_field == "verifier_id"
    assert VerifierRegistrar.inline_only


def test_every_verifier_is_registered():
    keys = set(VerifierRegistrar.keys())
    assert keys == {"exact_match", *_BENCHMARK_VERIFIERS}


@pytest.mark.parametrize("key", sorted(VerifierRegistrar.keys()))
def test_registered_class_satisfies_the_protocol(key):
    cls = VerifierRegistrar.get(key)
    assert issubclass(cls, VerifierBase)
    assert cls.__registry_key__ == key


def test_instances_are_structurally_offline_verifiers():
    verifier = ExactMatchVerifier()
    assert isinstance(verifier, Verifier)
    assert isinstance(verifier, OfflineVerifier)
    assert verifier.verifier_id == "exact_match"
    assert attachment_of(verifier) == "offline"


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_VERIFIERS.items()))
def test_a_benchmarks_own_verifier_shares_its_loaders_key(key, cls):
    """One benchmark name resolves tasks, references and the judge, with no alias table."""
    assert cls.__registry_key__ == key
    assert cls.__module__ == f"spectragent.benchmarks.{key}.verifier"
    assert key in set(TaskLoaderRegistrar.keys())
    assert key in set(EvalLoaderRegistrar.keys())
    assert key in set(VerifierRegistrar.keys())


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_VERIFIERS.items()))
def test_prompt_dir_is_derived_from_the_registry_key(key, cls):
    verifier = cls()
    assert verifier.prompt_dir == VERIFIER_PROMPT_ROOT / key
    assert verifier.prompt_dir.is_dir()


def test_llm_judges_declare_the_judge_as_wiring_and_need_a_model():
    """The judge model is a registry dependency, not a hardcoded client."""
    assert LLMJudgeVerifier.__wiring__["llm"] == "llm"
    for cls in _BENCHMARK_VERIFIERS.values():
        assert issubclass(cls, LLMJudgeVerifier)
        assert "llm" in cls.__wiring__
        assert "model" in cls.__needs__
        assert "live_environment" not in cls.__needs__


def test_webjudge_needs_no_reference_row():
    """It judges the recorded task text, so a task-variant branch is still judged."""
    assert "reference" not in OnlineMind2WebVerifier.__needs__
    assert "reference" in BrowseCompVerifier.__needs__
    assert "reference" in LexBenchBrowserVerifier.__needs__


def test_deterministic_verifier_requires_no_judge():
    """exact_match must not drag an LLM into a config that needs none."""
    assert not issubclass(ExactMatchVerifier, LLMJudgeVerifier)
    assert "llm" not in getattr(ExactMatchVerifier, "__wiring__", {})


async def test_base_verify_is_not_silently_a_pass():
    class _Unimplemented(VerifierBase):
        # ``__abstract__`` keeps conscribe from registering a test class
        # into the process-wide registry, which the assertions above read.
        __abstract__ = True
        __registry_key__ = "test_unimplemented_verifier"

    with pytest.raises(NotImplementedError, match="test_unimplemented_verifier"):
        await _Unimplemented().verify(None, None)  # type: ignore[arg-type]

    assert "test_unimplemented_verifier" not in set(VerifierRegistrar.keys())


def test_verifier_is_a_collected_layer_written_in_benchmark_blocks():
    """The registrar is collected, so a benchmark block can list verifiers; no top-level section."""
    from pydantic import ValidationError

    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars
    from spectragent.generated._composed import ComposedConfig

    assert collect_registrars()["verifier"] is VerifierRegistrar
    assert "evaluator" not in ComposedConfig.model_fields
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_every_package().validate({"verifier": [{"verifier_id": "exact_match"}]})
    [error] = refused.value.errors()
    assert error["type"] == "inline_only_layer"
    assert "benchmark[browsecomp].verifiers" in error["msg"]


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_VERIFIERS.items()))
def test_every_judge_can_have_its_model_named_in_yaml(key, cls):
    """A judge that cannot be selected is a judge that grades itself.

    ``__wiring__`` declaring ``llm`` is not enough — that is inherited and
    passes even when the *generated config* has no ``llm`` field. So this
    asserts on the artifact that actually drives YAML validation.
    """
    import spectragent.generated._composed as composed

    config_cls = getattr(composed, f"{key.title().replace('_', '')}VerifierConfig")
    assert "llm" in config_cls.model_fields, (
        f"{config_cls.__name__} has no 'llm' field, so the judge model cannot be chosen in YAML."
    )
    assert issubclass(cls, LLMJudgeVerifier)


def test_a_judges_sampling_settings_are_part_of_its_identity():
    """Two judges on one model that sample differently judge differently: two slugs, no secrets."""
    from spectragent.core.llm.providers.openai import ChatOpenAI

    def judge(**settings) -> BrowseCompVerifier:
        verifier = BrowseCompVerifier()
        verifier.llm = ChatOpenAI(  # type: ignore[assignment]
            model="gpt-4.1",
            api_key="sk-secret",
            base_url="https://user:pw@gateway.example/v1",
            **settings,
        )
        return verifier

    cold, hot = judge(temperature=0.0), judge(temperature=1.0)
    signature = cold.identity().config_signature

    assert cold.identity().slug != hot.identity().slug
    assert judge(temperature=0.0).identity().slug == cold.identity().slug
    assert signature["judge_settings"]["temperature"] == 0.0  # type: ignore[index]
    assert judge(seed=7).identity().slug != judge(seed=8).identity().slug
    assert judge(reasoning_effort="high").identity().slug != judge().identity().slug
    assert judge(max_completion_tokens=64).identity().slug != judge().identity().slug
    assert signature["judge_route"]["provider"] == "direct"  # type: ignore[index]
    written = json.dumps(signature)
    assert "sk-secret" not in written and "user:pw" not in written


def test_a_text_only_judge_advertises_no_vision_knob():
    """A judge that sends no images must not advertise a vision knob."""
    from spectragent.generated._composed import BrowsecompVerifierConfig

    assert set(BrowsecompVerifierConfig.model_fields) == {"verifier_id", "llm"}


def test_verifier_set_config_identity_is_stable_across_processes():
    script = textwrap.dedent(
        """\
        import json
        from dataclasses import dataclass, field
        from spectragent.verifiers import VerifierBase

        @dataclass
        class SetConfiguredVerifier(VerifierBase):
            __abstract__ = True
            __registry_key__ = "set_config_identity"
            labels: set[str] = field(default_factory=lambda: {"alpha", "beta", "gamma"})
            options: dict = field(default_factory=lambda: {
                "labels": frozenset({"delta", "epsilon", "zeta"}),
                "ordered": ["second", "first"],
            })

        identity = SetConfiguredVerifier().identity()
        print(json.dumps({"slug": identity.slug, "config": identity.config_signature}))
        """
    )
    identities = [
        json.loads(
            subprocess.check_output(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
            )
        )
        for seed in ("1", "2", "42")
    ]
    assert identities[0] == identities[1] == identities[2]
    assert identities[0]["config"]["labels"] == ["alpha", "beta", "gamma"]
    assert identities[0]["config"]["options"] == {
        "labels": ["delta", "epsilon", "zeta"],
        "ordered": ["second", "first"],
    }
