import assert from "node:assert/strict";
import test from "node:test";
import { startupRoute } from "./routing.js";

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
