"""Source file identity is stable across edits, distinct across paths, and contains no YAML."""

import hashlib

import pytest
from pydantic import ValidationError

from spectragent.schemas.config_source import ConfigSource


def test_same_file_keeps_identity_across_versions(tmp_path):
    path = tmp_path / "agent.yaml"
    original = b"model: first\napi_key: secret-source-sentinel\n"
    path.write_bytes(original)
    before = ConfigSource.from_path(path)
    path.write_bytes(b"model: second\n")
    after = ConfigSource.from_path(path)
    assert before.path == after.path == str(path.resolve())
    assert before.sha256 == hashlib.sha256(original).hexdigest()
    assert before.sha256 != after.sha256
    assert set(before.model_dump()) == {"path", "sha256"}
    assert "secret-source-sentinel" not in before.model_dump_json()


def test_same_basename_in_different_directories_is_a_different_source(tmp_path):
    paths = [tmp_path / name / "agent.yaml" for name in ("left", "right")]
    for path in paths:
        path.parent.mkdir()
        path.write_bytes(b"model: same\n")
    left, right = [ConfigSource.from_path(path) for path in paths]
    assert left.path != right.path and left.sha256 == right.sha256


def test_consumed_bytes_are_used_even_if_the_file_changed_or_disappeared(tmp_path):
    path = tmp_path / "agent.yaml"
    consumed = b"model: first\r\n"
    path.write_bytes(b"model: changed\n")
    source = ConfigSource.from_path(path, data=consumed)
    assert source.sha256 == hashlib.sha256(consumed).hexdigest()
    path.unlink()
    assert ConfigSource.from_path(path, data=consumed) == source


def test_factory_canonicalizes_symlink_but_reader_does_not_follow_new_links(tmp_path):
    original = tmp_path / "original.yaml"
    original.write_bytes(b"model: original\n")
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(original)
    source = ConfigSource.from_path(alias)
    assert source.path == str(original.resolve())
    recorded = source.model_dump()
    original.unlink()
    original.symlink_to(tmp_path / "elsewhere.yaml")
    assert ConfigSource.model_validate(recorded).path == recorded["path"]


@pytest.mark.parametrize("field,value", [("path", "relative.yaml"), ("sha256", "not-a-digest")])
def test_source_fields_are_validated(tmp_path, field, value):
    fields = {"path": str(tmp_path / "config.yaml"), "sha256": "0" * 64, field: value}
    with pytest.raises(ValidationError):
        ConfigSource.model_validate(fields)


@pytest.mark.parametrize(
    "path", [r"C:\experiments\agent.yaml", r"\\server\share\agent.yaml", "/experiments/agent.yaml"]
)
def test_recorded_paths_from_another_host_are_preserved(path):
    source = ConfigSource(path=path, sha256="0" * 64)
    assert source.path == path
