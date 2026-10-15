"""``spectragent flywheel``: the report subcommand on a chain root, ``run`` and its exit codes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.flywheel import chain as chain_module
from spectragent.flywheel.cli import TOKEN_LIMIT_EXIT_CODE, main
from spectragent.flywheel.metrics import ChainLayout
from spectragent.flywheel.report import NO_SPEC_LIMIT
from tests._helpers.flywheel_traces import KnownChain
from tests.test_flywheel.test_chain import BRANCH_YAML, RUN_YAML, Recorder, Sweep, commit_one_run


def test_report_from_a_root(
    layout: ChainLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert (
        main(
            [
                "report",
                "--root",
                str(layout.root),
                "--evidence",
                "live",
                "--name",
                "n",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    document = json.loads((out / "report.json").read_text())
    assert document["chain"] == {"name": "n", "evidence": "live"}
    use = document["token_use"]
    assert (use["limit"], use["limit_reached"], use["reason"]) == (None, None, NO_SPEC_LIMIT)
    privacy = document["metrics"]["privacy"]
    for gate in ("model_requests", "datasets"):
        assert privacy[gate]["planted"] == [], "no spec: nothing planted to search for"
    assert (out / "report.md").is_file()
    assert str(out / "report.json") in capsys.readouterr().out


def test_report_from_a_spec_reads_its_root_evidence_and_planted(
    layout: ChainLayout, tmp_path: Path
) -> None:
    spec = tmp_path / "chain.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "known",
                "root": str(layout.root),
                "evidence": "rehearsal",
                "record": {"config": "unused.yaml"},
                "token_limit": 1_000_000,
                "report": {"planted": [{"label": "teacher api key", "env": "FLYWHEEL_CLI_UNSET"}]},
            }
        )
    )
    out = tmp_path / "out"
    assert main(["report", "--spec", str(spec), "--out", str(out)]) == 0
    document = json.loads((out / "report.json").read_text())
    use = document["token_use"]
    assert (use["limit"], use["limit_reached"], use["reason"]) == (1_000_000, False, None)
    for gate in ("model_requests", "datasets"):
        (planted,) = document["metrics"]["privacy"][gate]["planted"]
        assert (planted["label"], planted["source"], planted["occurrences"]) == (
            "teacher api key",
            "env:FLYWHEEL_CLI_UNSET",
            None,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["report", "--root", "x"],  # no evidence
        ["report", "--spec", "x.yaml", "--evidence", "live"],  # evidence comes from the spec
        ["report"],
        ["run"],
    ],
)
def test_wrong_arguments_exit_2(argv: list[str], tmp_path: Path) -> None:
    spec = tmp_path / "x.yaml"
    spec.write_text("name: x\n")
    argv = [str(spec) if item == "x.yaml" else item for item in argv]
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2


def test_a_spec_that_does_not_validate_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "chain.yaml"
    spec.write_text("name: x\nroot: r\nevidence: live\nrecord: {config: a.yaml}\nsurprise: 1\n")
    assert main(["run", "--spec", str(spec)]) == 2
    assert "surprise" in capsys.readouterr().err


def test_run_and_a_failing_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "record.yaml"
    config.write_text(yaml.safe_dump(RUN_YAML))
    spec = tmp_path / "chain.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "c",
                "root": str(tmp_path / "root"),
                "evidence": "rehearsal",
                "record": {"config": str(config)},
            }
        )
    )
    monkeypatch.setattr(chain_module, "_bench_entry", lambda: Recorder(commit_one_run))
    assert main(["run", "--spec", str(spec), "--stages", "record,report"]) == 0
    assert (tmp_path / "root" / "report" / "report.json").is_file()

    def no_run(_raw: dict[str, Any], _argv: list[str]) -> int:
        return 0

    monkeypatch.setattr(chain_module, "_bench_entry", lambda: Recorder(no_run))
    assert main(["run", "--spec", str(spec), "--stages", "record"]) == 1


def test_an_unwritable_out_is_named_not_a_traceback(
    layout: ChainLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("a file where the report directory goes")
    out = blocker / "report"
    argv = ["report", "--root", str(layout.root), "--evidence", "live", "--out", str(out)]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert f"stage 'report': cannot write the report to {out}: " in err


def test_a_dataset_this_build_cannot_read_is_named_not_a_traceback(
    known: KnownChain, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "chain"
    shutil.copytree(known.root / "datasets", root / "datasets")
    (root / "datasets" / "first" / "manifest.json").write_text("{}")
    argv = ["report", "--root", str(root), "--evidence", "live", "--out", str(tmp_path / "out")]
    assert main(argv) == 1
    assert "datasets/first/manifest.json does not read back" in capsys.readouterr().err
    assert not (tmp_path / "out").exists(), "nothing is written for a report that did not compute"


def test_run_exits_4_at_the_token_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configs = {}
    for stage, raw in (("record", RUN_YAML), ("same_path", BRANCH_YAML)):
        configs[stage] = tmp_path / f"{stage}.yaml"
        configs[stage].write_text(yaml.safe_dump(raw))
    spec = tmp_path / "chain.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "c",
                "root": str(tmp_path / "root"),
                "evidence": "rehearsal",
                "token_limit": 5,
                "record": {"config": str(configs["record"])},
                "same_path": {"config": str(configs["same_path"])},
            }
        )
    )
    sweep = Sweep({"record": ([[10]], 4)})
    monkeypatch.setattr(chain_module, "_bench_entry", lambda: Recorder(sweep))

    assert main(["run", "--spec", str(spec)]) == TOKEN_LIMIT_EXIT_CODE == 4

    assert list(sweep.limits) == ["record"], "same_path never started"
    captured = capsys.readouterr()
    assert "record: exit [4], 1 run(s), 10 tokens" in captured.out
    assert f"report: {(tmp_path / 'root' / 'report').resolve()}" in captured.out
    assert (
        "spectragent flywheel: stopped at the token limit: the runs under the chain root "
        "recorded 10 tokens, reaching the token limit of 5 in stage 'record'; stages not "
        "started: same_path; 0 call(s) reported no usage"
    ) in captured.err
    document = json.loads((tmp_path / "root" / "report" / "report.json").read_text())
    assert document["token_use"]["limit_reached"] is True
