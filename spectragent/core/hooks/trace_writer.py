"""TraceWriter — RuntimeHook for full WAL-based trace persistence.

Intercepts env.observe and llm.ainvoke via AOP to capture all data
per step, then commits to disk via WALWriter.

Registered via conscribe as hook_id="trace_writer".

See docs/execution_dag.md M11 for design details.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.wal import WALWriter

logger = logging.getLogger(__name__)


class TraceWriter(HookBase):
    """Full-trace WAL persistence hook.

    Captures observation fragments (after_observe) and LLM messages
    (after_ainvoke), writes them to disk, and commits the manifest
    at each step boundary.

    Step boundary: after_ainvoke is the commit point — LLM reasoning
    is the most expensive, non-retryable part of each step.
    """

    __registry_key__ = "trace_writer"
    __intercepts__ = [
        Intercept("observe", role="env", advice="after"),
        Intercept("ainvoke", role="llm", advice="after"),
    ]

    # ── conscribe config-annotated fields ─────────────────────
    trace_dir: Annotated[
        str, Field(default="traces", description="Base directory for trace output.")
    ] = "traces"

    # ── internal state (not config) ───────────────────────────
    _wal: WALWriter | None = None
    _current_step: int = 0
    _pending_observation: Any = None

    @property
    def blocking(self) -> bool:
        return True

    # ── lifecycle hooks ───────────────────────────────────────

    async def on_run_start(
        self, *, agent: Any, env: Any, task: str, config: Any, **kwargs: Any
    ) -> None:
        run_id = str(uuid.uuid4())[:8]
        base_dir = Path(self.trace_dir)

        self._wal = WALWriter(base_dir, run_id)
        self._current_step = 0
        self._pending_observation = None

        trace_config = TraceConfig(
            agent_id=getattr(agent, "agent_id", getattr(agent, "agent_name", "")),
            loop_id="",  # Loop info not directly available here
            env_type=getattr(env, "env_type", ""),
            task=task,
            task_id=kwargs.get("task_id", ""),
            extra=config if isinstance(config, dict) else {},
        )
        self._wal.init_run(trace_config)
        logger.info("TraceWriter: initialized run %s at %s", run_id, self._wal.run_dir)

    async def on_run_end(self, *, result: Any, **kwargs: Any) -> None:
        if self._wal is None:
            return
        self._wal.write_result(result)
        self._wal.finalize(RunStatus.COMPLETED)
        logger.info(
            "TraceWriter: finalized run at %s (%d steps)",
            self._wal.run_dir,
            self._current_step,
        )

    async def on_run_error(
        self, *, error: BaseException, partial_steps: Any, **kwargs: Any
    ) -> None:
        if self._wal is None:
            return
        self._wal.finalize(RunStatus.ERROR)
        logger.warning(
            "TraceWriter: run errored at step %d: %s", self._current_step, error
        )

    # ── AOP intercept handlers ────────────────────────────────

    async def after_observe(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Buffer observation data — will be written at commit (after_ainvoke)."""
        self._pending_observation = result

    async def after_ainvoke(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Commit point: write observation + messages + step record to WAL."""
        if self._wal is None:
            return

        step = self._current_step
        obs_files: list[str] = []

        # Write buffered observation fragments
        if self._pending_observation is not None:
            obs = self._pending_observation
            # If observation is a Pydantic model with fragment fields, serialize each
            if hasattr(obs, "model_fields"):
                for field_name in obs.model_fields:
                    value = getattr(obs, field_name, None)
                    if value is not None:
                        rel = self._wal.write_observation(step, field_name, value)
                        obs_files.append(rel)
            else:
                # Fallback: serialize the whole observation
                rel = self._wal.write_observation(step, "observation", obs)
                obs_files.append(rel)
            self._pending_observation = None

        # Write LLM messages
        # args[0] is the messages list passed to ainvoke()
        input_msgs = args[0] if args else None
        in_rel, out_rel = self._wal.write_messages(step, input_msgs, result)

        # Write step record skeleton
        step_record = {
            "step": step,
            "llm_output": _extract_completion_text(result),
        }
        step_rel = self._wal.write_step_record(step, step_record)

        # Atomic commit
        self._wal.commit_step(
            step=step,
            step_file=step_rel,
            observation_files=obs_files,
            message_input_file=in_rel,
            message_output_file=out_rel,
        )

        self._current_step += 1
        logger.debug("TraceWriter: committed step %d", step)


def _extract_completion_text(result: Any) -> str:
    """Extract text from a ChatInvokeCompletion or similar result."""
    if hasattr(result, "completion"):
        comp = result.completion
        if isinstance(comp, str):
            return comp
        if hasattr(comp, "model_dump_json"):
            return comp.model_dump_json()
        return str(comp)
    return str(result)
