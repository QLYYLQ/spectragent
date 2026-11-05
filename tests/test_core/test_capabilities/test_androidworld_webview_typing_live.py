"""Typing with ``set_text`` into MiniWoB++ forms, a WebView's pages, on a real emulator.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). AndroidWorld's service prepares each task, and
``type`` by index types as M3A's ``input_text`` does, ``clear`` false, its tap
and its text in one action (``docs/architecture_design.md`` §11.2 "动作"):

- ``email-inbox-forward``: ``click`` by index opens the email its goal names and
  that email's forward form, whose two text fields are "to:" and the body.
  Text goes into "to:" while the WebView itself has focus; into "to:" again,
  the field that has focus, with the keyboard up, at its cursor; into the
  body, then back into "to:", replacing its text: each into the field its tap
  gave focus;
- ``enter-password``: text goes at the cursor of an empty password field,
  which reports its cursor at 0;
- ``book-flight``: after text went into the "To" field, typing into the
  departure date, jQuery's ``<input readonly>``, is refused. The service names
  the date field as focused before the input method drops the "To" field's
  connection, and ``key_events`` once typed into that gap (2026-11-04).

After such taps the service's ``findFocus`` names the WebView itself, not the
page's field, so ``set_text`` refused most of them with ``no_focused_field``
until the service looked for the field in the window's tree (40 of the 78
typing actions of the MiniWoB sweep of 2026-11-03), and refused every password
field without ``clear`` (``password_field``). Each test boots its own device.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.effects import AndroidTextEffect
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.android_runs import (
    androidworld_capability,
    androidworld_setup,
    bind_androidworld,
    installation_problem,
    write_task_list,
)

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]


@dataclass
class Prepared:
    capability: AndroidCapability
    cycle: int = 0


@asynccontextmanager
async def _prepared(data: Path, task: str) -> AsyncIterator[Prepared]:
    """The device with MiniWoB++ *task* prepared on it by AndroidWorld's service."""
    write_task_list(data, task, family="miniwob")
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{task}/42/0")
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    bind_androidworld(env, context.env, setup)
    await env.setup(context.env)
    try:
        yield Prepared(capability=capability)
    finally:
        await env.teardown()


async def _tree(prepared: Prepared) -> AndroidUITreeObs:
    prepared.cycle += 1
    await prepared.capability.begin_observation(prepared.cycle)
    tree = await prepared.capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return tree


async def _click(prepared: Prepared, text: str) -> None:
    """Tap the element whose text is *text*, by its index in a fresh tree."""
    tree = await _tree(prepared)
    for index, node in tree.tree().element_nodes():
        if node.text == text:
            tapped = await prepared.capability.execute_action(
                Action(name="android.click", params={"index": index})
            )
            assert tapped.success, tapped.error
            return
    raise AssertionError(f"no element {text!r}; the tree reads:\n{tree.serialized}")


async def _type(prepared: Prepared, index: int, text: str, *, clear: bool) -> ActionResult:
    typed = await prepared.capability.execute_action(
        Action(name="android.type", params={"index": index, "text": text, "clear": clear})
    )
    assert isinstance(typed.effect, AndroidTextEffect)
    return typed


async def _index_of(prepared: Prepared, view_id: str) -> int:
    """The index of the element whose view id is *view_id*, in a fresh tree."""
    tree = await _tree(prepared)
    for index, node in tree.tree().element_nodes():
        if node.view_id_resource_name == view_id:
            return index
    raise AssertionError(f"no element {view_id!r}; the tree reads:\n{tree.serialized}")


async def _fields(prepared: Prepared) -> dict[str, tuple[int, str]]:
    """The forward form's "to:" field and its body: each one's index in a fresh tree, and its text."""
    tree = await _tree(prepared)
    nodes = list(tree.tree().element_nodes())
    label = next(n for n, (_, node) in enumerate(nodes) if (node.text or "").startswith("to:"))
    to, body = [(index, node.text or "") for index, node in nodes[label:] if node.is_editable][:2]
    return {"to": to, "body": body}


async def _type_into(prepared: Prepared, field: str, text: str, *, clear: bool) -> ActionResult:
    return await _type(prepared, (await _fields(prepared))[field][0], text, clear=clear)


async def test_type_by_index_goes_into_the_field_its_tap_gave_focus(tmp_path: Path) -> None:
    async with _prepared(tmp_path, "email-inbox-forward") as form:
        tree = await _tree(form)
        goal = next(
            node.text
            for _, node in tree.tree().element_nodes()
            if node.text and re.search(r" by \w+ .* to ", node.text)
        )
        sender = re.search(r" by (\w+)", goal)
        assert sender is not None, goal
        await _click(form, sender[1])
        await _click(form, "Forward")
        body = (await _fields(form))["body"][1]

        first = await _type_into(form, "to", "Dor", clear=False)
        again = await _type_into(form, "to", "otea", clear=False)
        assert first.success, first.error
        assert again.success, again.error
        assert (await _fields(form))["to"][1] == "Dorotea"
        into_body = await _type_into(form, "body", "Hi", clear=False)
        back = await _type_into(form, "to", "Nollie", clear=True)
        fields = await _fields(form)

    assert into_body.success, into_body.error
    assert back.success, back.error
    assert fields["to"][1] == "Nollie"
    assert "Hi" in fields["body"][1] and len(fields["body"][1]) == len(body) + 2


async def test_type_by_index_goes_at_the_cursor_of_an_empty_password_field(
    tmp_path: Path,
) -> None:
    async with _prepared(tmp_path, "enter-password") as page:
        tree = await _tree(page)
        index = next(i for i, node in tree.tree().element_nodes() if node.is_password)

        typed = await _type(page, index, "xSOnT", clear=False)
        after = await _tree(page)

    assert typed.success, typed.error
    passwords = [node.text for _, node in after.tree().element_nodes() if node.is_password]
    assert len(passwords[0]) == 5, passwords


async def test_type_by_index_into_a_read_only_field_after_a_text_field_is_refused(
    tmp_path: Path,
) -> None:
    async with _prepared(tmp_path, "book-flight") as page:
        into_to = await _type(page, await _index_of(page, "flight-to"), "Qzx", clear=True)
        into_date = await _type(
            page, await _index_of(page, "datepicker"), "10/21/2016", clear=False
        )

    assert into_to.success, into_to.error
    assert not into_date.success
    assert into_date.error is not None and "not on a text field" in into_date.error
