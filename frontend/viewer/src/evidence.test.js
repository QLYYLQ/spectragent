import assert from "node:assert/strict";
import test from "node:test";
import { actionLabel, restoredActionOutcome, trailingCallCount, verdictLabel } from "./evidence.js";

test("restored actions use executed names rather than copied source actions", () => {
  assert.equal(
    actionLabel({
      origin: "restored",
      actions: [{ name: "source.click" }],
      restoration: { actions: [{ executed: { name: "browser.click" } }] },
    }),
    "browser.click",
  );
  assert.equal(
    actionLabel({
      origin: "restored",
      actions: [{ name: "source.click" }],
      restoration: { actions: [{ executed: null }] },
    }),
    "No execution recorded",
  );
});

test("a source success cannot turn a failed restoration into a success", () => {
  assert.equal(
    restoredActionOutcome({
      executed: { name: "browser.click" },
      recorded_success: true,
      live_success: false,
    }),
    "failed",
  );
  assert.equal(
    restoredActionOutcome({
      executed: null,
      recorded_success: true,
      live_success: null,
    }),
    "not executed",
  );
  assert.equal(
    restoredActionOutcome({
      executed: { name: "browser.click" },
      recorded_success: true,
      live_success: null,
    }),
    "unknown",
  );
});

test("policy and terminal action labels retain their own meaning", () => {
  assert.equal(
    actionLabel({ origin: "policy", actions: [{ name: "browser.click" }] }),
    "browser.click",
  );
  assert.equal(
    actionLabel({ origin: "terminal", actions: [] }),
    "Final observation",
  );
});

test("synthetic evaluation outcomes never become measured task failures", () => {
  assert.equal(verdictLabel({ verdict_source: "evaluation_error", predicted_label: 0 }), "evaluation_error");
  assert.equal(verdictLabel({ verdict_source: "not_evaluated", predicted_label: 0 }), "not_evaluated");
  assert.equal(verdictLabel({ verdict_source: "graded", predicted_label: 0 }), "failure");
  assert.equal(verdictLabel({ verdict_source: "graded", predicted_label: 1 }), "success");
});

test("trailing call counts respect channel selection and preserve measured zero", () => {
  const run = {
    config: { recording: { metrics: true, trajectory: false } },
    trailing_calls: [],
    trailing_call_metrics: [{ call_id: "failed-without-trajectory" }],
  };
  assert.equal(trailingCallCount(run), 1);
  run.trailing_call_metrics = [];
  assert.equal(trailingCallCount(run), 0);
  run.config.recording.metrics = false;
  assert.equal(trailingCallCount(run), null);
  run.config.recording.trajectory = true;
  run.trailing_calls = [{ call_id: "recorded-trajectory" }];
  assert.equal(trailingCallCount(run), 1);
});
