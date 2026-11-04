"""Downloads checked against their pin, and the browser benchmarks' data as their loaders read it (``docs/architecture_design.md`` §4.10.1 "数据")."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest

from spectragent.benchmarks import data
from spectragent.benchmarks.browsecomp.data import tasks_jsonl
from spectragent.benchmarks.data import download_file, download_huggingface_file, git_blob_hash
from spectragent.benchmarks.online_mind2web import data as online_mind2web
from spectragent.core.data_loader._common import BenchmarkDataError

_REVISION = "a" * 40


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Answer every download with *handler*; the requests it got."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client(headers: Mapping[str, str]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(respond), headers=dict(headers))

    monkeypatch.setattr(data, "_client", client)
    return seen


def _dataset(files: Mapping[str, bytes], *, lfs: bool = False, gated: bool = False):
    """A Hugging Face dataset at :data:`_REVISION` holding *files*, as its API and file URLs answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/api/datasets/org/set/tree/{_REVISION}"):
            listing = []
            for name, body in files.items():
                entry: dict[str, object] = {
                    "type": "file",
                    "path": name,
                    "oid": git_blob_hash(body),
                }
                if lfs:
                    entry["lfs"] = {"oid": hashlib.sha256(body).hexdigest(), "size": len(body)}
                listing.append(entry)
            return httpx.Response(200, json=listing)
        prefix = f"/datasets/org/set/resolve/{_REVISION}/"
        if path.startswith(prefix):
            if gated and "authorization" not in request.headers:
                return httpx.Response(401, text="Access to dataset org/set is restricted.")
            return httpx.Response(200, content=files[path.removeprefix(prefix)])
        return httpx.Response(404)

    return handler


def test_git_blob_hash_is_gits() -> None:
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob_hash(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.parametrize("lfs", [False, True])
def test_a_hugging_face_file_is_checked_against_the_datasets_record_at_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lfs: bool
) -> None:
    _serve(monkeypatch, _dataset({"tasks.json": b"[1, 2]"}, lfs=lfs))
    target = download_huggingface_file("org/set", _REVISION, "tasks.json", tmp_path, print)
    assert target == tmp_path / "tasks.json"
    assert target.read_bytes() == b"[1, 2]"


def test_a_hugging_face_file_that_is_not_the_one_at_the_commit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = _dataset({"tasks.json": b"[1, 2]"})

    def tampered(request: httpx.Request) -> httpx.Response:
        if "/resolve/" in request.url.path:
            return httpx.Response(200, content=b"[1, 2, 3]")
        return listed(request)

    _serve(monkeypatch, tampered)
    with pytest.raises(BenchmarkDataError) as refused:
        download_huggingface_file("org/set", _REVISION, "tasks.json", tmp_path, print)
    assert f"and the dataset records {git_blob_hash(b'[1, 2]')} for it at {_REVISION}" in str(
        refused.value
    )
    assert not (tmp_path / "tasks.json").exists()


def test_a_gated_dataset_without_a_token_is_refused_saying_how_to_get_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    _serve(monkeypatch, _dataset({"tasks.json": b"[]"}, gated=True))
    with pytest.raises(BenchmarkDataError) as refused:
        download_huggingface_file("org/set", _REVISION, "tasks.json", tmp_path, print)
    assert str(refused.value) == (
        "Hugging Face refused tasks.json of org/set (HTTP 401): the dataset is gated; accept its "
        "terms at https://huggingface.co/datasets/org/set and set HF_TOKEN to a token of that "
        "account (HF_TOKEN is not set)"
    )

    monkeypatch.setenv("HF_TOKEN", "hf_token_of_the_test")
    seen = _serve(monkeypatch, _dataset({"tasks.json": b"[]"}, gated=True))
    download_huggingface_file("org/set", _REVISION, "tasks.json", tmp_path, print)
    assert seen[-1].headers["authorization"] == "Bearer hf_token_of_the_test"


def test_a_file_the_dataset_does_not_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _dataset({"other.json": b"[]"}))
    with pytest.raises(
        BenchmarkDataError, match=f"org/set at {_REVISION} holds no file tasks.json"
    ):
        download_huggingface_file("org/set", _REVISION, "tasks.json", tmp_path, print)


def test_a_url_download_is_checked_against_its_pinned_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"a,b\n"))
    pinned = hashlib.sha256(b"a,b\n").hexdigest()
    assert (
        download_file("https://example.test/f.csv", pinned, tmp_path / "f.csv", print).read_bytes()
        == b"a,b\n"
    )
    with pytest.raises(BenchmarkDataError, match="the file there changed since it was pinned"):
        download_file("https://example.test/f.csv", "0" * 64, tmp_path / "g.csv", print)
    assert not (tmp_path / "g.csv").exists()


def test_a_failed_download_names_what_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(
        BenchmarkDataError, match="cannot download https://example.test/f.csv: HTTP 503"
    ):
        download_file("https://example.test/f.csv", "0" * 64, tmp_path / "f.csv", print)


_UPSTREAM = [
    {
        "task_id": "t1_110325",
        "confirmed_task": "Find the store hours.",
        "website": "https://www.example.com/",
        "reference_length": 6,
        "level": "medium",
    },
    {
        "task_id": "t2",
        "confirmed_task": "Compare the plans.",
        "website": "https://plans.example.com/",
        "reference_length": 11,
        "level": "hard",
    },
]


def test_online_mind2web_is_written_as_its_loader_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(repository: str, revision: str, path: str, into: Path, say: object) -> Path:
        assert (repository, revision, path) == (
            online_mind2web.DATA.repository,
            online_mind2web.DATA.revision,
            online_mind2web.TASKS_FILE,
        )
        target = into / path
        target.write_text(json.dumps(_UPSTREAM))
        return target

    monkeypatch.setattr(online_mind2web, "download_huggingface_file", download)
    online_mind2web.OnlineMind2WebData().fetch(tmp_path, lambda line: None)

    tasks = json.loads((tmp_path / "Online_Mind2Web.json").read_text())
    assert tasks[0] == {
        "task_id": "t1_110325",
        "benchmark": "online_mind2web",
        "env_type": "browser",
        "task_text": "Find the store hours.",
        "start_url": "https://www.example.com/",
        "difficulty": "medium",
        "metadata": {"reference_length": 6},
    }
    assert [task["task_id"] for task in json.loads((tmp_path / "hard.json").read_text())] == ["t2"]
    assert json.loads((tmp_path / "data_info.json").read_text()) == {
        "split": {"All": "Online_Mind2Web.json", "Hard": "hard.json"}
    }

    from spectragent.core.data_loader.task.online_mind2web import OnlineMind2WebTaskLoader

    hard = OnlineMind2WebTaskLoader(subset="Hard", data_dir=str(tmp_path))
    assert hard.task_ids() == ["t2"]
    assert hard.get_task_context("t2").env == {"start_url": "https://plans.example.com/"}


def test_an_online_mind2web_file_of_another_shape_is_refused_naming_its_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Online_Mind2Web.json"
    path.write_text(json.dumps([{"task_id": "t", "task_description": "d", "website": "w"}]))
    with pytest.raises(BenchmarkDataError) as refused:
        online_mind2web.read_upstream(path)
    message = str(refused.value)
    assert "is not a list of tasks with task_id, confirmed_task, website" in message
    assert message.endswith(
        "its first record has the keys ['task_description', 'task_id', 'website']"
    )


def test_browsecomp_rows_become_its_loaders_tasks_still_encrypted() -> None:
    text = "problem,answer,problem_topic,canary\ncXVlc3Rpb24=,YW5zd2Vy,Art,the canary\n"
    assert [json.loads(line) for line in tasks_jsonl(text)] == [
        {
            "task_id": "browsecomp_000",
            "benchmark": "browsecomp",
            "env_type": "browser",
            "reference": {"encrypted_question": "cXVlc3Rpb24=", "encrypted_answer": "YW5zd2Vy"},
            "metadata": {"canary": "the canary"},
        }
    ]
    with pytest.raises(BenchmarkDataError, match="has no column canary"):
        tasks_jsonl("problem,answer\nq,a\n")
