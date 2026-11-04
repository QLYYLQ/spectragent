import test from "node:test";
import assert from "node:assert/strict";
import {
  comparisonEvidence,
  folderNeighbour,
  folderSteps,
  frameStatus,
  imageCanBeEdited,
  nextObservation,
  observationFacts,
  pointerPresentation,
  recordedReply,
  sectionEvidence,
} from "./reviewEvidence.js";

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

test("the archive's tabs keep committed steps in order without filling gaps or reordering the input", () => {
  const steps = [4, 1, 3, 0, 7].map(index => ({index}));
  const tabs = folderSteps(steps, 4);
  assert.deepEqual(tabs.map(step => [step.index, step.relation]), [[0, "past"], [1, "past"], [3, "past"], [4, "current"], [7, "future"]]);
  assert.deepEqual(steps.map(step => step.index), [4, 1, 3, 0, 7]);
  assert.ok(!folderSteps(steps, 2).some(step => step.relation === "current"));
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

test("the folder marks each committed step before, at or after the reviewed one, and moves within them", () => {
  const steps = folderSteps([{index: 3}, {index: 0}, {index: 1}], 1);
  assert.deepEqual(steps.map((step) => [step.index, step.relation]), [[0, "past"], [1, "current"], [3, "future"]]);
  assert.equal(folderNeighbour(steps, 1, 1), 3);
  assert.equal(folderNeighbour(steps, 3, 1), 3);
  assert.equal(folderNeighbour(steps, 0, -1), 0);
  assert.equal(nextObservation(steps, 0)?.index, 1);
  assert.equal(nextObservation(steps, 1), null, "a gap after the step has no screen after it");
});

test("observation facts come from recorded fields, never from fragment names", () => {
  const android = observationFacts({
    "android.screenshot": {device: {package: "com.android.settings", activity: "com.android.settings.Settings"}},
    "android.ui_tree": {serialized: 'UI element 0: {"text": "Display"}\nUI element 1: {"text": "Sound"}'},
  });
  assert.equal(android.front, "com.android.settings · Settings");
  assert.deepEqual(android.text, {source: "android.ui_tree", lines: ['UI element 0: {"text": "Display"}', 'UI element 1: {"text": "Sound"}']});
  const browser = observationFacts({
    "browser.browser_meta": {url: "https://example.test/", page_title: "Example"},
    "browser.dom": {selector_map: {"7": {tag_name: "button", attributes: {"aria-label": "Search"}}}},
  });
  assert.equal(browser.page, "Example · https://example.test/");
  assert.deepEqual(browser.text.lines, ["[7] <button> Search"]);
  assert.deepEqual(observationFacts({}), {front: null, page: null, text: null});
});
