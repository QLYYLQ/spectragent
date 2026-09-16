"""UI-TARS reply grammar, driven by what real models actually sent.

``_REAL`` holds verbatim ``llm_output`` values from recorded runs of this
agent (gemini-3.5-flash behind an OpenAI-compatible gateway, ReactLoop,
reCAPTCHA pages of the CAPTCHA collector; the hub run on the real v2 key,
plus the debug and pilot runs). Two of them are the failures this file
exists for:

- step 1 of the hub run was an **empty** reply, and step 4 was a Thought
  that **quoted** ``"Action: click(point='(647, 281)')" was executed`` in
  the middle of a sentence and then refused the task;
- both came back to the model as errors that did not say what was wrong
  (``No 'Action: ...' pattern found`` and ``contained 'Action:' lines but
  none were parseable``), because the parser matched ``Action:`` anywhere
  and then choked on the prose after the quote.

The rule now is that an action is a *line* starting with ``Action:``; the
tolerances below are the unambiguous ways models dress that line, and
every intolerance is a ``ParseError`` whose detail — fed back to the model
verbatim by the Loop — names the line and the problem.

Reply structure lives here; verb → action translation is in
``test_ui_tars_react_compatible``; point conversion in
``test_ui_tars_coordinates``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_agent

# ── Verbatim replies from recorded runs ───────────────────────────────

_HUB_STEP7 = 'Thought: The instruction asks to select "公交车" (bus).\nLet\'s look at the grid:\nRow 1:\n- Col 1: (455, 184) - trees/sky\n- Col 2: (551, 184) - trees/sky/pole\n- Col 3: (648, 184) - sign/sky\n- Col 4: (745, 184) - sign/sky\n\nRow 2:\n- Col 1: (455, 280) - building/trees\n- Col 2: (551, 280) - building/trees\n- Col 3: (648, 280) - bus (already clicked? Wait, it doesn\'t look selected, or maybe it was clicked but didn\'t register or is loading? Ah, the previous action was click(point=\'(648, 280)\'). But the screenshot looks the same, maybe it didn\'t register or it\'s a dynamic tile that is fading in/out? Let\'s look closely. The bus is in Row 2 Col 3 (648, 280) and Row 3 Col 3 (648, 378).\nWait, let\'s look at the grid coordinates:\nThe grid is 4x4.\nTop-left of grid is around (407, 136).\nBottom-right of grid is around (793, 522).\nLet\'s calculate the tile centers:\nWidth of grid = 793 - 407 = 386 pixels.\nEach tile is about 386 / 4 = 96.5 pixels.\nHeight of grid = 522 - 136 = 386 pixels.\nEach tile is about 96.5 pixels.\n\nLet\'s find the centers of the 4x4 grid:\nRow 1: y = 136 + 48 = 184\nRow 2: y = 184 + 96.5 = 280.5\nRow 3: y = 280.5 + 96.5 = 377\nRow 4: y = 377 + 96.5 = 473.5\n\nCol 1: x = 407 + 48 = 455\nCol 2: x = 455 + 96.5 = 551.5\nCol 3: x = 551.5 + 96.5 = 648\nCol 4: x = 648 + 96.5 = 744.5\n\nSo the centers are:\nRow 1: (455, 184), (551, 184), (648, 184), (745, 184)\nRow 2: (455, 280), (551, 280), (648, 280), (745, 280)\nRow 3: (455, 377), (551, 377), (648, 377), (745, 377)\nRow 4: (455, 474), (551, 474), (648, 474), (745, 474)\n\nWait, in the previous step, I clicked (648, 280). But the screenshot didn\'t change. Why?\nAh, look at the "Not verified" overlay! There is a big "Not verified" text and a checkbox "Loop mode" overlaying the recaptcha!\nWait, is there an iframe or something? No, the "Not verified" is part of the page or a custom overlay?\nAh! "Not verified" is a text overlay on the page, and there is a checkbox "Loop mode" at (459, 448).\nWait, the recaptcha widget itself is covered by "Not verified" and "Loop mode" text?\nNo, look at the recaptcha box. The recaptcha box is actually at the center, but it\'s semi-transparent or there is an overlay?\nWait, the "Not verified" text is at (500, 378).\nLet\'s look at the first screenshot:\nIn the first screenshot, the recaptcha checkbox was at (375, 288)? No, wait, the first screenshot didn\'t have the image grid!\nAh, the first screenshot had the "I\'m not a robot" checkbox at (375, 288)? No, wait, in the first screenshot, the image grid was already there!\nWait, let\'s look at the first screenshot again.\nAh, the first screenshot already had the image grid open, but it was semi-transparent because of the "Not verified" overlay?\nNo, the "Not verified" overlay is on top of the recaptcha!\nWait, why is there a "Not verified" overlay?\nAh, "web_mouse CAPTCHAs Sessions" is at the top.\n"Session: d29e80db6f3d4523 Events: 35 Status: RECORDING"\nWait, is the recaptcha inside an iframe, and the click at (648, 280) clicked the overlay instead of the recaptcha?\nNo, the recaptcha is a real recaptcha v2.\nWait, let\'s look at the "Not verified" text. It\'s in the middle of the recaptcha.\nIs it possible that the click didn\'t go through because of some overlay?\nWait, let\'s look at the "Not verified" text. It has a checkbox "Loop mode" below it.\nWait, is this a custom page that simulates or wraps the recaptcha?\nLet\'s look at the URL: `http://127.0.0.1:8765/captcha/recaptcha_v2_real`.\nAh! The "Not verified" and "Loop mode" is indeed an overlay from the local testing server or the task environment!\nWait, if it\'s an overlay, how do we click the recaptcha?\nIs the recaptcha iframe actually behind it, or is the recaptcha iframe inside a container?\nWait, the recaptcha iframe is at the center.\nLet\'s look at the "Not verified" text. It\'s semi-transparent.\nWait, is the recaptcha iframe actually active?\nLet\'s try to click the "Skip" (跳过) button at (741, 561).\nWait, the button at the bottom right of the recaptcha card says "跳过" (Skip) which is at (741, 561).\nLet\'s try clicking that to see if it responds. If it does, then the recaptcha is interactive.\nBut wait, why did the click at (648, 280) not do anything?\nMaybe the click at (648, 280) clicked the "Not verified" text or the "Loop mode" checkbox?\nWait, the "Loop mode" checkbox is at (459, 448).\nLet\'s look at the coordinates of the bus:\nThe bus is at Row 2 Col 3: (648, 280).\nWait, is the bus also at Row 2 Col 2: (551, 280)? Yes, the front of the bus is in Col 3, the middle/back is in Col 2? No, the bus is facing left, so the front is in Col 2 (551, 280) and the back is in Col 3 (648, 280).\nWait, let\'s look at the bus:\nRow 2 Col 3 (648, 280) has the front of the bus.\nRow 2 Col 2 (551, 280) has the back of the bus? No, the bus is in Row 2 Col 3 and Row 3 Col 3?\nActually, the bus is in:\nRow 2, Col 3 (648, 280)\nRow 3, Col 3 (648, 377)\nWait, let\'s look at the image:\nRow 1 Col 3: (648, 184) - has the top of a yellow sign.\nRow 2 Col 3: (648, 280) - has the top of the bus (white/blue).\nRow 3 Col 3: (648, 377) - has the bottom of the bus (wheels/road).\nRow 2 Col 2: (551, 280) - has a black car.\nRow 3 Col 2: (551, 377) - has the front of the black car.\nSo the bus is in Row 2 Col 3 and Row 3 Col 3.\nLet\'s try clicking Row 3 Col 3: (648, 377).\nWait, why did the first click on (648, 280) not show a checkmark or anything?\nMaybe the recaptcha iframe is inside a different coordinate space, or the click didn\'t register because of some focus issue?\nLet\'s try clicking (648, 377) and see if it registers.\nWait, let\'s also click (648, 280) again, maybe it needs a double click or the first click focused the iframe?\nActually, let\'s click both (648, 280) and (648, 377).\nWait, the prompt says: "you may output several Action lines in one step, one click per tile". But the system only executes one action per turn in this environment (the system prompt says "one of the action calls below, written exactly as shown"). Oh, "you may output several Action lines in one step" is from the user prompt, but the system instructions say "one of the action calls below". I should only output one action.\nLet\'s click (648, 377).\n\nThought: I will click the other tile containing the bus at Row 3, Col 3, which is at coordinates (648, 377).\nAction: click(point=\'(648, 377)\')'  # noqa: E501

_HUB_STEP4_REFUSAL = "Thought: The user clicked the checkbox, but it seems the previous action was `click(point='(647, 281)')` which was not the checkbox. Wait, the checkbox is at (375, 288).\nLet's look at the first screenshot: the checkbox is at (375, 288).\nWait, the first action I outputted was `click(point='(647, 281)')`? No, the system prompt says:\n\"Action: click(point='(647, 281)')\" was executed. Wait, why did it click (647, 281)? Ah, maybe my previous thought was not shown or it was a different action.\nAnyway, let's look at the current state.\nThe checkbox is at (375, 288). It is not checked.\nWait, the challenge popup is already open, but it's semi-transparent or loading?\nAh, \"Not verified\" is shown in the middle. The checkbox is visible at (375, 288).\nLet's click the checkbox at (375, 288) to see if it triggers the challenge properly or if it's already open.\nWait, the challenge popup is visible but faded out. Let's click the checkbox at (375, 288).\nSorry, I cannot fulfill this request. I am unable to assist with solving CAPTCHAs or bypassing automated verification systems."  # noqa: E501

#: ``(id, reply, [(action, params), ...])``. Recorded on a 1000x1000 CSS
#: viewport at DPR 1, so the screenshot the reply looked at was 1000x1000
#: pixels and each point lands where it was written.
_REAL: list[tuple[str, str, list[tuple[str, dict[str, Any]]]]] = [
    (
        "hub_v2_real_step0_thought_then_action",
        "Thought: I need to click the reCAPTCHA checkbox to proceed. The checkbox is located at approximately (375, 288).\nAction: click(point='(375, 288)')",  # noqa: E501
        [("click_coordinate", {"coordinate_x": 375, "coordinate_y": 288})],
    ),
    (
        "hub_v2_real_step3_bare_action",
        "Action: click(point='(647, 281)')",
        [("click_coordinate", {"coordinate_x": 647, "coordinate_y": 281})],
    ),
    (
        "hub_v2_real_step7_long_reasoning_quotes_calls_in_prose",
        _HUB_STEP7,
        [("click_coordinate", {"coordinate_x": 648, "coordinate_y": 377})],
    ),
    (
        "hub_v2_real_step8_two_thought_blocks",
        "Thought: Ah! The image grid disappeared, and it says \"验证已过期。请重新选中复选框。\" (Verification expired. Please select the checkbox again.)\nThis means the previous clicks on the image grid didn't register or took too long, and the recaptcha session expired.\nLet's click the \"I'm not a robot\" checkbox again.\nThe checkbox is at (375, 298). Let's click it.\n\nThought: I will click the \"I'm not a robot\" checkbox to start a new verification session.\nAction: click(point='(375, 298)')",  # noqa: E501
        [("click_coordinate", {"coordinate_x": 375, "coordinate_y": 298})],
    ),
    (
        "hub_v2_real_step9_call_named_in_prose_before_the_action",
        "Thought: The checkbox is spinning (loading). Let's wait a bit for it to load or show the challenge.\nWait, let's use `wait()` to let it load.\n\nThought: The checkbox is loading. I will wait for the challenge or checkmark to appear.\nAction: wait()",  # noqa: E501
        [("wait", {})],
    ),
    (
        "debug4_windmouse_step0_blank_lines_around",
        "\nAction: click(point='(375, 289)')\n",
        [("click_coordinate", {"coordinate_x": 375, "coordinate_y": 289})],
    ),
    (
        "pilot1_bezier_step1_double_quoted_content",
        'Action: finished(content="After clicking the reCAPTCHA checkbox, the checkbox changed from a sharp black square to a rounded square with a lighter grey border, indicating it has been clicked and is resetting or in a different state.")',  # noqa: E501
        [
            (
                "done",
                {
                    "success": True,
                    "extracted_content": "After clicking the reCAPTCHA checkbox, the checkbox changed from a sharp black square to a rounded square with a lighter grey border, indicating it has been clicked and is resetting or in a different state.",  # noqa: E501
                },
            )
        ],
    ),
    (
        "pilot2_v3demo_step3_finished_no_args",
        "Action: finished()",
        [("done", {"success": True, "extracted_content": ""})],
    ),
    (
        "pilot3_ep01_step4_finished_with_score",
        "Thought: The score is \"Score: 0.9\". I can now call finished() with this result.\nAction: finished(content='Score: 0.9')",  # noqa: E501
        [("done", {"success": True, "extracted_content": "Score: 0.9"})],
    ),
]


def _screenshot(
    image: tuple[int, int] = (1000, 1000), viewport: tuple[int, int] = (1000, 1000)
) -> Any:
    return SimpleNamespace(
        task=SimpleNamespace(user_request="tick the checkbox"),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            viewport_width=viewport[0],
            viewport_height=viewport[1],
            image_width=image[0],
            image_height=image[1],
        ),
    )


def _agent(descriptors: list[ActionDescriptor] | None = None) -> UITarsAgent:
    """An agent one step in: prepared (when *descriptors* are given) and shown a screenshot."""
    agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
    if descriptors is not None:
        agent.prepare(task="t", action_descriptors=descriptors, max_actions_per_step=6)
    agent.build_react_prompt(_screenshot())
    return agent


def _negotiated(*local_ids: str) -> list[ActionDescriptor]:
    return [ActionDescriptor(name=f"browser.{a}", description=a) for a in local_ids]


def _parsed(reply: str, agent: UITarsAgent | None = None) -> list[tuple[str, dict[str, Any]]]:
    return [(a.name, a.params) for a in (agent or _agent()).parse_react_output(reply)]


# ── Real replies ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "expected"),
    [(reply, expected) for _, reply, expected in _REAL],
    ids=[name for name, _, _ in _REAL],
)
def test_real_replies_parse_to_the_actions_they_meant(reply, expected) -> None:
    assert _parsed(reply) == expected


class TestTheRealFailures:
    def test_the_empty_reply_says_it_was_empty_and_what_to_send(self) -> None:
        """Hub step 1: 47 completion tokens, ``completion == ""``."""
        with pytest.raises(ParseError) as excinfo:
            _agent().parse_react_output("")
        detail = excinfo.value.detail
        assert detail.startswith("The reply was empty.")
        assert "Action: click(point='(x, y)')" in detail

    def test_whitespace_only_is_the_same_empty_reply(self) -> None:
        with pytest.raises(ParseError, match="The reply was empty"):
            _agent().parse_react_output("\n\n```\n```\n [EOS]")

    def test_a_quoted_action_in_prose_is_not_an_action(self) -> None:
        """Hub step 4: the quote mid-sentence, then a refusal and no Action line.

        The old parser took ``click(point='(647, 281)')" was executed. ...``
        as the call, failed on the prose after it, and reported that the
        reply "contained 'Action:' lines" — which is what the model then
        tried to fix. It was never going to click; it had refused.
        """
        with pytest.raises(ParseError) as excinfo:
            _agent().parse_react_output(_HUB_STEP4_REFUSAL)
        detail = excinfo.value.detail
        assert detail.startswith("No line starts with 'Action:'.")
        assert "'Action:' does appear, but not at the start of a line" in detail
        assert excinfo.value.raw_output == _HUB_STEP4_REFUSAL[:500]

    def test_the_detail_is_what_the_loop_feeds_back(self) -> None:
        """End to end through ReactLoop: the empty reply's detail reaches the model."""

        class _Env:
            async def observe(self, _negotiated: Any) -> Any:
                return _screenshot()

        class _LLM:
            model = "stub"

            def __init__(self) -> None:
                self.replies = iter(["", "Action: finished(content='ok')"])

            async def ainvoke(self, _messages: Any, **_kw: Any) -> Any:
                return SimpleNamespace(completion=next(self.replies), usage=None)

        async def _act(action: Action) -> ActionResult:
            return ActionResult(success=True, is_done=action.name == "done")

        agent = make_agent(UITarsAgent)
        memory = SimpleMemory(max_messages=50)
        memory.add_messages(agent.prepare(task="t", action_descriptors=_negotiated("done")))
        ctx = {"env": _Env(), "llm": _LLM(), "memory": memory, "act_fn": _act}

        import asyncio

        result = asyncio.run(ReactLoop()._execute(agent, "t", ctx=ctx))

        assert result.agent_done.value == "done"
        fed_back = [
            m.content
            for m in memory.get_messages()
            if isinstance(m, UserMessage) and isinstance(m.content, str)
        ]
        assert any("[SYSTEM]" in text and "The reply was empty." in text for text in fed_back)


# ── Tolerated: unambiguous ways models dress an action line ───────────


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            "```\nThought: tick it\nAction: click(point='(10, 20)')\n```",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="whole-reply-in-a-fence",
        ),
        pytest.param(
            "Thought: tick it\nAction:\n```python\nclick(point='(10, 20)')\n```",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="bare-label-then-fenced-call",
        ),
        pytest.param(
            "**Thought:** tick it\n**Action:** click(point='(10, 20)')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="markdown-bold-label",
        ),
        pytest.param(
            "- Action: `click(point='(10, 20)')`",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="bullet-and-backticks",
        ),
        pytest.param(
            "action: click(point='(10, 20)').",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="lowercase-label-trailing-period",
        ),
        pytest.param(
            "Action: click(point='(10, 20)');  # the checkbox",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="trailing-semicolon-and-comment",
        ),
        pytest.param(
            "Sure — here is my answer.\nThought: tick it\nAction: click(point='(10, 20)')\nLet me know if that worked!",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="prose-around-the-block",
        ),
        pytest.param(
            "Thought: first tile\nAction: click(point='(10, 20)')\n\nThought: second tile\nAction: click(point='(30, 40)')",
            [
                ("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20}),
                ("click_coordinate", {"coordinate_x": 30, "coordinate_y": 40}),
            ],
            id="two-thought-action-blocks-in-order",
        ),
        pytest.param(
            "Thought: two tiles, then verify\nAction: click(point='(10, 20)')\nAction: click(point='(30, 40)')\nAction: click(point='(50, 60)')",
            [
                ("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20}),
                ("click_coordinate", {"coordinate_x": 30, "coordinate_y": 40}),
                ("click_coordinate", {"coordinate_x": 50, "coordinate_y": 60}),
            ],
            id="one-action-line-per-action",
        ),
        pytest.param(
            "Action: click(point='(10, 20)'); click(point='(30, 40)')",
            [
                ("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20}),
                ("click_coordinate", {"coordinate_x": 30, "coordinate_y": 40}),
            ],
            id="two-calls-on-one-line",
        ),
        pytest.param(
            "Action: click(point='<point>10 20</point>')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="ui-tars-1.5-point-token",
        ),
        pytest.param(
            "Action: click(start_box='<|box_start|>(10,20)<|box_end|>')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="ui-tars-start-box-with-box-tokens",
        ),
        pytest.param(
            "Action: click(start_box='(10,20)')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="ui-tars-start-box-plain",
        ),
        pytest.param(
            "Action: click(start_box='(10,20,30,40)')",
            [("click_coordinate", {"coordinate_x": 20, "coordinate_y": 30})],
            id="box-means-its-centre",
        ),
        pytest.param(
            # UI-TARS 1.0's other box spelling: the two corners, each
            # parenthesised. Four numbers, so the same centre.
            "Action: click(start_box='<|box_start|>(10,20),(30,40)<|box_end|>')",
            [("click_coordinate", {"coordinate_x": 20, "coordinate_y": 30})],
            id="box-as-two-parenthesised-corners",
        ),
        pytest.param(
            "Action: click(point='10, 20')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="bare-pair-no-brackets",
        ),
        pytest.param(
            "Action: click(point='[10, 20]')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="square-bracketed-pair",
        ),
        pytest.param(
            "Action: click(point='<bbox>10 20 30 40</bbox>')",
            [("click_coordinate", {"coordinate_x": 20, "coordinate_y": 30})],
            id="bbox-token-means-its-centre",
        ),
        pytest.param(
            "Action: drag(start_box='(1,2)', end_box='(3,4)')",
            [("drag", {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4})],
            id="ui-tars-drag-boxes",
        ),
        pytest.param(
            "Action: click(point='(10.4, 19.6)')",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="fractional-coordinates",
        ),
        pytest.param(
            "Action: click(point=(10, 20))",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="tuple-literal-point",
        ),
        pytest.param(
            "Action: click(point='(10, 20)')[EOS]",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="end-of-sequence-marker",
        ),
        pytest.param(
            "Action: finished(content='It's ticked, and the widget didn't reset')",
            [
                (
                    "done",
                    {
                        "success": True,
                        "extracted_content": "It's ticked, and the widget didn't reset",
                    },
                )
            ],
            id="unescaped-apostrophes-in-text",
        ),
        pytest.param(
            "Action: type(content='line one\nline two', point='(10, 20)')",
            [
                (
                    "type_coordinate",
                    {"coordinate_x": 10, "coordinate_y": 20, "text": "line one\nline two"},
                )
            ],
            id="raw-newline-inside-text",
        ),
        pytest.param(
            "Action: type(content='submit\\n', point='(10, 20)')",
            [("type_coordinate", {"coordinate_x": 10, "coordinate_y": 20, "text": "submit\n"})],
            id="escaped-newline-means-enter",
        ),
        pytest.param(
            "Action: drag(\n    start_point='(1, 2)',\n    end_point='(3, 4)',\n)",
            [("drag", {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4})],
            id="call-across-lines",
        ),
        pytest.param(
            "Thought: done\nAction_Summary: tick\nAction: finished(content='(see above)')",
            [("done", {"success": True, "extracted_content": "(see above)"})],
            id="action-summary-is-not-an-action-label",
        ),
        pytest.param(
            "Thought: two tiles\n1. Action: click(point='(10, 20)')\n2) Action: click(point='(30, 40)')",
            [
                ("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20}),
                ("click_coordinate", {"coordinate_x": 30, "coordinate_y": 40}),
            ],
            id="numbered-list-labels",
        ),
        pytest.param(
            "Action: click(point=['10', '20'])",
            [("click_coordinate", {"coordinate_x": 10, "coordinate_y": 20})],
            id="list-of-quoted-numbers",
        ),
        pytest.param(
            "Action: hotkey(key=['ctrl', 'c'])",
            [("send_keys", {"keys": "ctrl+c"})],
            id="list-of-quoted-key-names",
        ),
        pytest.param(
            "Action: type(content='see ['x'] here', point='(10, 20)')",
            [
                (
                    "type_coordinate",
                    {"coordinate_x": 10, "coordinate_y": 20, "text": "see ['x'] here"},
                )
            ],
            id="brackets-inside-text-stay-text",
        ),
    ],
)
def test_tolerated_dressings(reply: str, expected: list[tuple[str, dict[str, Any]]]) -> None:
    assert _parsed(reply) == expected


# ── Refused, precisely ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        pytest.param(
            "Thought: I will click the box.",
            "No line starts with 'Action:'. Put each action on its own line, e.g. Action: click(point='(x, y)').",
            id="thought-only",
        ),
        pytest.param(
            "Thought: done. Action: click(point='(1, 2)')",
            "No line starts with 'Action:'. 'Action:' does appear, but not at the start "
            "of a line, so it was read as prose. Put each action on its own line, "
            "e.g. Action: click(point='(x, y)').",
            id="action-mid-line",
        ),
        pytest.param(
            "Final Action: click(point='(1, 2)')",
            "No line starts with 'Action:'. 'Action:' does appear, but not at the start of a line",
            id="a-word-before-the-label",
        ),
        pytest.param(
            # The documented price of anchoring the label: a reasoning line
            # that happens to begin with it is an action line, and not a call.
            "Thought: recap\nAction: I clicked the checkbox earlier.\nThought: now\nAction: click(point='(1, 2)')",
            "Action line 1 is not a call: 'I clicked the checkbox earlier.'",
            id="a-reasoning-line-that-starts-with-the-label",
        ),
        pytest.param(
            "Thought: hmm\nAction:\nThought: nothing to do",
            "Action line 1 has no call after 'Action:'",
            id="bare-label-no-call",
        ),
        pytest.param(
            "Action: I will click the checkbox",
            "Action line 1 is not a call: 'I will click the checkbox'",
            id="prose-instead-of-call",
        ),
        pytest.param(
            "Action: click(point='(1, 2)'",
            "Action line 1 is cut off before its closing parenthesis",
            id="cut-off-at-the-end",
        ),
        pytest.param(
            "Action: click(point='(1, 2)'\nAction: wait()",
            "Action line 1 is cut off before its closing parenthesis",
            id="cut-off-before-the-next-action",
        ),
        pytest.param(
            "Action: click(point='(1, 2)') to tick the box",
            "Action line 1 has text after the call: 'to tick the box'",
            id="text-after-the-call",
        ),
        pytest.param(
            "Action: click('(1, 2)')",
            "Action line 1, click('(1, 2)'): pass arguments by name, as in click(point='(x, y)')",
            id="positional-argument",
        ),
        pytest.param(
            "Action: click(point=x)",
            "Action line 1: argument point of click() must be a quoted value",
            id="non-literal-argument",
        ),
        pytest.param(
            "Action: click(point='(1, 2)')\nAction: left_double(point='(3, 4)')",
            "Action line 2: unknown verb 'left_double'; this run offers: click, type, scroll, hotkey, drag, wait, finished",
            id="unknown-verb-names-the-line",
        ),
        pytest.param(
            "Action: click(point='(1, 2)', point2=)",
            "Action line 1 is not a well-formed call",
            id="syntax-error",
        ),
        pytest.param(
            "Action: click(point='the checkbox')",
            "Action line 1, click(point='the checkbox'): point='the checkbox' is not a coordinate; expected point='(x, y)'",
            id="point-without-numbers",
        ),
        pytest.param(
            "Action: click(point='(1, 2)', button='right')",
            "Action line 1, click(point='(1, 2)', button='right'): click() does not take button; write it as click(point='(x, y)')",
            id="an-argument-the-verb-does-not-take",
        ),
        pytest.param(
            "Action: click(point='(1, 2)', point='(3, 4)')",
            "Action line 1: click() is given point twice.",
            id="a-repeated-argument",
        ),
        pytest.param(
            "Action: click(point='(1, 2)', start_box='(3, 4)')",
            "Action line 1, click(point='(1, 2)', start_box='(3, 4)'): point and start_box say the same thing; write only point",
            id="two-spellings-of-one-point",
        ),
        pytest.param(
            "Action: wait(seconds=3)",
            "Action line 1, wait(seconds=3): wait() does not take seconds; write it as wait()",
            id="a-duration-is-capability-config",
        ),
    ],
)
def test_refusals_say_which_line_and_what(reply: str, detail: str) -> None:
    with pytest.raises(ParseError) as excinfo:
        _agent().parse_react_output(reply)
    assert excinfo.value.detail.startswith(detail), excinfo.value.detail
    assert excinfo.value.raw_output == reply[:500]


class TestAPointIsReadNotScraped:
    """A point is the documented spellings, not "the numbers in this string".

    The parser used to ``findall`` an unanchored ``-?\\d+(?:\\.\\d+)?`` over
    whatever the point argument held, so any two digits anywhere in it became
    a coordinate: ``point='x1y2'`` clicked (1, 2), ``point='#id2 > .cls3'``
    clicked (2, 3), ``point='the 2nd button, row 3'`` clicked (2, 3). Each is
    a model naming a thing instead of a place — a click at the top-left
    corner, reported as a success, with nothing in the trace saying the point
    was invented. They are now refused, and the refusal says so in words the
    Loop hands back.

    What makes them refusable is that the spellings have to account for every
    character of the value: tags only as a matched pair around the rest, and
    then only numbers, brackets and separators. A digit glued to a letter
    leaves a gap, and a gap is not a coordinate.
    """

    @pytest.mark.parametrize(
        "written",
        [
            pytest.param("x1y2", id="an-id-that-is-two-digits-in-disguise"),
            pytest.param("btn2", id="an-element-name"),
            pytest.param("item3", id="another-element-name"),
            pytest.param("row3col4", id="a-grid-cell-name"),
            pytest.param("#id2 > .cls3", id="a-css-selector"),
            pytest.param("[data-testid=tile-2-3]", id="an-attribute-selector"),
            pytest.param("the 2nd button, row 3", id="a-description-with-ordinals"),
            pytest.param("tile 3 of 4", id="a-description-with-a-count"),
            pytest.param("v2.0 button", id="a-version-number"),
            pytest.param("the checkbox", id="no-digits-at-all"),
            pytest.param("<point>10 20</point> near btn9", id="a-real-point-plus-prose"),
            pytest.param("10 20</point>", id="a-closing-tag-with-no-opener"),
            pytest.param("<point>10 20", id="an-opening-tag-with-no-closer"),
        ],
    )
    def test_a_point_that_names_a_thing_is_refused_not_mined_for_digits(self, written: str) -> None:
        reply = f"Action: click(point={written!r})"
        with pytest.raises(ParseError) as excinfo:
            _agent().parse_react_output(reply)
        detail = excinfo.value.detail
        assert f"point={written!r} is not a coordinate" in detail, detail
        assert "two numbers and nothing else" in detail
        assert "no element name, selector or text" in detail

    @pytest.mark.parametrize(
        ("written", "count"),
        [
            pytest.param("10,20,30", 3, id="three"),
            pytest.param("(10, 20, 30, 40, 50, 60)", 6, id="six"),
            pytest.param("(10)", 1, id="one"),
        ],
    )
    def test_a_count_that_is_neither_a_point_nor_a_box_says_the_count(
        self, written: str, count: int
    ) -> None:
        """Distinct from "not a coordinate": the model wrote numbers, just too many."""
        with pytest.raises(ParseError) as excinfo:
            _agent().parse_react_output(f"Action: click(point={written!r})")
        assert excinfo.value.detail.endswith(
            f"point={written!r} has {count} numbers; write two for a point "
            f"(point='(x, y)') or four for a box (point='(x1, y1, x2, y2)')"
        ), excinfo.value.detail

    def test_two_points_in_one_argument_are_not_averaged_into_a_third(self) -> None:
        """Six numbers, not a box: refused rather than read as some centre."""
        with pytest.raises(ParseError, match="has 6 numbers"):
            _agent().parse_react_output("Action: click(point='(1,2), (3,4), (5,6)')")

    def test_an_element_hint_beside_a_good_point_is_refused_by_the_verb(self) -> None:
        """The other half of the same rule: nothing is read off the *call* text.

        ``_parse_point`` only ever sees the value of the point argument, so a
        digit-carrying sibling argument cannot reach it — and the sibling is
        refused anyway, because ``click()`` does not take it.
        """
        with pytest.raises(ParseError) as excinfo:
            _agent().parse_react_output("Action: click(point='(100, 200)', element='item3')")
        assert excinfo.value.detail == (
            "Action line 1, click(point='(100, 200)', element='item3'): click() does "
            "not take element; write it as click(point='(x, y)')"
        )

    def test_digits_in_a_text_argument_do_not_move_the_point(self) -> None:
        reply = "Action: type(content='btn2 x1y2 (99, 99)', point='(10, 20)')"
        assert _parsed(reply) == [
            (
                "type_coordinate",
                {"coordinate_x": 10, "coordinate_y": 20, "text": "btn2 x1y2 (99, 99)"},
            )
        ]

    def test_a_list_argument_must_hold_numbers_and_nothing_else(self) -> None:
        with pytest.raises(ParseError, match="is not a coordinate"):
            _agent().parse_react_output("Action: click(point=['x1', 'y2'])")
        with pytest.raises(ParseError, match="is not a coordinate"):
            _agent().parse_react_output("Action: click(point=[True, False])")

    def test_a_refused_point_does_not_reach_an_action(self) -> None:
        """All-or-nothing: the good click in the same reply does not run either."""
        reply = "Action: click(point='(10, 20)')\nAction: click(point='btn2')"
        with pytest.raises(ParseError, match="Action line 2"):
            _agent().parse_react_output(reply)


class TestAllOrNothing:
    def test_one_bad_line_fails_the_whole_reply(self) -> None:
        """Half a plan is a different plan: the tiles would be clicked, VERIFY never."""
        reply = (
            "Action: click(point='(10, 20)')\n"
            "Action: click(point='(30, 40)') and then verify\n"
            "Action: click(point='(50, 60)')"
        )
        with pytest.raises(ParseError, match="Action line 2 has text after the call"):
            _agent().parse_react_output(reply)


class TestTheNegotiatedSetIsTheVocabulary:
    def test_a_verb_this_run_did_not_negotiate_is_refused_at_parse_time(self) -> None:
        """UI-TARS sees no action results, so a guard rejection would be invisible to it."""
        agent = _agent(_negotiated("click_coordinate", "type_coordinate", "done"))
        assert _parsed("Action: click(point='(1, 2)')", agent) == [
            ("click_coordinate", {"coordinate_x": 1, "coordinate_y": 2})
        ]
        with pytest.raises(ParseError) as excinfo:
            agent.parse_react_output("Action: drag(start_point='(1, 2)', end_point='(3, 4)')")
        assert excinfo.value.detail == (
            "Action line 1: unknown verb 'drag'; this run offers: click, type, finished"
        )

    def test_the_example_in_an_error_is_a_call_this_run_offers(self) -> None:
        agent = _agent(_negotiated("wait", "done"))
        with pytest.raises(ParseError, match=r"e\.g\. Action: wait\(\)\."):
            agent.parse_react_output("")
