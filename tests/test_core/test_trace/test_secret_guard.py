"""The guard: a trace directory must not contain credentials.

This is the test that protects secret fields **nobody has written yet**.
Every other test here asserts something about a rule; this one asserts
about the artifact. It plants real-shaped credentials in a real composed
configuration, drives a real ``AgentRuntime`` with the real
``TraceWriter``, and then reads every byte the run left on disk.

Two halves, and both are load-bearing:

* with redaction on (the default) nothing key-shaped, token-shaped or
  ``user:password@``-shaped may appear anywhere under the trace dir;
* with ``record_secrets=True`` the same strings **must** appear. Without
  that half the guard could pass because the run wrote nothing at all, or
  because the scanner looks in the wrong place — a green test that
  guarantees nothing is worse than no test.

The environment is a fake capability (no browser, no network), but the
*configuration* is validated against the real composed schema, which is
where the real secret-carrying fields live: ``llm.api_key``,
``backend.proxy_server``, ``backend.user_data_dir``, ``llm.base_url``.
"""

from __future__ import annotations

import copy
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import OMITTED
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"

# Planted credentials, one per kind the framework can hold. Each is also
# shaped like the real thing so the *pattern* scan below is exercised by
# something, not only the literal scan.
API_KEY = "sk-proj-9fQ2zLk4TgUvXr7NmPaB6HdEwYcJsRtVuZ1K"
GATEWAY_URL = "https://gw.internal.example/v1?access_token=Zk3Lm9QpRt7Vx2Bn"
PROXY_URL = "http://sweepbot:Hunter2Hunter2@proxy.internal.example:8080"
PROFILE_DIR = "/home/researcher/.config/chrome-profile-7a3f9c2b"

SECRETS = (API_KEY, GATEWAY_URL, PROXY_URL, PROFILE_DIR)

#: Shapes, not names. A blocklist of key *names* lags the next backend;
#: these catch a credential that reaches disk through a field nobody
#: thought to mark, whatever it is called.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style api key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic-style api key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}")),
    ("url with credentials", re.compile(r"\b[a-z]+://[^\s/:@\"]+:[^\s/@\"]+@")),
    ("url carrying a token", re.compile(r"[?&](access_token|token|key)=[A-Za-z0-9._-]{8,}")),
    ("websocket devtools token", re.compile(r"\bwss?://[^\s\"]*devtools/browser/[A-Za-z0-9-]{8,}")),
)


# ── a run that needs no browser ────────────────────────────────────


class _Cap(CapabilityBase):
    __registry_key__ = "secret_guard_cap"
    __wiring__ = {
        "action": ("action", ["secret_guard_cap.done"]),
        "observation": ("observation", ["browser.browser_meta"]),
    }

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="secret_guard_cap.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="42")

    async def resolve_fragment(self, fragment_key, **kwargs):
        from spectragent.core.capabilities.browser.observations import BrowserMetaObs

        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com", page_title="Example")
        return None


class _Env(EnvironmentBase):
    __registry_key__ = "secret_guard_env"


class _Agent:
    __wiring__ = {
        "action": ("action", ["done"]),
        "observation": ("observation", ["browser.browser_meta", "loop.task"]),
    }

    def build_react_prompt(self, obs):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        return [Action(name="done", params={})]

    def get_react_output_model(self):
        return None


class _Completion:
    completion = '{"action": "done"}'
    usage = None


class _LLM:
    model = "gpt-4o-mini"

    async def ainvoke(self, messages, **kw):
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


# ── the configuration under test ───────────────────────────────────


def _raw_config_with_secrets() -> dict[str, Any]:
    """The shipped local config, with a credential in every slot it has."""
    raw = copy.deepcopy(
        yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8"))
    )
    raw["agent"][0]["llm"]["api_key"] = API_KEY
    raw["agent"][0]["llm"]["base_url"] = GATEWAY_URL
    backend = raw["environment"][0]["capabilities"][0]["backend"]
    backend["proxy_server"] = PROXY_URL
    backend["user_data_dir"] = PROFILE_DIR
    return raw


async def _run_and_scan(tmp_path, composer, *, record_secrets: bool) -> Path:
    """Drive a full run whose ComposedRun carries the real secret config."""
    resolved = composer.top_level_type.model_validate(_raw_config_with_secrets())

    writer = TraceWriter(
        trace_dir=str(tmp_path / "traces"), record_secrets=record_secrets
    )
    run = make_run(
        agent=_Agent(),
        loop=ReactLoop(max_steps=2),
        env=make_env(_Env, {"secret_guard_cap": _Cap()}),
        llm=_LLM(),
        memory=_Memory(),
        hooks=[writer],
    )
    # ``make_run`` builds the object graph from mocks; the configuration
    # is the real validated one. That pairing is the point: the run needs
    # no browser, and the trace still records a config carrying every
    # credential shape the framework can hold.
    run = dataclasses.replace(run, top_level_cfg=resolved)

    await AgentRuntime.for_agent(run, AGENT_ID).execute(
        "find the price",
        task_id="guard--0001",
        benchmark="secret_guard",
        subset="all",
        timeout=10,
    )
    assert writer.run_dir is not None
    return writer.run_dir


def _all_text(run_dir: Path) -> list[tuple[Path, str]]:
    """Every readable byte under the trace dir, as text.

    Walks the directory rather than the manifest: a leak in a file
    nothing indexes is still a leak, and the manifest is exactly what a
    careless writer would forget to list it in.
    """
    out: list[tuple[Path, str]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        out.append((path, path.read_bytes().decode("utf-8", errors="replace")))
    return out


@pytest.fixture()
async def redacted_run(tmp_path, composer) -> Path:
    return await _run_and_scan(tmp_path, composer, record_secrets=False)


class TestTraceCarriesNoCredentials:
    def test_no_planted_secret_appears_anywhere(self, redacted_run):
        for path, text in _all_text(redacted_run):
            for secret in SECRETS:
                assert secret not in text, (
                    f"{path.relative_to(redacted_run)} leaked {secret[:12]}…"
                )

    def test_no_secret_shaped_string_appears_anywhere(self, redacted_run):
        """The half that protects fields that do not exist yet."""
        for path, text in _all_text(redacted_run):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                assert match is None, (
                    f"{path.relative_to(redacted_run)} contains a {label}: "
                    f"{match.group(0)[:40]!r}"
                )

    def test_the_fields_are_recorded_as_withheld_not_dropped(self, redacted_run):
        cfg = RunTrace(redacted_run).config
        llm = cfg.resolved["agent"][0]["llm"]
        backend = cfg.resolved["environment"][0]["capabilities"][0]["backend"]
        assert llm["api_key"] == OMITTED
        assert llm["base_url"] == OMITTED
        assert backend["proxy_server"] == OMITTED
        assert backend["user_data_dir"] == OMITTED

    def test_the_trace_says_it_is_redacted(self, redacted_run):
        assert RunTrace(redacted_run).config.secrets_recorded is False

    def test_the_config_hash_is_a_digest_of_secrets_not_a_copy_of_them(
        self, redacted_run
    ):
        """A new surface, knowingly taken — so it gets its own assertion.

        ``config_hash`` is computed *before* redaction, because a digest
        over the recorded view cannot tell ``max_steps: 10`` from
        ``max_steps: 100`` and so merges the arms of every sweep. The
        price is that credential-bearing material goes into the hasher.
        What makes that acceptable is the only thing pinned here: what
        comes out and lands in the manifest is a fixed-width digest, with
        none of its input in it.

        The whole-directory scan above already covers this. It is stated
        again because the input to this particular field is now, by
        design, secret — and a reader of that design should find the
        check next to it rather than have to trust that the scan reaches.
        """
        manifest = RunTrace(redacted_run).manifest
        assert manifest is not None
        assert re.fullmatch(r"[0-9a-f]{16}", manifest.config_hash)
        for secret in SECRETS:
            assert secret not in manifest.config_hash


class TestGuardCanActuallyDetectALeak:
    """Without this, the guard above could be green for the wrong reason."""

    async def test_record_secrets_puts_them_back(self, tmp_path, composer):
        run_dir = await _run_and_scan(tmp_path, composer, record_secrets=True)
        blob = "\n".join(text for _, text in _all_text(run_dir))
        for secret in SECRETS:
            assert secret in blob
        assert any(pattern.search(blob) for _, pattern in SECRET_PATTERNS)

    async def test_and_the_trace_admits_it(self, tmp_path, composer):
        run_dir = await _run_and_scan(tmp_path, composer, record_secrets=True)
        assert RunTrace(run_dir).config.secrets_recorded is True
