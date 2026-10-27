import test from "node:test";
import assert from "node:assert/strict";
import { imageCanBeEdited, sectionEvidence, trajectorySteps, frameStatus, recordedReply, comparisonEvidence } from "./reviewEvidence.js";

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

test("history excludes the current decision and all future evidence, preserving gaps", () => {
  const steps = [4, 1, 3, 0, 7].map(index => ({index}));
  assert.deepEqual(trajectorySteps(steps, 4).map(step => step.index), [0, 1, 3]);
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
