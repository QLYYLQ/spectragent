"""Data loader infrastructure — task and eval registries."""

from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar

BROWSER_REGISTRARS = {
    "task_loader": TaskLoaderRegistrar,
    "eval_loader": EvalLoaderRegistrar,
}

__all__ = [
    "BROWSER_REGISTRARS",
    "EvalLoaderRegistrar",
    "TaskLoaderRegistrar",
]
