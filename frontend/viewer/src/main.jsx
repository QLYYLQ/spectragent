import React, { useContext, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { startupRoute } from "./routing.js";
import {
  actionLabel,
  restoredActionOutcome,
  trailingCallCount,
  verdictLabel,
  historyLabel,
  historyExplanation,
  cursorLabel,
  everywhereLabel,
  gapLabel,
  modelCallTitle,
  decisionAnswerRows,
} from "./evidence.js";
import {
  number,
  text,
  duration,
  date,
  query,
  routeFromHash,
  navigate,
  runRoute,
  Icon,
  useRemote,
  Badge,
  statusLabel,
  Empty,
  RemoteState,
  AnnotationContext,
} from "./common.jsx";
import { ReviewDisabled, ReviewHome, ReviewSession } from "./review.jsx";
import { StepLabels } from "./stepLabels.jsx";
import { MarkLayer, MarkLegend, MarkToggle } from "./marks.jsx";
import { hasMarks } from "./marks.js";
import "./styles.css";

/** Display names for the restorer's verify setting (``strict`` or ``warn``). */
const VERIFY_LABELS = { strict: "严格", warn: "仅提示" };
const verifyLabel = (value) => VERIFY_LABELS[value] || value || "未知";

/** Display names for the phase a step failed in. */
const PHASE_LABELS = {
  observe: "观测",
  prompt: "构建提示词",
  parse: "解析模型输出",
  act: "执行动作",
  replay: "回放",
  restore: "恢复",
};

/** Display names for report kinds. */
const REPORT_KINDS = {
  replay_review: "回放审阅",
  evaluation: "评测",
  flywheel: "数据飞轮",
  session: "会话",
  diagnostics: "诊断",
  dataset: "数据集",
  unreadable: "无法读取",
};

/** Display names for replay review threshold scopes. */
const SCOPE_LABELS = { default: "默认", site: "站点", task: "任务" };

const yesNo = (value) => (value === true ? "是" : value === false ? "否" : text(value));

function downloadJSON(value, filename) {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function JsonBlock({ value }) {
  const [full, setFull] = useState(false);
  const output =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (output == null) return <p className="muted">未记录。</p>;
  const large = output.length > 40000;
  return (
    <>
      <pre className="json" tabIndex="0">
        {large && !full ? output.slice(0, 40000) : output}
      </pre>
      {large && (
        <button className="text-button" onClick={() => setFull(!full)}>
          {full
            ? "只显示预览"
            : `显示全部 ${number(output.length)} 个字符`}
        </button>
      )}
    </>
  );
}

function JsonPanel({ title, value, initialOpen = false }) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <details
      className="json-panel"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        {title}
        <span className="summary-hint">JSON</span>
      </summary>
      {open && (
        <div className="json-panel-body">
          <div className="json-tools">
            <button
              className="text-button"
              onClick={() =>
                downloadJSON(
                  value,
                  `${
                    title
                      .toLowerCase()
                      .replaceAll(/[^a-z0-9\u4e00-\u9fff]+/g, "-")
                      .replaceAll(/^-+|-+$/g, "") || "record"
                  }.json`,
                )
              }
            >
              下载 JSON ↓
            </button>
          </div>
          <JsonBlock value={value} />
        </div>
      )}
    </details>
  );
}

function Stat({ label, value, detail }) {
  return (
    <div className="stat">
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
      {detail && <span className="muted small">{detail}</span>}
    </div>
  );
}

function Verdicts({ verdicts = [], evaluationError }) {
  if (evaluationError)
    return (
      <span className="row-error" title={evaluationError}>
        评测结果无法读取
      </span>
    );
  return verdicts.length ? (
    <div className="verdicts">
      {verdicts.map((verdict, i) => (
        <div key={`${verdict.evaluator}-${i}`}>
          <span className="verdict-name">{verdict.evaluator}</span>
          <Badge value={verdictLabel(verdict)} />
          <span className="muted small">
            {statusLabel(verdict.verdict_source)}
          </span>
        </div>
      ))}
    </div>
  ) : (
    <span className="muted">未评测</span>
  );
}

function Ledger({ row }) {
  return (
    <div className="ledger-grid">
      <div>
        <span className="eyebrow">智能体结果</span>
        <Badge value={row.agent_done} />
        <small>智能体是怎样结束的</small>
      </div>
      <div>
        <span className="eyebrow">环境</span>
        <Badge value={row.env_status} />
        <small>记录下的环境状态</small>
      </div>
      <div>
        <span className="eyebrow">记录状态</span>
        <Badge value={row.trace_status} />
        <small>
          {row.complete_through >= 0
            ? `直到第 ${row.complete_through} 步都完整`
            : "没有完整记录的步"}
        </small>
      </div>
      <div>
        <span className="eyebrow">评测</span>
        <Verdicts
          verdicts={row.verdicts}
          evaluationError={row.evaluation_error}
        />
      </div>
    </div>
  );
}

function RunList({ revision, selected, setSelected, workspace }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [benchmark, setBenchmark] = useState("");
  const [execution, setExecution] = useState("");
  const state = useRemote("/api/runs", revision);
  const rows = state.data || [];
  const visible = rows.filter(
    (row) =>
      (!filter || row.trace_status === filter) &&
      (!benchmark || row.benchmark === benchmark) &&
      (!execution || row.execution_kind === execution) &&
      [
        row.task,
        row.task_id,
        row.run_id,
        row.key,
        row.model,
        row.agent_id,
        row.benchmark,
      ].some((value) =>
        String(value || "")
          .toLowerCase()
          .includes(search.toLowerCase()),
      ),
  );
  const benchmarks = [
    ...new Set(rows.map((row) => row.benchmark).filter(Boolean)),
  ].sort();
  function toggle(key) {
    setSelected((old) =>
      old.includes(key)
        ? old.filter((item) => item !== key)
        : old.length < 8
          ? [...old, key]
          : old,
    );
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">实验工作区</span>
          <h1>运行</h1>
          <p>从任务开始，一路查看到评测判定的证据。</p>
        </div>
        <div className="inline">
          <button
            className="button"
            onClick={() => navigate({ view: "groups" })}
          >
            对比实验
          </button>
          <button
            className="button primary"
            disabled={selected.length < 2}
            onClick={() =>
              navigate({ view: "compare", keys: JSON.stringify(selected) })
            }
          >
            <Icon name="compare" />
            对比{selected.length > 0 ? ` (${selected.length})` : ""}
          </button>
        </div>
      </div>
      <div className="stats">
        <Stat label="已记录的运行" value={number(rows.length)} />
        <Stat
          label="未关闭的记录"
          value={number(
            rows.filter((row) => row.trace_status === "writing").length,
          )}
          detail="可能仍在运行，也可能已中断"
        />
        <Stat
          label="不完整的记录"
          value={number(
            rows.filter((row) => row.trace_status === "incomplete").length,
          )}
        />
        <Stat
          label="已有评测"
          value={number(rows.filter((row) => row.verdicts?.length).length)}
        />
      </div>
      <div className="panel">
        <div className="toolbar">
          <label className="search">
            <Icon name="search" />
            <input
              aria-label="搜索运行"
              placeholder="搜索任务、模型、运行…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            aria-label="按记录状态筛选"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="">全部记录状态</option>
            {["complete", "incomplete", "writing"].map((value) => (
              <option key={value} value={value}>
                {statusLabel(value)}
              </option>
            ))}
          </select>
          <select
            aria-label="按运行方式筛选"
            value={execution}
            onChange={(event) => setExecution(event.target.value)}
          >
            <option value="">全部运行方式</option>
            {["live", "replay", "branch", "branch_replay", "unknown"].map(
              (value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ),
            )}
          </select>
          <select
            aria-label="按基准测试筛选"
            value={benchmark}
            onChange={(event) => setBenchmark(event.target.value)}
          >
            <option value="">全部基准测试</option>
            {benchmarks.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </div>
        <RemoteState state={state}>
          {() =>
            !visible.length ? (
              <Empty
                title={
                  rows.length
                    ? "没有符合条件的运行"
                    : workspace.config_source || workspace.config_hash
                      ? "没有使用这个配置的运行记录"
                      : "没有找到运行记录"
                }
              >
                {rows.length
                  ? "换个搜索词或筛选条件试试。"
                  : workspace.config_source
                    ? "这个根目录下没有来自这个配置文件的运行记录。配置来源不明的旧记录不在其中。存放在别处的运行需要换一个根目录打开。"
                    : workspace.config_hash
                      ? "这个根目录下没有使用这个配置摘要的运行记录。存放在别处的运行需要换一个根目录打开。"
                      : "请选择包含运行记录的目录，然后刷新。这里只显示已提交的记录。"}
              </Empty>
            ) : (
              <div className="table-scroll">
                <table className="runs-table">
                  <thead>
                    <tr>
                      <th className="check-col">
                        <span className="sr-only">对比</span>
                      </th>
                      <th>任务 / 运行</th>
                      <th>智能体 / 模型</th>
                      <th>运行方式</th>
                      <th>智能体结果</th>
                      <th>环境</th>
                      <th>记录状态</th>
                      <th>评测</th>
                      <th className="numeric">步数</th>
                      <th className="numeric">Token</th>
                      <th className="numeric">用时</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row) => (
                      <tr
                        key={row.key}
                        className={
                          selected.includes(row.key) ? "selected-row" : ""
                        }
                      >
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`对比 ${row.task_id || row.run_id || row.key}`}
                            checked={selected.includes(row.key)}
                            disabled={
                              selected.length >= 8 &&
                              !selected.includes(row.key)
                            }
                            onChange={() => toggle(row.key)}
                          />
                        </td>
                        <td className="task-cell">
                          <button
                            className="run-link"
                            onClick={() => navigate(runRoute(row.key))}
                          >
                            {row.task || row.task_id || row.run_id || row.key}
                          </button>
                          <div className="muted small mono" title={row.key}>
                            {row.task_id || row.run_id || row.key}
                          </div>
                          <div className="muted small">
                            {row.benchmark || "未指定基准测试"} ·{" "}
                            {date(row.created_at)}
                          </div>
                          <div
                            className="muted small"
                            title={row.config_source?.path}
                          >
                            {row.config_source
                              ? `${row.config_source.path.split("/").pop()} · 来源 ${row.config_source.sha256.slice(0, 10)}`
                              : "配置来源未知"}
                          </div>
                          {row.read_error && (
                            <span className="row-error" title={row.read_error}>
                              记录无法读取
                            </span>
                          )}
                        </td>
                        <td className="model-cell">
                          <span>{text(row.agent_id)}</span>
                          <small className="muted">{text(row.model)}</small>
                        </td>
                        <td>
                          <Badge value={row.execution_kind || "unknown"} />
                          {row.branch && (
                            <small className="strategy-label">
                              {historyLabel(row.strategy?.history_source)}
                              <br />
                              检查点 {row.branch.checkpoint_step} · 校验{" "}
                              {verifyLabel(row.strategy?.verify)}
                            </small>
                          )}
                        </td>
                        <td>
                          <Badge value={row.agent_done} />
                        </td>
                        <td>
                          <Badge value={row.env_status} />
                        </td>
                        <td>
                          <Badge value={row.trace_status} />
                        </td>
                        <td>
                          <Verdicts
                            verdicts={row.verdicts}
                            evaluationError={row.evaluation_error}
                          />
                        </td>
                        <td className="numeric">
                          {number(row.policy_steps)}
                          <small className="muted">
                            共 {number(row.total_steps)} 步
                          </small>
                        </td>
                        <td className="numeric">
                          {row.usage_measured === false
                            ? "未统计"
                            : number(row.tokens)}
                          {row.usage_measured &&
                            row.usage_complete === false && (
                              <small className="warn-text">
                                仅已知部分
                              </small>
                            )}
                          {row.usage_missing > 0 && (
                            <small className="warn-text">
                              {row.usage_missing} 次未知
                            </small>
                          )}
                        </td>
                        <td className="numeric">
                          {duration(row.execution_ms)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </RemoteState>
        <div className="table-footer">
          <span>
            显示 {number(visible.length)} 次 / 共 {number(rows.length)} 次运行
          </span>
          <span>
            {selected.length
              ? `已选 ${selected.length} 次 · 最多 8 次`
              : "选择两次或更多运行即可对比"}
          </span>
          {selected.length > 0 && (
            <button className="text-button" onClick={() => setSelected([])}>
              清除选择
            </button>
          )}
        </div>
      </div>
      <p className="footnote">
        智能体结果、环境状态、记录是否完整、评测判定，这几项彼此独立。记录未关闭，并不说明进程仍在运行。
      </p>
    </>
  );
}

function Messages({ messages }) {
  if (!messages?.length) return <p className="muted">未记录消息。</p>;
  return (
    <div className="messages">
      {messages.map((message, index) => (
        <article className="message" key={index}>
          <div className="message-heading">
            <Badge value={message.role} />
            <span className="mono muted small">
              {index + 1}
              {message.name ? ` · ${message.name}` : ""}
            </span>
          </div>
          {typeof message.content === "string" ? (
            <pre className="prose-code">{message.content}</pre>
          ) : Array.isArray(message.content) ? (
            message.content.map((part, partIndex) =>
              part.type === "image_url" ? (
                <div className="prompt-image" key={partIndex}>
                  {/^data:image\/(png|jpeg|gif|webp);base64,/.test(
                    part.image_url?.url || "",
                  ) ? (
                    <details>
                      <summary>
                        提示词中的图片 ·{" "}
                        {part.image_url.detail || "auto"}
                      </summary>
                      <img
                        src={part.image_url.url}
                        alt={`第 ${index + 1} 条消息中的图片`}
                        loading="lazy"
                      />
                    </details>
                  ) : (
                    <p className="small mono">
                      图片引用：{text(part.image_url?.url)}
                    </p>
                  )}
                </div>
              ) : (
                <pre className="prose-code" key={partIndex}>
                  {part.text || part.refusal || text(part)}
                </pre>
              ),
            )
          ) : (
            <JsonBlock value={message.content} />
          )}{" "}
          {message.tool_calls?.length > 0 && (
            <JsonPanel title="工具调用" value={message.tool_calls} />
          )}
        </article>
      ))}
    </div>
  );
}

function DecisionContents({ call }) {
  return <>
    <details className="message-details">
      <summary>判定时的状态与证据</summary>
      <StructuredData value={call.request?.state} />
    </details>
    <JsonPanel title="问题与判定标准" value={call.request?.questions} />
    <div className="decision-images">
      {Object.entries(call.request?.assets || {}).map(([key, asset]) => <figure key={key}>
        <img alt={`判定输入 ${key}`} src={`data:image/${asset.format === "png" ? "png" : "jpeg"};base64,${asset.data_base64}`} />
        <figcaption>{key}</figcaption>
        {asset.provenance && <JsonPanel title="图片来源与处理过程" value={asset.provenance} />}
      </figure>)}
    </div>
    {decisionAnswerRows(call.result).map(answer => <section className="panel padded" key={answer.id}>
      <div className="section-heading"><h4>{answer.id}</h4><Badge value={answer.type} /></div>
      <p>回答：<strong>{text(answer.value)}</strong></p>
      {answer.confidence !== null && <p>模型服务给出的置信度：{answer.confidence}</p>}
      {answer.probabilities && <StructuredData value={answer.probabilities} />}
      {answer.legend && <JsonPanel title="评分说明" value={answer.legend} initialOpen />}
    </section>)}
    {call.result && <JsonPanel title="原始判定结果与用量" value={call.result} />}
  </>;
}

function Calls({ calls, schemas = {} }) {
  if (!calls?.length)
    return (
      <Empty title="这一步没有模型调用">
        恢复步和终止观测可能没有策略模型调用。
      </Empty>
    );
  return (
    <div className="calls">
      {calls.map((call, index) => (
        <article
          className="call-card"
          key={`${call.step}-${call.call}-${index}`}
        >
          <div className="section-heading">
            <h3>
              {modelCallTitle(call)}{" "}
              {call.call ?? index}
              <span className="muted small"> · {call.step == null ? `作业 ${call.owner_id}` : `第 ${call.step} 步`}</span>
            </h3>
            <div className="inline">
              <Badge value={call.kind} />
              <Badge value={call.status} />
              <span className="small mono">{duration(call.latency_ms)}</span>
            </div>
          </div>
          <p className="muted small">
            调用方：{call.caller || "未知"} · 模型：
            {call.model || "未知"}
            {call.purpose === "prompt_helper" &&
              " · 由外部生成的提示词，不是智能体自己的思考。"}
          </p>
          {call.failure && (
            <div className="notice error">
              <strong>{call.failure.error || "模型调用失败"}</strong>
              <p>{call.failure.detail || "详见下方记录的失败信息。"}</p>
              <JsonPanel
                title="失败详情"
                value={call.failure}
                initialOpen
              />
            </div>
          )}
          {call.kind === "decision" ? <DecisionContents call={call} /> : <>
          <details className="message-details" open>
            <summary>
              请求 · {number(call.messages?.length || 0)} 条消息
            </summary>
            <Messages messages={call.messages} />
          </details>
          <JsonPanel title="原始请求消息" value={call.messages} />
          {call.completion && (
            <JsonPanel
              title="记录下的模型回复"
              value={call.completion}
              initialOpen
            />
          )}
          {call.output_format ? (
            <JsonPanel
              title="要求的输出格式"
              value={schemas[call.output_format.sha256] || call.output_format}
            />
          ) : (
            <p className="small muted">
              没有要求结构化输出格式。
            </p>
          )}
          </>}
          <JsonPanel title="完整调用记录" value={call} />
        </article>
      ))}
    </div>
  );
}

function Screenshot({ runKey, step, view = "agent", marks = null }) {
  const [failed, setFailed] = useState(false);
  const src = query("screenshot", { key: runKey, index: step, view });
  useEffect(() => setFailed(false), [src]);
  return failed ? (
    <div className="notice error">
      无法读取已提交的截图。
    </div>
  ) : (
    <figure className="screenshot">
      <a
        href={src}
        target="_blank"
        rel="noreferrer"
        title="按原始尺寸打开截图"
      >
        <span className="screenshot-marked">
          <img
            src={src}
            alt={`第 ${step} 步的${view === "agent" ? "智能体观测" : "浏览器状态"}`}
            onError={() => setFailed(true)}
          />
          {marks && <MarkLayer data={marks} />}
        </span>
      </a>
      <figcaption>
        <Icon name="image" />
        {view === "agent" ? "智能体观测" : "浏览器状态"} · 第 {step} 步
        <a href={src} target="_blank" rel="noreferrer">
          查看原图 ↗
        </a>
      </figcaption>
    </figure>
  );
}

function ImageViewSwitch({ view, setView }) {
  return (
    <div className="tabs compact image-switch" aria-label="截图视图">
      {[
        ["agent", "智能体观测"],
        ["browser", "浏览器状态 · 原始画面"],
      ].map(([key, label]) => (
        <button
          key={key}
          className={view === key ? "active" : ""}
          aria-pressed={view === key}
          onClick={() => setView(key)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function ObservationImage({ runKey, step, fragments }) {
  const [view, setView] = useState("agent");
  const [showMarks, setShowMarks] = useState(true);
  const marks = useRemote(query("marks", { key: runKey, index: step }));
  const shown = hasMarks(marks.data);
  return (
    <section>
      <div className="image-tools">
        <ImageViewSwitch view={view} setView={setView} />
        {shown && <MarkToggle show={showMarks} onChange={setShowMarks} keycap={false} />}
      </div>
      <p className="muted small">
        {view === "agent"
          ? cursorLabel(fragments)
          : "未加任何标记的原始截图 · 用于恢复和评测"}
        {shown && " · 编号标记是这一步在这一屏上做的动作，编号和下面的动作列表一致"}
      </p>
      <Screenshot
        runKey={runKey}
        step={step}
        view={view}
        marks={shown && showMarks ? marks.data : null}
      />
      {shown && <MarkLegend data={marks.data} hidden={!showMarks} />}
      {marks.error && <p className="muted small">动作标记读不出来：{marks.error}</p>}
      {view === "agent" && (
        <p className="muted small">
          根据记录的观测信息重建。实际发给模型的提示词见“模型调用”。
        </p>
      )}
    </section>
  );
}

function GapSummary({ gap, everywhere = [] }) {
  if (!gap) return null;
  return (
    <div className={`gap-summary ${gap.differences.length ? "has-gap" : ""}`}>
      <div className="section-heading">
        <strong>{gapLabel(gap, everywhere)}</strong>
        <span className="inline small">
          校验 <Badge value={gap.verdict} />
        </span>
      </div>
      {gap.differences.length > 0 && (
        <ul>
          {gap.differences.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
      {gap.unmeasured.length > 0 && (
        <details>
          <summary>{gap.unmeasured.length} 项无法对比</summary>
          <ul>
            {gap.unmeasured.map((item) => (
              <li key={item}>
                {item}
                {everywhere.includes(item) && (
                  <span className="muted small">（每个已检查的步都未测）</span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="muted small">
        校验按配置的容差进行。校验通过或 dHash 距离为 0，并不能证明截图完全相同。
      </p>
    </div>
  );
}

function RestorationComparison({ step, runKey, index, everywhere }) {
  const [view, setView] = useState("agent");
  const comparison = step.comparison;
  const source = comparison.source;
  const check = step.record.restoration;
  const prefix = step.record.origin === "restored";
  return (
    <section className="restoration-comparison">
      <div className="section-heading">
        <h3>原始记录 ↔ 实际回放</h3>
        <Badge
          value={
            comparison.history_source
              ? `${comparison.history_source}_history`
              : "history_unknown"
          }
        />
      </div>
      <p className="history-explanation">
        {historyExplanation(comparison.history_source)}{" "}
        {prefix
          ? "这一恢复步没有做新的策略决策。"
          : "在检查点上，新策略总是使用实际恢复出的观测。"}
      </p>
      <GapSummary gap={step.gap} everywhere={everywhere} />
      <ImageViewSwitch view={view} setView={setView} />
      <div className="restoration-columns">
        <article className="restoration-column source-column">
          <div className="section-heading">
            <h3>原始记录 · 第 {comparison.source_step} 步</h3>
            <span className="eyebrow">
              {prefix && comparison.history_source === "source"
                ? "智能体历史"
                : "参考证据"}
            </span>
          </div>
          {source ? (
            <>
              <p className="muted small">
                {view === "agent"
                  ? cursorLabel(source.fragments)
                  : "原始浏览器状态 · 原始画面"}
              </p>
              {source.has_screenshot ? (
                <Screenshot
                  runKey={comparison.source_key}
                  step={comparison.source_step}
                  view={view}
                />
              ) : (
                <Empty title="原始截图不可用">
                  {source.recording.observations
                    ? "没有已提交的截图。"
                    : "没有记录观测。"}
                </Empty>
              )}
              <p className="observation-summary">
                {source.record.observation_summary}
              </p>
              <button
                className="text-button"
                onClick={() =>
                  navigate(
                    runRoute(comparison.source_key, {
                      step: comparison.source_step,
                    }),
                  )
                }
              >
                打开原始步 ↗
              </button>
              <JsonPanel
                title="原始观测片段"
                value={source.fragments}
              />
            </>
          ) : (
            <div className="notice">
              <strong>
                原始记录不可用 ·{" "}
                {statusLabel(comparison.source_status)}
              </strong>
              <p>
                {comparison.error ||
                  "在所选根目录中打不开原始记录。已保存的恢复测量结果仍会显示。"}
              </p>
            </div>
          )}
          <JsonPanel
            title="智能体早先的回复 · 来源材料"
            value={
              step.record.source_history?.source_reply ??
              source?.record.llm_output
            }
          />
        </article>
        <article className="restoration-column actual-column">
          <div className="section-heading">
            <h3>
              {prefix ? "实际回放" : "检查点状态"} · 第 {index} 步
            </h3>
            <span className="eyebrow">
              {prefix && comparison.history_source === "restored"
                ? "智能体历史"
                : prefix
                  ? "环境证据"
                  : "新策略的输入"}
            </span>
          </div>
          <p className="muted small">
            {view === "agent"
              ? cursorLabel(step.fragments)
              : "恢复后的浏览器状态 · 原始画面"}
          </p>
          {step.has_screenshot ? (
            <Screenshot runKey={runKey} step={index} view={view} />
          ) : (
            <Empty title="回放截图不可用">
              这一步没有已提交的截图。
            </Empty>
          )}
          <p className="observation-summary">
            {step.record.observation_summary}
          </p>
          <JsonPanel
            title="实际观测片段"
            value={step.fragments}
          />
        </article>
      </div>
      {check?.evidence?.map((evidence) => (
        <div className="table-scroll" key={evidence.capability_id}>
          <table className="gap-table">
            <caption>
              {evidence.capability_id} · 记录下的恢复测量结果
            </caption>
            <thead>
              <tr>
                <th>测量项</th>
                <th>原始记录</th>
                <th>实际回放</th>
                <th>对比</th>
              </tr>
            </thead>
            <tbody>
              <tr
                className={evidence.url_match === false ? "difference-row" : ""}
              >
                <th>URL</th>
                <td>{text(evidence.recorded_url)}</td>
                <td>{text(evidence.restored_url)}</td>
                <td>
                  {evidence.url_match == null
                    ? "未测量"
                    : evidence.url_match
                      ? "一致"
                      : "有变化"}
                </td>
              </tr>
              <tr
                className={
                  evidence.signature_match === false ? "difference-row" : ""
                }
              >
                <th>页面结构签名 (DOM)</th>
                <td>原始签名</td>
                <td>
                  {evidence.signature_match == null
                    ? "未知"
                    : `缺少 ${evidence.signature_missing} 项 · 多出 ${evidence.signature_extra} 项`}
                </td>
                <td>
                  {evidence.signature_match == null
                    ? "未测量"
                    : evidence.signature_match
                      ? "一致"
                      : "有变化"}
                </td>
              </tr>
              <tr
                className={
                  evidence.screenshot_distance > 0 ? "difference-row" : ""
                }
              >
                <th>截图 dHash</th>
                <td>原始画面</td>
                <td>原始画面</td>
                <td>
                  {evidence.screenshot_distance == null
                    ? "未测量"
                    : `距离 ${evidence.screenshot_distance}`}
                </td>
              </tr>
              <tr
                className={evidence.document_misses > 0 ? "difference-row" : ""}
              >
                <th>页面文档未命中次数</th>
                <td>—</td>
                <td>{evidence.document_misses ?? "未测量"}</td>
                <td>离线回放证据</td>
              </tr>
            </tbody>
          </table>
        </div>
      ))}
      {!!check?.actions?.length && (
        <section>
          <h3>动作对应关系</h3>
          {check.actions.map((action, i) => (
            <article className="action-correspondence" key={i}>
              <div className="section-heading">
                <strong>
                  动作 {i + 1} · {action.method}
                </strong>
                <Badge value={restoredActionOutcome(action)} />
              </div>
              <div className="restoration-columns">
                <div>
                  <h4>记录的动作</h4>
                  <JsonBlock value={action.recorded} />
                  <p className="small">
                    原始是否成功：{yesNo(action.recorded_success)}
                  </p>
                  <JsonPanel
                    title="原始结果"
                    value={comparison.results?.[i]?.source}
                    initialOpen
                  />
                </div>
                <div>
                  <h4>实际执行</h4>
                  {action.executed ? (
                    <JsonBlock value={action.executed} />
                  ) : (
                    <p>未执行 · {action.failure || "原因未知"}</p>
                  )}
                  <p className="small">
                    实际是否成功：{yesNo(action.live_success)}
                  </p>
                  <p
                    className={
                      comparison.results?.[i]?.status === "changed"
                        ? "gap-label"
                        : "muted small"
                    }
                  >
                    返回结果：
                    {statusLabel(comparison.results?.[i]?.status || "unavailable")}
                  </p>
                  {action.live_error && (
                    <p className="row-error">{action.live_error}</p>
                  )}
                  <JsonPanel
                    title="实际返回结果"
                    value={action.result}
                    initialOpen
                  />
                </div>
              </div>
              {action.detail && <p className="muted small">{action.detail}</p>}
            </article>
          ))}
        </section>
      )}
      {!prefix && (
        <p className="notice">
          恢复会在原始检查点的动作之前停下。本次运行的新决策见“动作”和“模型调用”标签页。
        </p>
      )}
    </section>
  );
}

function StepActions({ record }) {
  const restored = record.origin === "restored";
  const check = record.restoration;
  return (
    <>
      {restored && (
        <>
          <div className="notice">
            <strong>恢复步</strong>
            <p>
              这些动作和结果描述的是本次运行的恢复执行。下方的来源材料单独保留了早先的决策；恢复过程没有产生新的智能体回复或思考。
            </p>
          </div>
          {record.source_history && (
            <JsonPanel
              title="来源材料与恢复对应关系"
              value={record.source_history}
            />
          )}
          {check ? (
            <section>
              <div className="section-heading">
                <h3>实际恢复执行</h3>
                <Badge value={check.verdict} />
              </div>
              {check.reasons.length > 0 && (
                <div className="notice error">{check.reasons.join(", ")}</div>
              )}
              {check.actions.length ? (
                check.actions.map((action, index) => (
                  <article className="call-card" key={index}>
                    <div className="section-heading">
                      <h3>{action.executed?.name || `动作 ${index + 1}`}</h3>
                      <Badge value={restoredActionOutcome(action)} />
                    </div>
                    <p className="muted small">
                      重新定位方式：{action.method}
                      {action.failure ? ` · ${action.failure}` : ""}
                    </p>
                    {action.detail && <p className="small">{action.detail}</p>}
                    {action.live_error && (
                      <div className="notice error">{action.live_error}</div>
                    )}
                    {action.executed ? (
                      <JsonPanel
                        title="已执行的动作"
                        value={action.executed}
                        initialOpen
                      />
                    ) : (
                      <p className="muted">
                        这个动作没有提交执行。
                      </p>
                    )}
                    <JsonPanel
                      title="恢复动作的证据"
                      value={action}
                    />
                  </article>
                ))
              ) : (
                <p className="muted">
                  这次检查没有记录恢复动作。
                </p>
              )}
            </section>
          ) : (
            <div className="notice error">
              没有记录恢复检查，实际重新执行的结果未知。
            </div>
          )}
        </>
      )}
      {check && (
        <JsonPanel
          title={
            restored ? "恢复检查" : "检查点恢复检查"
          }
          value={check}
          initialOpen
        />
      )}
      {record.truncated_actions > 0 && (
        <div className="notice">
          模型给出的 {record.truncated_actions} 个动作超出了动作数量上限，已被丢弃。
        </div>
      )}
      {record.skipped_actions > 0 && (
        <div className="notice">
          第 {record.action_results.length} 个动作失败，这一步停在那里：最后{" "}
          {record.skipped_actions} 个动作没有执行，也没有结果。
        </div>
      )}
      <JsonPanel
        title={restored ? "已执行的恢复动作" : "动作"}
        value={record.actions}
        initialOpen={!restored}
      />
      <JsonPanel
        title={restored ? "实际恢复结果" : "动作结果"}
        value={record.action_results}
        initialOpen={!restored}
      />
      {record.llm_output && (
        <JsonPanel
          title={
            restored
              ? "原始的模型输出解析结果（复制而来）"
              : "模型输出解析结果"
          }
          value={record.llm_output}
        />
      )}
    </>
  );
}

function StepDetail({ runKey, index, revision, everywhere = [] }) {
  const state = useRemote(query("step", { key: runKey, index }), revision);
  const [tab, setTab] = useState("observation");
  const annotations = useContext(AnnotationContext);
  return (
    <RemoteState state={state}>
      {(step) => (
        <div className="step-detail">
          <div className="section-heading">
            <div>
              <h2>第 {step.record.step} 步</h2>
              <div className="inline">
                <Badge value={step.record.origin} />
                <span className="muted small">
                  {duration(step.record.duration_ms)} ·{" "}
                  {step.recording.metrics
                    ? `${number(step.call_metrics.length)} 次模型调用`
                    : "未统计模型调用"}
                  {step.record.recording_ms != null &&
                    ` · 记录耗时 ${duration(step.record.recording_ms)}`}
                </span>
              </div>
            </div>
            <span className="mono muted small">
              {step.record.timestamp
                ? new Date(step.record.timestamp * 1000).toLocaleTimeString(
                    "zh-CN",
                  )
                : ""}
            </span>
          </div>
          {step.record.error && (
            <div className="notice error">
              <strong>
                {step.record.error_phase
                  ? `${PHASE_LABELS[step.record.error_phase] || step.record.error_phase}阶段失败`
                  : "这一步失败"}
              </strong>
              <p>{step.record.error}</p>
            </div>
          )}
          <div className="tabs compact" aria-label="这一步的内容">
            {[
              ["observation", "观测"],
              [
                "actions",
                step.record.origin === "restored"
                  ? "恢复动作"
                  : step.recording.trajectory
                    ? `动作 (${step.record.actions?.length || 0})`
                    : "动作（未记录）",
              ],
              [
                "calls",
                step.recording.metrics
                  ? `模型调用 (${step.call_metrics.length})`
                  : "模型调用",
              ],
              ["archive", `归档证据 (${step.archive.length})`],
              ...(annotations ? [["labels", "标注"]] : []),
            ].map(([key, label]) => (
              <button
                key={key}
                aria-pressed={tab === key}
                className={tab === key ? "active" : ""}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === "observation" && (
            <>
              {step.comparison && (
                <RestorationComparison
                  step={step}
                  runKey={runKey}
                  index={index}
                  everywhere={everywhere}
                />
              )}
              {!step.comparison && step.has_screenshot && (
                <ObservationImage
                  runKey={runKey}
                  step={index}
                  fragments={step.fragments}
                />
              )}
              <p className="observation-summary">
                {step.record.origin === "restored" && (
                  <strong>恢复后的观测：</strong>
                )}
                {step.record.observation_summary}
              </p>
              {!Object.keys(step.fragments).length && (
                <Empty title="没有观测片段">
                  {step.recording.observations
                    ? "这一步没有已提交的观测片段。"
                    : "记录设置中没有选择保存观测。"}
                </Empty>
              )}
              {Object.entries(step.fragments).map(([name, fragment]) => (
                <JsonPanel key={name} title={name} value={fragment} />
              ))}
              {step.record.restoration && (
                <JsonPanel
                  title="恢复检查"
                  value={step.record.restoration}
                  initialOpen
                />
              )}
            </>
          )}
          {tab === "actions" && (
            <>
              {step.recording.trajectory ? (
                <StepActions record={step.record} />
              ) : (
                <Empty title="未记录动作">
                  记录设置中没有选择保存智能体轨迹。
                </Empty>
              )}
              <JsonPanel title="步骤记录" value={step.record} />
            </>
          )}
          {tab === "calls" &&
            (step.recording.trajectory ? (
              <Calls calls={step.calls} schemas={step.output_schemas} />
            ) : (
              <>
                <Empty title="未记录模型的输入和回复">
                  记录设置中没有选择保存智能体轨迹。
                </Empty>
                <JsonPanel
                  title="调用统计与错误"
                  value={step.call_metrics}
                  initialOpen
                />
              </>
            ))}
          {tab === "labels" && annotations && (
            <StepLabels
              runKey={runKey}
              index={index}
              revision={revision}
              annotations={annotations}
              hasScreenshot={step.has_screenshot}
            />
          )}
          {tab === "archive" && (
            <>
              <p className="muted small">
                清单中登记的环境证据。这些条目描述的是已归档的文件；查看器不会回放或执行它们。
              </p>
              {step.archive.length ? (
                <StructuredData value={step.archive} />
              ) : (
                <Empty title="没有归档证据">
                  这一步没有已提交的环境归档记录。
                </Empty>
              )}
            </>
          )}
        </div>
      )}
    </RemoteState>
  );
}

function Trajectory({ run, route, revision }) {
  const selected = Number(route.step ?? run.steps[0]?.step ?? 0);
  const everywhere = run.unmeasured_everywhere?.facts || [];
  if (!run.steps.length)
    return (
      <Empty title="没有已提交的步">
        可以查看“结果与清单”和“收尾调用”，了解准备阶段、取消或中断的模型调用留下的证据。
      </Empty>
    );
  return (
    <div className="trajectory">
      <aside className="timeline" aria-label="步骤时间线">
        <div className="timeline-title">
          已提交的步 <span>{run.steps.length}</span>
        </div>
        {everywhereLabel(run.unmeasured_everywhere) && (
          <p className="everywhere-note muted small">
            {everywhereLabel(run.unmeasured_everywhere)}
          </p>
        )}
        {run.steps.map((step) => (
          <button
            key={step.step}
            className={`timeline-step ${step.step === selected ? "active" : ""}`}
            aria-current={step.step === selected ? "step" : undefined}
            onClick={() =>
              navigate(
                runRoute(route.key, { tab: "trajectory", step: step.step }),
              )
            }
          >
            <div>
              <span className="step-number">
                {String(step.step).padStart(2, "0")}
              </span>
              <Badge value={step.origin} />
              {run.execution.checkpoint === step.step && (
                <span className="checkpoint-label">检查点</span>
              )}
              {step.error && (
                <span className="error-dot" title={step.error}>
                  !
                </span>
              )}
            </div>
            <span className="action-name">
              {run.config.recording.trajectory
                ? actionLabel(step, run.action_verbs)
                : "未记录动作"}
            </span>
            {step.gap && (
              <span
                className={
                  step.gap.differences.length ? "gap-label" : "muted small"
                }
              >
                {gapLabel(step.gap, everywhere)}
              </span>
            )}
            <span className="muted small">
              {duration(step.duration_ms)}
              {step.call_count ? ` · ${step.call_count} 次调用` : ""}
            </span>
          </button>
        ))}
      </aside>
      <StepDetail
        runKey={route.key}
        index={selected}
        revision={revision}
        everywhere={everywhere}
      />
    </div>
  );
}

function Evaluations({ evaluations, error }) {
  if (error)
    return (
      <div className="notice error" role="alert">
        <strong>评测证据无法读取</strong>
        <p>{error}</p>
        <p>这次运行记录的步和模型调用仍可查看。</p>
      </div>
    );
  return !Object.keys(evaluations).length ? (
    <Empty title="未评测">
      评测判定与智能体结果分开记录。运行一次评测即可添加判定。
    </Empty>
  ) : (
    <div className="evaluation-list">
      {Object.entries(evaluations).map(([slug, result]) => (
        <article className="panel padded" key={slug}>
          <div className="section-heading">
            <div>
              <span className="eyebrow">
                {statusLabel(result.verdict_source)}
              </span>
              <h3>{result.evaluator_id || slug}</h3>
            </div>
            <Badge value={verdictLabel(result)} />
          </div>
          <p className="muted small">
            {date(result.timestamp)} · {result.benchmark} / {result.subset}
          </p>
          {result.failure_category && (
            <p>
              失败类别：<strong>{result.failure_category}</strong>
            </p>
          )}
          <JsonPanel
            title="评测详情"
            value={result.evaluation_details}
            initialOpen
          />
          <JsonPanel
            title="评测器配置"
            value={result.evaluator_config}
          />
          <JsonPanel title="完整评测记录" value={result} />
        </article>
      ))}
    </div>
  );
}

function ExecutionContext({ run }) {
  const execution = run.execution;
  if (!execution)
    return <div className="notice">没有运行来源信息。</div>;
  return (
    <section className="panel padded">
      <div className="section-heading">
        <h3>运行来源</h3>
        <Badge value={run.summary.execution_kind || "unknown"} />
      </div>
      <dl className="properties">
        <div>
          <dt>执行循环 (Loop)</dt>
          <dd>{text(run.summary.loop_id)}</dd>
        </div>
        <div>
          <dt>网络来源</dt>
          <dd>{statusLabel(execution.network_source)}</dd>
        </div>
        {run.summary.branch && (
          <>
            <div>
              <dt>智能体历史</dt>
              <dd>{historyLabel(execution.history_source)}</dd>
            </div>
            <div>
              <dt>恢复策略</dt>
              <dd>
                {text(execution.restorer)} · 校验 {verifyLabel(execution.verify)}
              </dd>
            </div>
            <div>
              <dt>分叉后可继续的步数</dt>
              <dd>
                检查点决策 + 之后 {text(execution.continue_steps)} 步 · 上限{" "}
                {text(execution.max_steps)} 步
              </dd>
            </div>
          </>
        )}
        <div>
          <dt>与原始运行的关系</dt>
          <dd>
            {execution.relationship ? statusLabel(execution.relationship) : "—"}
          </dd>
        </div>
        {execution.source_run_id && (
          <div>
            <dt>原始运行</dt>
            <dd>{execution.source_run_id}</dd>
          </div>
        )}
        {execution.checkpoint != null && (
          <div>
            <dt>检查点所在步</dt>
            <dd>{execution.checkpoint}</dd>
          </div>
        )}
        {execution.source_resolution && (
          <div>
            <dt>原始记录位置</dt>
            <dd>{statusLabel(execution.source_resolution)}</dd>
          </div>
        )}
      </dl>
      {run.summary.branch && (
        <>
          <p className="history-explanation">
            {historyExplanation(execution.history_source)}
          </p>
          {run.restoration_overview && (
            <div className="restoration-overview">
              <span>
                恢复检查{" "}
                <strong>
                  {run.restoration_overview.checked}/
                  {run.restoration_overview.expected}
                </strong>
              </span>
              <span>
                通过 <strong>{run.restoration_overview.accepted}</strong>
              </span>
              <span>
                有差异 <strong>{run.restoration_overview.warned}</strong>
              </span>
              <span>
                已偏离 <strong>{run.restoration_overview.diverged}</strong>
              </span>
              <span>
                测得差异的步{" "}
                <strong>
                  {run.restoration_overview.different_steps.length}
                </strong>
              </span>
              {everywhereLabel(run.unmeasured_everywhere) && (
                <span className="everywhere-note">
                  {everywhereLabel(run.unmeasured_everywhere)}
                </span>
              )}
              {run.restoration_overview.different_steps.length > 0 && (
                <button
                  className="text-button"
                  onClick={() =>
                    navigate(
                      runRoute(run.summary.key, {
                        step: run.restoration_overview.different_steps[0],
                      }),
                    )
                  }
                >
                  查看第一处差异 →
                </button>
              )}
            </div>
          )}
        </>
      )}
      {execution.source_key && (
        <button
          className="text-button"
          onClick={() =>
            navigate(
              runRoute(
                execution.source_key,
                execution.checkpoint == null
                  ? {}
                  : { step: execution.checkpoint },
              ),
            )
          }
        >
          {execution.checkpoint == null
            ? "打开原始运行"
            : `打开原始检查点 · 第 ${execution.checkpoint} 步`}{" "}
          ↗
        </button>
      )}
      {run.summary.branch && (
        <JsonPanel title="分叉来源" value={run.summary.branch} />
      )}
    </section>
  );
}

function ReplayLog({ log, runKey }) {
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [limit, setLimit] = useState(100);
  if (!log || (!log.recorded && !log.error))
    return (
      <Empty title="没有回放日志">
        这次运行没有已提交的回放日志。无法从它的动作或最终结果推断回放时每个请求是怎样处理的。
      </Empty>
    );
  const entries = log.entries.filter(
    (entry) =>
      (!kind ||
        (kind === "summary"
          ? Boolean(entry.folded_kind)
          : entry.kind === kind)) &&
      [entry.url, entry.reason, entry.method].some((value) =>
        String(value || "")
          .toLowerCase()
          .includes(search.toLowerCase()),
      ),
  );
  return (
    <>
      {log.error && (
        <div className="notice error" role="alert">
          <strong>回放证据无法读取</strong>
          <p>{log.error}</p>
          <p>下方显示的条目只是部分证据。</p>
        </div>
      )}
      <div className="stats">
        {[
          ["hit", "归档命中"],
          ["miss", "归档未命中"],
          ["exhausted", "已用尽的响应"],
          ["substituted", "替代的响应"],
          ["synthetic", "合成的响应"],
        ].map(([key, label]) => (
          <Stat
            key={key}
            label={label}
            value={log.error ? "—" : number(log.counts?.[key])}
          />
        ))}
      </div>
      <div className="panel">
        <div className="toolbar">
          <label className="search">
            <Icon name="search" />
            <input
              aria-label="搜索回放处理记录"
              placeholder="搜索 URL、请求方法、原因…"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setLimit(100);
              }}
            />
          </label>
          <select
            aria-label="按处理结果筛选"
            value={kind}
            onChange={(event) => {
              setKind(event.target.value);
              setLimit(100);
            }}
          >
            <option value="">全部处理结果</option>
            {[
              "hit",
              "miss",
              "exhausted",
              "substituted",
              "synthetic",
              "summary",
            ].map(
              (value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ),
            )}
          </select>
        </div>
        {entries.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>提交于</th>
                  <th>处理结果</th>
                  <th className="numeric">次数</th>
                  <th>请求</th>
                  <th>原因 / 应答</th>
                  <th>证据</th>
                </tr>
              </thead>
              <tbody>
                {entries.slice(0, limit).map((entry, index) => (
                  <tr key={index}>
                    <td>
                      {entry.step == null ? (
                        "收尾"
                      ) : (
                        <button
                          className="text-button"
                          onClick={() =>
                            navigate(runRoute(runKey, { step: entry.step }))
                          }
                        >
                          第 {entry.step} 步 ↗
                        </button>
                      )}
                    </td>
                    <td>
                      <Badge value={entry.kind} />
                      {entry.folded_kind && (
                        <div className="muted small">
                          {statusLabel(entry.folded_kind)} · 重复
                        </div>
                      )}
                      {entry.diverged && (
                        <div className="row-error">已偏离</div>
                      )}
                    </td>
                    <td className="numeric">{number(entry.weight)}</td>
                    <td>
                      <strong>{entry.method}</strong>
                      <div className="mono small">{entry.url}</div>
                      <div className="muted small">
                        {entry.resource_type}
                        {entry.frame ? ` · 框架 ${entry.frame}` : ""}
                      </div>
                    </td>
                    <td>
                      {text(entry.reason)}
                      <div className="muted small">
                        {text(entry.answer)}
                        {entry.status != null ? ` · ${entry.status}` : ""}
                      </div>
                      {entry.source_outcome && (
                        <div className="muted small">
                          原始结果：{entry.source_outcome}
                        </div>
                      )}
                    </td>
                    <td>
                      <JsonPanel title="回放处理记录" value={entry} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title={
              log.entries.length
                ? "没有符合条件的处理记录"
                : "没有任何处理记录"
            }
          >
            {log.entries.length
              ? "换个搜索词或处理结果试试。"
              : "回放日志已开启，但没有已提交的处理记录。"}
          </Empty>
        )}
        <div className="table-footer">
          <span>
            符合条件 {number(entries.length)} 条 · 共记录{" "}
            {number(log.entries.length)} 条
          </span>
          {entries.length > limit && (
            <button
              className="text-button"
              onClick={() => setLimit(limit + 100)}
            >
              再显示 {Math.min(entries.length - limit, 100)} 条
            </button>
          )}
        </div>
      </div>
      <p className="footnote">
        计数包含汇总条目所代表的重复请求。“第 N 步”指收集这条记录的那次提交；“收尾”条目是在运行结束收尾时收集的。
      </p>
    </>
  );
}

function RunDetail({ route, revision }) {
  const state = useRemote(query("run", { key: route.key }), revision);
  const tab = route.tab || "trajectory";
  return (
    <>
      <button className="back-link" onClick={() => navigate({ view: "runs" })}>
        <Icon name="back" />
        全部运行
      </button>
      <RemoteState state={state}>
        {(run) => (
          <>
            <div className="page-heading run-heading">
              <div>
                <span className="eyebrow">
                  {run.summary.benchmark || "已记录的运行"}
                  {run.summary.subset ? ` / ${run.summary.subset}` : ""}
                </span>
                <h1>
                  {run.summary.task ||
                    run.summary.task_id ||
                    run.summary.run_id}
                </h1>
                <p className="mono small">
                  {run.summary.run_id}
                  {route.key !== "." && ` · ${route.key}`}
                </p>
              </div>
              <button
                className="button"
                onClick={() =>
                  downloadJSON(run, `${run.summary.run_id || "run"}.json`)
                }
              >
                下载运行 JSON ↓
              </button>
            </div>
            <Ledger row={run.summary} />
            <div className="run-meta">
              <span>
                <strong>{text(run.summary.agent_id)}</strong> /{" "}
                {text(run.summary.loop_id)}
              </span>
              <span>{text(run.summary.model)}</span>
              <span>{number(run.summary.policy_steps)} 个策略步</span>
              <span>
                {run.summary.usage_measured === false
                  ? "未统计 token"
                  : `${number(run.summary.tokens)} token${run.summary.usage_complete === false ? " · 仅已知部分" : ""}`}
                {run.summary.usage_missing > 0
                  ? ` · ${run.summary.usage_missing} 次调用没有用量数据`
                  : ""}
              </span>
              <span>{duration(run.summary.execution_ms)}</span>
            </div>
            {run.manifest.incomplete_reason && (
              <div className="notice error">
                <strong>记录不完整</strong>
                <p>{run.manifest.incomplete_reason}</p>
              </div>
            )}
            {run.summary.error && (
              <div className="notice error">
                <strong>运行出错</strong>
                <p>{run.summary.error}</p>
              </div>
            )}
            <ExecutionContext run={run} />
            <div className="tabs" aria-label="运行内容">
              {[
                ["trajectory", "轨迹"],
                [
                  "evaluations",
                  run.evaluation_error
                    ? "评测 (!)"
                    : `评测 (${Object.keys(run.evaluations).length})`,
                ],
                [
                  "trailing",
                  `收尾调用 (${number(trailingCallCount(run))})`,
                ],
                [
                  "replay",
                  run.replay_log?.error
                    ? "回放日志 (!)"
                    : `回放日志 (${run.replay_log?.entries.length || 0})`,
                ],
                ["config", "配置"],
                ["result", "结果与清单"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  aria-pressed={tab === key}
                  className={tab === key ? "active" : ""}
                  onClick={() =>
                    navigate(
                      runRoute(route.key, {
                        tab: key,
                        ...(route.step ? { step: route.step } : {}),
                      }),
                    )
                  }
                >
                  {label}
                </button>
              ))}
            </div>
            {tab === "trajectory" && (
              <Trajectory run={run} route={route} revision={revision} />
            )}
            {tab === "evaluations" && (
              <Evaluations
                evaluations={run.evaluations}
                error={run.evaluation_error}
              />
            )}
            {tab === "trailing" && (
              <>
                <p className="muted">
                  运行收尾时提交、不属于任何已提交步的调用。它们作为证据保留，不算额外的策略步。
                </p>
                {run.config.recording.trajectory ? (
                  <Calls
                    calls={run.trailing_calls}
                    schemas={run.trailing_output_schemas}
                  />
                ) : (
                  <>
                    <Empty title="未记录模型的输入和回复">
                      记录设置中没有选择保存智能体轨迹。
                    </Empty>
                    {run.config.recording.metrics ? (
                      <JsonPanel
                        title="调用统计与错误"
                        value={run.trailing_call_metrics}
                        initialOpen
                      />
                    ) : (
                      <p className="muted">未统计模型调用。</p>
                    )}
                  </>
                )}
              </>
            )}
            {tab === "replay" && (
              <ReplayLog log={run.replay_log} runKey={route.key} />
            )}
            {tab === "config" && (
              <>
                <div className="panel padded">
                  <div className="section-heading">
                    <h2>记录的配置</h2>
                    <code>{run.summary.config_hash || "无摘要"}</code>
                  </div>
                  <p className="muted">
                    这次运行解析后的完整配置，以及运行前协商确定的观测和动作。
                  </p>
                  {run.summary.config_source ? (
                    <dl className="properties">
                      <div>
                        <dt>记录的来源路径</dt>
                        <dd>{run.summary.config_source.path}</dd>
                      </div>
                      <div>
                        <dt>来源文件 SHA-256</dt>
                        <dd className="mono">
                          {run.summary.config_source.sha256}
                        </dd>
                      </div>
                    </dl>
                  ) : (
                    <div className="notice">
                      配置来源未知。这份记录没有写明它来自哪个 YAML 文件。
                    </div>
                  )}
                  <JsonPanel
                    title="配置快照"
                    value={run.config}
                    initialOpen
                  />
                </div>
              </>
            )}
            {tab === "result" && (
              <>
                {run.summary.answer && (
                  <div className="panel padded">
                    <h2>记录的答案</h2>
                    <pre className="prose-code">{text(run.summary.answer)}</pre>
                  </div>
                )}
                <JsonPanel
                  title="智能体结果"
                  value={run.result}
                  initialOpen
                />
                <JsonPanel title="记录清单" value={run.manifest} />
                <JsonPanel title="用量统计" value={run.usage} />
              </>
            )}
          </>
        )}
      </RemoteState>
    </>
  );
}

function StructuredData({ value, depth = 0 }) {
  const [limit, setLimit] = useState(50);
  if (value == null || typeof value !== "object")
    return <span className="data-value">{text(value)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <p className="muted small">没有条目。</p>;
    if (
      value.every(
        (item) => item && typeof item === "object" && !Array.isArray(item),
      )
    ) {
      const keys = [...new Set(value.flatMap((item) => Object.keys(item)))];
      return (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  {keys.map((key) => (
                    <th key={key}>{key.replaceAll("_", " ")}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {value.slice(0, limit).map((row, index) => (
                  <tr key={index}>
                    {keys.map((key) => (
                      <td key={key}>
                        {row[key] && typeof row[key] === "object" ? (
                          <JsonPanel
                            title={key.replaceAll("_", " ")}
                            value={row[key]}
                          />
                        ) : (
                          text(row[key])
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {value.length > limit && (
            <button
              className="text-button"
              onClick={() => setLimit(limit + 100)}
            >
              再显示 {Math.min(100, value.length - limit)} 条（共 {value.length}{" "}
              条）
            </button>
          )}
        </>
      );
    }
    return <JsonBlock value={value} />;
  }
  if (depth > 2) return <JsonPanel title="详情" value={value} />;
  const scalar = Object.entries(value).filter(
    ([, item]) => item == null || typeof item !== "object",
  );
  const nested = Object.entries(value).filter(
    ([, item]) => item != null && typeof item === "object",
  );
  return (
    <div className="structured-data">
      {scalar.length > 0 && (
        <dl className="properties">
          {scalar.map(([key, item]) => (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd>{text(item)}</dd>
            </div>
          ))}
        </dl>
      )}
      {nested.map(([key, item]) => (
        <section className="data-section" key={key}>
          <h3>{key.replaceAll("_", " ")}</h3>
          <StructuredData value={item} depth={depth + 1} />
        </section>
      ))}
    </div>
  );
}

function EvidenceColumn({ summary, revision }) {
  const run = useRemote(query("run", { key: summary.key }), revision);
  const [selected, setSelected] = useState(null);
  const index = selected ?? run.data?.steps[0]?.step;
  const step = useRemote(
    index == null ? null : query("step", { key: summary.key, index }),
    revision,
  );
  return (
    <article className="evidence-column">
      <button
        className="run-link"
        onClick={() => navigate(runRoute(summary.key))}
      >
        {summary.run_id || summary.key}
      </button>
      <p className="muted small">
        {summary.agent_id} · {summary.model}
      </p>
      <RemoteState state={run}>
        {(data) =>
          data.steps.length ? (
            <label className="evidence-step">
              <span className="eyebrow">已提交的步</span>
              <select
                value={index}
                aria-label={`${summary.run_id || summary.key} 的步`}
                onChange={(event) => setSelected(Number(event.target.value))}
              >
                {data.steps.map((item) => (
                  <option key={item.step} value={item.step}>
                    {item.step} · {statusLabel(item.origin)} ·{" "}
                    {data.config.recording.trajectory
                      ? actionLabel(item, data.action_verbs)
                      : "未记录动作"}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p className="muted">没有已提交的步。</p>
          )
        }
      </RemoteState>
      {index != null && (
        <RemoteState state={step}>
          {(data) => (
            <>
              <div className="inline">
                <Badge value={data.record.origin} />
                <span className="muted small">
                  {duration(data.record.duration_ms)}
                </span>
              </div>
              {data.has_screenshot ? (
                <ObservationImage
                  runKey={summary.key}
                  step={index}
                  fragments={data.fragments}
                />
              ) : (
                <div className="empty">
                  <Icon name="image" />
                  <p>
                    {data.recording.observations
                      ? "没有已提交的截图"
                      : "没有选择保存观测"}
                  </p>
                </div>
              )}
              {data.recording.trajectory ? (
                <StepActions record={data.record} />
              ) : (
                <Empty title="未记录动作">
                  记录设置中没有选择保存智能体轨迹。
                </Empty>
              )}
              {data.record.error && (
                <div className="notice error">{data.record.error}</div>
              )}
              <button
                className="text-button"
                onClick={() => navigate(runRoute(summary.key, { step: index }))}
              >
                查看这一步的全部证据 ↗
              </button>
            </>
          )}
        </RemoteState>
      )}
    </article>
  );
}

function ComparedEvidence({ runs, revision }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="panel compared-evidence"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        对比各步证据
        <span className="muted small">截图与动作</span>
      </summary>
      {open && (
        <div className="evidence-columns">
          {runs.map((run) => (
            <EvidenceColumn key={run.key} summary={run} revision={revision} />
          ))}
        </div>
      )}
    </details>
  );
}

function Compare({ route, revision }) {
  let keys = [];
  try {
    const parsed = JSON.parse(route.keys || "[]");
    if (Array.isArray(parsed))
      keys = parsed.filter((key) => typeof key === "string");
  } catch {
    /* Invalid links are rendered below. */
  }
  const params = new URLSearchParams();
  keys.forEach((key) => params.append("key", key));
  const state = useRemote(
    keys.length >= 2 ? `/api/compare?${params}` : null,
    revision,
  );
  return (
    <>
      <button className="back-link" onClick={() => navigate({ view: "runs" })}>
        <Icon name="back" />
        全部运行
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">对比证据</span>
          <h1>运行对比</h1>
          <p>并排查看各次运行的结果、评测判定和配置差异。</p>
        </div>
      </div>
      {keys.length < 2 ? (
        <Empty title="请至少选择两次运行">
          回到运行列表，勾选要对比的运行。
        </Empty>
      ) : (
        <RemoteState state={state}>
          {(comparison) => (
            <>
              {!comparison.same_task && (
                <div className="notice">
                  这些运行并非都针对同一个任务。判定和用量上的差异，可能来自任务本身的不同。
                </div>
              )}
              <div className="table-scroll panel">
                <table className="comparison-table">
                  <thead>
                    <tr>
                      <th>项目</th>
                      {comparison.runs.map((run) => (
                        <th key={run.key}>
                          <button
                            className="run-link"
                            onClick={() => navigate(runRoute(run.key))}
                          >
                            {run.task_id || run.run_id}
                          </button>
                          <span className="muted small mono">{run.key}</span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["智能体", "agent_id"],
                      ["模型", "model"],
                      ["执行循环 (Loop)", "loop_id"],
                      ["运行方式", "execution_kind"],
                      ["智能体结果", "agent_done"],
                      ["环境", "env_status"],
                      ["记录状态", "trace_status"],
                      ["完整记录到第几步", "complete_through"],
                      ["策略步数", "policy_steps"],
                      ["总步数", "total_steps"],
                      ["Token", "tokens"],
                      ["没有用量数据的调用", "usage_missing"],
                      ["运行用时", "execution_ms"],
                    ].map(([label, key]) => (
                      <tr key={key}>
                        <th>{label}</th>
                        {comparison.runs.map((run) => (
                          <td key={run.key}>
                            {[
                              "execution_kind",
                              "agent_done",
                              "env_status",
                              "trace_status",
                            ].includes(key) ? (
                              <Badge value={run[key]} />
                            ) : key === "execution_ms" ? (
                              duration(run[key])
                            ) : (
                              text(run[key])
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                    {comparison.runs.some((run) => run.evaluation_error) && (
                      <tr>
                        <th>评测证据</th>
                        {comparison.runs.map((run) => (
                          <td key={run.key}>
                            <Verdicts
                              verdicts={run.verdicts}
                              evaluationError={run.evaluation_error}
                            />
                          </td>
                        ))}
                      </tr>
                    )}
                    {comparison.evaluator_comparisons.map((item) => (
                      <tr key={item.evaluator}>
                        <th>评测器 · {item.evaluator}</th>
                        {item.verdicts.map((verdict, index) => (
                          <td key={index}>
                            {comparison.runs[index]?.evaluation_error ? (
                              <span
                                className="row-error"
                                title={comparison.runs[index].evaluation_error}
                              >
                                评测结果无法读取
                              </span>
                            ) : verdict == null ? (
                              <span className="muted">未评测</span>
                            ) : (
                              <StructuredData value={verdict} />
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <section className="panel padded">
                <div className="section-heading">
                  <h2>配置差异</h2>
                  <span className="muted small">
                    {comparison.config_differences.length} 处配置不同
                  </span>
                </div>
                {comparison.config_differences.length ? (
                  <div className="table-scroll">
                    <table className="comparison-table">
                      <thead>
                        <tr>
                          <th>配置路径</th>
                          {comparison.runs.map((run) => (
                            <th key={run.key}>{run.run_id || run.key}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {comparison.config_differences.map((difference) => (
                          <tr key={difference.path}>
                            <th className="mono">{difference.path}</th>
                            {difference.values.map((value, index) => (
                              <td key={index}>
                                {value && typeof value === "object" ? (
                                  <JsonBlock value={value} />
                                ) : (
                                  text(value)
                                )}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="muted">
                    记录的配置之间没有差异。
                  </p>
                )}
              </section>
              <ComparedEvidence runs={comparison.runs} revision={revision} />
              <JsonPanel title="对比数据" value={comparison} />
            </>
          )}
        </RemoteState>
      )}
    </>
  );
}

function ExperimentComparison({ route, revision }) {
  const runs = useRemote("/api/runs", revision);
  const [left, setLeft] = useState(route.left || "");
  const [right, setRight] = useState(route.right || "");
  const [evaluator, setEvaluator] = useState(route.evaluator || "");
  const [filter, setFilter] = useState("");
  useEffect(() => {
    setLeft(route.left || "");
    setRight(route.right || "");
    setEvaluator(route.evaluator || "");
  }, [route.left, route.right, route.evaluator]);
  const comparison = useRemote(
    route.left && route.right && route.evaluator
      ? query("compare-groups", {
          left: route.left,
          right: route.right,
          evaluator: route.evaluator,
        })
      : null,
    revision,
  );
  const groups = new Map();
  (runs.data || []).forEach((run) => {
    if (!run.config_hash) return;
    const group = groups.get(run.config_hash) || {
      label: `${run.agent_id || "未知智能体"} · ${run.model || "未知模型"}`,
      count: 0,
    };
    group.count += 1;
    groups.set(run.config_hash, group);
  });
  const evaluators = [
    ...new Set(
      (runs.data || []).flatMap((run) =>
        (run.verdicts || []).map((verdict) => verdict.evaluator),
      ),
    ),
  ].sort();
  const outcomes = {
    both_passed: "两边都通过",
    left_only_passed: "A 通过 / B 失败",
    right_only_passed: "B 通过 / A 失败",
    both_failed: "两边都失败",
    unpaired: "无法配对的任务",
    ungraded: "未评分的任务",
    ambiguous: "多次尝试、无法确定",
    incompatible_evaluator: "评测器设置不同",
  };
  const runLinks = (values) =>
    values.length ? (
      <div className="cohort-runs">
        {values.map((run) => (
          <div key={run.key}>
            <button
              className="run-link"
              onClick={() => navigate(runRoute(run.key))}
            >
              {run.run_id || run.key}
            </button>
            <Badge value={run.agent_done} />
            {run.evaluation_error && (
              <div className="row-error" title={run.evaluation_error}>
                评测结果无法读取
              </div>
            )}
          </div>
        ))}
      </div>
    ) : (
      <span className="muted">没有对应的运行</span>
    );
  return (
    <>
      <button className="back-link" onClick={() => navigate({ view: "runs" })}>
        <Icon name="back" />
        全部运行
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">按任务配对</span>
          <h1>实验对比</h1>
          <p>找出一种配置成功、另一种配置失败的任务。</p>
        </div>
      </div>
      <RemoteState state={runs}>
        {() => (
          <form
            className="panel padded compare-form"
            onSubmit={(event) => {
              event.preventDefault();
              navigate({ view: "groups", left, right, evaluator });
            }}
          >
            {[
              ["A · 配置", left, setLeft],
              ["B · 配置", right, setRight],
            ].map(([label, value, setter]) => (
              <label key={label}>
                <span className="eyebrow">{label}</span>
                <select
                  required
                  value={value}
                  onChange={(event) => setter(event.target.value)}
                >
                  <option value="">选择配置</option>
                  {[...groups.entries()].map(([hash, group]) => (
                    <option key={hash} value={hash}>
                      {hash.slice(0, 10)} · {group.label} · {group.count} 次运行
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <label>
              <span className="eyebrow">评测器</span>
              <select
                required
                value={evaluator}
                onChange={(event) => setEvaluator(event.target.value)}
              >
                <option value="">选择评测器</option>
                {evaluators.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            <button
              className="button primary"
              disabled={!left || !right || left === right || !evaluator}
            >
              对比实验
            </button>
          </form>
        )}
      </RemoteState>
      <p className="footnote">
        按基准测试、子集和任务配对。只对比评测器设置相同、且已评分的判定。同一任务有多次尝试时视为无法确定，不会替你挑选其中一次。
      </p>
      {comparison.data || comparison.loading || comparison.error ? (
        <RemoteState state={comparison}>
          {(data) => (
            <>
              <div className="stats">
                {Object.entries(outcomes).map(([key, label]) => (
                  <Stat
                    key={key}
                    label={label}
                    value={number(data.counts[key])}
                  />
                ))}
              </div>
              <div className="panel padded">
                <div className="outcome-filter">
                  <label htmlFor="outcome-filter">对比结果</label>
                  <select
                    id="outcome-filter"
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                  >
                    <option value="">全部任务</option>
                    {Object.entries(outcomes).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="table-scroll">
                  <table className="comparison-table">
                    <thead>
                      <tr>
                        <th>任务</th>
                        <th>结果</th>
                        <th>A · {data.left.slice(0, 10)}</th>
                        <th>B · {data.right.slice(0, 10)}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.rows
                        .filter((row) => !filter || row.outcome === filter)
                        .map((row, index) => (
                          <tr key={`${row.task_id}-${index}`}>
                            <td>
                              <strong>{row.task_id}</strong>
                              <div className="muted small">
                                {row.benchmark} / {row.subset}
                              </div>
                            </td>
                            <td>{outcomes[row.outcome] || row.outcome}</td>
                            <td>{runLinks(row.left)}</td>
                            <td>{runLinks(row.right)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
                {!data.rows.length && (
                  <Empty title="没有符合条件的任务证据">
                    请选择有任务记录和评测结果的配置。
                  </Empty>
                )}
              </div>
              <JsonPanel title="实验对比数据" value={data} />
            </>
          )}
        </RemoteState>
      ) : (
        <Empty title="请选择两种配置和一个评测器">
          之后可以打开任意一次配对的运行，查看它的轨迹和证据。
        </Empty>
      )}
    </>
  );
}

function ReplayReviewReport({ report }) {
  return <>
    <div className="section-heading"><h3>独立回放审阅</h3><Badge value={report.manifest.status} /></div>
    <StructuredData value={report.manifest.summary} />
    <JsonPanel title="问题、策略与证据选项" value={report.config} />
    {report.items.map(item => <section className="panel padded" key={item.index}>
      <h3>第 {item.index + 1} 组观测</h3>
      <dl className="properties">
        <div><dt>执行状态</dt><dd>{statusLabel(item.assessment.status)}</dd></div>
        <div><dt>信息判定</dt><dd>{item.assessment.information ?? "未评估"}</dd></div>
        <div><dt>阈值</dt><dd>{item.assessment.thresholds ? `${SCOPE_LABELS[item.assessment.thresholds.scope] || item.assessment.thresholds.scope}：≥ ${item.assessment.thresholds.accept_information_min} 接受，≤ ${item.assessment.thresholds.investigate_information_max} 需调查` : "未使用"}</dd></div>
        <div><dt>审阅去向</dt><dd>{item.assessment.route ? statusLabel(item.assessment.route) : "—"}</dd></div>
      </dl>
      <p>{item.assessment.reasons.join(" · ")}</p>
      {item.assessment.detail && <p className="notice error">{item.assessment.detail}</p>}
      <JsonPanel title="证据、来源引用与恢复检查" value={item.evidence} />
      <Calls calls={item.calls} />
    </section>)}
  </>;
}

function Reports({ route, revision, workspace }) {
  const list = useRemote("/api/reports", revision);
  const report = useRemote(
    route.key ? query("report", { key: route.key }) : null,
    revision,
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">研究产物</span>
          <h1>报告与数据集</h1>
          <p>已记录的评测、数据飞轮指标、诊断信息和数据集清单。</p>
        </div>
      </div>
      {(workspace.config_source || workspace.config_hash) && (
        <div className="notice">
          <strong>报告保持原有范围</strong>
          <p>
            报告是在整个根目录中查找的，保留原有的输入和统计分母。配置筛选只作用于运行列表，不会筛选或重新计算报告中的统计。
          </p>
        </div>
      )}
      <div className="report-layout">
        <aside className="panel report-list">
          <div className="timeline-title">产物</div>
          <RemoteState state={list}>
            {(reports) =>
              !reports.length ? (
                <Empty title="没有找到报告">
                  工作区中有报告时，会显示在这里。
                </Empty>
              ) : (
                reports.map((item) => (
                  <button
                    key={item.key}
                    onClick={() => navigate({ view: "reports", key: item.key })}
                    className={`report-button ${route.key === item.key ? "active" : ""}`}
                  >
                    <span className="eyebrow">
                      {REPORT_KINDS[item.kind] || item.kind}
                    </span>
                    <strong>{item.title || item.key}</strong>
                    <span className="mono muted small">{item.key}</span>
                    {item.error && (
                      <span className="row-error">{item.error}</span>
                    )}
                  </button>
                ))
              )
            }
          </RemoteState>
        </aside>
        <div className="report-content">
          {route.key ? (
            <RemoteState state={report}>
              {(item) => (
                <div className="panel padded">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">
                        {REPORT_KINDS[item.kind] || item.kind}
                      </span>
                      <h2>{item.key.split("/").pop()}</h2>
                    </div>
                    <button
                      className="button"
                      onClick={() =>
                        downloadJSON(
                          item.data,
                          item.key.split("/").pop() || "report.json",
                        )
                      }
                    >
                      下载 JSON ↓
                    </button>
                  </div>
                  {item.kind === "replay_review" ? <ReplayReviewReport report={item.data} /> : <StructuredData value={item.data} />}
                  <JsonPanel title="原始报告" value={item.data} />
                </div>
              )}
            </RemoteState>
          ) : (
            <Empty title="请选择一份报告">
              选择左侧的一项产物，查看它记录的测量结果和来源。
            </Empty>
          )}
        </div>
      </div>
    </>
  );
}

function WorkspaceScope({ workspace }) {
  return workspace.config_source || workspace.config_hash ? (
    <div className="notice">
      <strong>
        运行列表筛选 ·{" "}
        {workspace.config_source ? "配置文件历史" : "配置摘要"}
      </strong>
      {workspace.config_source && (
        <p className="mono small">{workspace.config_source}</p>
      )}
      <p>
        {workspace.config_hash ? (
          <>
            配置摘要：
            <span className="mono small">{workspace.config_hash}</span>
          </>
        ) : (
          "包含这个文件所有已记录的版本。配置来源不明的记录不在其中。"
        )}
      </p>
      <p className="small">
        查找根目录：<span className="mono">{workspace.root}</span>
      </p>
    </div>
  ) : (
    <p className="footnote">
      查找根目录：<span className="mono">{workspace.root}</span>
    </p>
  );
}

function App() {
  const [urlRoute, setRoute] = useState(routeFromHash);
  const workspace = useRemote("/api/workspace", 0);
  const route = startupRoute(urlRoute, workspace.data?.initial_run);
  const [revision, setRevision] = useState(0);
  const [live, setLive] = useState(false);
  const [selected, setSelected] = useState([]);
  useEffect(() => {
    const listener = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setRevision((value) => value + 1), 5000);
    return () => clearInterval(timer);
  }, [live]);
  const view = route.view || "runs";
  const annotations = workspace.data?.annotations || null;
  const reviewing = view === "review" && Boolean(route.queue) && Boolean(annotations);
  return (
    <AnnotationContext.Provider value={annotations}>
    <div className={`app ${reviewing ? "focus-mode" : ""}`}>
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content").focus();
        }}
      >
        跳到正文
      </a>
      <aside className="sidebar">
        <a className="brand" href="#view=runs">
          <img
            className="brand-mark"
            src="/brand/mark.svg"
            alt="Spectragent 棱镜标志"
            width="42"
            height="42"
          />
          <span>
            Spectragent<small>实验查看器</small>
          </span>
        </a>
        <div className="nav-label">工作区</div>
        <nav aria-label="主导航">
          <button
            className={!["reports", "review"].includes(view) ? "active" : ""}
            onClick={() => navigate({ view: "runs" })}
          >
            <Icon name="runs" />
            运行
          </button>
          <button
            className={view === "review" ? "active" : ""}
            onClick={() => navigate({ view: "review" })}
          >
            <Icon name="review" />
            审阅
          </button>
          <button
            className={view === "reports" ? "active" : ""}
            onClick={() => navigate({ view: "reports" })}
          >
            <Icon name="reports" />
            报告与数据集
          </button>
        </nav>
        <div className="sidebar-bottom">
          <span className="connection-dot" />
          本地工作区
          {annotations ? (
            <p>
              运行记录只读
              <br />
              标注人：{annotations.annotator}
            </p>
          ) : (
            <p>
              证据只读
              <br />
              不会运行智能体
            </p>
          )}
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            <span className="breadcrumb">工作区</span>
            <Icon name="arrow" />
            {view === "reports"
              ? "报告与数据集"
              : view === "review"
                ? "审阅"
                : view === "run"
                ? "运行详情"
                : ["compare", "groups"].includes(view)
                  ? "对比"
                  : "运行"}
          </span>
          <div className="topbar-actions">
            <label className="auto-refresh">
              <input
                type="checkbox"
                checked={live}
                onChange={(event) => setLive(event.target.checked)}
              />
              自动刷新 · 5 秒
            </label>
            <button
              className="button icon-button"
              aria-label="刷新证据"
              onClick={() => setRevision((value) => value + 1)}
            >
              <Icon name="refresh" />
              刷新
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex="-1">
          <RemoteState state={workspace}>
            {() => (
              <>
                {view !== "review" && <WorkspaceScope workspace={workspace.data} />}
                {view === "run" && route.key ? (
                  <RunDetail route={route} revision={revision} />
                ) : view === "compare" ? (
                  <Compare route={route} revision={revision} />
                ) : view === "groups" ? (
                  <ExperimentComparison route={route} revision={revision} />
                ) : view === "reports" ? (
                  <Reports
                    route={route}
                    revision={revision}
                    workspace={workspace.data}
                  />
                ) : view === "review" ? (
                  !annotations ? (
                    <ReviewDisabled root={workspace.data.root} />
                  ) : route.queue ? (
                    <ReviewSession
                      route={route}
                      revision={revision}
                      annotations={annotations}
                    />
                  ) : (
                    <ReviewHome revision={revision} annotations={annotations} />
                  )
                ) : (
                  <RunList
                    revision={revision}
                    selected={selected}
                    setSelected={setSelected}
                    workspace={workspace.data}
                  />
                )}
              </>
            )}
          </RemoteState>
        </main>
      </div>
    </div>
    </AnnotationContext.Provider>
  );
}

createRoot(document.getElementById("root")).render(<App />);
