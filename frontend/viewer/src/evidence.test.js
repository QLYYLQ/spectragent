import assert from "node:assert/strict";
import test from "node:test";
import {
  actionLabel,
  restoredActionOutcome,
  trailingCallCount,
  verdictLabel,
  cursorLabel,
  historyExplanation,
  gapLabel,
  modelCallTitle,
  decisionAnswerRows,
} from "./evidence.js";

test("Decision kind is independent of Agent decision purpose", () => {
  assert.equal(modelCallTitle({kind: "decision", purpose: "replay_review"}), "Replay review");
  assert.equal(modelCallTitle({kind: "chat", purpose: "agent_decision"}), "Agent decision");
  assert.equal(modelCallTitle({kind: "decision", purpose: "prompt_helper"}), "Prompt helper");
});

test("native answer rows preserve zero, structured legends and absent Noul confidence", () => {
  const rows = decisionAnswerRows({answers: {
    binary: {type: "noul", noul: 0},
    score: {type: "score", score: 1.2, confidence: 0.8, legend: {0: {meaning: "low"}}, probabilities: {0: 0.2, 1: 0.8}},
    box: {type: "box", box: {left: 0, top: 0, right: 0.5, bottom: 1}, confidence: 0.9},
  }});
  assert.equal(rows[0].value, 0);
  assert.equal(rows[0].confidence, null);
  assert.deepEqual(rows[1].legend, {0: {meaning: "low"}});
  assert.equal(rows[2].value.right, 0.5);
});

test("cursor labels distinguish disabled, unknown and recorded positions", () => {
  assert.equal(cursorLabel({}), "Cursor metadata unavailable");
  assert.equal(
    cursorLabel({ "browser.screenshot": { cursor: null } }),
    "Cursor disabled",
  );
  assert.match(
    cursorLabel({ "browser.screenshot": { cursor: { position: null } } }),
    /position unknown/,
  );
  assert.equal(
    cursorLabel({
      "browser.screenshot": { cursor: { position: [0, 0], size_css_px: 48 } },
    }),
    "Cursor rendered · (0, 0) CSS px · size 48 CSS px",
  );
});

test("history and measured gaps do not equate verification with identical history", () => {
  assert.match(historyExplanation("source"), /original conversation/);
  assert.match(historyExplanation("restored"), /actual restored observations/);
  assert.match(historyExplanation(null), /not recorded/);
  assert.equal(
    gapLabel({ verdict: "ok", differences: ["distance 1"], unmeasured: [] }),
    "1 measured differences",
  );
  assert.equal(
    gapLabel({ differences: [], unmeasured: ["unknown"] }),
    "Comparison incomplete",
  );
});

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
  assert.equal(
    verdictLabel({ verdict_source: "evaluation_error", predicted_label: 0 }),
    "evaluation_error",
  );
  assert.equal(
    verdictLabel({ verdict_source: "not_evaluated", predicted_label: 0 }),
    "not_evaluated",
  );
  assert.equal(
    verdictLabel({ verdict_source: "graded", predicted_label: 0 }),
    "failure",
  );
  assert.equal(
    verdictLabel({ verdict_source: "graded", predicted_label: 1 }),
    "success",
  );
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
