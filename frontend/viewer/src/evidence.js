/**
 * Names describe actions actually submitted in this run, including restoration.
 * `verbs` is the run's `action_verbs` (name → the verb a person reads), which
 * the server takes from `inspection/marks.py`, the wording's one source; an
 * action it does not name keeps its recorded name.
 */
export function actionLabel(record, verbs) {
  const actions =
    record.origin === "restored"
      ? (record.restoration?.actions || [])
          .map((action) => action.executed)
          .filter(Boolean)
      : record.actions || [];
  return (
    actions.map((action) => verbs[action.name] ?? action.name).join("、") ||
    (record.origin === "terminal"
      ? "终止观测"
      : record.origin === "restored"
        ? "未记录执行"
        : "无动作")
  );
}

export function restoredActionOutcome(action) {
  if (!action.executed) return "not executed";
  if (action.live_error || action.live_success === false) return "failed";
  if (action.live_success === true) return "success";
  return "unknown";
}

/**
 * A verdict's pass or fail, as a badge value. A verdict that judged nothing is
 * `not_judged`, never a failed task; one that found only a score has no pass
 * or fail (`null`), and the page shows its score instead (`verdictScore`).
 */
export function verdictLabel(verdict) {
  if (verdict.status !== "judged") return "not_judged";
  return verdict.label === 1
    ? "success"
    : verdict.label === 0
      ? "failure"
      : null;
}

/** A judged verdict's score, on its benchmark's own scale; null when it found none. */
export function verdictScore(verdict) {
  return verdict.status === "judged" && verdict.score != null
    ? `分数 ${Number(verdict.score).toLocaleString("zh-CN")}`
    : null;
}

/** Where a verifier judged: at the scoring point, against the live environment, or offline from the trace. */
const ATTACHMENT_LABELS = { live: "活体判分", offline: "离线判分" };
export const attachmentLabel = (value) => ATTACHMENT_LABELS[value] ?? String(value);

/** Missing call channels are unknown, while a selected empty channel is zero. */
export function trailingCallCount(run) {
  if (run.config.recording.metrics) return run.trailing_call_metrics.length;
  if (run.config.recording.trajectory) return run.trailing_calls.length;
  return null;
}

export function historyLabel(source) {
  return source === "source"
    ? "沿用原始历史"
    : source === "restored"
      ? "使用恢复后的历史"
      : "历史来源未知";
}

export function historyExplanation(source) {
  if (source === "source")
    return "智能体保留原来的对话。实际回放中出现的差异不会替换进这段历史。";
  if (source === "restored")
    return "智能体收到的是实际恢复出的观测、重新定位后的动作和实际返回的结果。早先的回复仍标为来源材料。";
  return "未记录历史策略。仅凭来源材料无法确定智能体用的是哪一种历史。";
}

export function cursorLabel(fragments) {
  const fragment = Object.entries(fragments || {}).find(([key]) =>
    key.endsWith(".screenshot"),
  )?.[1];
  if (!fragment || fragment.error) return "没有光标信息";
  if (!fragment.cursor) return "未开启光标显示";
  if (!fragment.cursor.position)
    return "已开启光标显示 · 位置未知（没有画出光标）";
  return `已画出光标 · 位置 (${fragment.cursor.position.join(", ")}) CSS 像素 · 大小 ${fragment.cursor.size_css_px} CSS 像素`;
}

/** Why a restored step did not pass, by the reason a check recorded (`RestorationCheck.reasons`). */
const RESTORATION_REASONS = {
  foreground_mismatch: "前台应用不一致",
  clock_difference: "设备时钟不一致",
  ui_tree_mismatch: "界面树不一致",
  keyboard_focus_mismatch: "键盘或焦点不一致",
  screenshot_distance: "截图差异超限",
  document_miss: "页面没有回放命中",
  url_mismatch: "网址不一致",
  signature_mismatch: "页面结构不一致",
  media_mismatch: "媒体状态不一致",
  retarget_not_found: "找不到原动作的目标",
  retarget_ambiguous: "原动作的目标不唯一",
  action_failed: "动作结果不同",
};
export const reasonLabel = (value) =>
  RESTORATION_REASONS[value] || String(value).replaceAll("_", " ");

/** The capabilities whose evidence this view does not read (`not_shown`: "<capability>: not shown in this view"). */
export function notShownCapabilities(gap) {
  return (gap?.not_shown || []).map((item) => item.split(":")[0]);
}

// `everywhere` lists the facts no checked step of the run measured
// (`run.unmeasured_everywhere.facts`): the run states them once, so a step
// is incomplete only for the unmeasured facts that vary between steps.
// Evidence of a capability this view does not read is never "no measured
// difference": its own checks decided the verdict, and may have failed.
export function gapLabel(gap, everywhere = []) {
  if (!gap) return "无对比";
  if (gap.differences.length) return `测得 ${gap.differences.length} 处差异`;
  const hidden = notShownCapabilities(gap);
  if (hidden.length) return `${hidden.join("、")} 的测量不在这一视图里展示`;
  const varying = gap.unmeasured.filter((item) => !everywhere.includes(item));
  return varying.length ? "对比不完整" : "未测得差异";
}

// The run-level statement of those facts: unknown, never a zero difference.
export function everywhereLabel(everywhere) {
  if (!everywhere?.facts?.length) return null;
  const where =
    everywhere.checked_steps === 1
      ? "唯一一个已检查的步"
      : `全部 ${everywhere.checked_steps} 个已检查的步`;
  return `${where}都未测（结果未知）：${everywhere.facts.join("；")}`;
}
export function modelCallTitle(call) {
  if (call.purpose === "replay_review") return "回放审阅";
  if (call.purpose === "prompt_helper") return "提示词辅助";
  if (call.purpose === "agent_decision") return "智能体决策";
  return call.purpose || "模型调用";
}

export function decisionAnswerRows(result) {
  return Object.entries(result?.answers || {}).map(([id, answer]) => ({
    id,
    type: answer.type,
    value: answer.choice ?? answer.score ?? answer.noul ?? answer.box,
    confidence: answer.confidence ?? null,
    probabilities: answer.probabilities ?? null,
    legend: answer.legend ?? null,
  }));
}
