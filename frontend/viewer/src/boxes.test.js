import assert from "node:assert/strict";
import test from "node:test";
import {
  boxFromDrag,
  boxStyle,
  hasArea,
  moveBox,
  resizeBox,
  sameRegions,
  toImagePoint,
} from "./boxes.js";

const size = { width: 1280, height: 720 };

test("a pointer on a scaled image maps to original pixels", () => {
  const rect = { left: 100, top: 50, width: 640, height: 360 };
  assert.deepEqual(toImagePoint(420, 230, rect, size), { x: 640, y: 360 });
  assert.deepEqual(toImagePoint(0, 0, rect, size), { x: 0, y: 0 });
  assert.deepEqual(toImagePoint(5000, 5000, rect, size), { x: 1280, y: 720 });
});

test("a drag in any direction gives an ordered, rounded box inside the image", () => {
  assert.deepEqual(
    boxFromDrag({ x: 300.6, y: 200.2 }, { x: 100.4, y: -40 }, size),
    [100, 0, 301, 200],
  );
  assert.deepEqual(boxFromDrag({ x: 1200, y: 700 }, { x: 1400, y: 900 }, size), [
    1200, 700, 1280, 720,
  ]);
});

test("a click is not a box", () => {
  assert.equal(hasArea(boxFromDrag({ x: 10, y: 10 }, { x: 11, y: 12 }, size), 3), false);
  assert.equal(hasArea([0, 0, 3, 3], 3), true);
});

test("moving keeps the size and stops at the edges", () => {
  assert.deepEqual(moveBox([10, 10, 110, 60], 50, 20, size), [60, 30, 160, 80]);
  assert.deepEqual(moveBox([10, 10, 110, 60], -500, 0, size), [0, 10, 100, 60]);
  assert.deepEqual(moveBox([10, 10, 110, 60], 5000, 5000, size), [1180, 670, 1280, 720]);
});

test("resizing moves only the dragged side and flips past the opposite one", () => {
  assert.deepEqual(resizeBox([10, 10, 110, 60], "se", { x: 200, y: 100 }, size), [
    10, 10, 200, 100,
  ]);
  assert.deepEqual(resizeBox([10, 10, 110, 60], "w", { x: 50, y: 999 }, size), [
    50, 10, 110, 60,
  ]);
  assert.deepEqual(resizeBox([10, 10, 110, 60], "n", { x: 0, y: 80 }, size), [
    10, 60, 110, 80,
  ]);
  // Collapsing to nothing keeps the previous box.
  assert.deepEqual(resizeBox([10, 10, 110, 60], "e", { x: 10, y: 0 }, size), [
    10, 10, 110, 60,
  ]);
});

test("box placement is in percent of the image", () => {
  assert.deepEqual(boxStyle([128, 72, 640, 360], size), {
    left: "10%",
    top: "10%",
    width: "40%",
    height: "40%",
  });
});

test("regions compare as a multiset", () => {
  const a = { image: "before", kind: "evidence", box: [0, 0, 10, 10] };
  const b = { image: "after", kind: "evidence", box: [0, 0, 10, 10] };
  assert.equal(sameRegions([a, b], [b, a]), true);
  assert.equal(sameRegions([a, a], [a, b]), false);
  assert.equal(sameRegions([a], [{ ...a, box: [0, 0, 10, 11] }]), false);
  assert.equal(sameRegions([], undefined), true);
});
