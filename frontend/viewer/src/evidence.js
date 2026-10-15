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
