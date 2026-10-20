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
* the **negotiation result** is recorded, fully qualified — the archive
  binding included;
* ``config_hash`` hashes the *configuration*, so two tasks of one sweep
  share a hash and a changed knob breaks it;
* the environment-evidence provenance (``env_evidence_privacy``) and the
  task's setup context (``task_setup``, redacted by value) are recorded beside
  it — each only when it has a value — and move the hash not at all.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from spectragent.core.archive.privacy import (
    MARKER_REDACTED,
    PRIVACY_POLICY,
    REDACTED,
    RedactionEntry,
    RedactionLedger,
)
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.negotiation import ArchiveNegotiationResult
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import TaskSetupRecord, TraceConfig, TraceNegotiation
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.restoration import BranchProvenance
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    STEPS_KEY,
    ArchiveConsumerHook,
    FakeBlobArchiver,
    FakeStepArchiver,
    make_archive_run,
)
from tests._helpers.assembly import AGENT_ID, make_env, make_run

pytestmark = pytest.mark.usefixtures("archive_fakes")

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"


class _Cap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
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

    async def setup(self, *, negotiation=None, start_url=None):
        # Declared so a task context can carry a start URL here.
        await super().setup(negotiation=negotiation)

    async def resolve_fragment(self, fragment_key, **kwargs):
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com", page_title="Example")
        return None


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "runcfg_env"


class _Agent:
    __execution_limit_fields__ = frozenset()
    __wiring__ = {
        # ``click`` is declared optional and never emitted — it must still
        # show up as active, because "what the model was allowed to do" is
        # a different fact from "what it did".
        "action": ("action", ["done"], ["click"]),
        "observation": ("observation", ["browser.browser_meta", "loop.task"]),
    }

    async def build_react_prompt(self, obs, *, context):
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
    inference_kind = "chat"
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
    tmp_path,
    composer,
    *,
    mutate=None,
    task_id="runcfg--0001",
    record_secrets=False,
    branch=None,
    env_context=None,
    config_source=None,
) -> Path:
    raw = copy.deepcopy(yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8")))
    if mutate is not None:
        mutate(raw)
    resolved = composer.top_level_type.model_validate(raw)

    writer = TraceWriter(trace_dir=str(tmp_path / task_id), record_secrets=record_secrets)
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
        branch=branch,
        env_context=env_context,
        config_source=config_source,
    )
    assert writer.run_dir is not None
    return writer.run_dir


@pytest.fixture()
async def trace(tmp_path, composer) -> RunTrace:
    return RunTrace(await _run(tmp_path, composer))


class TestConfigurationSource:
    async def test_source_is_recorded_without_changing_effective_configuration_hash(
        self, tmp_path, composer
    ):
        original = ConfigSource.from_path(tmp_path / "a.yaml", data=b"model: first\n")
        changed = ConfigSource.from_path(tmp_path / "b.yaml", data=b"model: second\n")
        first = RunTrace(await _run(tmp_path, composer, task_id="one", config_source=original))
        second = RunTrace(await _run(tmp_path, composer, task_id="two", config_source=changed))
        unknown = RunTrace(await _run(tmp_path, composer, task_id="unknown"))
        assert first.config.config_source == original
        assert second.config.config_source == changed
        assert (
            first.manifest.config_hash
            == second.manifest.config_hash
            == unknown.manifest.config_hash
        )
        assert unknown.config.config_source is None
        assert "config_source" not in json.loads((unknown.trace_dir / "config.json").read_text())

    async def test_branch_keeps_its_launch_config_source_separate_from_parent_run(
        self, tmp_path, composer
    ):
        source = ConfigSource.from_path(tmp_path / "branch.yaml", data=b"agent_loop: branch\n")
        branch = TestBranchProvenance._provenance(0)
        trace = RunTrace(await _run(tmp_path, composer, branch=branch, config_source=source))
        assert trace.config.config_source == source
        assert trace.config.branch == branch
        assert trace.config.config_source is not None
        assert trace.config.config_source.path != branch.source_trace


class TestResolvedConfigIsRecorded:
    def test_the_decisive_knobs_are_there(self, trace):
        agent = trace.config.resolved["agent"][0]
        cap = trace.config.resolved["environment"][0]["capabilities"][0]
        assert agent["agent_id"] == "browser_use"
        assert agent["observation_mode"] == "dom"
        assert cap["screenshot_format"] == "png"
        assert cap["mouse_trajectory"]["trajectory_type"] == "instant"
        assert cap["typing_rhythm"]["rhythm_type"] == "instant"
        # How far the run was pinned for replay is part of what was run.
        assert cap["replayability"]["replayability_id"] == "none"
        # And where its network came from.
        assert cap["network_source"]["source_id"] == "live"
        assert cap["dom_cleaner"]["cleaner_type"] == "browser_use"

    def test_it_is_the_resolved_object_not_the_yaml_source(self, trace):
        """Defaults and wired receptors the source never mentions.

        ``configs/browser_use_local.yaml`` sets neither of these; they
        exist only after validation, which is exactly why the source text
        is not what a trace should record.
        """
        agent = trace.config.resolved["agent"][0]
        source = yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8"))
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

        empty = TraceNegotiation.from_results(observation=_EmptyObsResult(), action=None)
        assert empty is not None
        assert empty.active_fragments == []

        on_disk = TraceConfig.model_validate_json(TraceConfig(negotiation=None).model_dump_json())
        assert on_disk.negotiation is None


class TestConfigHash:
    async def test_two_tasks_one_configuration_share_a_hash(self, tmp_path, composer):
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
        ("label", "mutate", "read"),
        [
            (
                "max_steps",
                lambda raw: raw["agent"][0]["agent_loop"].update(max_steps=7),
                lambda resolved: resolved["agent"][0]["agent_loop"]["max_steps"],
            ),
            (
                "temperature",
                lambda raw: raw["agent"][0]["llm"].update(temperature=0.7),
                lambda resolved: resolved["agent"][0]["llm"]["temperature"],
            ),
        ],
    )
    async def test_a_changed_number_is_recorded_and_breaks_the_hash(
        self, tmp_path, composer, label, mutate, read
    ):
        """Numbers are recorded raw: a reader sees what a sweep varied."""
        a = RunTrace(await _run(tmp_path, composer, task_id=f"t-{label}-a"))
        b = RunTrace(await _run(tmp_path, composer, mutate=mutate, task_id=f"t-{label}-b"))
        assert read(a.config.resolved) != read(b.config.resolved)
        assert read(b.config.resolved) in (7, 0.7)
        assert a.manifest.config_hash != b.manifest.config_hash

    async def test_a_changed_unmarked_string_breaks_the_hash(self, tmp_path, composer):
        """The sweep case for what stays withheld.

        ``model`` is a free-form string nobody marked, so the *recorded*
        config shows ``"<omitted>"`` — identical bytes in both runs. A
        digest taken over that view would merge the arms of the sweep, and
        silently. The digest is therefore taken over the unredacted config,
        before redaction.
        """

        def mutate(raw):
            raw["agent"][0]["llm"].update(model="gpt-4o-mini")

        a = RunTrace(await _run(tmp_path, composer, task_id="t-model-a"))
        b = RunTrace(await _run(tmp_path, composer, mutate=mutate, task_id="t-model-b"))
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
        b = RunTrace(await _run(tmp_path, composer, task_id="t-secret", record_secrets=True))
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
    """Step timings are filled from AOP ``around`` advice, never by the Loop."""

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

    async def test_measurements_do_not_change_the_public_prompt(self):
        """Recording enriches shared history without changing model input."""
        from spectragent.agents.browser_use import BrowserUseAgent
        from spectragent.schemas.observation.fragments import HistoryObs, TaskObs
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

        context = ReactPromptContext(step=1, phase="policy", policy_step=1)
        rendered_plain = await agent.build_react_prompt(
            SimpleNamespace(
                task=TaskObs(user_request="continue"),
                history=HistoryObs(items=[plain]),
                platform=SimpleNamespace(operating_system="linux", fragment_id="browser.platform"),
            ),
            context=context,
        )
        rendered_stamped = await agent.build_react_prompt(
            SimpleNamespace(
                task=TaskObs(user_request="continue"),
                history=HistoryObs(items=[stamped]),
                platform=SimpleNamespace(operating_system="linux", fragment_id="browser.platform"),
            ),
            context=context,
        )
        assert rendered_plain == rendered_stamped
        text = "\n".join(message.text for message in rendered_stamped)
        assert "1234" not in text
        assert "999" not in text

    @pytest.mark.parametrize("agent_id", ["browser_use", "skyvern", "ui_tars"])
    async def test_origin_and_restoration_do_not_reach_the_prompt(self, agent_id):
        """A restored step is a copy of the source's step, and must read the same.

        ``origin`` and ``restoration`` ride on the same ``StepRecord`` that
        ``HistoryObs`` carries into the prompt. A branch run's policy step
        sees restored steps in ``loop.history``; if a renderer showed either
        field, the branch's context would differ from the source run's at
        the very step the branch exists to re-decide. Checked for every
        agent that can run a branch: through its history renderer, or — for
        an agent that never negotiates ``loop.history`` — by its declaration.
        """
        from spectragent.agents._registrar import AgentRegistrar
        from spectragent.schemas.observation.fragments import HistoryObs, TaskObs
        from spectragent.schemas.restoration import (
            RestorationCheck,
            RestorationEvidence,
            RestoredAction,
        )
        from spectragent.schemas.step_record import StepRecord

        agent_cls = AgentRegistrar.get(agent_id)
        entry = agent_cls.__wiring__["observation"]
        required, optional = entry["keys"], entry["optional_keys"]
        demands = {key.rsplit(".", 1)[-1] for key in (*required, *optional)}
        if "history" not in demands:
            # Never negotiated, so ``HistoryObs`` never reaches this agent.
            return

        plain = StepRecord(
            step=0,
            llm_output="thinking",
            actions=[Action(name="browser.click", params={"index": 1})],
            action_results=[ActionResult(success=True, extracted_content="ok")],
        )
        restored = plain.model_copy(deep=True)
        restored.origin = "restored"
        restored.restoration = RestorationCheck(
            step=0,
            verdict="warn",
            reason="screenshot_distance",
            reasons=["screenshot_distance"],
            evidence=[
                RestorationEvidence(
                    capability_id="browser",
                    url_match=True,
                    recorded_url="https://restored-marker.example/",
                    screenshot_distance=4321,
                )
            ],
            actions=[
                RestoredAction(
                    recorded=Action(name="browser.click", params={"index": 1}),
                    executed=Action(name="browser.click", params={"index": 8765}),
                    method="geometry_signature",
                    detail="marker-detail",
                    recorded_success=True,
                    live_success=True,
                )
            ],
        )

        agent = agent_cls()
        context = ReactPromptContext(step=1, phase="policy", policy_step=1)
        rendered_plain = await agent.build_react_prompt(
            SimpleNamespace(
                task=TaskObs(user_request="continue"),
                history=HistoryObs(items=[plain]),
                platform=SimpleNamespace(operating_system="linux", fragment_id="browser.platform"),
            ),
            context=context,
        )
        rendered_restored = await agent_cls().build_react_prompt(
            SimpleNamespace(
                task=TaskObs(user_request="continue"),
                history=HistoryObs(items=[restored]),
                platform=SimpleNamespace(operating_system="linux", fragment_id="browser.platform"),
            ),
            context=context,
        )
        assert rendered_plain == rendered_restored
        text = "\n".join(message.text for message in rendered_restored)
        for marker in ("restored", "8765", "4321", "marker-detail", "screenshot_distance"):
            assert marker not in text


class TestBranchProvenance:
    """``TraceConfig.branch``: recorded typed, per task, out of the digest (§5.3)."""

    @staticmethod
    def _provenance(index: int, step: int = 1) -> BranchProvenance:
        return BranchProvenance(
            source_run_id="src-run",
            source_trace="2026/src-run",
            source_benchmark="online_mind2web",
            source_subset="All",
            source_task_id="t-9",
            checkpoint_step=step,
            branch_index=index,
            branch_group_id=f"src-run/s{step}",
        )

    async def test_branches_share_a_hash(self, tmp_path, composer):
        """Two tasks of one branch sweep: different branch, task id and setup."""
        a = RunTrace(
            await _run(
                tmp_path,
                composer,
                task_id="src-run-s1-b0",
                branch=self._provenance(0),
                env_context={"start_url": "https://a.example/"},
            )
        )
        b = RunTrace(
            await _run(
                tmp_path,
                composer,
                task_id="src-run-s2-b1",
                branch=self._provenance(1, step=2),
                env_context={"start_url": "https://b.example/"},
            )
        )
        assert a.config.branch != b.config.branch
        assert a.config.task_setup != b.config.task_setup
        assert a.manifest.config_hash == b.manifest.config_hash

    @pytest.mark.parametrize(
        ("label", "mutate"),
        [
            ("temperature", lambda raw: raw["agent"][0]["llm"].update(temperature=0.9)),
            ("max_steps", lambda raw: raw["agent"][0]["agent_loop"].update(max_steps=5)),
        ],
    )
    async def test_sweep_knobs_move_it(self, tmp_path, composer, label, mutate):
        """Sweep-level knobs still reach the digest, through ``resolved``."""
        a = RunTrace(
            await _run(tmp_path, composer, task_id=f"k-{label}-a", branch=self._provenance(0))
        )
        b = RunTrace(
            await _run(
                tmp_path,
                composer,
                mutate=mutate,
                task_id=f"k-{label}-b",
                branch=self._provenance(0),
            )
        )
        assert a.config.branch == b.config.branch
        assert a.manifest.config_hash != b.manifest.config_hash

    async def test_continue_steps_moves_it(self, tmp_path, composer):
        """A branch sweep's own knob, through the branch loop's resolved block."""

        def branch_loop(continue_steps):
            def mutate(raw):
                raw["agent"][0]["agent_loop"] = {
                    "loop_id": "branch",
                    "continue_steps": continue_steps,
                    "restorer": {"restorer_id": "prefix_replay"},
                }

            return mutate

        a = RunTrace(
            await _run(
                tmp_path,
                composer,
                mutate=branch_loop(0),
                task_id="cs-a",
                branch=self._provenance(0),
            )
        )
        b = RunTrace(
            await _run(
                tmp_path,
                composer,
                mutate=branch_loop(2),
                task_id="cs-b",
                branch=self._provenance(0),
            )
        )
        loop = a.config.resolved["agent"][0]["agent_loop"]
        assert (loop["loop_id"], loop["restorer"]["restorer_id"]) == ("branch", "prefix_replay")
        assert a.manifest.config_hash != b.manifest.config_hash

    def test_the_branch_is_not_configuration(self):
        base = TraceConfig(agent_id="a", resolved={"x": 1})
        branched = TraceConfig(agent_id="a", resolved={"x": 1}, branch=self._provenance(3))
        assert base.configuration_digest() == branched.configuration_digest()
        assert base.configuration_digest(resolved={"x": 1}) == branched.configuration_digest(
            resolved={"x": 1}
        )

    async def test_round_trip(self, tmp_path, composer):
        provenance = self._provenance(2)
        trace = RunTrace(await _run(tmp_path, composer, task_id="rt", branch=provenance))
        assert isinstance(trace.config.branch, BranchProvenance)
        assert trace.config.branch == provenance

    async def test_none_off_branch(self, tmp_path, composer):
        trace = RunTrace(await _run(tmp_path, composer, task_id="plain"))
        assert trace.config.branch is None
        # And a snapshot written before the field existed reads back without it.
        assert TraceConfig.model_validate({"agent_id": "a"}).branch is None


# ── The archive axis, environment evidence and the task's setup context ──


_TOKEN_URL = "https://shop.example/landing?ref=guard&token=Zk3Lm9QpRt7Vx2BnQ8"


async def _archive_run(
    tmp_path: Path,
    *,
    archive: tuple[Any, ...] = (),
    env_context: dict[str, Any] | None = None,
    record_secrets: bool = False,
    task_id: str = "arc--0001",
) -> RunTrace:
    """A run of the fake archiving capability; *archive* goes to a consumer hook.

    The consumer is ``ArchiveConsumerHook``, not the trace hook: the runtime
    knows consumers only through the ``ArchiveConsumer`` Protocol, and what
    is recorded here is the runtime's resolution, whoever consumes.
    """
    writer = TraceWriter(trace_dir=str(tmp_path / task_id), record_secrets=record_secrets)
    hooks: list[Any] = [writer]
    if archive:
        hooks.append(ArchiveConsumerHook(list(archive)))
    run = make_archive_run(hooks=hooks)
    await AgentRuntime.for_agent(run, AGENT_ID).execute(
        "find the price",
        task_id=task_id,
        benchmark="runcfg",
        subset="all",
        timeout=10,
        env_context=env_context,
    )
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir)


class _UnsortedArchiveResult:
    """A hand-built archive result whose mapping is not in key order."""

    active_archivers = frozenset({STEPS_KEY, BLOBS_KEY})
    archive_bindings = {STEPS_KEY: CAPABILITY_ID, BLOBS_KEY: CAPABILITY_ID}


class TestArchiveNegotiationIsRecorded:
    async def test_the_binding_is_recorded_sorted(self, tmp_path):
        trace = await _archive_run(tmp_path, archive=(FakeStepArchiver(), FakeBlobArchiver()))
        neg = trace.config.negotiation
        assert neg is not None
        assert neg.active_archivers == [BLOBS_KEY, STEPS_KEY]
        assert list(neg.archive_bindings.items()) == [
            (BLOBS_KEY, CAPABILITY_ID),
            (STEPS_KEY, CAPABILITY_ID),
        ]

    async def test_empty_when_no_hook_archives(self, tmp_path):
        trace = await _archive_run(tmp_path)
        neg = trace.config.negotiation
        assert neg is not None  # observation and action were still negotiated
        assert neg.active_archivers == []
        assert neg.archive_bindings == {}

    def test_three_absent_results_are_no_negotiation(self):
        assert TraceNegotiation.from_results(None, None, None) is None

    def test_an_archive_result_alone_is_a_negotiation(self):
        neg = TraceNegotiation.from_results(
            archive=ArchiveNegotiationResult(
                active_archivers=frozenset({STEPS_KEY}),
                archive_bindings={STEPS_KEY: CAPABILITY_ID},
            )
        )
        assert neg is not None
        assert neg.active_archivers == [STEPS_KEY]
        assert neg.active_fragments == []

    def test_sorted_whatever_order_upstream_had(self):
        neg = TraceNegotiation.from_results(archive=_UnsortedArchiveResult())
        assert neg is not None
        assert list(neg.archive_bindings) == [BLOBS_KEY, STEPS_KEY]


def _config_json(trace: RunTrace) -> dict[str, Any]:
    """``config.json`` as written — to see which keys are on disk."""
    return json.loads((trace.trace_dir / "config.json").read_text(encoding="utf-8"))


class TestEnvEvidenceProvenance:
    async def test_recorded_when_archivers_are_bound(self, tmp_path):
        trace = await _archive_run(tmp_path, archive=(FakeStepArchiver(), FakeBlobArchiver()))
        assert trace.config.negotiation is not None
        assert trace.config.negotiation.active_archivers == [BLOBS_KEY, STEPS_KEY]
        assert trace.config.env_evidence_privacy == [MARKER_REDACTED]

    async def test_bound_archivers_that_declare_nothing_record_an_empty_list(self, tmp_path):
        trace = await _archive_run(
            tmp_path, archive=(FakeStepArchiver(redact=False), FakeBlobArchiver())
        )
        assert trace.config.env_evidence_privacy == []
        assert _config_json(trace)["env_evidence_privacy"] == []

    async def test_absent_when_nothing_archives(self, tmp_path):
        trace = await _archive_run(tmp_path)
        assert trace.config.env_evidence_privacy is None
        assert "env_evidence_privacy" not in _config_json(trace)

    def test_defaults_read_back_from_an_older_snapshot(self):
        """Defaulted fields: a snapshot without them still validates."""
        cfg = TraceConfig.model_validate({"agent_id": "a"})
        assert cfg.env_evidence_privacy is None
        assert cfg.task_setup is None


class TestPerTaskFieldsAppearOnlyWithAValue:
    """``config.json`` carries no placeholder for a feature the run did not use."""

    async def test_a_plain_run_writes_none_of_them(self, tmp_path):
        trace = await _archive_run(tmp_path)
        on_disk = _config_json(trace)
        for key in ("branch", "task_setup", "env_evidence_privacy", "extra"):
            assert key not in on_disk, key
        # Deleted: whether archivers were bound is the archive binding itself.
        assert "env_evidence_recorded" not in on_disk
        # An absent key reads back as the field's default.
        config = trace.config
        assert (config.branch, config.task_setup, config.env_evidence_privacy) == (None,) * 3
        assert config.extra == {}

    async def test_each_appears_with_its_value(self, tmp_path):
        trace = await _archive_run(
            tmp_path,
            archive=(FakeStepArchiver(),),
            env_context={"start_url": "https://a.example/"},
        )
        on_disk = _config_json(trace)
        assert on_disk["task_setup"]["context"] == {"start_url": "https://a.example/"}
        assert on_disk["env_evidence_privacy"] == [MARKER_REDACTED]
        assert "branch" not in on_disk

    def test_extra_appears_when_the_caller_passes_some(self):
        assert "extra" in json.loads(TraceConfig(extra={"domain": "travel"}).model_dump_json())
        assert "extra" not in json.loads(TraceConfig().model_dump_json())


class TestTheDigestAndTheArchiveAxis:
    async def test_the_archive_binding_moves_the_hash(self, tmp_path):
        """Through ``negotiation``, with no special case."""
        a = await _archive_run(tmp_path, task_id="arc-a")
        b = await _archive_run(tmp_path, archive=(FakeStepArchiver(),), task_id="arc-b")
        assert a.manifest is not None and b.manifest is not None
        assert a.manifest.config_hash != b.manifest.config_hash

    async def test_two_tasks_differing_only_in_start_url_share_a_hash(self, tmp_path):
        a = await _archive_run(
            tmp_path, env_context={"start_url": "https://a.example/"}, task_id="url-a"
        )
        b = await _archive_run(
            tmp_path, env_context={"start_url": "https://b.example/"}, task_id="url-b"
        )
        assert a.config.task_setup != b.config.task_setup
        assert a.manifest is not None and b.manifest is not None
        assert a.manifest.config_hash == b.manifest.config_hash

    def test_provenance_and_task_setup_are_not_configuration(self):
        base = TraceConfig(agent_id="a")
        varied = TraceConfig(
            agent_id="a",
            env_evidence_privacy=[MARKER_REDACTED],
            task_setup=TaskSetupRecord(
                context={"start_url": "https://x.example/"}, redactions=RedactionLedger()
            ),
        )
        assert base.configuration_digest() == varied.configuration_digest()

    def test_the_archive_binding_is_configuration(self):
        base = TraceConfig(agent_id="a", negotiation=TraceNegotiation())
        bound = TraceConfig(
            agent_id="a",
            negotiation=TraceNegotiation(
                active_archivers=[STEPS_KEY], archive_bindings={STEPS_KEY: CAPABILITY_ID}
            ),
        )
        assert base.configuration_digest() != bound.configuration_digest()


class TestTaskSetupIsRecorded:
    async def test_typed_redacted_and_with_its_ledger(self, tmp_path):
        trace = await _archive_run(tmp_path, env_context={"start_url": _TOKEN_URL})
        record = trace.config.task_setup
        assert isinstance(record, TaskSetupRecord)
        assert record.context == {
            "start_url": f"https://shop.example/landing?ref=guard&token={REDACTED}"
        }
        assert record.redactions == RedactionLedger(
            policy=PRIVACY_POLICY,
            entries=[RedactionEntry(location="/start_url", field="token", rule="url_query")],
        )

    async def test_none_without_a_context(self, tmp_path):
        trace = await _archive_run(tmp_path)
        assert trace.config.task_setup is None

    async def test_record_secrets_records_it_verbatim_and_says_so(self, tmp_path):
        trace = await _archive_run(
            tmp_path, env_context={"start_url": _TOKEN_URL}, record_secrets=True
        )
        record = trace.config.task_setup
        assert record is not None
        assert record.context == {"start_url": _TOKEN_URL}
        assert record.redactions.entries == []
        assert trace.config.secrets_recorded is True

    async def test_a_namespaced_context_is_redacted_by_value_too(self, tmp_path):
        context = {CAPABILITY_ID: {"start_url": "https://u:p@shop.example/"}}
        trace = await _archive_run(tmp_path, env_context=context)
        record = trace.config.task_setup
        assert record is not None
        assert record.context == {CAPABILITY_ID: {"start_url": f"https://{REDACTED}@shop.example/"}}
        assert [(e.location, e.rule) for e in record.redactions.entries] == [
            (f"/{CAPABILITY_ID}/start_url", "url_userinfo")
        ]

    async def test_a_context_that_is_not_an_object_is_kept_under_value(self, tmp_path):
        """The record never fails the run over the context's shape."""
        writer = TraceWriter(trace_dir=str(tmp_path / "nonobject"))
        await writer.on_run_start(
            run_id="nonobject",
            task_setup=["https://u:p@h.example/"],  # type: ignore[arg-type]
        )
        assert writer.run_dir is not None
        record = RunTrace(writer.run_dir).config.task_setup
        assert record is not None
        assert record.context == {"value": [f"https://{REDACTED}@h.example/"]}
        assert [e.location for e in record.redactions.entries] == ["/value/0"]
