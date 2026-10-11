"""Privacy of the exported dataset (arch §4.15 "隐私", "校验").

Credentials planted wherever a recorded run can hold them — the network
archive (a CDP authorization header, a proxy URL with a password, a Cookie, a
POST password), the run's environment fingerprint, the DOM fragment (a
password field's value), the raw DOM and an unredacted start URL — reach no
byte under ``output_dir``; a credential that does reach a rendered prompt
fails the export closed; and no file of the dataset is a file of ``env/``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, DOMObs
from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.models import TaskSetupRecord
from spectragent.export.errors import CredentialLeakError
from spectragent.schemas.action import Action
from tests._helpers.export_runs import exporter_entry, run_export
from tests._helpers.export_traces import (
    BROWSER_USE_NEGOTIATION,
    ExportStep,
    click_at,
    screenshot,
    ui_tars_step,
    write_run,
    write_ui_tars_run,
)
from tests._helpers.occlusion_pages import INDEX, LOADER_ID, Box, raw_document, sticky_state
from tests._helpers.trace_builder import archive_file

CDP_TOKEN = "Bearer cdpPlantedToken0123456789abcdef"
PROXY = "http://sweepbot:PlantedProxyPass42@proxy.internal.example:8080"
COOKIE = "sid=PlantedCookieValue9f8e7d6c"
POST_PASSWORD = "PlantedPostPassword7c6b5a"
DOM_PASSWORD = "PlantedDomPassword3e2d1c"
URL_TOKEN = "PlantedUrlTokenZk3Lm9QpRt"
PLANTED = (CDP_TOKEN, PROXY, COOKIE, POST_PASSWORD, DOM_PASSWORD, URL_TOKEN)


def _recorded(root: Path) -> Path:
    """A DOM teacher's run on the sticky page with every credential planted."""
    password = Box("password", 40, (580, 760, 200, 30), 8, "body", tag="input")
    state = sticky_state(extra=(password,))
    node = state.selector_map[40]
    node.attributes.update({"type": "password", "value": DOM_PASSWORD})
    document = raw_document(extra=(password,))
    document["capture"]["snapshot"]["strings"].append(DOM_PASSWORD)
    network = (
        "WARC/1.1\r\nWARC-Type: request\r\n\r\n"
        f"POST /login HTTP/1.1\r\nHost: shop.example.test\r\nAuthorization: {CDP_TOKEN}\r\n"
        f"Cookie: {COOKIE}\r\nProxy: {PROXY}\r\n\r\nuser=a&password={POST_PASSWORD}\r\n"
    ).encode()
    extra = [
        archive_file(
            "browser.dom_raw",
            "dom-0-tab0.json.gz",
            gzip_json(document),
            step=0,
            media_type="application/gzip",
            content_addressed=True,
            ids={"tab": "tab-1"},
        ),
        archive_file("browser.network", "exchanges.warc", network, step=0),
        archive_file(
            "browser",
            "fingerprint.json",
            json.dumps(
                {"cdp_url": f"ws://127.0.0.1:9222/devtools/browser/{CDP_TOKEN[7:]}"}
            ).encode(),
            scope="run",
            run_version=0,
        ),
    ]
    fragments = [
        screenshot(),
        DOMObs(element_tree=state, selector_map=dict(state.selector_map)),
        BrowserMetaObs(url="https://shop.example.test/sticky", loader_id=LOADER_ID),
    ]
    click = Action(name="browser.click", params={"index": INDEX["clear"]})
    return write_run(
        root,
        "teacher",
        [ExportStep(actions=[click], fragments=fragments, extra_files=extra)],
        agent_id="browser_use",
        benchmark="export_test",
        negotiation=BROWSER_USE_NEGOTIATION,
        task_setup=TaskSetupRecord(
            context={"start_url": f"https://shop.example.test/?token={URL_TOKEN}"},
            redactions=RedactionLedger(),
        ),
    )


def _export(composer: ConfigComposer, tmp_path: Path) -> tuple[Path, Path]:
    run = _recorded(tmp_path / "src")
    out = tmp_path / "out"
    run_export(
        composer,
        exporter_entry(
            tmp_path / "src",
            out,
            translator={"translator_id": "index_to_coordinate", "occlusion": "check"},
        ),
    )
    return run, out


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_planted_credentials_never_reach_output(composer: ConfigComposer, tmp_path: Path) -> None:
    run, out = _export(composer, tmp_path)
    blob = b"".join(path.read_bytes() for path in _files(out))
    assert b"browser.click_coordinate" in blob, "the step was exported"
    for secret in PLANTED:
        assert secret.encode() not in blob, secret
    # They were in the source, so the test planted where the export could have read.
    source = b"".join(path.read_bytes() for path in _files(run))
    for secret in (CDP_TOKEN, COOKIE, POST_PASSWORD, DOM_PASSWORD, URL_TOKEN):
        assert secret.encode() in source, secret


def test_no_output_file_comes_from_env(composer: ConfigComposer, tmp_path: Path) -> None:
    run, out = _export(composer, tmp_path)
    env_files = _files(run / "env")
    assert env_files, "the source recorded environment evidence"
    env_hashes = {hashlib.sha256(path.read_bytes()).hexdigest() for path in env_files}
    out_hashes = {hashlib.sha256(path.read_bytes()).hexdigest() for path in _files(out)}
    assert env_hashes.isdisjoint(out_hashes)
    assert not any("env" in path.relative_to(out).parts for path in _files(out))


def test_secret_in_rendered_prompt_fails_closed_and_leaves_no_output(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    """A credential in the task text reaches the prompt: the export refuses, writes nothing."""
    api_key = "sk-proj-9fQ2zLk4TgUvXr7NmPaB6HdEwYcJsRtVuZ1K"
    write_ui_tars_run(
        tmp_path / "src",
        "leaky",
        [ui_tars_step(click_at(10, 10))],
        task=f"Log in with the key {api_key} and buy a kettle",
    )
    with pytest.raises(CredentialLeakError) as excinfo:
        run_export(composer, exporter_entry(tmp_path / "src", tmp_path / "out"))
    assert excinfo.value.label == "openai-style api key"
    assert excinfo.value.sample_id == "leaky/s0"
    assert excinfo.value.file.endswith(".jsonl")
    assert api_key not in str(excinfo.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"], "no output, no .partial"


def test_a_run_recorded_with_its_secrets_is_never_a_source(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    write_ui_tars_run(tmp_path / "src", "ok", [ui_tars_step(click_at(10, 10))])
    write_ui_tars_run(
        tmp_path / "src", "raw", [ui_tars_step(click_at(10, 10))], secrets_recorded=True
    )
    report = run_export(composer, exporter_entry(tmp_path / "src", tmp_path / "out"))
    assert list(report.manifest["sources"]["skipped"]) == ["raw"]
    assert [s["path"] for s in report.manifest["sources"]["exported"]] == ["ok"]
