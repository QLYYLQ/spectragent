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

export function historyLabel(source) {
  return source === "source"
    ? "Source history"
    : source === "restored"
      ? "Restored history"
      : "History source unknown";
}

export function historyExplanation(source) {
  if (source === "source")
    return "The Agent keeps the original conversation. Differences in actual replay are not substituted into that history.";
  if (source === "restored")
    return "The Agent receives actual restored observations, retargeted actions and returned results. Earlier replies remain attributed source material.";
  return "The history strategy was not recorded. Source material alone does not establish which history the Agent used.";
}

export function cursorLabel(fragments) {
  const fragment = Object.entries(fragments || {}).find(([key]) =>
    key.endsWith(".screenshot"),
  )?.[1];
  if (!fragment || fragment.error) return "Cursor metadata unavailable";
  if (!fragment.cursor) return "Cursor disabled";
  if (!fragment.cursor.position)
    return "Cursor enabled · position unknown (no overlay)";
  return `Cursor rendered · (${fragment.cursor.position.join(", ")}) CSS px · size ${fragment.cursor.size_css_px} CSS px`;
}

export function gapLabel(gap) {
  if (!gap) return "No comparison";
  if (gap.differences.length)
    return `${gap.differences.length} measured differences`;
  return gap.unmeasured.length
    ? "Comparison incomplete"
    : "No measured differences";
}
