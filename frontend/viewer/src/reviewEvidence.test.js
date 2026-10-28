import test from "node:test";
import assert from "node:assert/strict";
import { imageCanBeEdited, sectionEvidence, trajectorySteps, frameStatus, recordedReply, comparisonEvidence, pointerPresentation } from "./reviewEvidence.js";

test("a source comparison never appears in the main evidence, irrespective of its wording", () => {
  const item = {
    run: "branch", images: [{role: "checkpoint"}, {role: "source", section: "comparison"}],
    fields: [{text: "任务"}, {text: "任意翻译", section: "comparison"}],
    links: [{run: "source", section: "comparison"}],
  };
  assert.deepEqual(sectionEvidence(item).images, [{role: "checkpoint"}]);
  assert.equal(sectionEvidence(item).fields.length, 1);
  assert.equal(sectionEvidence(item, "comparison").run, null);
  assert.deepEqual(sectionEvidence(item, "comparison").links, item.links);
});

test("only the specified image can accept gold boxes while other queue images remain editable", () => {
  for (const role of ["current", "before_action", "next"]) {
    assert.equal(imageCanBeEdited(role, ["next"]), role === "next");
    assert.equal(imageCanBeEdited(role, null), true);
  }
  assert.equal(imageCanBeEdited("next", []), false);
});

test("archive groups committed evidence before, at and after the review anchor without filling gaps", () => {
  const steps = [4, 1, 3, 0, 7].map(index => ({index}));
  const groups = trajectorySteps(steps, 4);
  assert.deepEqual(groups.past.map(step => step.index), [0, 1, 3]);
  assert.equal(groups.current.index, 4);
  assert.deepEqual(groups.future.map(step => step.index), [7]);
  assert.deepEqual(steps.map(step => step.index), [4, 1, 3, 0, 7]);
  assert.equal(trajectorySteps(steps, 2).current, null);
});

test("pointer overlay uses its image dimensions and preserves a measured zero", () => {
  const pointer = pointerPresentation({status: "known", width: 1200, height: 800, x: 0, y: 200});
  assert.equal(pointer.placed, true);
  assert.deepEqual(pointer.style, {left: "0%", top: "25%"});
  assert.match(pointer.label, /\(0, 200\)/);
  assert.equal(pointerPresentation({status: "known", width: 1200, height: 800, x: 1100, y: 100}).labelLeft, true);
});

test("unknown, invalid and out-of-image pointers are explained instead of invented", () => {
  for (const values of [{}, {status: "unknown", x: 10, y: 20}, {status: "known", x: null, y: null},
    {status: "known", x: Infinity, y: 20}, {status: "known", x: -1, y: 20},
    {status: "known", x: 100, y: 20}, {status: "known", x: 10, y: 50}]) {
    const pointer = pointerPresentation({width: 100, height: 50, problem: "未记录此时位置", ...values});
    assert.equal(pointer.placed, false);
    assert.equal(pointer.style, null);
    assert.equal(pointer.label, "未记录此时位置");
  }
  const unmapped = pointerPresentation({status: "known", viewport_x: 400, viewport_y: 300, problem: "截图几何未知"});
  assert.equal(unmapped.placed, false);
  assert.match(unmapped.label, /\(400, 300\).*CSS 像素；截图几何未知/);
});

test("missing or timed out frames are not presented as captured observations", () => {
  assert.match(frameStatus(null), /没有/);
  assert.match(frameStatus({outcome: "timeout"}), /超时/);
  assert.match(frameStatus({outcome: "no_tab"}), /关闭/);
});


test("restored replies remain explicit source material rather than invented new thinking", () => {
  assert.equal(recordedReply({origin: "restored", llm_output: "", source_history: {source_reply: "Original decision"}}), "Original decision");
  assert.equal(recordedReply({origin: "policy", llm_output: "New decision", source_history: {source_reply: "Original decision"}}), "New decision");
  assert.equal(recordedReply({origin: "restored", llm_output: "Unattributed copy"}), undefined);
});


test("pixel comparison can cross main and collapsed evidence while side-by-side stays focused", () => {
  const item = {images: [{role: "branch"}, {role: "source", section: "comparison"}]};
  assert.deepEqual(comparisonEvidence(item, "side").map(image => image.role), ["branch"]);
  assert.deepEqual(comparisonEvidence(item, "difference").map(image => image.role), ["branch", "source"]);
});
