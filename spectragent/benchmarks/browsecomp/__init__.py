"""BrowseComp: questions answered by browsing, with encrypted reference answers."""

from spectragent.benchmarks.browsecomp.benchmark import BrowseCompBenchmark
from spectragent.benchmarks.browsecomp.verifier import BrowseCompVerifier

__all__ = ["BrowseCompBenchmark", "BrowseCompVerifier"]
