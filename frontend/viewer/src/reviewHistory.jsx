/** Read-only committed evidence before and after the review item's checkpoint. */
import React, { useState } from "react";
import { query, RemoteState, useRemote } from "./common.jsx";
import { MarkLayer, MarkLegend } from "./marks.jsx";
import { frameStatus, pointerPresentation, recordedReply, trajectorySteps } from "./reviewEvidence.js";

const originText = { policy: "决策", restored: "恢复", terminal: "终止观测" };
const asText = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
const imageUrl = (params, index, kind, frame) => query("review/trajectory-image", {
  ...params, index, kind, ...(frame == null ? {} : {frame_id: frame}),
});

function RecordedText({ title, value, open = false, empty = "未记录" }) {
  return <details className="history-record" open={open}>
    <summary>{title}</summary>
    <pre>{value == null || value === "" ? empty : asText(value)}</pre>
  </details>;
}

function EvidenceImage({ src, alt, marks, pointer }) {
  const [failed, setFailed] = useState(false);
  const position = pointerPresentation(pointer);
  return failed ? <p className="history-missing">图片读取失败，请重新加载或打开原运行查看记录健康。</p> :
    <a className="history-image-link" href={src} target="_blank" rel="noreferrer" title="打开原图">
      <span className="history-image-surface">
        <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />
        {marks && <MarkLayer data={marks} />}
        {position.placed && <span className={`history-pointer ${position.labelLeft ? "label-left" : ""}`} style={position.style} aria-label={position.label}>
          <span>鼠标</span>
        </span>}
      </span>
    </a>;
}

function ImageInfo({ marks, pointer, showMarks, showPointer }) {
  return <div className="history-image-info">
    {showPointer && <p className="history-pointer-reading">{pointerPresentation(pointer).label}</p>}
    {showMarks && <MarkLegend data={marks} />}
  </div>;
}

function StepFile({ data, params, index, showMarks, showPointer, setShowMarks, setShowPointer }) {
  const frames = (data.frames || []).filter(frame => frame.kind === "before_action");
  const [picked, setPicked] = useState(null);
  const frame = frames.find(frame => frame.id === picked) || frames[0];
  const [requestsOpen, setRequestsOpen] = useState(false);
  const requests = useRemote(requestsOpen ? query("review/trajectory-step", {...params, index, include_messages: true}) : null, params.queue_sha256);
  const record = data.record;
  const restored = record.origin === "restored";
  return <>
    <div className="history-image-tools">
      <label className="toggle"><input type="checkbox" checked={showMarks} onChange={event => setShowMarks(event.target.checked)} />动作位置</label>
      <label className="toggle" title="显示记录位置的辅助标记；原图自带的光标保持原样"><input type="checkbox" checked={showPointer} onChange={event => setShowPointer(event.target.checked)} />当时鼠标位置</label>
      {frames.length > 1 && <label className="history-frame-picker">操作前截图
        <select value={frame.id} onChange={event => setPicked(event.target.value)}>
          {frames.map(candidate => <option key={candidate.id} value={candidate.id}>
            第 {(candidate.action_index ?? 0) + 1} 个动作 · {candidate.action || "未记录动作名"} · {frameStatus(candidate)}
          </option>)}
        </select>
      </label>}
    </div>
    <div className="history-images">
      <figure>
        <figcaption>第 {index} 步 · 观察截图 <span>{record.origin === "terminal" ? "结束后的观察" : restored ? "恢复过程中实际采集的观察" : "模型作出此步决定时的画面"}</span></figcaption>
        {data.has_screenshot ? <EvidenceImage key={`obs-${index}`} src={imageUrl(params, index, "observation")} alt={`第 ${index} 步观察`}
          marks={showMarks ? data.observation_marks : null} pointer={showPointer ? data.observation_pointer : null} /> :
          <p className="history-missing">这一步未记录观察截图</p>}
        <ImageInfo marks={data.observation_marks} pointer={data.observation_pointer} showMarks={showMarks} showPointer={showPointer} />
      </figure>
      <figure className="history-action-frame">
        <figcaption>同一步 · 操作前画面 <span>辅助证据，未送给模型</span></figcaption>
        {frame?.has_image ? <EvidenceImage key={frame.id} src={imageUrl(params, index, "before_action", frame.id)} alt={`第 ${index} 步第 ${(frame.action_index ?? 0) + 1} 个动作之前`}
          marks={showMarks ? frame.marks : null} pointer={showPointer ? frame.pointer : null} /> :
          <p className="history-missing">{frameStatus(frame)}{frame?.detail && <span>{frame.detail}</span>}</p>}
        <ImageInfo marks={frame?.marks} pointer={frame?.pointer} showMarks={showMarks} showPointer={showPointer} />
      </figure>
    </div>
    {restored && <p className="history-note">这是实际恢复步骤，没有重新让策略模型决定。原回复和原动作属于来源材料。</p>}
    {record.origin === "terminal" && <p className="history-note">这里只记录结束后的观察，没有下一次决定。</p>}
    <RecordedText title={restored ? "源运行回复（恢复材料，并非本次新思考）" : "智能体回复（完整记录）"} value={recordedReply(record)} open />
    <div className="history-actions">
      <RecordedText title={restored ? "本次实际恢复动作" : "这一决定的动作"} value={record.actions} open />
      <RecordedText title="动作返回结果" value={record.action_results} open />
    </div>
    {record.skipped_actions > 0 && <p className="history-note">先前动作失败，末尾 {record.skipped_actions} 个动作没有执行。</p>}
    {restored && <RecordedText title="完整恢复尝试、异常与来源" value={{restoration: record.restoration, source_history: record.source_history}} />}
    <section className="history-calls" aria-label="记录的模型思考与调用">
      <h4>记录的模型思考与调用</h4>
      {(data.calls || []).length ? data.calls.map((call, position) => <article key={call.call_id || position}>
        <p className="history-call-title">{call.purpose === "agent_decision" ? "智能体决策" : call.purpose === "prompt_helper" ? "提示词辅助（不是智能体自己的思考）" : call.purpose || "模型调用"}
          <span> {call.model || "模型未记录"} · {call.status || "状态未记录"}</span></p>
        <RecordedText title="模型思考（提供方返回的原文）" value={call.completion?.thinking} open empty="本次调用未记录可读的原生思考；不代表模型没有思考。" />
        <RecordedText title="模型回复原文" value={call.completion?.completion} />
        {call.failure && <RecordedText title="调用失败记录" value={call.failure} open />}
      </article>) : <p className="history-note">这一步没有记录模型调用{data.recording?.trajectory === false ? "（未开启轨迹调用记录）" : ""}。</p>}
    </section>
    {(data.calls || []).length > 0 && <details className="history-record" onToggle={event => setRequestsOpen(event.currentTarget.open)}>
      <summary>展开实际模型请求（含历史窗口，按需加载）</summary>
      {requestsOpen && <RemoteState state={requests}>{full => (full.calls || []).map((call, position) =>
        <RecordedText key={call.call_id || position} title={`调用 ${position + 1} · ${call.purpose || "模型调用"}`} value={call.messages} open />
      )}</RemoteState>}
    </details>}
    <RecordedText title="全部观察片段（完整记录）" value={data.fragments} />
  </>;
}

function HistoryFiles({ data, params, columns }) {
  const [hovered, setHovered] = useState(null);
  const [pinned, setPinned] = useState(null);
  const [showMarks, setShowMarks] = useState(true);
  const [showPointer, setShowPointer] = useState(false);
  const selected = pinned ?? hovered ?? data.anchor_step;
  const detail = useRemote(pinned == null ? null : query("review/trajectory-step", {...params, index: pinned}), params.queue_sha256);
  const {past, current: anchor, future} = trajectorySteps(data.steps, data.anchor_step);
  const preview = data.steps.find(step => step.index === selected);
  const card = (step, relation) => <button
    key={step.index}
    className={`history-file ${selected === step.index ? "active" : ""} ${pinned === step.index ? "pinned" : ""} ${relation}`}
    onMouseEnter={() => setHovered(step.index)}
    onFocus={() => setHovered(step.index)}
    onClick={() => setPinned(step.index)}
    aria-pressed={pinned === step.index}
    title={`第 ${step.index} 步 · 悬停预览，点击固定`}
  >
    <span className="history-file-tab">{{past: "此前", current: "当前审阅", future: "之后"}[relation]} · 第 {step.index} 步 <small>{originText[step.origin] || step.origin}</small></span>
    <span className="history-file-body">
      {step.has_screenshot && <img loading="lazy" src={imageUrl(params, step.index, "observation")} alt="" />}
      <span>{step.action_summary || (step.record?.actions || []).map(action => action.name).join("、") || "无动作"}</span>
    </span>
  </button>;
  return <section className="trajectory-evidence" aria-label="档案" style={{"--history-columns": columns || 2}}>
    <div className="trajectory-heading"><h3>档案</h3><span>悬停抽出预览 · 点击固定详情</span></div>
    <p className="history-note">当前审阅第 {data.anchor_step} 步，可查看此前和之后已记录的观察与动作。之后的记录供审阅参考，不属于模型当时的输入。</p>
    <div className="trajectory-layout">
      <div className="history-stack" onMouseLeave={() => setHovered(null)}>
        {anchor && card(anchor, "current")}
        <section className="history-group" aria-label="此前的档案">
          <h4>此前 <span>{past.length} 步</span></h4>
          <div className="history-papers">{past.map(step => card(step, "past"))}</div>
          {!past.length && <p className="history-note">没有更早的已记录步骤。</p>}
        </section>
        <section className="history-group" aria-label="之后的档案">
          <h4>之后 <span>{future.length} 步</span></h4>
          <div className="history-papers">{future.map(step => card(step, "future"))}</div>
          {!future.length && <p className="history-note">没有后续的已记录步骤。</p>}
        </section>
      </div>
      <article className="history-detail">
        <div className="history-detail-heading"><h3>第 {selected} 步的档案 <span>{selected < data.anchor_step ? "此前" : selected > data.anchor_step ? "之后" : "当前审阅"}</span></h3>
          {pinned != null ? <button className="text-button" onClick={() => setPinned(null)}>取消固定</button> : <span>预览</span>}
        </div>
        {pinned == null ? <div className="history-preview">
          {preview?.has_screenshot && <EvidenceImage key={`preview-${selected}`} src={imageUrl(params, selected, "observation")} alt={`第 ${selected} 步观察预览`} />}
          <p>{preview?.action_summary || (preview?.record?.actions || []).map(action => action.name).join("、") || "无动作"}</p>
          <button className="button" onClick={() => setPinned(selected)}>打开第 {selected} 步完整档案</button>
          <p className="history-note">完整观察、思考、动作结果和操作前截图在打开后加载。</p>
        </div> : <RemoteState state={detail}>{step => <StepFile key={pinned} data={step} params={params} index={pinned}
          showMarks={showMarks} showPointer={showPointer} setShowMarks={setShowMarks} setShowPointer={setShowPointer} />}</RemoteState>}
      </article>
    </div>
  </section>;
}

export function ReviewHistory({ queueId, item, version, columns }) {
  const params = {queue_id: queueId, item_id: item.item_id, queue_sha256: version};
  const remote = useRemote(query("review/trajectory", params), version);
  return <RemoteState state={remote}>{data => <HistoryFiles key={`${item.item_id}:${version}`} data={data} params={params} columns={columns} />}</RemoteState>;
}
