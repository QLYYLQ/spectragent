import assert from "node:assert/strict";
import test from "node:test";
import { actionLabel, restoredActionOutcome } from "./evidence.js";

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
