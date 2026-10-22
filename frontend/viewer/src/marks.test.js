import assert from "node:assert/strict";
import test from "node:test";
import { arrowLength, hasMarks, legendRows, markColor, markLayout } from "./marks.js";

const served = {
  width: 1280,
  height: 720,
  problem: null,
  sets: [
    {
      run: "r",
      step: 1,
      label: "",
      problem: null,
      marks: [
        { number: 1, kind: "click", x: 446, y: 468, text: "点击 (223, 234) → 按钮“最多播放”", succeeded: true },
        { number: 2, kind: "scroll", x: 100, y: 50, direction: "down", text: "向下滚动" },
        { number: 3, kind: "drag", x: 1, y: 2, end_x: 30, end_y: 40, text: "拖动 (1, 2) → (30, 40)" },
        { number: 4, kind: "type", x: 5, y: 6, typed: "贵州茅台", text: "输入“贵州茅台”" },
        { number: 5, kind: null, text: "等待 1 秒" },
        { number: 6, kind: "scroll", x: null, y: null, direction: "up", unplaced: "滚动时鼠标的位置没有记录", text: "向上滚动" },
        { number: 7, kind: "click", x: 9, y: 9, text: "点击 (9, 9)", succeeded: false, error: "boom" },
      ],
    },
    { run: "s", step: 1, label: "原运行的动作", problem: null, marks: [] },
  ],
};

test("marks are laid out in image pixels, one colour per set", () => {
  const layout = markLayout(served);
  assert.equal(layout.length, arrowLength(1280, 720));
  assert.equal(layout.length, 91);
  const [own, other] = layout.sets;
  assert.equal(own.color, markColor(0));
  assert.notEqual(own.color, other.color);
  assert.deepEqual(
    own.shapes.map((shape) => shape.number),
    [1, 2, 3, 4, 7],
  );
  const scroll = own.shapes[1];
  assert.deepEqual(scroll.end, { x: 100, y: 50 + 91 });
  assert.equal(scroll.angle, 90);
  assert.deepEqual(own.shapes[2].end, { x: 30, y: 40 });
  assert.equal(own.shapes[0].angle, undefined);
  assert.equal(own.shapes[3].typed, "贵州茅台");
});

test("the legend lists every action, saying which are not drawn and why", () => {
  const [own, other] = legendRows(served);
  assert.equal(own.label, "动作");
  assert.equal(other.label, "原运行的动作");
  const notes = Object.fromEntries(own.marks.map((mark) => [mark.number, mark]));
  assert.equal(notes[1].drawn, true);
  assert.equal(notes[1].note, null);
  assert.equal(notes[5].drawn, false);
  assert.equal(notes[5].note, "没有位置，不画在图上");
  assert.equal(notes[6].note, "滚动时鼠标的位置没有记录");
  assert.equal(notes[7].failed, true);
  assert.equal(notes[7].error, "boom");
});

test("an image without geometry draws nothing", () => {
  assert.deepEqual(markLayout({ width: null, height: null, sets: served.sets }).sets, []);
  assert.equal(hasMarks(served), true);
  assert.equal(hasMarks({ sets: [] }), false);
  assert.equal(hasMarks(null), false);
});

test("dots on the same spot are fanned out, not hidden under each other", () => {
  const layout = markLayout({
    width: 1280,
    height: 720,
    sets: [
      {
        label: "",
        marks: [
          { number: 1, kind: "click", x: 450, y: 36, text: "点击" },
          { number: 2, kind: "type", x: 451, y: 36, typed: "贵州茅台", text: "输入" },
          { number: 3, kind: "click", x: 900, y: 400, text: "点击" },
        ],
      },
    ],
  });
  assert.deepEqual(
    layout.sets[0].shapes.map((shape) => shape.stack),
    [0, 1, 0],
  );
});
