"""Shared trace reconstruction preserves recorded types and committed evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from spectragent.core.negotiation.observation import ObservationNegotiationResult
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import (
    first_difference,
    recorded_form,
    recorded_observation,
    recorded_step_info,
)
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import StepInfoObs
from tests._helpers.fragment_samples import PNG_BYTES
from tests._helpers.trace_builder import (
    as_chat,
    build_reference_trace,
    data_url,
    fragment,
    new_wal,
    prompt,
    step_record,
)
from tests.test_core.test_archive.test_archive_boundaries import imported_modules, module_name


@pytest.fixture
def recorded(tmp_path: Path) -> RunTrace:
    return RunTrace(build_reference_trace(tmp_path).run_dir)


@pytest.fixture
def negotiated() -> ObservationNegotiationResult:
    env = frozenset({"browser.screenshot", "browser.browser_meta"})
    loop = frozenset({"loop.task", "loop.step_info"})
    return ObservationNegotiationResult(
        active_fragments=env | loop,
        env_sourced=env,
        loop_sourced=loop,
        required_fragments=frozenset({"browser.screenshot"}),
    )


def test_recorded_observation_retains_optional_failures_and_absent_loop_fragments(
    recorded: RunTrace, negotiated: ObservationNegotiationResult
) -> None:
    observation, missing = recorded_observation(recorded, 0, negotiated)
    assert missing == []
    assert observation is not None
    assert observation.screenshot.image_bytes == PNG_BYTES
    assert isinstance(observation.browser_meta, FragmentFailure)
    assert observation.browser_meta.error == "TimeoutError"
    assert observation.task.user_request == "find the answer"
    assert observation.step_info is None


def test_missing_and_failed_required_fragments_are_named(
    recorded: RunTrace, negotiated: ObservationNegotiationResult
) -> None:
    required = replace(negotiated, required_fragments=negotiated.env_sourced)
    assert recorded_observation(recorded, 0, required) == (None, ["browser.browser_meta"])
    assert recorded_observation(recorded, 1, negotiated) == (None, ["browser.browser_meta"])
    assert recorded_observation(recorded, 99, negotiated) == (
        None,
        ["browser.browser_meta", "browser.screenshot"],
    )


def test_selected_environment_fragments_leave_loop_population_to_the_caller(
    recorded: RunTrace, negotiated: ObservationNegotiationResult
) -> None:
    observation, missing = recorded_observation(
        recorded, 0, negotiated, keys=negotiated.env_sourced
    )
    assert missing == []
    assert observation is not None
    assert observation.screenshot.image_bytes == PNG_BYTES
    assert observation.task is None
    assert observation.step_info is None


def test_step_info_is_read_only_from_committed_fragments(tmp_path: Path) -> None:
    wal = new_wal(tmp_path, "step-info")
    info = StepInfoObs(step=1, max_steps=7, today="2026-10-15")
    encoded = fragment(0, info)
    wal.commit_step(0, step_record(0), fragments=[encoded])
    trace = RunTrace(wal.run_dir)
    assert recorded_step_info(trace, 0) == info
    assert recorded_step_info(trace, 1) is None

    entry = trace.manifest.steps[0]
    path = wal.run_dir / entry.fragments["loop.step_info"].file
    path.write_text("broken")
    with pytest.raises(TraceSchemaError):
        recorded_step_info(trace, 0)


def test_message_context_uses_the_recorders_round_trip(recorded: RunTrace) -> None:
    live = prompt("what do you see?", images=[data_url(PNG_BYTES)])
    from_trace = as_chat(recorded.llm_messages(0)[0]).messages
    assert recorded_form(live) == recorded_form(from_trace)
    changed = recorded_form(prompt("changed"))
    assert first_difference(changed, recorded_form(from_trace)).startswith("message 1 differs")


def test_trace_readers_do_not_import_loop_implementations() -> None:
    import spectragent.core.trace

    root = Path(spectragent.core.trace.__file__).parent
    offenders = {}
    for path in root.rglob("*.py"):
        names = imported_modules(
            path.read_text(), module_name(path), is_package=path.name == "__init__.py"
        )
        forbidden = sorted(
            name
            for name in names
            if name == "spectragent.core.loops" or name.startswith("spectragent.core.loops.")
        )
        if forbidden:
            offenders[module_name(path)] = forbidden
    assert offenders == {}
