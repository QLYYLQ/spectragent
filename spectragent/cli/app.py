"""One discoverable command tree and one output contract for experiments."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import io
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from typer.core import TyperCommand, TyperOption

if TYPE_CHECKING:
    # The click typer builds on; typer names these types only in its own modules.
    from typer._click.core import Parameter
    from typer._click.shell_completion import CompletionItem

    from spectragent.core.registries import CapabilityFlag

#: Where a command keeps the start flags it was given (flag → value) in ``ctx.meta``.
_START_FLAGS = "spectragent.start_flags"
#: Where ``config template`` keeps the capabilities' template flags it was given.
_TEMPLATE_FLAGS = "spectragent.template_flags"

#: The commands a leaf module's own parser runs: name, module, help.
_LEAF_COMMANDS: tuple[tuple[str, str, str], ...] = (
    (
        "replay-review",
        "spectragent.replay_review.cli",
        "Review recorded replay evidence independently. Supports --json and --dry-run.",
    ),
    (
        "run",
        "spectragent.core.run_cli",
        "Run one task. Add --tui for an interactive monitor, or --json.",
    ),
    (
        "bench",
        "spectragent.core.benchmark_runner",
        "Run selected benchmark tasks. Supports --tui and --json.",
    ),
    (
        "eval",
        "spectragent.verifiers.cli",
        "Judge recorded runs with a benchmark's offline verifiers. Supports --json.",
    ),
    (
        "export",
        "spectragent.export.cli",
        "Export recorded runs as training data. Supports --json.",
    ),
    (
        "flywheel",
        "spectragent.flywheel.cli",
        "Run a data chain, compute a report or diagnose replay. Supports --json.",
    ),
)


class _WithCapabilityFlags(TyperCommand):
    """A command that also takes flags every capability declares, one help panel per capability.

    The flags come from each capability package's ``declarations.py``, read
    when the command first parses or shows its help. Their values are kept in
    ``ctx.meta`` under :attr:`meta_key` for the command to read
    (:func:`_given_flags`), never passed as parameters: the command knows no
    capability's flag by name.
    """

    #: Where the given flags are kept in ``ctx.meta``.
    meta_key: ClassVar[str]
    #: What the flags are, for the help panels: ``start flags``.
    kind: ClassVar[str]
    _flags_added = False

    @staticmethod
    def declared() -> Mapping[str, Sequence[CapabilityFlag]]:
        """Each capability's flags of this kind, by ``capability_id``."""
        raise NotImplementedError

    @staticmethod
    def metavar(flag: CapabilityFlag) -> str:
        raise NotImplementedError

    def get_params(self, ctx: typer.Context) -> list[Parameter]:
        if not self._flags_added:
            from spectragent.core.run_cli import start_flag_help

            self._flags_added = True
            taken = {option for param in self.params for option in param.opts}
            declared = self.declared()
            for capability_id, flags in declared.items():
                for flag in flags:
                    if flag.flag in taken:
                        raise ValueError(
                            f"the {capability_id} capability declares {flag.flag} among its "
                            f"{self.kind}, and the {self.name} command already takes it"
                        )
            self.params.extend(
                TyperOption(
                    param_decls=[flag.flag],
                    metavar=self.metavar(flag),
                    help=start_flag_help(capability_id, flag),
                    expose_value=False,
                    callback=partial(_keep_flag, self.meta_key),
                    rich_help_panel=f"{capability_id} {self.kind} (refused without a "
                    f"{capability_id} capability)",
                )
                for capability_id, flags in declared.items()
                for flag in flags
            )
        return super().get_params(ctx)


class _WithStartFlags(_WithCapabilityFlags):
    """A command that also takes every capability's start flags (``docs/architecture_design.md`` §4.9)."""

    meta_key = _START_FLAGS
    kind = "start flags"

    @staticmethod
    def declared() -> Mapping[str, Sequence[CapabilityFlag]]:
        from spectragent.core.registries import start_flags

        return start_flags()

    @staticmethod
    def metavar(flag: CapabilityFlag) -> str:
        from spectragent.schemas.task import StartFlag

        assert isinstance(flag, StartFlag)
        return flag.key.upper()


class _WithTemplateFlags(_WithCapabilityFlags):
    """``config template``, which also takes every capability's template flags (§4.17)."""

    meta_key = _TEMPLATE_FLAGS
    kind = "template flags"

    @staticmethod
    def declared() -> Mapping[str, Sequence[CapabilityFlag]]:
        from spectragent.core.registries import template_flags

        return template_flags()

    @staticmethod
    def metavar(flag: CapabilityFlag) -> str:
        from spectragent.core.registries import TemplateFlag

        assert isinstance(flag, TemplateFlag)
        return flag.receptor.upper()


def _keep_flag(
    meta_key: str, ctx: typer.Context, param: Parameter, value: str | None
) -> str | None:
    if value is not None:
        ctx.meta.setdefault(meta_key, {})[param.opts[0]] = value
    return value


def _given_flags(ctx: typer.Context, meta_key: str = _START_FLAGS) -> dict[str, str]:
    """The capability flags this command was given, flag → value: its start flags unless told."""
    return dict(ctx.meta.get(meta_key, {}))


class _Leaf(TyperCommand):
    """A command run by a leaf module's own argparse parser, its one argument schema.

    Tab completion offers the options of that parser when the module builds it
    with ``build_parser()`` (``spectragent run``'s start flags among them).
    """

    def shell_complete(self, ctx: typer.Context, incomplete: str) -> list[CompletionItem]:
        module = next(module for name, module, _ in _LEAF_COMMANDS if name == self.name)
        build = getattr(importlib.import_module(module), "build_parser", None)
        if build is None:
            return super().shell_complete(ctx, incomplete)
        parser: argparse.ArgumentParser = build()
        mirror = TyperCommand(self.name)
        # argparse lists a parser's arguments only in ``_actions``; argcomplete reads it too.
        mirror.params.extend(
            TyperOption(param_decls=list(action.option_strings), help=action.help)
            for action in parser._actions
            if action.option_strings and action.help != argparse.SUPPRESS
        )
        return mirror.shell_complete(ctx, incomplete)


@dataclass
class Output:
    json: bool = False

    def emit(self, value: Any) -> None:
        if self.json:
            print(json.dumps(value, ensure_ascii=False, default=str))
            return
        console = Console()
        if isinstance(value, dict) and "checked" in value:
            _render_configuration(console, value)
            return
        if isinstance(value, dict) and "runs" in value and isinstance(value["runs"], list):
            rows = value["runs"]
            if all(isinstance(row, dict) and "key" in row for row in rows):
                table = Table(show_lines=False)
                for label in (
                    "Run",
                    "Agent",
                    "Model",
                    "Agent outcome",
                    "Environment",
                    "Trace",
                    "Verdicts",
                    "Steps",
                    "Tokens",
                ):
                    table.add_column(label)
                for row in rows:
                    verdicts = (
                        "unreadable"
                        if row.get("verdict_error")
                        else ", ".join(
                            f"{item['slug']}: {_verdict_word(item)}"
                            for item in row.get("verdicts", [])
                        )
                        or "none"
                    )
                    presented = {**row, "verdicts": verdicts}
                    table.add_row(
                        *[
                            Text(str(presented.get(key) if presented.get(key) is not None else "—"))
                            for key in (
                                "key",
                                "agent_id",
                                "model",
                                "agent_done",
                                "env_status",
                                "trace_status",
                                "verdicts",
                                "policy_steps",
                                "tokens",
                            )
                        ]
                    )
                console.print(table)
                if len(value) == 1:
                    return
        if isinstance(value, dict) and "output" in value:
            for line in value["output"]:
                console.print(Text(str(line)))
            if not value["output"] and isinstance(value.get("result"), dict):
                result = value["result"]
                recording = value.get("recording") or {}
                table = Table("Result", "Value")
                for label, item in (
                    ("Agent ending", result.get("agent_done")),
                    ("Environment", result.get("env_status")),
                    ("Recording", recording.get("status")),
                    ("Complete through", recording.get("complete_through")),
                    ("Recording detail", recording.get("reason")),
                    ("Verdicts", "; ".join(value.get("verdicts") or []) or "none"),
                    ("Trace", value.get("trace_dir")),
                    ("Answer", result.get("answer")),
                    ("Error", result.get("error")),
                ):
                    if item is not None and item != "":
                        table.add_row(label, Text(str(item)))
                console.print(table)
            if value.get("error"):
                Console(stderr=True).print(Text(json.dumps(value["error"], ensure_ascii=False)))
        else:
            console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _verdict_word(item: dict[str, Any]) -> str:
    """One verdict of a run summary in a word: passed, failed, its score, or not judged."""
    if item["status"] != "judged":
        return "not judged"
    if item["label"] is not None:
        return "passed" if item["label"] == 1 else "failed"
    return f"score {item['score']}"


def _output(ctx: typer.Context, json_output: bool = False) -> Output:
    inherited = ctx.find_root().obj
    return Output(json=json_output or (isinstance(inherited, Output) and inherited.json))


def _validation_error(exc: Exception) -> dict[str, Any]:
    from spectragent.cli.configuration_errors import ConfigInspectionError, contract_details
    from spectragent.core.composition_evidence import safe_error_message

    if isinstance(exc, ConfigInspectionError):
        return {
            **_validation_error(exc.cause),
            **contract_details(exc.cause),
            "stage": exc.stage,
        }
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        return {
            "type": type(exc).__name__,
            "message": "Invalid YAML syntax",
            "line": mark.line + 1 if mark is not None else None,
            "column": mark.column + 1 if mark is not None else None,
        }
    if isinstance(exc, ValidationError):
        # Pydantic's normal string representation includes input values.
        return {
            "type": type(exc).__name__,
            "issues": [
                {
                    "location": list(issue["loc"]),
                    "type": issue["type"],
                    "message": safe_error_message(issue),
                }
                for issue in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            ],
        }
    return {"type": type(exc).__name__, "message": str(exc)}


def _render_configuration(console: Console, value: dict[str, Any]) -> None:
    console.print(Text(f"Checked: {value['checked']} · no environment or model launched"))
    generated = value.get("generated", {})
    if generated:
        console.print(Text(f"Generated configuration: {generated.get('status', 'unknown')}"))
    if generated.get("remedy"):
        console.print(Text(f"Regenerate with: {generated['remedy']}"), style="yellow")
    if "components" not in value:
        return
    tree = Tree("Configured components")
    for layer, entries in value["components"].items():
        branch = tree.add(Text(f"{layer} ({len(entries)})"))
        for entry in entries[:5]:
            branch.add(Text(f"{entry['instance']} → {entry['implementation']}"))
        if len(entries) > 5:
            branch.add(Text(f"… {len(entries) - 5} more; --json includes every instance"))
    console.print(tree)
    from spectragent.cli.configuration_provenance import render_provenance

    render_provenance(console, value.get("provenance") or {})
    negotiation = value.get("negotiation") or {}
    for label, field in (
        ("Active observations", "active_fragments"),
        ("Required observations", "required_fragments"),
        ("Guarded actions", "active_actions"),
        ("Recorded environment evidence", "active_archivers"),
    ):
        console.print(Text(f"{label}: {', '.join(negotiation.get(field, [])) or 'none'}"))
    table = Table("Hook", "Blocking", "Interceptions")
    for hook in value.get("hooks", []):
        points = ", ".join(
            f"{point['advice']} {point['role']}.{point['method']}" for point in hook["intercepts"]
        )
        table.add_row(hook["hook_id"], str(hook["blocking"]), points)
    console.print(table)
    console.print(
        Text(
            "Use --json for field inputs, declaration/MRO origins, resolved fields and fingerprints."
        )
    )


def _checked(output: Output, operation: Callable[[], Any]) -> None:
    try:
        output.emit(operation())
    except Exception as exc:
        error = _validation_error(exc)
        if output.json:
            output.emit({"error": error, "exit_code": 2})
        else:
            Console(stderr=True).print(Text(json.dumps(error, ensure_ascii=False)))
        raise typer.Exit(2) from None


def _delegate(ctx: typer.Context, name: str, module: str) -> None:
    args = list(ctx.args)
    json_output = "--json" in args
    use_tui = "--tui" in args
    args = [arg for arg in args if arg not in ("--json", "--tui")]
    output = _output(ctx, json_output)
    if use_tui and (name not in ("run", "bench") or output.json):
        raise typer.BadParameter(
            "--tui is available for run/bench and cannot be combined with --json"
        )
    if use_tui and not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise typer.BadParameter("--tui requires an interactive terminal")
    entry = importlib.import_module(module).main
    captured = io.StringIO()
    result: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}
    if name in ("run", "bench") and "--help" not in args and "-h" not in args:
        from spectragent.monitoring import ProgressState

        state = ProgressState()
        kwargs.update(progress=state, report=result.update)
        if use_tui:
            kwargs["execute"] = lambda awaitable: _display_operation(awaitable, state, tui=True)
        elif not output.json:
            kwargs["execute"] = lambda awaitable: _display_operation(awaitable, state, tui=False)
    try:
        # Leaf parsers remain the single argument schema and execution path.
        # Their stdout is a result; Python logging continues to stderr.
        with redirect_stdout(captured):
            code = entry(args, **kwargs)
    except SystemExit as exc:
        code = int(exc.code or 0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        code = 130
        result["interrupted"] = True
    except Exception as exc:
        code = 2 if isinstance(exc, (ValidationError, ValueError, OSError)) else 1
        result["error"] = _validation_error(exc)
    payload = {
        "command": name,
        "exit_code": code,
        **result,
        "output": captured.getvalue().splitlines(),
    }
    if code == 0 and any(flag in args for flag in ("--help", "-h")):
        payload["output"].extend(
            [
                "",
                "Display options:",
                "  --json  Write one JSON result to stdout; logs stay on stderr.",
            ]
        )
        if name in ("run", "bench"):
            payload["output"].append(
                "  --tui   Interactive terminal monitor; cannot be combined with --json."
            )
    output.emit(payload)
    raise typer.Exit(code)


def _display_operation(awaitable: Any, state: Any, *, tui: bool) -> Any:
    if tui:
        from spectragent.tui import run_operation as run_display
    else:
        from spectragent.monitoring.terminal import run_with_progress as run_display
    try:
        return run_display(lambda: awaitable, state)
    finally:
        # A terminal may be closed before its operation task first runs.
        # The boundary already owns this coroutine; close an unstarted one
        # too, rather than leave it to emit an unawaited-coroutine warning.
        if inspect.iscoroutine(awaitable):
            awaitable.close()


def create_app() -> typer.Typer:
    app = typer.Typer(
        help="Run, inspect, evaluate and compare recorded agent experiments.",
        no_args_is_help=True,
        pretty_exceptions_enable=False,
    )

    @app.callback()
    def options(
        ctx: typer.Context,
        json_output: bool = typer.Option(
            False, "--json", help="Write one JSON result to stdout; logs stay on stderr."
        ),
    ) -> None:
        ctx.obj = Output(json=json_output)

    config_app = typer.Typer(
        help="Validate, inspect and generate composed configuration.", no_args_is_help=True
    )
    components_app = typer.Typer(
        help="Discover registered framework components.", no_args_is_help=True
    )
    runs_app = typer.Typer(
        help="Read committed runs without starting agents or models.", no_args_is_help=True
    )
    app.add_typer(config_app, name="config")
    app.add_typer(components_app, name="components")
    app.add_typer(runs_app, name="runs")

    def config_operation(
        ctx: typer.Context,
        config: Path,
        agent_id: str | None,
        env_id: str | None,
        benchmark: str | None,
        subset: str | None,
        task_id: str | None,
        json_output: bool,
        detailed: bool,
    ) -> None:
        from spectragent.cli.configuration import explain_config

        start = _given_flags(ctx)

        def inspect() -> dict[str, Any]:
            data = explain_config(
                config,
                agent_id=agent_id,
                env_id=env_id,
                start=start,
                benchmark=benchmark,
                subset=subset,
                task_id=task_id,
            )
            if not detailed:
                data = {
                    key: data[key]
                    for key in (
                        "checked",
                        "launched",
                        "agent_instance",
                        "resolved_config_sha256",
                        "generated",
                    )
                }
            return data

        _checked(_output(ctx, json_output), inspect)

    benchmark_help = (
        "A benchmark (shipped alias or benchmark YAML path), composed with the configuration "
        "as a run composes it; alone, it prepares with no task context."
    )
    task_id_help = "Prepare with this task of --benchmark: its environment and loop inputs."
    subset_help = (
        "The subset of --benchmark, written into its task loader before the configuration is "
        "composed, as bench and run write it."
    )

    @config_app.command("check", cls=_WithStartFlags)
    def config_check(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        agent_id: str | None = typer.Option(None),
        env_id: str | None = typer.Option(None),
        benchmark: str | None = typer.Option(None, help=benchmark_help),
        subset: str | None = typer.Option(None, help=subset_help),
        task_id: str | None = typer.Option(None, help=task_id_help),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Validate YAML and prepare an agent, without launching a browser or model."""
        config_operation(
            ctx, config, agent_id, env_id, benchmark, subset, task_id, json_output, False
        )

    @config_app.command("explain", cls=_WithStartFlags)
    def config_explain(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        agent_id: str | None = typer.Option(None),
        env_id: str | None = typer.Option(None),
        benchmark: str | None = typer.Option(None, help=benchmark_help),
        subset: str | None = typer.Option(None, help=subset_help),
        task_id: str | None = typer.Option(None, help=task_id_help),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Explain inputs, declaration/MRO origins, injection, negotiations and fingerprints."""
        config_operation(
            ctx, config, agent_id, env_id, benchmark, subset, task_id, json_output, True
        )

    @config_app.command("generate")
    def config_generate(
        ctx: typer.Context, json_output: bool = typer.Option(False, "--json")
    ) -> None:
        """Regenerate composed configuration and registry fingerprints."""
        from spectragent.scripts.generate_all import main as generate

        stream = io.StringIO()
        with redirect_stdout(stream):
            code = generate([])
        _output(ctx, json_output).emit(
            {"exit_code": code, "output": stream.getvalue().splitlines()}
        )
        raise typer.Exit(code)

    @config_app.command("template", cls=_WithTemplateFlags)
    def config_template(
        ctx: typer.Context,
        source: Annotated[
            Path | None,
            typer.Option(
                "--from",
                exists=True,
                dir_okay=False,
                help="Expand an existing YAML selection, preserving values unless a flag replaces a component.",
            ),
        ] = None,
        agent: str | None = typer.Option(
            None, help="Agent registry key; new templates use browser_use.dom."
        ),
        loop: str | None = typer.Option(None, help="Loop registry key; new templates use react."),
        llm: str | None = typer.Option(None, help="Full LLM registry key, e.g. openai.direct."),
        model: str | None = typer.Option(None, help="Model name; required for a new template."),
        environment: str | None = typer.Option(
            None,
            help="Environment registry key; new templates use the one the agent's wiring "
            "requires, and need this flag when its wiring can run in several.",
        ),
        benchmark: str | None = typer.Option(
            None,
            help="A benchmark (shipped alias or benchmark YAML path), composed with the template "
            "as a run composes it; the capability settings it supplies are left out of the YAML.",
        ),
        record: Annotated[
            list[str] | None,
            typer.Option(
                "--record",
                help="Repeat for metrics, errors, trajectory, observations; or use all/none for these four channels. New templates use metrics.",
            ),
        ] = None,
        output_path: Annotated[
            Path | None,
            typer.Option(
                "--output",
                help="Write a new YAML file; by default write YAML to stdout. Existing files are refused.",
            ),
        ] = None,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Write a fully expanded, commented YAML template without launching anything.

        Without --from, select BrowserUse (or --agent) with ReAct, openai.direct and
        simple memory, the environment the agent's wiring requires (or --environment)
        with each capability's declared starting choices, no environment archive, and
        metrics only. Supply --model; no model is guessed. --from supports any valid
        run skeleton, including archive/branch configs. With --benchmark, the settings
        the benchmark supplies are left out. Required missing fields are errors.
        Task-specific checks use config check.
        """
        from spectragent.cli.config_templates import TemplateChoices, render_config_template

        output = _output(ctx, json_output)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8")) if source else None
            if source is not None and not isinstance(raw, dict):
                raise ValueError("The configuration must be a YAML mapping")
            template = render_config_template(
                TemplateChoices(
                    agent=agent,
                    loop=loop,
                    llm=llm,
                    model=model,
                    environment=environment,
                    recording=tuple(record) if record is not None else None,
                    capability_flags=_given_flags(ctx, _TEMPLATE_FLAGS),
                    benchmark=benchmark,
                ),
                raw,
            )
            if output_path is not None:
                with output_path.open("x", encoding="utf-8") as stream:
                    stream.write(template)
            if output.json:
                output.emit(
                    {"yaml": template, "output_path": str(output_path) if output_path else None}
                )
            elif output_path is None:
                sys.stdout.write(template)
            else:
                Console(stderr=True).print(Text(f"Wrote {output_path}"))
        except Exception as exc:
            error = _validation_error(exc)
            if output.json:
                output.emit({"error": error, "exit_code": 2})
            else:
                Console(stderr=True).print(Text(json.dumps(error, ensure_ascii=False)))
            raise typer.Exit(2) from None

    @components_app.command("list")
    def components_list(
        ctx: typer.Context,
        layer: str | None = typer.Option(None),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from spectragent.cli.configuration import list_components

        _checked(_output(ctx, json_output), lambda: list_components(layer))

    @runs_app.command("list")
    def runs_list(
        ctx: typer.Context,
        root: Annotated[Path, typer.Option()] = Path("traces"),
        search: str = typer.Option(""),
        config: Annotated[
            Path | None,
            typer.Option(
                "--config",
                help="Only runs launched from this source file, across all versions; the file need not exist.",
            ),
        ] = None,
        config_hash: str | None = typer.Option(
            None, help="Exact recorded effective configuration hash."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from spectragent.inspection.repository import open_repository

        _checked(
            _output(ctx, json_output),
            lambda: {
                "runs": open_repository(
                    root, config_source=config, config_hash=config_hash
                ).list_runs(search=search)
            },
        )

    @runs_app.command("show")
    def runs_show(
        ctx: typer.Context,
        key: Annotated[str, typer.Argument(help="Run path relative to --root.")],
        root: Annotated[Path, typer.Option()] = Path("traces"),
        step: int | None = typer.Option(None, min=0),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from spectragent.inspection.repository import open_repository

        _checked(
            _output(ctx, json_output),
            lambda: (
                open_repository(root).run(key)
                if step is None
                else open_repository(root).step(key, step)
            ),
        )

    @runs_app.command("compare")
    def runs_compare(
        ctx: typer.Context,
        keys: Annotated[list[str], typer.Argument(help="Run paths relative to --root.")],
        root: Annotated[Path, typer.Option()] = Path("traces"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from spectragent.inspection.repository import open_repository

        if len(keys) < 2:
            raise typer.BadParameter("Provide at least two run keys")
        _checked(_output(ctx, json_output), lambda: open_repository(root).compare(keys))

    @runs_app.command("compare-groups")
    def runs_compare_groups(
        ctx: typer.Context,
        left: str = typer.Option(..., help="Left configuration hash."),
        right: str = typer.Option(..., help="Right configuration hash."),
        verifier: str = typer.Option(
            ...,
            help="The verdict slug both groups were judged by (the same verifier and settings).",
        ),
        root: Annotated[Path, typer.Option()] = Path("traces"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Compare configuration groups on their common judged tasks."""
        from spectragent.inspection.repository import open_repository

        _checked(
            _output(ctx, json_output),
            lambda: open_repository(root).compare_groups(left, right, verifier),
        )

    @app.command("view")
    def view(
        root: Annotated[Path, typer.Option()] = Path("traces"),
        config: Annotated[
            Path | None,
            typer.Option(
                "--config",
                help="Filter this root by source YAML path, including historical versions.",
            ),
        ] = None,
        config_hash: str | None = typer.Option(
            None, help="Filter by the recorded effective configuration hash."
        ),
        initial_run: str | None = typer.Option(
            None, "--run", help="Initially open this run path relative to --root."
        ),
        host: str = typer.Option("127.0.0.1"),
        port: int = typer.Option(8765, min=1, max=65535),
        open_browser: bool = typer.Option(False, "--open"),
        annotations: Annotated[
            Path | None,
            typer.Option(
                "--annotations",
                help="Label review queues and steps; labels are appended in this directory only.",
            ),
        ] = None,
        annotator: str | None = typer.Option(
            None, help="Name recorded on every label (default: your OS user name)."
        ),
    ) -> None:
        """Serve a local result viewer; read-only unless --annotations is given."""
        from spectragent.viewer.server import serve

        if annotator is not None and annotations is None:
            raise typer.BadParameter("--annotator needs --annotations DIR")
        serve(
            root,
            host=host,
            port=port,
            open_browser=open_browser,
            initial_run=initial_run,
            config_source=config,
            config_hash=config_hash,
            annotations=annotations,
            annotator=annotator,
        )

    @app.command("tui")
    def tui(
        root: Annotated[Path, typer.Option()] = Path("traces"),
        config: Annotated[
            Path | None,
            typer.Option(
                "--config",
                help="Filter this root by source YAML path, including historical versions.",
            ),
        ] = None,
        config_hash: str | None = typer.Option(
            None, help="Filter by the recorded effective configuration hash."
        ),
        initial_run: str | None = typer.Option(
            None, "--run", help="Initially open this run path relative to --root."
        ),
    ) -> None:
        """Browse existing runs in an interactive terminal."""
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise typer.BadParameter("tui requires an interactive terminal")
        from spectragent.tui import browse

        browse(root, initial_run=initial_run, config_source=config, config_hash=config_hash)

    for name, module, help_text in _LEAF_COMMANDS:

        def register(command_name: str, module_name: str, description: str) -> None:
            @app.command(
                command_name,
                cls=_Leaf,
                help=description,
                context_settings={
                    "allow_extra_args": True,
                    "ignore_unknown_options": True,
                    "help_option_names": [],
                },
            )
            def leaf(ctx: typer.Context) -> None:
                _delegate(ctx, command_name, module_name)

        register(name, module, help_text)
    return app


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        result = create_app()(args=arguments, prog_name="spectragent", standalone_mode=False)
        return int(result or 0)
    except typer.Exit as exc:
        return exc.exit_code
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except Exception as exc:
        code = getattr(exc, "exit_code", 2 if isinstance(exc, (ValueError, OSError)) else 1)
        error = _validation_error(exc)
        if "--json" in arguments:
            Output(json=True).emit({"error": error, "exit_code": code})
        else:
            Console(stderr=True).print(Text(json.dumps(error, ensure_ascii=False)))
        return code
