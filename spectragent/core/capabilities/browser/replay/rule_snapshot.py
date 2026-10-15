"""Read a replay's frozen rule input through the committed archive boundary."""

import json

from pydantic import ValidationError

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.replay.site_rules import (
    SiteRuleError,
    SiteRules,
    SiteRulesSnapshot,
)


def recorded_site_rules(reader: ArchiveReader) -> SiteRules:
    """Restore and verify the rules a replay actually used, without reading their source.

    The enclosing run record is read through the archive's committed accessor;
    its checksum and every rule-file checksum are independently checked.
    A live run or a replay without a snapshot is a named input error.
    """
    entry = reader.archive_run_file("browser")
    if entry is None:
        raise SiteRuleError(f"run {reader.run_id!r} has no browser rule snapshot")
    try:
        fingerprint = json.loads(reader.read_extra(entry.path))
        source = fingerprint["network_source"]
        snapshot = SiteRulesSnapshot.model_validate(source["site_rules_snapshot"])
        if source.get("source_id") != "archive":
            raise ValueError("network source is not archive")
        if source.get("site_rules_sha256") != snapshot.sha256:
            raise ValueError("rule snapshot digest differs from the network fingerprint")
        return SiteRules.from_snapshot(snapshot)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise SiteRuleError(
            f"run {reader.run_id!r} has an invalid browser rule snapshot: {exc}"
        ) from exc
