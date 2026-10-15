"""The generated config surface must actually import.

`spectragent/generated/__init__.py` guards its import so that
``discover()`` survives the window where ``spectragent config generate`` has wiped
``_composed.py`` and not yet rewritten it. That guard used to be a bare
``except Exception``, which turned a genuine codegen defect into a silent
``ComposedConfig is None`` — the committed artifact raised
``NameError: name 'AwsProviderConfig' is not defined`` at import and
nothing noticed, because every consumer just saw ``None``.

These tests are the tripwire: if codegen emits something unimportable
again, this fails loudly instead of degrading.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import BaseModel


def test_composed_module_imports_cleanly() -> None:
    """The heavy artifact imports without raising."""
    module = importlib.import_module("spectragent.generated._composed")
    assert module.ComposedConfig is not None


def test_composed_config_is_exported_and_is_a_model() -> None:
    """The public surface is a real model, not the ``None`` fallback."""
    from spectragent.generated import ComposedConfig

    assert ComposedConfig is not None, (
        "ComposedConfig degraded to None — _composed.py failed to import. "
        "Run `uv run python -m spectragent.scripts.generate_all`."
    )
    assert issubclass(ComposedConfig, BaseModel)


def test_every_layer_the_composer_needs_is_present() -> None:
    """Each registrar layer must have a field on the top-level model."""
    from spectragent.core.registries import collect_registrars
    from spectragent.generated import ComposedConfig

    missing = set(collect_registrars()) - set(ComposedConfig.model_fields)
    assert not missing, f"generated config is stale — missing layers: {sorted(missing)}"


def test_guard_only_swallows_a_missing_module() -> None:
    """A codegen defect must propagate, not degrade to ``None``.

    Narrowing the guard to ``ImportError`` is the whole point: the
    wipe-and-regenerate window still works, everything else surfaces.
    """
    import ast
    import pathlib

    import spectragent.generated as generated_pkg

    # Inspect the handler in the AST, not the raw text — the module's own
    # docstring mentions ``except Exception`` while explaining why it is
    # gone, so a substring scan would report the opposite of the truth.
    source = pathlib.Path(generated_pkg.__file__).read_text()
    handlers = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "the bootstrap guard disappeared"
    caught = {h.type.id for h in handlers if isinstance(h.type, ast.Name)}
    assert caught == {"ImportError"}, f"guard widened back out to {sorted(caught)}"

    assert generated_pkg.ComposedConfig is not None


@pytest.mark.parametrize("name", ["ComposedConfig"])
def test_public_names_resolve(name: str) -> None:
    import spectragent.generated as generated_pkg

    assert getattr(generated_pkg, name, None) is not None
