import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { startupRoute } from "./routing.js";
import { actionLabel, restoredActionOutcome } from "./evidence.js";
import "./styles.css";

const number = (value) =>
  value == null ? "—" : Number(value).toLocaleString();
const text = (value) =>
  value == null || value === ""
    ? "—"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
const duration = (ms) =>
  ms == null
    ? "—"
    : ms < 1000
      ? `${ms} ms`
      : ms < 60000
        ? `${(ms / 1000).toFixed(1)} s`
        : `${(ms / 60000).toFixed(1)} min`;
const date = (value) => (!value ? "—" : new Date(value).toLocaleString());
const query = (path, params = {}) =>
  `/api/${path}?${new URLSearchParams(params)}`;
const routeFromHash = () =>
  Object.fromEntries(new URLSearchParams(window.location.hash.slice(1)));
const navigate = (route) => {
  window.location.hash = new URLSearchParams(route).toString();
};
const runRoute = (key, extra = {}) => ({ view: "run", key, ...extra });

function Icon({ name, ...props }) {
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

function useRemote(url, revision) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: Boolean(url),
    url,
  });
  useEffect(() => {
    if (!url) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    const controller = new AbortController();
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
      .then((data) => setState({ data, error: null, loading: false, url }))
      .catch((error) => {
        if (error.name !== "AbortError")
          setState({ data: null, error: error.message, loading: false, url });
      });
    return () => controller.abort();
  }, [url, revision]);
  return state;
}

function Badge({ value, kind = "" }) {
  const label =
    value == null || value === ""
      ? "not recorded"
      : String(value).replaceAll("_", " ");
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
        ].includes(String(value))
      ? "warn"
      : ["complete", "completed", "success", "policy", "ok", "hit"].includes(
            String(value),
          )
        ? "good"
        : "neutral";
  return <span className={`badge ${tone} ${kind}`}>{label}</span>;
}

function Empty({ title, children }) {
  return (
    <div className="empty">
      <Icon name="runs" width="30" height="30" />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

function RemoteState({ state, children }) {
  if (state.error)
    return (
      <div role="alert" className="notice error">
        <strong>Could not read this evidence</strong>
        <p>{state.error}</p>
      </div>
    );
  if (state.loading)
    return (
      <div className="loading" role="status">
        <span className="spinner" />
        Reading committed evidence…
      </div>
    );
  return children(state.data);
}

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
  if (output == null) return <p className="muted">Not recorded.</p>;
  const large = output.length > 40000;
  return (
    <>
      <pre className="json" tabIndex="0">
        {large && !full ? output.slice(0, 40000) : output}
      </pre>
      {large && (
        <button className="text-button" onClick={() => setFull(!full)}>
          {full
            ? "Show preview"
            : `Show all ${number(output.length)} characters`}
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
                  `${title.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}.json`,
                )
              }
            >
              Download JSON ↓
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
        Evaluation unreadable
      </span>
    );
  return verdicts.length ? (
    <div className="verdicts">
      {verdicts.map((verdict, i) => (
        <div key={`${verdict.evaluator}-${i}`}>
          <span className="verdict-name">{verdict.evaluator}</span>
          <Badge
            value={
              verdict.predicted_label === 1
                ? "success"
                : verdict.predicted_label === 0
                  ? "failure"
                  : null
            }
          />
          <span className="muted small">{verdict.verdict_source}</span>
        </div>
      ))}
    </div>
  ) : (
    <span className="muted">Not evaluated</span>
  );
}

function Ledger({ row }) {
  return (
    <div className="ledger-grid">
      <div>
        <span className="eyebrow">Agent outcome</span>
        <Badge value={row.agent_done} />
        <small>How the agent ended</small>
      </div>
      <div>
        <span className="eyebrow">Environment</span>
        <Badge value={row.env_status} />
        <small>Recorded environment status</small>
      </div>
      <div>
        <span className="eyebrow">Recording</span>
        <Badge value={row.trace_status} />
        <small>
          {row.complete_through >= 0
            ? `Complete through step ${row.complete_through}`
            : "No complete step recorded"}
        </small>
      </div>
      <div>
        <span className="eyebrow">Evaluation</span>
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
          <span className="eyebrow">Experiment workspace</span>
          <h1>Runs</h1>
          <p>Follow the evidence from task to verdict.</p>
        </div>
        <div className="inline">
          <button
            className="button"
            onClick={() => navigate({ view: "groups" })}
          >
            Compare experiments
          </button>
          <button
            className="button primary"
            disabled={selected.length < 2}
            onClick={() =>
              navigate({ view: "compare", keys: JSON.stringify(selected) })
            }
          >
            <Icon name="compare" />
            Compare{selected.length > 0 ? ` (${selected.length})` : ""}
          </button>
        </div>
      </div>
      <div className="stats">
        <Stat label="Recorded runs" value={number(rows.length)} />
        <Stat
          label="Open traces"
          value={number(
            rows.filter((row) => row.trace_status === "writing").length,
          )}
          detail="May be running or interrupted"
        />
        <Stat
          label="Incomplete traces"
          value={number(
            rows.filter((row) => row.trace_status === "incomplete").length,
          )}
        />
        <Stat
          label="With evaluations"
          value={number(rows.filter((row) => row.verdicts?.length).length)}
        />
      </div>
      <div className="panel">
        <div className="toolbar">
          <label className="search">
            <Icon name="search" />
            <input
              aria-label="Search runs"
              placeholder="Search task, model, run…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            aria-label="Filter by recording status"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="">All recording states</option>
            {["complete", "incomplete", "writing"].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
          <select
            aria-label="Filter by execution kind"
            value={execution}
            onChange={(event) => setExecution(event.target.value)}
          >
            <option value="">All execution kinds</option>
            {["live", "replay", "branch", "branch_replay", "unknown"].map(
              (value) => (
                <option key={value} value={value}>
                  {value.replaceAll("_", " ")}
                </option>
              ),
            )}
          </select>
          <select
            aria-label="Filter by benchmark"
            value={benchmark}
            onChange={(event) => setBenchmark(event.target.value)}
          >
            <option value="">All benchmarks</option>
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
                    ? "No matching runs"
                    : workspace.config_source || workspace.config_hash
                      ? "No matching configuration history"
                      : "No traces found"
                }
              >
                {rows.length
                  ? "Try a different search or filter."
                  : workspace.config_source
                    ? "No recorded run in this root names this configuration source. Older traces with unknown config source are excluded. Runs stored elsewhere require a different root directory."
                    : workspace.config_hash
                      ? "No recorded run in this root has this configuration digest. Runs stored elsewhere require a different root directory."
                      : "Select a directory containing recorded runs, then refresh. Only committed trace evidence appears here."}
              </Empty>
            ) : (
              <div className="table-scroll">
                <table className="runs-table">
                  <thead>
                    <tr>
                      <th className="check-col">
                        <span className="sr-only">Compare</span>
                      </th>
                      <th>Task / run</th>
                      <th>Agent / model</th>
                      <th>Execution</th>
                      <th>Agent</th>
                      <th>Environment</th>
                      <th>Recording</th>
                      <th>Evaluation</th>
                      <th className="numeric">Steps</th>
                      <th className="numeric">Tokens</th>
                      <th className="numeric">Time</th>
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
                            aria-label={`Compare ${row.task_id || row.run_id || row.key}`}
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
                            {row.benchmark || "Unspecified benchmark"} ·{" "}
                            {date(row.created_at)}
                          </div>
                          <div
                            className="muted small"
                            title={row.config_source?.path}
                          >
                            {row.config_source
                              ? `${row.config_source.path.split("/").pop()} · source ${row.config_source.sha256.slice(0, 10)}`
                              : "Config source unknown"}
                          </div>
                          {row.read_error && (
                            <span className="row-error" title={row.read_error}>
                              Unreadable trace
                            </span>
                          )}
                        </td>
                        <td className="model-cell">
                          <span>{text(row.agent_id)}</span>
                          <small className="muted">{text(row.model)}</small>
                        </td>
                        <td>
                          <Badge value={row.execution_kind || "unknown"} />
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
                            {number(row.total_steps)} total
                          </small>
                        </td>
                        <td className="numeric">
                          {number(row.tokens)}
                          {row.usage_missing > 0 && (
                            <small className="warn-text">
                              {row.usage_missing} unknown
                            </small>
                          )}
                        </td>
                        <td className="numeric">{duration(row.duration_ms)}</td>
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
            {number(visible.length)} of {number(rows.length)} runs
          </span>
          <span>
            {selected.length
              ? `${selected.length} selected · maximum 8`
              : "Select two or more runs to compare"}
          </span>
          {selected.length > 0 && (
            <button className="text-button" onClick={() => setSelected([])}>
              Clear selection
            </button>
          )}
        </div>
      </div>
      <p className="footnote">
        Agent outcome, environment status, recording completeness, and evaluator
        verdicts are independent. An open trace does not prove its process is
        still running.
      </p>
    </>
  );
}

function Messages({ messages }) {
  if (!messages?.length) return <p className="muted">No messages recorded.</p>;
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
                        Recorded prompt image ·{" "}
                        {part.image_url.detail || "auto"}
                      </summary>
                      <img
                        src={part.image_url.url}
                        alt={`Prompt image in message ${index + 1}`}
                        loading="lazy"
                      />
                    </details>
                  ) : (
                    <p className="small mono">
                      Image reference: {text(part.image_url?.url)}
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
            <JsonPanel title="Tool calls" value={message.tool_calls} />
          )}
        </article>
      ))}
    </div>
  );
}

function Calls({ calls, schemas = {} }) {
  if (!calls?.length)
    return (
      <Empty title="No model calls in this step">
        Restored and terminal steps may have no policy model call.
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
              Call {call.call ?? index}
              <span className="muted small"> · step {call.step}</span>
            </h3>
            <div className="inline">
              <Badge value={call.status} />
              <span className="small mono">{duration(call.latency_ms)}</span>
            </div>
          </div>
          {call.failure && (
            <div className="notice error">
              <strong>{call.failure.error || "Model call failed"}</strong>
              <p>{call.failure.detail || "See the recorded failure below."}</p>
              <JsonPanel
                title="Failure details"
                value={call.failure}
                initialOpen
              />
            </div>
          )}
          <details className="message-details" open>
            <summary>
              Request · {number(call.messages?.length || 0)} messages
            </summary>
            <Messages messages={call.messages} />
          </details>
          <JsonPanel title="Raw request messages" value={call.messages} />
          {call.completion && (
            <JsonPanel
              title="Recorded completion"
              value={call.completion}
              initialOpen
            />
          )}
          {call.output_format ? (
            <JsonPanel
              title="Output schema"
              value={schemas[call.output_format.sha256] || call.output_format}
            />
          ) : (
            <p className="small muted">
              No structured output schema requested.
            </p>
          )}
          <JsonPanel title="Full call record" value={call} />
        </article>
      ))}
    </div>
  );
}

function Screenshot({ runKey, step }) {
  const [failed, setFailed] = useState(false);
  const src = query("screenshot", { key: runKey, index: step });
  useEffect(() => setFailed(false), [src]);
  return failed ? (
    <div className="notice error">
      The committed screenshot could not be read.
    </div>
  ) : (
    <figure className="screenshot">
      <a
        href={src}
        target="_blank"
        rel="noreferrer"
        title="Open screenshot at original size"
      >
        <img
          src={src}
          alt={`Recorded browser observation at step ${step}`}
          onError={() => setFailed(true)}
        />
      </a>
      <figcaption>
        <Icon name="image" />
        Observation · step {step}
        <a href={src} target="_blank" rel="noreferrer">
          Open original ↗
        </a>
      </figcaption>
    </figure>
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
            <strong>Restored step</strong>
            <p>
              Current execution is recorded by the restoration check. The source
              actions, results, and model output below are copied history.
            </p>
          </div>
          {check ? (
            <section>
              <div className="section-heading">
                <h3>Actual restoration execution</h3>
                <Badge value={check.verdict} />
              </div>
              {check.reasons.length > 0 && (
                <div className="notice error">{check.reasons.join(", ")}</div>
              )}
              {check.actions.length ? (
                check.actions.map((action, index) => (
                  <article className="call-card" key={index}>
                    <div className="section-heading">
                      <h3>{action.executed?.name || `Action ${index + 1}`}</h3>
                      <Badge value={restoredActionOutcome(action)} />
                    </div>
                    <p className="muted small">
                      Retargeting: {action.method}
                      {action.failure ? ` · ${action.failure}` : ""}
                    </p>
                    {action.detail && <p className="small">{action.detail}</p>}
                    {action.live_error && (
                      <div className="notice error">{action.live_error}</div>
                    )}
                    {action.executed ? (
                      <JsonPanel
                        title="Executed action"
                        value={action.executed}
                        initialOpen
                      />
                    ) : (
                      <p className="muted">
                        This action was not submitted for execution.
                      </p>
                    )}
                    <JsonPanel
                      title="Restored action evidence"
                      value={action}
                    />
                  </article>
                ))
              ) : (
                <p className="muted">
                  No restoration actions recorded for this check.
                </p>
              )}
            </section>
          ) : (
            <div className="notice error">
              No restoration check was recorded. Actual re-execution outcomes
              are unknown.
            </div>
          )}
        </>
      )}
      {check && (
        <JsonPanel
          title={
            restored ? "Restoration check" : "Checkpoint restoration check"
          }
          value={check}
          initialOpen
        />
      )}
      {record.truncated_actions > 0 && (
        <div className="notice">
          {record.truncated_actions} model-emitted actions were discarded{" "}
          {restored ? "in the copied source step" : "by the action limit"}.
        </div>
      )}
      <JsonPanel
        title={restored ? "Source actions (copied)" : "Actions"}
        value={record.actions}
        initialOpen={!restored}
      />
      <JsonPanel
        title={restored ? "Source action results (copied)" : "Action results"}
        value={record.action_results}
        initialOpen={!restored}
      />
      {record.llm_output && (
        <JsonPanel
          title={
            restored
              ? "Source parsed model output (copied)"
              : "Parsed model output"
          }
          value={record.llm_output}
        />
      )}
    </>
  );
}

function StepDetail({ runKey, index, revision }) {
  const state = useRemote(query("step", { key: runKey, index }), revision);
  const [tab, setTab] = useState("observation");
  return (
    <RemoteState state={state}>
      {(step) => (
        <div className="step-detail">
          <div className="section-heading">
            <div>
              <h2>Step {step.record.step}</h2>
              <div className="inline">
                <Badge value={step.record.origin} />
                <span className="muted small">
                  {duration(step.record.duration_ms)} ·{" "}
                  {number(step.calls.length)} model calls
                </span>
              </div>
            </div>
            <span className="mono muted small">
              {step.record.timestamp
                ? new Date(step.record.timestamp * 1000).toLocaleTimeString()
                : ""}
            </span>
          </div>
          {step.record.error && (
            <div className="notice error">
              <strong>{step.record.error_phase || "Step"} failure</strong>
              <p>{step.record.error}</p>
            </div>
          )}
          <div className="tabs compact" aria-label="Step sections">
            {[
              ["observation", "Observation"],
              [
                "actions",
                step.record.origin === "restored"
                  ? "Restoration actions"
                  : `Actions (${step.record.actions?.length || 0})`,
              ],
              ["calls", `Model calls (${step.calls.length})`],
              ["archive", `Archive (${step.archive.length})`],
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
              {step.has_screenshot && (
                <Screenshot runKey={runKey} step={index} />
              )}
              <p className="observation-summary">
                {step.record.origin === "restored" && (
                  <strong>Source observation summary (copied): </strong>
                )}
                {step.record.observation_summary}
              </p>
              {!Object.keys(step.fragments).length && (
                <Empty title="No observation fragments">
                  This step has no committed observation fragments.
                </Empty>
              )}
              {Object.entries(step.fragments).map(([name, fragment]) => (
                <JsonPanel key={name} title={name} value={fragment} />
              ))}
              {step.record.restoration && (
                <JsonPanel
                  title="Restoration check"
                  value={step.record.restoration}
                  initialOpen
                />
              )}
            </>
          )}
          {tab === "actions" && (
            <>
              <StepActions record={step.record} />
              <JsonPanel title="Step record" value={step.record} />
            </>
          )}
          {tab === "calls" && (
            <Calls calls={step.calls} schemas={step.output_schemas} />
          )}
          {tab === "archive" && (
            <>
              <p className="muted small">
                Manifest-indexed environment evidence. These records describe
                archived artifacts; this viewer does not replay or execute them.
              </p>
              {step.archive.length ? (
                <StructuredData value={step.archive} />
              ) : (
                <Empty title="No archived evidence">
                  This step has no committed environment archive records.
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
  if (!run.steps.length)
    return (
      <Empty title="No committed steps">
        Inspect the result and trailing calls for evidence from setup,
        cancellation, or an interrupted model call.
      </Empty>
    );
  return (
    <div className="trajectory">
      <aside className="timeline" aria-label="Step timeline">
        <div className="timeline-title">
          Committed steps <span>{run.steps.length}</span>
        </div>
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
              {step.error && (
                <span className="error-dot" title={step.error}>
                  !
                </span>
              )}
            </div>
            <span className="action-name">{actionLabel(step)}</span>
            <span className="muted small">
              {duration(step.duration_ms)}
              {step.call_count ? ` · ${step.call_count} calls` : ""}
            </span>
          </button>
        ))}
      </aside>
      <StepDetail runKey={route.key} index={selected} revision={revision} />
    </div>
  );
}

function Evaluations({ evaluations, error }) {
  if (error)
    return (
      <div className="notice error" role="alert">
        <strong>Evaluation evidence is unreadable</strong>
        <p>{error}</p>
        <p>The run's recorded steps and model calls remain available.</p>
      </div>
    );
  return !Object.keys(evaluations).length ? (
    <Empty title="Not evaluated">
      Evaluator verdicts are recorded separately from the agent outcome. Run an
      evaluation to add a verdict.
    </Empty>
  ) : (
    <div className="evaluation-list">
      {Object.entries(evaluations).map(([slug, result]) => (
        <article className="panel padded" key={slug}>
          <div className="section-heading">
            <div>
              <span className="eyebrow">{result.verdict_source}</span>
              <h3>{result.evaluator_id || slug}</h3>
            </div>
            <Badge
              value={result.predicted_label === 1 ? "success" : "failure"}
            />
          </div>
          <p className="muted small">
            {date(result.timestamp)} · {result.benchmark} / {result.subset}
          </p>
          {result.failure_category && (
            <p>
              Failure category: <strong>{result.failure_category}</strong>
            </p>
          )}
          <JsonPanel
            title="Evaluation details"
            value={result.evaluation_details}
            initialOpen
          />
          <JsonPanel
            title="Evaluator configuration"
            value={result.evaluator_config}
          />
          <JsonPanel title="Full evaluation record" value={result} />
        </article>
      ))}
    </div>
  );
}

function ExecutionContext({ run }) {
  const execution = run.execution;
  if (!execution)
    return <div className="notice">Execution provenance is unavailable.</div>;
  return (
    <section className="panel padded">
      <div className="section-heading">
        <h3>Execution provenance</h3>
        <Badge value={run.summary.execution_kind || "unknown"} />
      </div>
      <dl className="properties">
        <div>
          <dt>Loop</dt>
          <dd>{text(run.summary.loop_id)}</dd>
        </div>
        <div>
          <dt>Network source</dt>
          <dd>{text(execution.network_source)}</dd>
        </div>
        <div>
          <dt>Source relationship</dt>
          <dd>{text(execution.relationship)}</dd>
        </div>
        {execution.source_run_id && (
          <div>
            <dt>Source run</dt>
            <dd>{execution.source_run_id}</dd>
          </div>
        )}
        {execution.checkpoint != null && (
          <div>
            <dt>Checkpoint step</dt>
            <dd>{execution.checkpoint}</dd>
          </div>
        )}
        {execution.source_resolution && (
          <div>
            <dt>Source location</dt>
            <dd>{execution.source_resolution.replaceAll("_", " ")}</dd>
          </div>
        )}
      </dl>
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
            ? "Open source run"
            : `Open source checkpoint · step ${execution.checkpoint}`}{" "}
          ↗
        </button>
      )}
      {run.summary.branch && (
        <JsonPanel title="Branch provenance" value={run.summary.branch} />
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
      <Empty title="No replay log recorded">
        This run has no committed replay-log archive. Replay decisions cannot be
        inferred from its actions or final outcome.
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
          <strong>Replay evidence is unreadable</strong>
          <p>{log.error}</p>
          <p>Any entries shown below are partial evidence.</p>
        </div>
      )}
      <div className="stats">
        {[
          ["hit", "Archive hits"],
          ["miss", "Archive misses"],
          ["exhausted", "Exhausted responses"],
          ["synthetic", "Synthetic responses"],
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
              aria-label="Search replay decisions"
              placeholder="Search URL, method, reason…"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setLimit(100);
              }}
            />
          </label>
          <select
            aria-label="Filter replay decision kind"
            value={kind}
            onChange={(event) => {
              setKind(event.target.value);
              setLimit(100);
            }}
          >
            <option value="">All decisions</option>
            {["hit", "miss", "exhausted", "synthetic", "summary"].map(
              (value) => (
                <option key={value}>{value}</option>
              ),
            )}
          </select>
        </div>
        {entries.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Commit</th>
                  <th>Decision</th>
                  <th className="numeric">Count</th>
                  <th>Request</th>
                  <th>Reason / answer</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {entries.slice(0, limit).map((entry, index) => (
                  <tr key={index}>
                    <td>
                      {entry.step == null ? (
                        "Trailing"
                      ) : (
                        <button
                          className="text-button"
                          onClick={() =>
                            navigate(runRoute(runKey, { step: entry.step }))
                          }
                        >
                          Step {entry.step} ↗
                        </button>
                      )}
                    </td>
                    <td>
                      <Badge value={entry.kind} />
                      {entry.folded_kind && (
                        <div className="muted small">
                          {entry.folded_kind} repeated
                        </div>
                      )}
                      {entry.diverged && (
                        <div className="row-error">Diverged</div>
                      )}
                    </td>
                    <td className="numeric">{number(entry.weight)}</td>
                    <td>
                      <strong>{entry.method}</strong>
                      <div className="mono small">{entry.url}</div>
                      <div className="muted small">
                        {entry.resource_type}
                        {entry.frame ? ` · ${entry.frame} frame` : ""}
                      </div>
                    </td>
                    <td>
                      {text(entry.reason)}
                      <div className="muted small">
                        {text(entry.answer)}
                        {entry.status != null ? ` · ${entry.status}` : ""}
                      </div>
                    </td>
                    <td>
                      <JsonPanel title="Replay decision" value={entry} />
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
                ? "No matching decisions"
                : "No decisions recorded"
            }
          >
            {log.entries.length
              ? "Try a different search or decision filter."
              : "The replay log was enabled but contains no committed decisions."}
          </Empty>
        )}
        <div className="table-footer">
          <span>
            {number(entries.length)} matching entries ·{" "}
            {number(log.entries.length)} recorded entries
          </span>
          {entries.length > limit && (
            <button
              className="text-button"
              onClick={() => setLimit(limit + 100)}
            >
              Show next {Math.min(entries.length - limit, 100)}
            </button>
          )}
        </div>
      </div>
      <p className="footnote">
        Counts include repeated requests represented by summary entries. Step
        labels identify the commit that collected each decision; trailing
        entries were collected at close-out.
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
        All runs
      </button>
      <RemoteState state={state}>
        {(run) => (
          <>
            <div className="page-heading run-heading">
              <div>
                <span className="eyebrow">
                  {run.summary.benchmark || "Recorded run"}
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
                Download run JSON ↓
              </button>
            </div>
            <Ledger row={run.summary} />
            <div className="run-meta">
              <span>
                <strong>{text(run.summary.agent_id)}</strong> /{" "}
                {text(run.summary.loop_id)}
              </span>
              <span>{text(run.summary.model)}</span>
              <span>{number(run.summary.policy_steps)} policy steps</span>
              <span>
                {number(run.summary.tokens)} tokens
                {run.summary.usage_missing > 0
                  ? ` · ${run.summary.usage_missing} calls without usage`
                  : ""}
              </span>
              <span>{duration(run.summary.duration_ms)}</span>
            </div>
            {run.manifest.incomplete_reason && (
              <div className="notice error">
                <strong>Recording is incomplete</strong>
                <p>{run.manifest.incomplete_reason}</p>
              </div>
            )}
            {run.summary.error && (
              <div className="notice error">
                <strong>Run error</strong>
                <p>{run.summary.error}</p>
              </div>
            )}
            <ExecutionContext run={run} />
            <div className="tabs" aria-label="Run sections">
              {[
                ["trajectory", "Trajectory"],
                [
                  "evaluations",
                  run.evaluation_error
                    ? "Evaluations (!)"
                    : `Evaluations (${Object.keys(run.evaluations).length})`,
                ],
                ["trailing", `Trailing calls (${run.trailing_calls.length})`],
                [
                  "replay",
                  run.replay_log?.error
                    ? "Replay log (!)"
                    : `Replay log (${run.replay_log?.entries.length || 0})`,
                ],
                ["config", "Configuration"],
                ["result", "Result & manifest"],
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
                  Calls committed at close-out, outside a committed step. They
                  are preserved evidence, not additional policy steps.
                </p>
                <Calls
                  calls={run.trailing_calls}
                  schemas={run.trailing_output_schemas}
                />
              </>
            )}
            {tab === "replay" && (
              <ReplayLog log={run.replay_log} runKey={route.key} />
            )}
            {tab === "config" && (
              <>
                <div className="panel padded">
                  <div className="section-heading">
                    <h2>Recorded configuration</h2>
                    <code>{run.summary.config_hash || "No digest"}</code>
                  </div>
                  <p className="muted">
                    The resolved configuration and negotiated contracts captured
                    for this run.
                  </p>
                  {run.summary.config_source ? (
                    <dl className="properties">
                      <div>
                        <dt>Recorded source path</dt>
                        <dd>{run.summary.config_source.path}</dd>
                      </div>
                      <div>
                        <dt>Source file SHA-256</dt>
                        <dd className="mono">
                          {run.summary.config_source.sha256}
                        </dd>
                      </div>
                    </dl>
                  ) : (
                    <div className="notice">
                      Configuration source unknown. This trace does not record
                      which YAML file produced it.
                    </div>
                  )}
                  <JsonPanel
                    title="Configuration snapshot"
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
                    <h2>Recorded answer</h2>
                    <pre className="prose-code">{text(run.summary.answer)}</pre>
                  </div>
                )}
                <JsonPanel
                  title="Agent result"
                  value={run.result}
                  initialOpen
                />
                <JsonPanel title="Recording manifest" value={run.manifest} />
                <JsonPanel title="Usage accounting" value={run.usage} />
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
    if (!value.length) return <p className="muted small">No entries.</p>;
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
              Show next {Math.min(100, value.length - limit)} of {value.length}{" "}
              entries
            </button>
          )}
        </>
      );
    }
    return <JsonBlock value={value} />;
  }
  if (depth > 2) return <JsonPanel title="Details" value={value} />;
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
              <span className="eyebrow">Committed step</span>
              <select
                value={index}
                aria-label={`Step for ${summary.run_id || summary.key}`}
                onChange={(event) => setSelected(Number(event.target.value))}
              >
                {data.steps.map((item) => (
                  <option key={item.step} value={item.step}>
                    {item.step} · {item.origin} · {actionLabel(item)}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p className="muted">No committed steps.</p>
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
                <Screenshot runKey={summary.key} step={index} />
              ) : (
                <div className="empty">
                  <Icon name="image" />
                  <p>No committed screenshot</p>
                </div>
              )}
              <StepActions record={data.record} />
              {data.record.error && (
                <div className="notice error">{data.record.error}</div>
              )}
              <button
                className="text-button"
                onClick={() => navigate(runRoute(summary.key, { step: index }))}
              >
                Inspect this step's full evidence ↗
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
        Compare step evidence
        <span className="muted small">Screenshots and actions</span>
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
        All runs
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Compare evidence</span>
          <h1>Run comparison</h1>
          <p>
            Align outcomes, evaluator verdicts, and configuration differences.
          </p>
        </div>
      </div>
      {keys.length < 2 ? (
        <Empty title="Select at least two runs">
          Return to the run list and select runs to compare.
        </Empty>
      ) : (
        <RemoteState state={state}>
          {(comparison) => (
            <>
              {!comparison.same_task && (
                <div className="notice">
                  These runs do not all refer to the same task. Differences in
                  verdicts and usage may reflect different workloads.
                </div>
              )}
              <div className="table-scroll panel">
                <table className="comparison-table">
                  <thead>
                    <tr>
                      <th>Evidence</th>
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
                      ["Agent", "agent_id"],
                      ["Model", "model"],
                      ["Loop", "loop_id"],
                      ["Execution", "execution_kind"],
                      ["Agent outcome", "agent_done"],
                      ["Environment", "env_status"],
                      ["Recording", "trace_status"],
                      ["Complete through", "complete_through"],
                      ["Policy steps", "policy_steps"],
                      ["Total steps", "total_steps"],
                      ["Tokens", "tokens"],
                      ["Calls without usage", "usage_missing"],
                      ["Duration", "duration_ms"],
                    ].map(([label, key]) => (
                      <tr key={key}>
                        <th>{label}</th>
                        {comparison.runs.map((run) => (
                          <td key={run.key}>
                            {[
                              "agent_done",
                              "env_status",
                              "trace_status",
                            ].includes(key) ? (
                              <Badge value={run[key]} />
                            ) : key === "duration_ms" ? (
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
                        <th>Evaluation evidence</th>
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
                        <th>Evaluator · {item.evaluator}</th>
                        {item.verdicts.map((verdict, index) => (
                          <td key={index}>
                            {comparison.runs[index]?.evaluation_error ? (
                              <span
                                className="row-error"
                                title={comparison.runs[index].evaluation_error}
                              >
                                Evaluation unreadable
                              </span>
                            ) : verdict == null ? (
                              <span className="muted">Not evaluated</span>
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
                  <h2>Configuration differences</h2>
                  <span className="muted small">
                    {comparison.config_differences.length} changed paths
                  </span>
                </div>
                {comparison.config_differences.length ? (
                  <div className="table-scroll">
                    <table className="comparison-table">
                      <thead>
                        <tr>
                          <th>Configuration path</th>
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
                    The recorded configurations have no differences.
                  </p>
                )}
              </section>
              <ComparedEvidence runs={comparison.runs} revision={revision} />
              <JsonPanel title="Comparison data" value={comparison} />
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
      label: `${run.agent_id || "agent"} · ${run.model || "model"}`,
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
    both_passed: "Both passed",
    left_only_passed: "A passed / B failed",
    right_only_passed: "B passed / A failed",
    both_failed: "Both failed",
    unpaired: "Unpaired tasks",
    ungraded: "Ungraded tasks",
    ambiguous: "Ambiguous attempts",
    incompatible_evaluator: "Different evaluator settings",
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
                Evaluation unreadable
              </div>
            )}
          </div>
        ))}
      </div>
    ) : (
      <span className="muted">No matching run</span>
    );
  return (
    <>
      <button className="back-link" onClick={() => navigate({ view: "runs" })}>
        <Icon name="back" />
        All runs
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Matched tasks</span>
          <h1>Experiment comparison</h1>
          <p>Find where one configuration succeeds and another fails.</p>
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
              ["A · Configuration", left, setLeft],
              ["B · Configuration", right, setRight],
            ].map(([label, value, setter]) => (
              <label key={label}>
                <span className="eyebrow">{label}</span>
                <select
                  required
                  value={value}
                  onChange={(event) => setter(event.target.value)}
                >
                  <option value="">Select a configuration</option>
                  {[...groups.entries()].map(([hash, group]) => (
                    <option key={hash} value={hash}>
                      {hash.slice(0, 10)} · {group.label} · {group.count} runs
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <label>
              <span className="eyebrow">Evaluator</span>
              <select
                required
                value={evaluator}
                onChange={(event) => setEvaluator(event.target.value)}
              >
                <option value="">Select an evaluator</option>
                {evaluators.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            <button
              className="button primary"
              disabled={!left || !right || left === right || !evaluator}
            >
              Compare experiments
            </button>
          </form>
        )}
      </RemoteState>
      <p className="footnote">
        Pairs match benchmark, subset, and task identity. Only graded verdicts
        with matching evaluator settings are compared. Repeated attempts remain
        ambiguous; no attempt is picked for you.
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
                  <label htmlFor="outcome-filter">Case outcome</label>
                  <select
                    id="outcome-filter"
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                  >
                    <option value="">All tasks</option>
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
                        <th>Task</th>
                        <th>Outcome</th>
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
                  <Empty title="No matching task evidence">
                    Choose configurations with recorded tasks and evaluator
                    results.
                  </Empty>
                )}
              </div>
              <JsonPanel title="Experiment comparison data" value={data} />
            </>
          )}
        </RemoteState>
      ) : (
        <Empty title="Choose two configurations and an evaluator">
          Open any matched run to investigate its trajectory and evidence.
        </Empty>
      )}
    </>
  );
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
          <span className="eyebrow">Research artifacts</span>
          <h1>Reports & datasets</h1>
          <p>
            Recorded evaluations, flywheel metrics, diagnostics, and dataset
            manifests.
          </p>
        </div>
      </div>
      {(workspace.config_source || workspace.config_hash) && (
        <div className="notice">
          <strong>Reports retain their original scope</strong>
          <p>
            Reports are discovered across this root and retain their original
            inputs and denominators. Configuration filters apply to run lists;
            they do not filter or recompute report statistics.
          </p>
        </div>
      )}
      <div className="report-layout">
        <aside className="panel report-list">
          <div className="timeline-title">Artifacts</div>
          <RemoteState state={list}>
            {(reports) =>
              !reports.length ? (
                <Empty title="No reports found">
                  Reports will appear when present under this workspace.
                </Empty>
              ) : (
                reports.map((item) => (
                  <button
                    key={item.key}
                    onClick={() => navigate({ view: "reports", key: item.key })}
                    className={`report-button ${route.key === item.key ? "active" : ""}`}
                  >
                    <span className="eyebrow">{item.kind}</span>
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
                      <span className="eyebrow">{item.kind}</span>
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
                      Download JSON ↓
                    </button>
                  </div>
                  <StructuredData value={item.data} />
                  <JsonPanel title="Raw report" value={item.data} />
                </div>
              )}
            </RemoteState>
          ) : (
            <Empty title="Choose a report">
              Select an artifact to inspect its recorded measurements and
              provenance.
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
        Run list filter ·{" "}
        {workspace.config_source
          ? "configuration file history"
          : "configuration digest"}
      </strong>
      {workspace.config_source && (
        <p className="mono small">{workspace.config_source}</p>
      )}
      <p>
        {workspace.config_hash ? (
          <>
            Configuration digest:{" "}
            <span className="mono small">{workspace.config_hash}</span>
          </>
        ) : (
          "All recorded versions of this file. Traces with unknown configuration source are excluded."
        )}
      </p>
      <p className="small">
        Search root: <span className="mono">{workspace.root}</span>
      </p>
    </div>
  ) : (
    <p className="footnote">
      Search root: <span className="mono">{workspace.root}</span>
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
  return (
    <div className="app">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content").focus();
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar">
        <a className="brand" href="#view=runs">
          <img
            className="brand-mark"
            src="/brand/mark.svg"
            alt="Spectragent prism mark"
            width="42"
            height="42"
          />
          <span>
            All4One<small>EXPERIMENT VIEWER</small>
          </span>
        </a>
        <div className="nav-label">Workspace</div>
        <nav aria-label="Main navigation">
          <button
            className={view !== "reports" ? "active" : ""}
            onClick={() => navigate({ view: "runs" })}
          >
            <Icon name="runs" />
            Runs
          </button>
          <button
            className={view === "reports" ? "active" : ""}
            onClick={() => navigate({ view: "reports" })}
          >
            <Icon name="reports" />
            Reports & datasets
          </button>
        </nav>
        <div className="sidebar-bottom">
          <span className="connection-dot" />
          Local workspace
          <p>
            Read-only evidence
            <br />
            No agent execution
          </p>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            <span className="breadcrumb">Workspace</span>
            <Icon name="arrow" />
            {view === "reports"
              ? "Reports & datasets"
              : view === "run"
                ? "Run details"
                : ["compare", "groups"].includes(view)
                  ? "Comparison"
                  : "Runs"}
          </span>
          <div className="topbar-actions">
            <label className="auto-refresh">
              <input
                type="checkbox"
                checked={live}
                onChange={(event) => setLive(event.target.checked)}
              />
              Auto-refresh · 5s
            </label>
            <button
              className="button icon-button"
              aria-label="Refresh evidence"
              onClick={() => setRevision((value) => value + 1)}
            >
              <Icon name="refresh" />
              Refresh
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex="-1">
          <RemoteState state={workspace}>
            {() => (
              <>
                <WorkspaceScope workspace={workspace.data} />
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
  );
}

createRoot(document.getElementById("root")).render(<App />);
