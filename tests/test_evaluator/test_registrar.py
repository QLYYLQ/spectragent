"""EvaluatorRegistrar shape — evaluation is a registry like every other axis."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
from spectragent.evaluator import (
    BaseEvaluator,
    BrowseCompEvaluator,
    EvaluatorBase,
    EvaluatorRegistrar,
    ExactMatchEvaluator,
    LexBenchBrowserEvaluator,
    LLMJudgeEvaluator,
    OnlineMind2WebEvaluator,
)
from spectragent.evaluator.prompts import EVALUATOR_PROMPT_ROOT

_BENCHMARK_EVALUATORS = {
    "browsecomp": BrowseCompEvaluator,
    "online_mind2web": OnlineMind2WebEvaluator,
    "lexbench_browser": LexBenchBrowserEvaluator,
}


def test_registrar_is_keyed_on_evaluator_id():
    assert EvaluatorRegistrar.discriminator_field == "evaluator_id"


def test_every_evaluator_is_registered():
    keys = set(EvaluatorRegistrar.keys())
    assert keys == {"exact_match", *_BENCHMARK_EVALUATORS}


@pytest.mark.parametrize("key", sorted(EvaluatorRegistrar.keys()))
def test_registered_class_satisfies_the_protocol(key):
    cls = EvaluatorRegistrar.get(key)
    assert issubclass(cls, EvaluatorBase)
    assert cls.__registry_key__ == key


def test_instances_are_structurally_evaluators():
    evaluator = ExactMatchEvaluator()
    assert isinstance(evaluator, BaseEvaluator)
    assert evaluator.evaluator_id == "exact_match"


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_EVALUATORS.items()))
def test_benchmark_evaluators_share_the_loaders_key(key, cls):
    """One benchmark name resolves tasks, references and a grader.

    This is what removes the alias table: nothing maps ``browsecomp`` to
    ``BrowseComp`` because nothing needs to.
    """
    assert cls.__registry_key__ == key
    assert key in set(TaskLoaderRegistrar.keys())
    assert key in set(EvalLoaderRegistrar.keys())
    assert key in set(EvaluatorRegistrar.keys())


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_EVALUATORS.items()))
def test_prompt_dir_is_derived_from_the_registry_key(key, cls):
    evaluator = cls()
    assert evaluator.prompt_dir == EVALUATOR_PROMPT_ROOT / key
    assert evaluator.prompt_dir.is_dir()


def test_llm_judges_declare_the_judge_as_wiring():
    """The judge model is a registry dependency, not a hardcoded client."""
    assert LLMJudgeEvaluator.__wiring__["llm"] == "llm"
    for cls in _BENCHMARK_EVALUATORS.values():
        assert issubclass(cls, LLMJudgeEvaluator)
        assert "llm" in cls.__wiring__


def test_deterministic_evaluator_requires_no_judge():
    """exact_match must not drag an LLM into a config that needs none."""
    assert not issubclass(ExactMatchEvaluator, LLMJudgeEvaluator)
    assert "llm" not in getattr(ExactMatchEvaluator, "__wiring__", {})


async def test_base_evaluate_is_not_silently_a_pass():
    class _Unimplemented(EvaluatorBase):
        # ``__abstract__`` keeps conscribe from registering a test class
        # into the process-wide registry, which the assertions above read.
        __abstract__ = True
        __registry_key__ = "test_unimplemented_evaluator"

    with pytest.raises(NotImplementedError, match="test_unimplemented_evaluator"):
        await _Unimplemented().evaluate(None, None)  # type: ignore[arg-type]

    assert "test_unimplemented_evaluator" not in set(EvaluatorRegistrar.keys())


def test_evaluator_is_a_generated_config_layer():
    """The registrar is wired into codegen, so YAML can select it."""
    from spectragent.generated._composed import ComposedConfig

    assert "evaluator" in ComposedConfig.model_fields
    from spectragent.core.registries import collect_registrars

    assert collect_registrars()["evaluator"] is EvaluatorRegistrar


@pytest.mark.parametrize("key,cls", sorted(_BENCHMARK_EVALUATORS.items()))
def test_every_judge_can_have_its_model_named_in_yaml(key, cls):
    """A judge that cannot be selected is a judge that grades itself.

    ``__wiring__`` declaring ``llm`` is not enough — that is inherited and
    passes even when the *generated config* has no ``llm`` field, which is
    precisely how ``browsecomp`` ended up binding the agent's own model
    silently: a judge with no knobs of its own has a parameter-less
    ``__init__``, and conscribe used to emit a discriminator-only model
    for it.

    So this asserts on the artifact that actually drives YAML validation.
    """
    import spectragent.generated._composed as composed

    config_cls = getattr(composed, f"{key.title().replace('_', '')}EvaluatorConfig")
    assert "llm" in config_cls.model_fields, (
        f"{config_cls.__name__} has no 'llm' field, so the judge model "
        f"cannot be chosen in YAML and binds to whatever LLM the run "
        f"happens to contain — the model under test grading itself."
    )
    assert issubclass(cls, LLMJudgeEvaluator)


def test_a_text_only_judge_advertises_no_vision_knob():
    """A judge that sends no images must not advertise a vision knob.

    The judge base declares no config field at all, so a text-only grader
    inherits only its judge receptor. ``image_detail`` is declared by the
    image-judging evaluators that read it, in their own modules.
    """
    from spectragent.generated._composed import BrowsecompEvaluatorConfig

    assert set(BrowsecompEvaluatorConfig.model_fields) == {"evaluator_id", "llm"}


def test_evaluator_set_config_identity_is_stable_across_processes():
    script = textwrap.dedent(
        """\
        import json
        from dataclasses import dataclass, field
        from spectragent.evaluator import EvaluatorBase

        @dataclass
        class SetConfiguredEvaluator(EvaluatorBase):
            __abstract__ = True
            __registry_key__ = "set_config_identity"
            labels: set[str] = field(default_factory=lambda: {"alpha", "beta", "gamma"})
            options: dict = field(default_factory=lambda: {
                "labels": frozenset({"delta", "epsilon", "zeta"}),
                "ordered": ["second", "first"],
            })

        evaluator = SetConfiguredEvaluator()
        print(json.dumps({"slug": evaluator.verdict_slug, "config": evaluator.config_signature()}))
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
