import assert from "node:assert/strict";
import test from "node:test";
import { actionRoute, selectedActionIndex, startupRoute } from "./routing.js";

test("an initial focus opens a nested run without a URL route", () => {
  assert.deepEqual(startupRoute({}, "record/run-one"), {
    view: "run",
    key: "record/run-one",
  });
});

test("the explicit run URL retains its step and section", () => {
  const route = {
    view: "run",
    key: "source/run-two",
    step: "3",
    tab: "evaluations",
  };
  assert.deepEqual(startupRoute(route, "record/run-one"), route);
});

test("the run list and reports remain reachable with an initial focus", () => {
  for (const view of ["runs", "reports"]) {
    assert.deepEqual(startupRoute({ view }, "run-one"), { view });
  }
});

test("no focus preserves the default list and a single-run root accepts dot", () => {
  assert.deepEqual(startupRoute({}, null), {});
  assert.deepEqual(startupRoute({}, "."), { view: "run", key: "." });
});

test("single-action links retain their run, committed step and zero-based action ordinal", () => {
  const route = actionRoute("fork/run", 12, 0);
  assert.deepEqual(startupRoute(route, "another"), { view: "run", key: "fork/run", tab: "trajectory", step: 12, action: 0 });
  assert.equal(selectedActionIndex("0"), 0);
  assert.equal(selectedActionIndex("13"), 13);
  for (const value of [undefined, null, "", "-1", "1.5", "1e3", "wat", "999999999999999999999999"])
    assert.equal(selectedActionIndex(value), null);
});
