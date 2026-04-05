"""Generate typed config models from browser data loader registries.

Usage:
    python spectragent/core/data_loader/generate_config.py
"""

import sys

from conscribe import discover, generate_layer_config_source

from spectragent import GENERATED_DIR
from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar


def main() -> None:
    # 1. Discover all loader modules to trigger registration
    task_mods = discover("spectragent.core.data_loader.task")
    eval_mods = discover("spectragent.core.data_loader.eval")
    print(f"Discovered {len(task_mods)} task modules, {len(eval_mods)} eval modules")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Generate task loader config
    task_result = TaskLoaderRegistrar.build_config()
    task_keys = TaskLoaderRegistrar.keys()
    print(f"Task loaders ({len(task_keys)}): {task_keys}")

    task_source = generate_layer_config_source(task_result)
    task_path = GENERATED_DIR / "task_loader_config.py"
    task_path.write_text(task_source)
    print(f"Wrote {task_path}")

    # 3. Generate eval loader config
    eval_result = EvalLoaderRegistrar.build_config()
    eval_keys = EvalLoaderRegistrar.keys()
    print(f"Eval loaders ({len(eval_keys)}): {eval_keys}")

    eval_source = generate_layer_config_source(eval_result)
    eval_path = GENERATED_DIR / "eval_loader_config.py"
    eval_path.write_text(eval_source)
    print(f"Wrote {eval_path}")

    # 4. Verify consistency
    if set(task_keys) != set(eval_keys):
        print(
            f"WARNING: registry keys mismatch! task={task_keys}, eval={eval_keys}",
            file=sys.stderr,
        )
        sys.exit(1)

    expected = {"browsecomp", "lexbench_browser", "online_mind2web"}
    if set(task_keys) != expected:
        print(
            f"WARNING: expected {expected}, got {set(task_keys)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
