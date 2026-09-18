"""What a run was *configured* as, read back off disk.

Identity ("a browser_use agent on react") says which components ran. It
cannot tell two runs apart, and telling two runs apart is the whole job:
the knobs that decided this round — observation mode, mouse trajectory,
screenshot encoding, which LLM route — lived nowhere in a trace, so every
cross-run comparison depended on an external harness keeping notes.

Three facts are under test here, all read through ``RunTrace`` the way an
Evaluator would:

* the **resolved** config is recorded, not the YAML source — defaults and
  wired receptors the source text never mentioned are in it;
* the **negotiation result** is recorded, fully qualified;
* ``config_hash`` hashes the *configuration*, so two tasks of one sweep
  share a hash and a changed knob breaks it.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import TraceConfig, TraceNegotiation
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"


class _Cap(CapabilityBase):
    __registry_key__ = "runcfg_cap"
    __wiring__ = {
        "action": ("action", ["runcfg_cap.done", "runcfg_cap.click"]),
        "observation": ("observation", ["browser.browser_meta"]),
    }

    def get_action_descriptors(self, **ctx):
        return [
            ActionDescriptor(name="runcfg_cap.done", description="Done"),
            ActionDescriptor(name="runcfg_cap.click", description="Click"),
        ]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="42")

    async def resolve_fragment(self, fragment_key, **kwargs):
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com", page_title="Example")
        return None


class _Env(EnvironmentBase):
    __registry_key__ = "runcfg_env"


class _Agent:
    __wiring__ = {
        # ``click`` is declared optional and never emitted — it must still
        # show up as active, because "what the model was allowed to do" is
        # a different fact from "what it did".
        "action": ("action", ["done"], ["click"]),
        "observation": ("observation", ["browser.browser_meta", "loop.task"]),
    }

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        return [Action(name="done", params={})]

    def get_react_output_model(self):
        return None


class _EmptyObsResult:
    """A negotiation that ran and bound nothing — not one that never ran."""

    active_fragments: frozenset[str] = frozenset()
    required_fragments: frozenset[str] = frozenset()
    env_sourced: frozenset[str] = frozenset()
    loop_sourced: frozenset[str] = frozenset()
    fragment_bindings: dict[str, str] = {}


class _Completion:
    completion = '{"action": "done"}'
    usage = None


class _SlowLLM:
    model = "gpt-4o-mini"

    async def ainvoke(self, messages, **kw):
        import asyncio

        await asyncio.sleep(0.02)
        return _Completion()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self):
        self.messages: list[Any] = []

    def add_message(self, m):
        self.messages.append(m)

    def add_messages(self, ms):
        self.messages.extend(ms)

    def get_context_messages(self):
        return list(self.messages)

    def clear(self):
        self.messages.clear()


async def _run(
    tmp_path, composer, *, mutate=None, task_id="runcfg--0001", record_secrets=False
) -> Path:
    raw = copy.deepcopy(
        yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8"))
    )
    if mutate is not None:
        mutate(raw)
    resolved = composer.top_level_type.model_validate(raw)

    writer = TraceWriter(
        trace_dir=str(tmp_path / task_id), record_secrets=record_secrets
    )
    run = make_run(
        agent=_Agent(),
        loop=ReactLoop(max_steps=2),
        env=make_env(_Env, {"runcfg_cap": _Cap()}),
        llm=_SlowLLM(),
        memory=_Memory(),
        hooks=[writer],
    )
    run = dataclasses.replace(run, top_level_cfg=resolved)
    await AgentRuntime.for_agent(run, AGENT_ID).execute(
        "find the price",
        task_id=task_id,
        benchmark="runcfg",
        subset="all",
        timeout=10,
    )
    assert writer.run_dir is not None
    return writer.run_dir


@pytest.fixture()
async def trace(tmp_path, composer) -> RunTrace:
    return RunTrace(await _run(tmp_path, composer))


class TestResolvedConfigIsRecorded:
    def test_the_decisive_knobs_are_there(self, trace):
        agent = trace.config.resolved["agent"][0]
        cap = trace.config.resolved["environment"][0]["capabilities"][0]
        assert agent["agent_id"] == "browser_use"
        assert agent["observation_mode"] == "dom"
        assert cap["screenshot_format"] == "png"
        assert cap["mouse_trajectory"]["trajectory_type"] == "instant"
        assert cap["typing_rhythm"]["rhythm_type"] == "instant"
        assert cap["dom_cleaner"]["cleaner_type"] == "browser_use"

    def test_it_is_the_resolved_object_not_the_yaml_source(self, trace):
        """Defaults and wired receptors the source never mentions.

        ``configs/browser_use_local.yaml`` sets neither of these; they
        exist only after validation, which is exactly why the source text
        is not what a trace should record.
        """
        agent = trace.config.resolved["agent"][0]
        source = yaml.safe_load(
            (CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8")
        )
        assert "coordinate_space" not in source["agent"][0]
        assert agent["coordinate_space"] == "screenshot_pixels"
        assert agent["llm"]["provider"]["name"] == "direct"

    def test_which_llm_route_was_used(self, trace):
        """The fact today's probes showed decides whether structured
        output works at all: the protocol and the route, not the model
        string."""
        llm = trace.config.resolved["agent"][0]["llm"]
        assert llm["model_type"] == "openai"
        assert llm["provider"]["name"] == "direct"


class TestNegotiationIsRecorded:
    def test_active_and_required_fragments_fully_qualified(self, trace):
        neg = trace.config.negotiation
        assert neg is not None
        assert neg.active_fragments == ["browser.browser_meta", "loop.task"]
        assert "browser.browser_meta" in neg.required_fragments
        assert neg.env_sourced_fragments == ["browser.browser_meta"]
        assert neg.loop_sourced_fragments == ["loop.task"]

    def test_bindings_say_what_a_relative_name_meant_here(self, trace):
        neg = trace.config.negotiation
        assert neg.fragment_bindings["browser_meta"] == "browser.browser_meta"
        assert neg.action_bindings["done"] == "runcfg_cap.done"

    def test_actions_the_model_was_allowed_to_emit(self, trace):
        """Not the actions it *did* emit — the step records carry those."""
        neg = trace.config.negotiation
        assert neg.active_actions == ["runcfg_cap.click", "runcfg_cap.done"]

    def test_sorted_so_two_processes_agree(self, trace):
        neg = trace.config.negotiation
        assert neg.active_fragments == sorted(neg.active_fragments)
        assert neg.active_actions == sorted(neg.active_actions)

    def test_never_negotiated_is_null_not_empty(self):
        """``null`` and ``{...empty lists...}`` are different claims.

        "This run never negotiated" and "negotiation ran and bound
        nothing" — the second is a real, diagnosable state, an
        environment whose capabilities provide none of what the agent
        asked for. The same distinction redaction draws between ``null``
        and ``"<omitted>"``, and it has to survive the round trip to
        disk, which is where an evaluator reads it.
        """
        assert TraceNegotiation.from_results(None, None) is None

        empty = TraceNegotiation.from_results(
            observation=_EmptyObsResult(), action=None
        )
        assert empty is not None
        assert empty.active_fragments == []

        on_disk = TraceConfig.model_validate_json(
            TraceConfig(negotiation=None).model_dump_json()
        )
        assert on_disk.negotiation is None


class TestConfigHash:
    async def test_two_tasks_one_configuration_share_a_hash(
        self, tmp_path, composer
    ):
        """The property that was false: hashing the whole snapshot folded
        in ``task_id``, so every task of a sweep got its own hash."""
        a = RunTrace(await _run(tmp_path, composer, task_id="t-a"))
        b = RunTrace(await _run(tmp_path, composer, task_id="t-b"))
        assert a.config.task_id != b.config.task_id
        assert a.manifest.config_hash == b.manifest.config_hash

    async def test_a_changed_closed_set_knob_breaks_the_hash(self, tmp_path, composer):
        def flip(raw):
            raw["agent"][0]["observation_mode"] = "vision"

        a = RunTrace(await _run(tmp_path, composer, task_id="t-c"))
        b = RunTrace(await _run(tmp_path, composer, mutate=flip, task_id="t-d"))
        assert a.manifest.config_hash != b.manifest.config_hash

    @pytest.mark.parametrize(
        ("label", "mutate"),
        [
            ("max_steps", lambda raw: raw["agent"][0]["agent_loop"].update(max_steps=7)),
            ("temperature", lambda raw: raw["agent"][0]["llm"].update(temperature=0.7)),
            ("model", lambda raw: raw["agent"][0]["llm"].update(model="gpt-4o-mini")),
        ],
    )
    async def test_a_changed_unmarked_knob_breaks_the_hash(
        self, tmp_path, composer, label, mutate
    ):
        """The sweep case, and the one that was broken.

        ``max_steps`` / ``temperature`` / ``model`` are open-typed and
        unmarked, so the *recorded* config shows ``"<omitted>"`` for each
        — identical bytes in both runs. A digest taken over that view
        merges the arms of every sweep a researcher actually runs, and it
        does so silently. The digest is therefore taken over the
        unredacted config, before redaction.
        """
        a = RunTrace(await _run(tmp_path, composer, task_id=f"t-{label}-a"))
        b = RunTrace(
            await _run(tmp_path, composer, mutate=mutate, task_id=f"t-{label}-b")
        )
        # The recorded views really are indistinguishable...
        assert a.config.resolved == b.config.resolved
        # ...and the hash still tells the two runs apart.
        assert a.manifest.config_hash != b.manifest.config_hash

    async def test_record_secrets_does_not_move_the_hash(self, tmp_path, composer):
        """It changes what lands on disk, not what the run was set up as.

        So a local debugging run and a shared run of the same YAML stay
        comparable — which is only true because the digest is taken from
        the config, not from the recorded view.
        """
        a = RunTrace(await _run(tmp_path, composer, task_id="t-plain"))
        b = RunTrace(
            await _run(tmp_path, composer, task_id="t-secret", record_secrets=True)
        )
        assert a.config.secrets_recorded is False
        assert b.config.secrets_recorded is True
        assert a.config.resolved != b.config.resolved
        assert a.manifest.config_hash == b.manifest.config_hash

    def test_a_reader_cannot_recompute_the_writers_hash(self, trace):
        """And must not silently think it did.

        The trace does not contain the values the digest covers, so a
        reader-side digest is a different, lower-fidelity thing. The
        payload records which fidelity it used precisely so the two can
        never compare equal by accident.
        """
        assert trace.config.configuration_digest() != trace.manifest.config_hash

    def test_a_withheld_field_cannot_change_the_recorded_view(self):
        """The reader-fidelity digest, on its own terms: two snapshots
        whose api keys differ but were both withheld are the same
        snapshot, because that is all the snapshot says."""
        base = TraceConfig(agent_id="a", resolved={"llm": {"api_key": "<omitted>"}})
        same = TraceConfig(agent_id="a", resolved={"llm": {"api_key": "<omitted>"}})
        assert base.configuration_digest() == same.configuration_digest()

    def test_negotiation_is_part_of_the_configuration(self):
        base = TraceConfig(agent_id="a")
        with_neg = TraceConfig(
            agent_id="a",
            negotiation=TraceNegotiation(active_actions=["browser.click"]),
        )
        assert base.configuration_digest() != with_neg.configuration_digest()


class TestStepTimings:
    """B7 — filled from AOP ``around`` advice, never by the Loop."""

    def test_duration_and_llm_latency_are_measured(self, trace):
        rec = trace.step(0)
        assert rec is not None
        assert rec.duration_ms is not None
        assert rec.llm_latency_ms is not None
        # The fake LLM sleeps 20ms; the step contains that call.
        assert rec.llm_latency_ms >= 15
        assert rec.duration_ms >= rec.llm_latency_ms

    def test_unmeasured_is_none_not_zero(self):
        """The same honest sentinel ``token_usage`` uses: a zero here
        would claim a step that took no time."""
        from spectragent.schemas.step_record import StepRecord

        assert StepRecord(step=0).duration_ms is None
        assert StepRecord(step=0).llm_latency_ms is None

    def test_the_stamp_does_not_reach_the_prompt(self):
        """The seam this measurement buys, pinned.

        ``_stamp_timings`` mutates the Loop's own ``StepRecord``, and that
        object is also the payload of ``HistoryObs`` — it is rendered into
        prompts. If a renderer dumped the record wholesale, turning
        ``trace_writer`` on would change what the model reads, and a
        persistence hook would have become part of the experiment.

        Pinned against the real renderer rather than asserted in prose,
        because the failure is silent: nothing errors, the prompt just
        quietly differs between a traced run and an untraced one.
        """
        from spectragent.agents.browser_use import BrowserUseAgent
        from spectragent.schemas.observation.fragments import HistoryObs
        from spectragent.schemas.step_record import StepRecord

        agent = BrowserUseAgent()
        plain = StepRecord(
            step=0,
            llm_output="thinking",
            actions=[Action(name="browser.click", params={"index": 1})],
            action_results=[ActionResult(success=True, extracted_content="ok")],
        )
        stamped = plain.model_copy(deep=True)
        stamped.duration_ms = 1234
        stamped.llm_latency_ms = 999

        rendered_plain = agent._render_history(HistoryObs(items=[plain]))
        rendered_stamped = agent._render_history(HistoryObs(items=[stamped]))
        assert rendered_plain == rendered_stamped
        assert "1234" not in rendered_stamped
        assert "999" not in rendered_stamped
