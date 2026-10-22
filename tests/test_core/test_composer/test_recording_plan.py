"""Recording scope and bounded persistence are real composed configuration."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.registries import collect_registrars


def test_selected_recording_scope_and_budgets_reach_the_hook():
    source = Path(__file__).resolve().parents[3] / "configs/ui_tars_local.yaml"
    raw = yaml.safe_load(source.read_text())
    entry = next(item for item in raw["hook"] if item["hook_id"] == "trace_writer")
    entry.update(
        recording={"metrics": True, "errors": True, "trajectory": False, "observations": False},
        pending_segments=3,
        write_retries=1,
        retry_delay=0.2,
        close_timeout=0.75,
        closeout_collection_timeout=1.5,
    )
    run = ConfigComposer(registrars=collect_registrars()).compose(raw)
    writer = next(hook for hook in run.instances("hook") if isinstance(hook, TraceWriter))
    assert writer.recording.metrics and writer.recording.errors
    assert not writer.recording.trajectory and not writer.recording.observations
    assert (
        writer.pending_segments,
        writer.write_retries,
        writer.retry_delay,
        writer.close_timeout,
        writer.closeout_collection_timeout,
    ) == (3, 1, 0.2, 0.75, 1.5)


@pytest.mark.parametrize(
    "value",
    [
        {"recording": {"metric": True}},
        {"pending_segments": 0},
        {"write_retries": -1},
        {"retry_delay": -0.1},
        {"close_timeout": 0},
        {"closeout_collection_timeout": 0},
    ],
)
def test_invalid_recording_configuration_is_rejected(value):
    from spectragent.generated._composed import TraceWriterHookConfig

    with pytest.raises(ValidationError):
        TraceWriterHookConfig.model_validate(value)
