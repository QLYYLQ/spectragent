import assert from "node:assert/strict";
import test from "node:test";
import { isTextTarget, keyAction } from "./keys.js";

const context = { choices: 2, kinds: 3, suggestion: true, selection: false };
const press = (key, extra = {}) => ({ key, code: "", ...extra });

test("digits choose answers and Shift+digits pick box kinds by key position", () => {
  assert.deepEqual(keyAction(press("1", { code: "Digit1" }), context), { type: "choice", index: 0 });
  assert.equal(keyAction(press("3", { code: "Digit3" }), context), null);
  assert.deepEqual(keyAction(press("!", { code: "Digit1", shiftKey: true }), context), { type: "kind", index: 0 });
  assert.deepEqual(keyAction(press("#", { code: "Digit3", shiftKey: true }), context), { type: "kind", index: 2 });
  assert.equal(keyAction(press("$", { code: "Digit4", shiftKey: true }), context), null);
});

test("suggestion keys only act when there is a suggestion", () => {
  for (const key of ["a", "e", "r", "g"]) {
    assert.ok(keyAction(press(key), context));
    assert.equal(keyAction(press(key), { ...context, suggestion: false }), null);
  }
});

test("arrows navigate items unless a box is selected, then they nudge it", () => {
  assert.deepEqual(keyAction(press("ArrowRight"), context), { type: "next" });
  assert.deepEqual(keyAction(press("ArrowLeft"), context), { type: "previous" });
  const selected = { ...context, selection: true };
  assert.deepEqual(keyAction(press("ArrowRight"), selected), { type: "nudge", dx: 1, dy: 0 });
  assert.deepEqual(keyAction(press("ArrowUp", { shiftKey: true }), selected), { type: "nudge", dx: 0, dy: -10 });
  assert.deepEqual(keyAction(press("Delete"), selected), { type: "deleteBox" });
  assert.equal(keyAction(press("Backspace"), context), null);
});

test("typing in the note keeps its keys except leaving and saving", () => {
  assert.equal(keyAction(press("a", { inText: true }), context), null);
  assert.equal(keyAction(press("Enter", { inText: true }), context), null);
  assert.deepEqual(keyAction(press("Enter", { inText: true, metaKey: true }), context), { type: "save" });
  assert.deepEqual(keyAction(press("Escape", { inText: true }), context), { type: "leaveText" });
});

test("browser shortcuts are never taken", () => {
  assert.equal(keyAction(press("r", { metaKey: true }), context), null);
  assert.equal(keyAction(press("1", { code: "Digit1", ctrlKey: true }), context), null);
  assert.equal(keyAction(press("j", { altKey: true }), context), null);
});

test("zoom keys and help", () => {
  assert.deepEqual(keyAction(press("+"), context), { type: "zoom", step: 1 });
  assert.deepEqual(keyAction(press("-"), context), { type: "zoom", step: -1 });
  assert.deepEqual(keyAction(press("0", { code: "Digit0" }), context), { type: "zoom", step: 0 });
  assert.deepEqual(keyAction(press("?"), context), { type: "help" });
});

test("text targets", () => {
  assert.equal(isTextTarget({ tagName: "TEXTAREA" }), true);
  assert.equal(isTextTarget({ tagName: "INPUT", type: "text" }), true);
  assert.equal(isTextTarget({ tagName: "INPUT", type: "checkbox" }), false);
  assert.equal(isTextTarget({ tagName: "SELECT" }), true);
  assert.equal(isTextTarget({ tagName: "BUTTON" }), false);
  assert.equal(isTextTarget(null), false);
});

test("up and down move between questions when there are several", () => {
  const many = { ...context, questions: 3 };
  assert.deepEqual(keyAction(press("ArrowUp"), many), { type: "question", step: -1 });
  assert.deepEqual(keyAction(press("ArrowDown"), many), { type: "question", step: 1 });
  assert.equal(keyAction(press("ArrowDown"), context), null);
  assert.deepEqual(keyAction(press("ArrowDown"), { ...many, selection: true }), { type: "nudge", dx: 0, dy: 1 });
});
