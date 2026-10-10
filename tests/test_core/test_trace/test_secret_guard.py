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

One credential arrives by another road: the task's setup context. The run
is started with a ``start_url`` carrying a ``?token=``, which the trace
records as ``TraceConfig.task_setup`` — redacted by value with the shared
privacy rules, so the token is replaced while the rest of the URL survives
for a branch task to re-create the environment from.

And the trace carries environment evidence: free-form bytes under ``env/``
that no field declaration covers and the framework never redacts (the
capability does — M29 for the browser). So the run binds an archiving fake
capability, the scan reads ``env/`` like everything else — gunzipping a file
whose first bytes are ``1f 8b`` or whose name ends in ``.gz`` before it looks
— and a positive control plants the api key in a plain and a gzipped
evidence file and requires the scan to find it in **each**.
"""

from __future__ import annotations

import copy
import dataclasses
import gzip
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.archive import ArchiveBatch, ArchiveFile, ArchiveProvenance, ArchiverBase
from spectragent.core.archive.privacy import REDACTED
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import OMITTED
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run
from tests._helpers.secret_scan import GZIP_MAGIC, all_text, readable

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"

# Planted credentials, one per kind the framework can hold. Each is also
# shaped like the real thing so the *pattern* scan below is exercised by
# something, not only the literal scan.
API_KEY = "sk-proj-9fQ2zLk4TgUvXr7NmPaB6HdEwYcJsRtVuZ1K"
GATEWAY_URL = "https://gw.internal.example/v1?access_token=Zk3Lm9QpRt7Vx2Bn"
PROXY_URL = "http://sweepbot:Hunter2Hunter2@proxy.internal.example:8080"
PROFILE_DIR = "/home/researcher/.config/chrome-profile-7a3f9c2b"
#: Not configuration: the task's setup context, routed to the capability's
#: ``setup(start_url=...)`` and recorded as ``TraceConfig.task_setup``.
START_URL = "https://shop.example/landing?ref=guard&token=Zk3Lm9QpRt7Vx2BnQ8"

SECRETS = (API_KEY, GATEWAY_URL, PROXY_URL, PROFILE_DIR, START_URL)

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

    async def setup(self, *, negotiation=None, start_url: str | None = None) -> None:
        await super().setup(negotiation=negotiation)


#: What the benign archiver records — evidence with nothing secret in it.
EVIDENCE_TEXT = b"benign evidence: page title 'Example', status 200\n"
EVIDENCE_KEY = "secret_guard_archive.evidence"
LEAK_KEY = "secret_guard_archive.leak"


class _EvidenceArchiver(ArchiverBase):
    """Benign evidence, written plain and gzipped — so ``env/`` exists and is scanned."""

    __registry_key__ = EVIDENCE_KEY


class _LeakArchiver(ArchiverBase):
    """The positive control: the planted api key, written plain and gzipped."""

    __registry_key__ = LEAK_KEY


class _ArchiveCap(CapabilityBase):
    """An archiving capability: at setup, it captures one plain and one ``.gz`` file per archiver.

    It never redacts what it captures — that is the point: the framework
    does not either, so the guard is the framework's one check on ``env/``.
    """

    __registry_key__ = "secret_guard_archive"
    __wiring__ = {"archiver": ("archiver", [EVIDENCE_KEY, LEAK_KEY])}

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        self.__dict__["_bound"] = tuple(a.archiver_id for a in archivers)

    async def setup(self, *, negotiation=None) -> None:
        await super().setup(negotiation=negotiation)
        buffer: list[ArchiveFile] = []
        for key in self.__dict__.get("_bound", ()):
            text = EVIDENCE_TEXT if key == EVIDENCE_KEY else f"leaked: {API_KEY}\n".encode()
            stem = key.rpartition(".")[2]
            for name, data, media_type in (
                (f"{stem}.txt", text, "text/plain"),
                (f"{stem}.txt.gz", gzip.compress(text), "application/gzip"),
            ):
                buffer.append(
                    ArchiveFile(
                        archiver_id=key,
                        name=name,
                        media_type=media_type,
                        data=data,
                        meta=ArchiveProvenance(captured_at=time.time()),
                    )
                )
        self.__dict__["_buffer"] = buffer

    async def collect_archive(self) -> ArchiveBatch:
        files, self.__dict__["_buffer"] = self.__dict__.get("_buffer", []), []
        return ArchiveBatch(tuple(files))


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
    raw = copy.deepcopy(yaml.safe_load((CONFIG_DIR / "browser_use_local.yaml").read_text("utf-8")))
    raw["agent"][0]["llm"]["api_key"] = API_KEY
    raw["agent"][0]["llm"]["base_url"] = GATEWAY_URL
    backend = raw["environment"][0]["capabilities"][0]["backend"]
    backend["proxy_server"] = PROXY_URL
    backend["user_data_dir"] = PROFILE_DIR
    return raw


async def _run_and_scan(
    tmp_path, composer, *, record_secrets: bool, archiver: ArchiverBase | None = None
) -> Path:
    """Drive a full run whose ComposedRun carries the real secret config.

    The trace hook archives *archiver*'s evidence — the benign one unless a
    test plants a leak.
    """
    resolved = composer.top_level_type.model_validate(_raw_config_with_secrets())

    writer = TraceWriter(
        trace_dir=str(tmp_path / "traces"),
        record_secrets=record_secrets,
        archive=[archiver if archiver is not None else _EvidenceArchiver()],
    )
    run = make_run(
        agent=_Agent(),
        loop=ReactLoop(max_steps=2),
        env=make_env(_Env, {"secret_guard_cap": _Cap(), "secret_guard_archive": _ArchiveCap()}),
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
        env_context={"start_url": START_URL},
    )
    assert writer.run_dir is not None
    return writer.run_dir


@pytest.fixture()
async def redacted_run(tmp_path, composer) -> Path:
    return await _run_and_scan(tmp_path, composer, record_secrets=False)


class TestTraceCarriesNoCredentials:
    def test_no_planted_secret_appears_anywhere(self, redacted_run):
        for path, text in all_text(redacted_run):
            for secret in SECRETS:
                assert secret not in text, f"{path.relative_to(redacted_run)} leaked {secret[:12]}…"

    def test_no_secret_shaped_string_appears_anywhere(self, redacted_run):
        """The half that protects fields that do not exist yet."""
        for path, text in all_text(redacted_run):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                assert match is None, (
                    f"{path.relative_to(redacted_run)} contains a {label}: {match.group(0)[:40]!r}"
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

    def test_the_scan_reaches_env_and_reads_through_gzip(self, redacted_run):
        """The scan above covered environment evidence — and decoded the gzipped file."""
        evidence = redacted_run / "env/steps/000" / EVIDENCE_KEY
        scanned = dict(all_text(redacted_run))
        assert scanned[evidence / "evidence.txt"] == EVIDENCE_TEXT.decode()
        assert scanned[evidence / "evidence.txt.gz"] == EVIDENCE_TEXT.decode()
        assert (evidence / "evidence.txt.gz").read_bytes()[:2] == GZIP_MAGIC
        config = RunTrace(redacted_run).config
        assert config.env_evidence_recorded is True

    def test_the_setup_context_is_redacted_by_value(self, redacted_run):
        """The token is replaced, the rest of the URL survives, the ledger says where."""
        record = RunTrace(redacted_run).config.task_setup
        assert record is not None
        assert record.context == {
            "start_url": f"https://shop.example/landing?ref=guard&token={REDACTED}"
        }
        assert [e.model_dump() for e in record.redactions.entries] == [
            {"location": "/start_url", "field": "token", "rule": "url_query"}
        ]

    def test_the_config_hash_is_a_digest_of_secrets_not_a_copy_of_them(self, redacted_run):
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
        blob = "\n".join(text for _, text in all_text(run_dir))
        for secret in SECRETS:
            assert secret in blob
        assert any(pattern.search(blob) for _, pattern in SECRET_PATTERNS)

    async def test_and_the_trace_admits_it(self, tmp_path, composer):
        run_dir = await _run_and_scan(tmp_path, composer, record_secrets=True)
        assert RunTrace(run_dir).config.secrets_recorded is True

    async def test_a_secret_in_environment_evidence_is_found_plain_and_gzipped(
        self, tmp_path, composer
    ):
        """Redaction on, and still a leak: the framework never redacts evidence.

        The guard has to find the planted key in the plain file *and* in the
        gzipped one — per file, so the gzip path is proven rather than
        carried by its plain twin. The compressed bytes themselves do not
        contain it: only decoding finds it.
        """
        run_dir = await _run_and_scan(
            tmp_path, composer, record_secrets=False, archiver=_LeakArchiver()
        )
        leak = run_dir / "env/steps/000" / LEAK_KEY
        plain, gzipped = leak / "leak.txt", leak / "leak.txt.gz"
        assert API_KEY in readable(plain)
        assert API_KEY in readable(gzipped)
        assert API_KEY.encode() not in gzipped.read_bytes()
        assert any(pattern.search(readable(gzipped)) for label, pattern in SECRET_PATTERNS), (
            "the shape scan must catch it in the decoded bytes"
        )
        # Nowhere else: the rest of the run is still redacted.
        leaking = [path for path, text in all_text(run_dir) if API_KEY in text]
        assert leaking == [plain, gzipped]

    def test_a_file_that_only_looks_gzipped_is_scanned_as_it_is(self, tmp_path):
        fake = tmp_path / "evidence.gz"
        fake.write_bytes(b"\x1f\x8b" + API_KEY.encode())
        assert API_KEY in readable(fake)
        nested = tmp_path / "nested.bin"
        nested.write_bytes(gzip.compress(gzip.compress(API_KEY.encode())))
        assert readable(nested) == API_KEY
