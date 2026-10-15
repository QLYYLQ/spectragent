"""One discoverable command tree and one output contract for experiments."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


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
                    "Evaluation",
                    "Steps",
                    "Tokens",
                ):
                    table.add_column(label)
                for row in rows:
                    verdicts = row.get("verdicts", [])
                    evaluation = (
                        "unreadable"
                        if row.get("evaluation_error")
                        else ", ".join(
                            f"{item['evaluator']}: "
                            + ("passed" if item["predicted_label"] == 1 else "failed")
                            if item["verdict_source"] == "graded"
                            else f"{item['evaluator']}: {item['verdict_source']}"
                            for item in verdicts
                        )
                        or "not evaluated"
                    )
                    presented = {**row, "evaluation": evaluation}
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
                                "evaluation",
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
                    ("Evaluation", value.get("evaluation")),
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


def _output(ctx: typer.Context, json_output: bool = False) -> Output:
    inherited = ctx.find_root().obj
    return Output(json=json_output or (isinstance(inherited, Output) and inherited.json))


def _validation_error(exc: Exception) -> dict[str, Any]:
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
                    "message": issue["msg"]
                    if issue["type"] not in ("value_error", "assertion_error", "union_tag_invalid")
                    else "The value does not satisfy the declared configuration contract",
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
        Text("Use --json for resolved fields, action parameter contracts and fingerprints.")
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
        start_url: str | None,
        replay_archive: str | None,
        task_id: str | None,
        json_output: bool,
        detailed: bool,
    ) -> None:
        from spectragent.cli.configuration import explain_config

        def inspect() -> dict[str, Any]:
            data = explain_config(
                config,
                agent_id=agent_id,
                env_id=env_id,
                start_url=start_url,
                replay_archive=replay_archive,
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

    @config_app.command("check")
    def config_check(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        agent_id: str | None = typer.Option(None),
        env_id: str | None = typer.Option(None),
        start_url: str | None = typer.Option(None),
        replay_archive: str | None = typer.Option(None),
        task_id: str | None = typer.Option(
            None, help="Use this configured loader task's environment and loop inputs."
        ),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Validate YAML and prepare an agent, without launching a browser or model."""
        config_operation(
            ctx, config, agent_id, env_id, start_url, replay_archive, task_id, json_output, False
        )

    @config_app.command("explain")
    def config_explain(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        agent_id: str | None = typer.Option(None),
        env_id: str | None = typer.Option(None),
        start_url: str | None = typer.Option(None),
        replay_archive: str | None = typer.Option(None),
        task_id: str | None = typer.Option(None),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show redacted resolved fields, component identities, negotiations and fingerprints."""
        config_operation(
            ctx, config, agent_id, env_id, start_url, replay_archive, task_id, json_output, True
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
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from spectragent.inspection.repository import open_repository

        _checked(
            _output(ctx, json_output),
            lambda: {"runs": open_repository(root).list_runs(search=search)},
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
        evaluator: str = typer.Option(..., help="Exact evaluator slug used on both groups."),
        root: Annotated[Path, typer.Option()] = Path("traces"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Compare configuration groups on their common evaluated tasks."""
        from spectragent.inspection.repository import open_repository

        _checked(
            _output(ctx, json_output),
            lambda: open_repository(root).compare_groups(left, right, evaluator),
        )

    @app.command("view")
    def view(
        root: Annotated[Path, typer.Option()] = Path("traces"),
        host: str = typer.Option("127.0.0.1"),
        port: int = typer.Option(8765, min=1, max=65535),
        open_browser: bool = typer.Option(False, "--open"),
    ) -> None:
        """Serve a local, read-only result viewer."""
        from spectragent.viewer.server import serve

        serve(root, host=host, port=port, open_browser=open_browser)

    @app.command("tui")
    def tui(root: Annotated[Path, typer.Option()] = Path("traces")) -> None:
        """Browse existing runs in an interactive terminal."""
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise typer.BadParameter("tui requires an interactive terminal")
        from spectragent.tui import browse

        browse(root)

    leaf_commands = (
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
            "spectragent.evaluator.cli",
            "Evaluate recorded runs independently. Supports --json.",
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
    for name, module, help_text in leaf_commands:

        def register(command_name: str, module_name: str, description: str) -> None:
            @app.command(
                command_name,
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
