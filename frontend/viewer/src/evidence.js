/** Names describe actions actually submitted in this run, including restoration. */
export function actionLabel(record) {
  const actions =
    record.origin === "restored"
      ? (record.restoration?.actions || [])
          .map((action) => action.executed)
          .filter(Boolean)
      : record.actions || [];
  return (
    actions.map((action) => action.name).join(", ") ||
    (record.origin === "terminal"
      ? "Final observation"
      : record.origin === "restored"
        ? "No execution recorded"
        : "No action")
  );
}

export function restoredActionOutcome(action) {
  if (!action.executed) return "not executed";
  if (action.live_error || action.live_success === false) return "failed";
  if (action.live_success === true) return "success";
  return "unknown";
}

/** A synthetic evaluator record is coverage evidence, not a task verdict. */
export function verdictLabel(verdict) {
  if (verdict.verdict_source !== "graded")
    return verdict.verdict_source || "not_evaluated";
  return verdict.predicted_label === 1
    ? "success"
    : verdict.predicted_label === 0
      ? "failure"
      : null;
}

/** Missing call channels are unknown, while a selected empty channel is zero. */
export function trailingCallCount(run) {
  if (run.config.recording.metrics) return run.trailing_call_metrics.length;
  if (run.config.recording.trajectory) return run.trailing_calls.length;
  return null;
}
