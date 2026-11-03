"""Online-Mind2Web: live web tasks, judged by WebJudge on each run's trace."""

from spectragent.benchmarks.online_mind2web.benchmark import OnlineMind2WebBenchmark
from spectragent.benchmarks.online_mind2web.verifier import OnlineMind2WebVerifier

__all__ = ["OnlineMind2WebBenchmark", "OnlineMind2WebVerifier"]
