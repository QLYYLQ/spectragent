/** Names describe actions actually submitted in this run, including restoration. */
/** Browser actions by the verb a person reads; any other action keeps its recorded name. */
const ACTION_VERBS = {
  click: "点击",
  click_coordinate: "点击",
  type: "输入",
  type_coordinate: "输入",
  send_keys: "按键",
  scroll: "滚动",
  drag: "拖动",
  wait: "等待",
  done: "完成",
  navigate: "打开网址",
  go_back: "后退",
  go_forward: "前进",
  switch_tab: "切换标签页",
  select_dropdown: "下拉选择",
  extract_content: "读取页面",
  screenshot: "截图",
};

export function actionVerb(name) {
  const local = String(name).split(".").pop();
  return name.startsWith("browser.") && ACTION_VERBS[local] ? ACTION_VERBS[local] : name;
}

export function actionLabel(record) {
  const actions =
    record.origin === "restored"
      ? (record.restoration?.actions || [])
          .map((action) => action.executed)
          .filter(Boolean)
      : record.actions || [];
  return (
    actions.map((action) => actionVerb(action.name)).join("、") ||
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

export function gapLabel(gap) {
  if (!gap) return "无对比";
  if (gap.differences.length) return `测得 ${gap.differences.length} 处差异`;
  return gap.unmeasured.length ? "对比不完整" : "未测得差异";
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
