/** Shared viewer primitives: formatting, routing, remote reads and small UI parts. */
import React, { createContext, useEffect, useState } from "react";

export const number = (value) =>
  value == null ? "—" : Number(value).toLocaleString("zh-CN");
export const text = (value) =>
  value == null || value === ""
    ? "—"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
export const duration = (ms) =>
  ms == null
    ? "—"
    : ms < 1000
      ? `${ms} 毫秒`
      : ms < 60000
        ? `${(ms / 1000).toFixed(1)} 秒`
        : `${(ms / 60000).toFixed(1)} 分钟`;
export const date = (value) =>
  !value ? "—" : new Date(value).toLocaleString("zh-CN");
export const query = (path, params = {}) =>
  `/api/${path}?${new URLSearchParams(params)}`;
export const routeFromHash = () =>
  Object.fromEntries(new URLSearchParams(window.location.hash.slice(1)));
export const navigate = (route) => {
  window.location.hash = new URLSearchParams(route).toString();
};
export const runRoute = (key, extra = {}) => ({ view: "run", key, ...extra });

export function Icon({ name, ...props }) {
  const paths = {
    runs: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    reports: (
      <>
        <path d="M5 3h10l4 4v14H5z" />
        <path d="M14 3v5h5M8 12h8M8 16h6" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2" />
      </>
    ),
    back: <path d="m14 6-6 6 6 6" />,
    search: (
      <>
        <circle cx="10" cy="10" r="6" />
        <path d="m15 15 6 6" />
      </>
    ),
    arrow: <path d="m9 6 6 6-6 6" />,
    compare: (
      <>
        <path d="M8 3v18M16 3v18M4 7l4-4 4 4M12 17l4 4 4-4" />
      </>
    ),
    image: (
      <>
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8" cy="8" r="1" />
        <path d="m3 17 6-6 4 4 3-3 5 5" />
      </>
    ),
    review: (
      <>
        <path d="M4 7V4h3M10 4h4M17 4h3v3M20 10v4M20 17v3h-3M14 20h-4M7 20H4v-3M4 14v-4" />
        <path d="m8.5 12 2.5 2.5 4.5-5" />
      </>
    ),
    keyboard: (
      <>
        <rect x="2.5" y="6" width="19" height="12" rx="1.5" />
        <path d="M6 10h1M9.5 10h1M13 10h1M16.5 10h1M7 14h10" />
      </>
    ),
    close: <path d="M6 6l12 12M18 6 6 18" />,
  };
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name] || paths.runs}
    </svg>
  );
}

export function useRemote(url, revision) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: Boolean(url),
    url,
  });
  useEffect(() => {
    if (!url) {
      setState({ data: null, error: null, loading: false, url });
      return;
    }
    const controller = new AbortController();
    let active = true;
    setState((previous) =>
      previous.url === url && previous.data !== null
        ? { ...previous, error: null, loading: false }
        : { data: null, error: null, loading: true, url },
    );
    fetch(url, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok)
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : JSON.stringify(data.detail || data),
          );
        return data;
      })
      .then((data) => {
        if (active) setState({ data, error: null, loading: false, url });
      })
      .catch((error) => {
        if (active && error.name !== "AbortError")
          setState({ data: null, error: error.message, loading: false, url });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [url, revision]);
  // Effects run after rendering. A new route must never render the previous
  // request's evidence (or a null idle value) under its new identifiers.
  return state.url === url
    ? state
    : { data: null, error: null, loading: Boolean(url), url };
}

/** Display names for recorded status and kind values; the values themselves stay English. */
const STATUS_LABELS = {
  // Recording completeness
  complete: "完整",
  incomplete: "不完整",
  writing: "写入中",
  // How the agent and the environment ended
  done: "已完成",
  completed: "已完成",
  error: "出错",
  failed: "失败",
  failure: "失败",
  success: "成功",
  cancelled: "已取消",
  timeout: "超时",
  max_steps: "达到步数上限",
  vetoed: "被否决",
  // Step origin
  policy: "策略步",
  restored: "恢复步",
  terminal: "终止观测",
  // Restoration checks and restored actions
  ok: "通过",
  warn: "有差异",
  diverged: "已偏离",
  "not executed": "未执行",
  matches: "一致",
  changed: "有变化",
  unavailable: "不可用",
  source_history: "沿用原始历史",
  restored_history: "使用恢复后的历史",
  history_unknown: "历史来源未知",
  // Execution kind and network source
  live: "实时",
  replay: "回放",
  branch: "分叉",
  branch_replay: "分叉回放",
  archive: "归档",
  unknown: "未知",
  // Where a source run was found
  found: "已找到",
  missing: "缺失",
  ambiguous: "有多个候选",
  outside_root: "不在根目录内",
  none: "无",
  inconsistent_reference: "引用不一致",
  missing_step: "缺少该步",
  unreadable: "无法读取",
  // Replay log decisions
  hit: "命中",
  miss: "未命中",
  exhausted: "已用尽",
  substituted: "已替代",
  synthetic: "合成",
  summary: "汇总",
  // Model calls and decision answers
  chat: "对话",
  decision: "判定",
  choice: "选择",
  score: "评分",
  noul: "是非概率",
  box: "框选区域",
  // Evaluation verdict sources
  graded: "已评分",
  not_evaluated: "未评测",
  evaluation_error: "评测出错",
  // Replay review items
  assessed: "已评估",
  missing_source: "缺少原始记录",
  scope_unrecorded: "未记录范围",
  missing_evidence: "缺少证据",
  corrupt_evidence: "证据损坏",
  alignment_error: "对齐出错",
  unsupported_input: "不支持的输入",
  input_limit: "超出输入上限",
  http_error: "HTTP 出错",
  transport_error: "传输出错",
  invalid_response: "回复无效",
  review_error: "审阅出错",
  candidate_acceptable: "候选可接受",
  investigate_difference: "需调查差异",
  further_review: "需进一步审阅",
  // Labels and review queues
  accepted: "已接受",
  corrected: "已修正",
  rejected: "已驳回",
  skipped: "已跳过",
  labeled: "已标注",
  pending: "待处理",
  // Message roles
  user: "用户",
  assistant: "助手",
  system: "系统",
  tool: "工具",
  developer: "开发者",
};

/** The Chinese display name of a recorded value; unknown values keep their own text. */
export const statusLabel = (value) =>
  value == null || value === ""
    ? "未记录"
    : Object.hasOwn(STATUS_LABELS, String(value))
      ? STATUS_LABELS[String(value)]
      : String(value).replaceAll("_", " ");

export function Badge({ value, kind = "" }) {
  const label = statusLabel(value);
  const tone = [
    "incomplete",
    "error",
    "failed",
    "failure",
    "vetoed",
    "diverged",
    "miss",
  ].includes(String(value))
    ? "bad"
    : [
          "writing",
          "timeout",
          "cancelled",
          "max_steps",
          "restored",
          "warn",
          "exhausted",
          "substituted",
        ].includes(String(value))
      ? "warn"
      : ["complete", "completed", "success", "policy", "ok", "hit"].includes(
            String(value),
          )
        ? "good"
        : "neutral";
  return <span className={`badge ${tone} ${kind}`}>{label}</span>;
}

export function Empty({ title, children }) {
  return (
    <div className="empty">
      <Icon name="runs" width="30" height="30" />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

export function RemoteState({ state, children }) {
  if (state.error)
    return (
      <div role="alert" className="notice error">
        <strong>无法读取这份记录</strong>
        <p>{state.error}</p>
      </div>
    );
  if (state.loading)
    return (
      <div className="loading" role="status">
        <span className="spinner" />
        正在读取已提交的记录…
      </div>
    );
  return children(state.data);
}

/** A JSON write to this viewer; the browser adds the Origin header the server checks. */
export async function postJSON(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : data.detail
          ? data.detail
              .map((problem) => `${(problem.loc || []).slice(1).join(".")}: ${problem.msg}`)
              .join("; ")
          : `查看器返回了 ${response.status}`,
    );
    error.status = response.status;
    throw error;
  }
  return data;
}

/** Replace the hash route without adding a history entry (item-to-item review moves). */
export function replaceRoute(route) {
  window.history.replaceState(null, "", `#${new URLSearchParams(route)}`);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

/** The annotation directory and annotator when labeling is on, else null. */
export const AnnotationContext = createContext(null);

export function Keycap({ children }) {
  return <kbd className="keycap">{children}</kbd>;
}
