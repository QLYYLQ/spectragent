"""Stub for config — replaced when full config module is ported."""

from dataclasses import dataclass


@dataclass
class _Config:
	ANONYMIZED_TELEMETRY: bool = True


CONFIG = _Config()
