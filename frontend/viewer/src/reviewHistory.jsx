/**
 * The review page's archive: the committed steps before and after the
 * reviewed one, laid out like a folder of tabs. One step's card is open at a
 * time (its observation, the screen after its actions, what it did and what
 * the model replied); the rest are tabs, chosen by click or ← →.
 */
import React, { useEffect, useRef, useState } from "react";
import { query, RemoteState, useRemote } from "./common.jsx";
import { MarkLayer, MarkLegend } from "./marks.jsx";
import {
  folderNeighbour,
  folderSteps,
  frameStatus,
  nextObservation,
  observationFacts,
  pointerPresentation,
  recordedReply,
  tallestRatio,
} from "./reviewEvidence.js";

const originText = { policy: "决策", restored: "恢复", terminal: "终止观测" };
const relationText = { past: "此前", current: "当前审阅", future: "之后" };
const asText = (value) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));
const imageUrl = (params, index, kind, frame) =>
  query("review/trajectory-image", { ...params, index, kind, ...(frame == null ? {} : { frame_id: frame }) });

function RecordedText({ title, value, open = false, empty = "未记录" }) {
  return (
    <details className="history-record" open={open}>
      <summary>{title}</summary>
      <pre>{value == null || value === "" ? empty : asText(value)}</pre>
    </details>
  );
}

/** One image of a comparison row: the shared box, the image fit inside it top-aligned, its marks on top. */
export function ComparedImage({ src, alt, marks, pointer, missing }) {
  const [failed, setFailed] = useState(false);
  const position = pointerPresentation(pointer);
  if (!src || failed)
    return <div className="compare-box empty"><p className="history-missing">{failed ? "图片读取失败，请重新加载或打开原运行查看记录健康。" : missing}</p></div>;
  return (
    <a className="compare-box" href={src} target="_blank" rel="noreferrer" title="打开原图">
      <span className="compare-surface">
        <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />
        {marks && <MarkLayer data={marks} />}
        {position.placed && (
          <span className={`history-pointer ${position.labelLeft ? "label-left" : ""}`} style={position.style} aria-label={position.label}>
            <span>鼠标</span>
          </span>
        )}
      </span>
    </a>
  );
}

function ImageInfo({ marks, pointer, showMarks, showPointer }) {
  return (
    <div className="history-image-info">
      {showPointer && pointer && <p className="history-pointer-reading">{pointerPresentation(pointer).label}</p>}
      {showMarks && <MarkLegend data={marks} />}
    </div>
  );
}

function StepCard({ data, params, index, steps, show, setShow }) {
  const frames = (data.frames || []).filter((frame) => frame.kind === "before_action");
  const [picked, setPicked] = useState(null);
  const frame = frames.find((candidate) => candidate.id === picked) || frames[0];
  const [requestsOpen, setRequestsOpen] = useState(false);
  const requests = useRemote(
    requestsOpen ? query("review/trajectory-step", { ...params, index, include_messages: true }) : null,
    params.queue_sha256,
  );
  const record = data.record;
  const restored = record.origin === "restored";
  const after = nextObservation(steps, index);
  const facts = observationFacts(data.fragments);
  const summary = steps.find((step) => step.index === index)?.action_summary;
  const figures = [
    {
      key: "observation",
      title: `第 ${index} 步 · 观测`,
      note: record.origin === "terminal" ? "结束后的观测" : restored ? "恢复过程中实际采集的观测" : "模型作出这一步决定时的画面",
      src: data.has_screenshot ? imageUrl(params, index, "observation") : null,
      missing: "这一步没有记录观测截图",
      marks: data.observation_marks,
      pointer: data.observation_pointer,
    },
    ...(frames.length
      ? [{
          key: "frame",
          title: "同一步 · 动作前画面",
          note: "辅助证据，没有送给模型",
          src: frame?.has_image ? imageUrl(params, index, "before_action", frame.id) : null,
          missing: frameStatus(frame),
          marks: frame?.marks,
          pointer: frame?.pointer,
        }]
      : []),
    {
      key: "after",
      title: after ? `动作之后 · 第 ${after.index} 步的观测` : "动作之后",
      note: after ? `画的是第 ${index} 步的动作，看它们之后屏幕变成什么样` : record.origin === "terminal" ? "终止观测之后没有动作" : "下一步没有提交，没有之后的画面",
      src: after?.has_screenshot ? imageUrl(params, after.index, "observation") : null,
      missing: after ? "下一步没有记录观测截图" : "没有之后的画面",
      marks: data.after_marks,
      pointer: null,
    },
  ];
  return (
    <>
      <div className="history-image-tools">
        <label className="toggle"><input type="checkbox" checked={show.marks} onChange={(event) => setShow({ ...show, marks: event.target.checked })} />动作位置</label>
        <label className="toggle" title="显示记录位置的辅助标记；原图自带的光标保持原样"><input type="checkbox" checked={show.pointer} onChange={(event) => setShow({ ...show, pointer: event.target.checked })} />当时鼠标位置</label>
        {frames.length > 1 && (
          <label className="history-frame-picker">动作前截图
            <select value={frame.id} onChange={(event) => setPicked(event.target.value)}>
              {frames.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  第 {(candidate.action_index ?? 0) + 1} 个动作 · {candidate.action || "未记录动作名"} · {frameStatus(candidate)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <div className="compare-row" style={{ "--compare-count": figures.length, "--compare-ratio": tallestRatio(figures.map((figure) => figure.marks)) ?? undefined }}>
        {figures.map((figure) => (
          <figure key={figure.key} className="compare-figure">
            <figcaption>{figure.title}<span>{figure.note}</span></figcaption>
            <ComparedImage
              key={figure.src || figure.key}
              src={figure.src}
              alt={figure.title}
              missing={figure.missing}
              marks={show.marks ? figure.marks : null}
              pointer={show.pointer ? figure.pointer : null}
            />
            <ImageInfo marks={figure.marks} pointer={figure.pointer} showMarks={show.marks} showPointer={show.pointer} />
          </figure>
        ))}
      </div>
      {(facts.front || facts.page) && (
        <dl className="folder-facts">
          {facts.front && <><dt>前台应用</dt><dd>{facts.front}</dd></>}
          {facts.page && <><dt>页面</dt><dd>{facts.page}</dd></>}
        </dl>
      )}
      {restored && <p className="history-note">这是实际恢复的一步，没有重新让策略模型决定。原回复和原动作属于来源材料。</p>}
      {record.origin === "terminal" && <p className="history-note">这里只记录结束后的观测，没有下一次决定。</p>}
      <div className="history-actions">
        <div className="folder-action">
          <h4>{restored ? "本次实际恢复的动作" : "这一步的动作"}</h4>
          <pre>{summary || "（没有动作）"}</pre>
          {record.skipped_actions > 0 && <p className="history-note">先前的动作失败，末尾 {record.skipped_actions} 个动作没有执行。</p>}
        </div>
        <RecordedText title={restored ? "源运行的回复（恢复材料，不是本次新思考）" : "模型的回复"} value={recordedReply(record)} open />
      </div>
      {facts.text && (
        <RecordedText title={`界面文字 · ${facts.text.source}（${facts.text.lines.length} 行）`} value={facts.text.lines.join("\n")} />
      )}
      <div className="history-actions">
        <RecordedText title="动作原文" value={record.actions} />
        <RecordedText title="动作返回结果" value={record.action_results} />
      </div>
      {restored && <RecordedText title="完整恢复尝试、异常与来源" value={{ restoration: record.restoration, source_history: record.source_history }} />}
      <section className="history-calls" aria-label="记录的模型思考与调用">
        <h4>记录的模型思考与调用</h4>
        {(data.calls || []).length ? data.calls.map((call, position) => (
          <article key={call.call_id || position}>
            <p className="history-call-title">
              {call.purpose === "agent_decision" ? "智能体决策" : call.purpose === "prompt_helper" ? "提示词辅助（不是智能体自己的思考）" : call.purpose || "模型调用"}
              <span> {call.model || "模型未记录"} · {call.status || "状态未记录"}</span>
            </p>
            <RecordedText title="模型思考（提供方返回的原文）" value={call.completion?.thinking} empty="本次调用未记录可读的原生思考；不代表模型没有思考。" />
            <RecordedText title="模型回复原文" value={call.completion?.completion} />
            {call.failure && <RecordedText title="调用失败记录" value={call.failure} open />}
          </article>
        )) : <p className="history-note">这一步没有记录模型调用{data.recording?.trajectory === false ? "（未开启轨迹调用记录）" : ""}。</p>}
      </section>
      {(data.calls || []).length > 0 && (
        <details className="history-record" onToggle={(event) => setRequestsOpen(event.currentTarget.open)}>
          <summary>展开实际模型请求（含历史窗口，按需加载）</summary>
          {requestsOpen && (
            <RemoteState state={requests}>
              {(full) => (full.calls || []).map((call, position) => (
                <RecordedText key={call.call_id || position} title={`调用 ${position + 1} · ${call.purpose || "模型调用"}`} value={call.messages} open />
              ))}
            </RemoteState>
          )}
        </details>
      )}
      <RecordedText title="全部观测片段（完整记录）" value={data.fragments} />
    </>
  );
}

function EvidenceFolder({ data, params }) {
  const steps = folderSteps(data.steps, data.anchor_step);
  const [selected, setSelected] = useState(data.anchor_step);
  const [show, setShow] = useState({ marks: true, pointer: false });
  const tabs = useRef(null);
  const detail = useRemote(query("review/trajectory-step", { ...params, index: selected }), params.queue_sha256);
  const current = steps.find((step) => step.index === selected);
  useEffect(() => {
    // Scroll the strip sideways only: scrolling the tab into view would move the page too.
    const strip = tabs.current;
    const tab = strip?.querySelector('[aria-selected="true"]');
    if (strip && tab) strip.scrollLeft = tab.offsetLeft - strip.clientWidth / 2 + tab.clientWidth / 2;
  }, [selected]);
  const move = (offset, focus = false) => {
    const next = folderNeighbour(steps, selected, offset);
    setSelected(next);
    if (focus) requestAnimationFrame(() => tabs.current?.querySelector(`[data-index="${next}"]`)?.focus());
  };
  // ← → and Home / End move between tabs here; the review page's own
  // ← → (previous and next item) keep working everywhere else.
  const onKeyDown = (event) => {
    const offsets = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    event.stopPropagation();
    const offset = offsets[event.key];
    move(Number.isFinite(offset) ? offset : offset < 0 ? -steps.length : steps.length, true);
  };
  return (
    <section className="trajectory-evidence evidence-folder" aria-label="档案">
      <div className="trajectory-heading">
        <h3>档案</h3>
        <span>每次只打开一步 · 点标签或在标签上按 ← → 换一步</span>
      </div>
      <p className="history-note">当前审阅第 {data.anchor_step} 步。下面按顺序陈列这次运行已提交的每一步：观测、动作、动作之后的画面和模型的回复。之后的记录供审阅参考，不是模型当时的输入。</p>
      <div className="folder-tabs" role="tablist" aria-label="已提交的步骤" ref={tabs} onKeyDown={onKeyDown}>
        {steps.map((step) => (
          <button
            key={step.index}
            type="button"
            role="tab"
            id={`folder-tab-${step.index}`}
            data-index={step.index}
            aria-selected={step.index === selected}
            aria-controls="folder-card"
            tabIndex={step.index === selected ? 0 : -1}
            className={`folder-tab ${step.relation} ${step.origin}`}
            onClick={() => setSelected(step.index)}
            title={step.action_summary || ""}
          >
            <span className="folder-tab-relation">{relationText[step.relation]}</span>
            <span className="folder-tab-step">第 {step.index} 步</span>
            <span className="folder-tab-origin">{originText[step.origin] || step.origin}</span>
          </button>
        ))}
      </div>
      <article className={`folder-card ${current?.relation || ""}`} role="tabpanel" id="folder-card" aria-labelledby={`folder-tab-${selected}`}>
        <div className="folder-card-head">
          <h3>第 {selected} 步 <span>{relationText[current?.relation]} · {originText[current?.origin] || current?.origin}</span></h3>
          <div className="folder-card-nav">
            <button type="button" className="text-button" disabled={steps[0]?.index === selected} onClick={() => move(-1)}>← 上一步</button>
            <button type="button" className="text-button" disabled={steps.at(-1)?.index === selected} onClick={() => move(1)}>下一步 →</button>
          </div>
        </div>
        <RemoteState state={detail}>
          {(step) => <StepCard key={selected} data={step} params={params} index={selected} steps={steps} show={show} setShow={setShow} />}
        </RemoteState>
      </article>
    </section>
  );
}

export function ReviewHistory({ queueId, item, version }) {
  const params = { queue_id: queueId, item_id: item.item_id, queue_sha256: version };
  const remote = useRemote(query("review/trajectory", params), version);
  return <RemoteState state={remote}>{(data) => <EvidenceFolder key={`${item.item_id}:${version}`} data={data} params={params} />}</RemoteState>;
}
