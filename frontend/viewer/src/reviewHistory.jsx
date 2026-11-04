/**
 * The review page's archive. The step under review is pinned: its evidence
 * (observation, the screen after its actions, what it did and what the model
 * replied) always stays shown. The run's other committed steps are a stack of
 * folders, earlier ones to the left of the pinned step's place and later
 * ones to the right; one of them is open at a time, beside the pinned step,
 * its screens at the same size and aligned to the top with the pinned ones.
 * On a phone the pinned step shrinks to a sticky strip that a tap expands.
 */
import React, { useEffect, useRef, useState } from "react";
import { query, RemoteState, useRemote } from "./common.jsx";
import { MarkLayer, MarkLegend } from "./marks.jsx";
import {
  actionLine,
  defaultNeighbour,
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

function ImageInfo({ marks, pointer, show }) {
  return (
    <div className="history-image-info">
      {show.pointer && pointer && <p className="history-pointer-reading">{pointerPresentation(pointer).label}</p>}
      {show.marks && <MarkLegend data={marks} />}
    </div>
  );
}

/** A step's two screens: its observation, and the next observation with this step's actions on it. */
function stepFigures(data, params, index, steps) {
  const record = data.record;
  const after = nextObservation(steps, index);
  return [
    {
      key: "observation",
      title: `第 ${index} 步 · 观测`,
      note: record.origin === "terminal" ? "结束后的观测" : record.origin === "restored" ? "恢复过程中实际采集的观测" : "模型作出这一步决定时的画面",
      src: data.has_screenshot ? imageUrl(params, index, "observation") : null,
      missing: "这一步没有记录观测截图",
      marks: data.observation_marks,
      pointer: data.observation_pointer,
    },
    {
      key: "after",
      title: after ? `动作之后 · 第 ${after.index} 步的观测` : "动作之后",
      note: after ? `画的是第 ${index} 步的动作` : record.origin === "terminal" ? "终止观测之后没有动作" : "下一步没有提交，没有之后的画面",
      src: after?.has_screenshot ? imageUrl(params, after.index, "observation") : null,
      missing: after ? "下一步没有记录观测截图" : "没有之后的画面",
      marks: data.after_marks,
      pointer: null,
    },
  ];
}

function StepImages({ figures, ratio, show }) {
  return (
    <div className="compare-row" style={{ "--compare-count": figures.length, "--compare-ratio": ratio ?? undefined }}>
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
          <ImageInfo marks={figure.marks} pointer={figure.pointer} show={show} />
        </figure>
      ))}
    </div>
  );
}

function StepText({ data, params, index, steps, show }) {
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
  const facts = observationFacts(data.fragments);
  const summary = steps.find((step) => step.index === index)?.action_summary;
  return (
    <>
      {(facts.front || facts.page) && (
        <dl className="folder-facts">
          {facts.front && <><dt>前台应用</dt><dd>{facts.front}</dd></>}
          {facts.page && <><dt>页面</dt><dd>{facts.page}</dd></>}
        </dl>
      )}
      {restored && <p className="history-note">这是实际恢复的一步，没有重新让策略模型决定。原回复和原动作属于来源材料。</p>}
      {record.origin === "terminal" && <p className="history-note">这里只记录结束后的观测，没有下一次决定。</p>}
      <div className="folder-action">
        <h4>{restored ? "本次实际恢复的动作" : "这一步的动作"}</h4>
        <pre>{summary || "（没有动作）"}</pre>
        {record.skipped_actions > 0 && <p className="history-note">先前的动作失败，末尾 {record.skipped_actions} 个动作没有执行。</p>}
      </div>
      <RecordedText title={restored ? "源运行的回复（恢复材料，不是本次新思考）" : "模型的回复"} value={recordedReply(record)} open />
      {frames.length > 0 && (
        <details className="history-record">
          <summary>同一步的动作前画面（{frames.length} 张，辅助证据，没有送给模型）</summary>
          <div className="folder-frames">
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
            <StepImages
              show={show}
              figures={[{
                key: frame.id,
                title: `第 ${(frame.action_index ?? 0) + 1} 个动作之前`,
                note: frameStatus(frame),
                src: frame.has_image ? imageUrl(params, index, "before_action", frame.id) : null,
                missing: frameStatus(frame),
                marks: frame.marks,
                pointer: frame.pointer,
              }]}
            />
          </div>
        </details>
      )}
      {facts.text && (
        <RecordedText title={`界面文字 · ${facts.text.source}（${facts.text.lines.length} 行）`} value={facts.text.lines.join("\n")} />
      )}
      <RecordedText title="动作原文" value={record.actions} />
      <RecordedText title="动作返回结果" value={record.action_results} />
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

/** One folder of the stack: its step's ear, a thumbnail of its screen and its action in one line. */
function Folder({ step, params, opened, onOpen, order }) {
  return (
    <button
      type="button"
      role="tab"
      id={`folder-tab-${step.index}`}
      data-index={step.index}
      aria-selected={opened}
      aria-controls="folder-neighbour"
      tabIndex={opened ? 0 : -1}
      className={`stack-folder ${step.relation} ${step.origin}`}
      style={{ zIndex: opened ? 50 : order }}
      onClick={() => onOpen(step.index)}
      title={step.action_summary || ""}
    >
      <span className="stack-ear">第 {step.index} 步 <small>{originText[step.origin] || step.origin}</small></span>
      <span className="stack-body">
        {step.has_screenshot ? <img src={imageUrl(params, step.index, "observation")} alt="" loading="lazy" /> : <span className="stack-noshot">无截图</span>}
        <span className="stack-line">{actionLine(step.action_summary)}</span>
      </span>
    </button>
  );
}

function EvidenceFolder({ data, params }) {
  const anchor = data.anchor_step;
  const steps = folderSteps(data.steps, anchor);
  const others = steps.filter((step) => step.index !== anchor);
  const earlier = others.filter((step) => step.index < anchor);
  const later = others.filter((step) => step.index > anchor);
  const pinnedStep = steps.find((step) => step.index === anchor);
  const [opened, setOpened] = useState(() => defaultNeighbour(others, anchor));
  const [show, setShow] = useState({ marks: true, pointer: false });
  const [pinnedOpen, setPinnedOpen] = useState(false);
  const stack = useRef(null);
  const pinned = useRemote(query("review/trajectory-step", { ...params, index: anchor }), params.queue_sha256);
  const neighbour = useRemote(opened == null ? null : query("review/trajectory-step", { ...params, index: opened }), params.queue_sha256);
  const openedStep = others.find((step) => step.index === opened);
  useEffect(() => {
    // Bring the opened folder into the stack's view: scroll the stack sideways only, never the page.
    const strip = stack.current;
    const folder = strip?.querySelector('[aria-selected="true"]');
    if (strip && folder) strip.scrollLeft = folder.offsetLeft - strip.clientWidth / 2 + folder.clientWidth / 2;
  }, [opened]);
  const move = (offset) => {
    if (opened == null) return;
    const next = folderNeighbour(others, opened, offset);
    setOpened(next);
    requestAnimationFrame(() => stack.current?.querySelector(`[data-index="${next}"]`)?.focus());
  };
  // ← → move the opened neighbour, Home / End go to the first and last; only
  // on the stack, so the review page's own ← → (previous and next item) keep
  // working everywhere else.
  const onKeyDown = (event) => {
    const offsets = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    event.stopPropagation();
    move(offsets[event.key]);
  };
  // The pinned and the opened screens share one box size: the tallest image of the four.
  const ratio = tallestRatio([
    pinned.data?.observation_marks, pinned.data?.after_marks,
    neighbour.data?.observation_marks, neighbour.data?.after_marks,
  ]);
  return (
    <section className={`trajectory-evidence evidence-folder ${pinnedOpen ? "pinned-open" : ""}`} aria-label="档案">
      <div className="trajectory-heading">
        <h3>档案</h3>
        <span>当前审阅的这一步固定在左边；从画面下方的文件夹里抽出一步放在旁边对照 · 在文件夹上按 ← → 换一步</span>
      </div>
      <p className="history-note">之后的步骤供审阅参考，不是模型当时的输入。</p>
      <div className="history-image-tools">
        <label className="toggle"><input type="checkbox" checked={show.marks} onChange={(event) => setShow({ ...show, marks: event.target.checked })} />动作位置</label>
        <label className="toggle" title="显示记录位置的辅助标记；原图自带的光标保持原样"><input type="checkbox" checked={show.pointer} onChange={(event) => setShow({ ...show, pointer: event.target.checked })} />当时鼠标位置</label>
      </div>
      <button type="button" className="pinned-strip" aria-expanded={pinnedOpen} onClick={() => setPinnedOpen(!pinnedOpen)}>
        {pinnedStep?.has_screenshot && <img src={imageUrl(params, anchor, "observation")} alt="" />}
        <span className="pinned-strip-text">
          <strong>当前审阅 · 第 {anchor} 步</strong>
          <span>{actionLine(pinnedStep?.action_summary)}</span>
        </span>
        <span className="pinned-strip-toggle">{pinnedOpen ? "收起 ▴" : "展开 ▾"}</span>
      </button>
      <div className="folder-desk">
        <header className="desk-head pinned">
          <h3>第 {anchor} 步 <span>当前审阅 · {originText[pinnedStep?.origin] || pinnedStep?.origin} · 固定</span></h3>
        </header>
        <header className="desk-head neighbour" id="folder-neighbour" role="tabpanel" aria-labelledby={opened == null ? undefined : `folder-tab-${opened}`}>
          {openedStep ? (
            <h3>第 {opened} 步 <span>{relationText[openedStep.relation]} · {originText[openedStep.origin] || openedStep.origin}</span></h3>
          ) : <h3>这次运行没有别的已提交步骤</h3>}
          {openedStep && (
            <div className="folder-card-nav">
              <button type="button" className="text-button" disabled={others[0]?.index === opened} onClick={() => move(-1)}>← 上一步</button>
              <button type="button" className="text-button" disabled={others.at(-1)?.index === opened} onClick={() => move(1)}>下一步 →</button>
            </div>
          )}
        </header>
        <div className="desk-images pinned">
          <RemoteState state={pinned}>{(step) => <StepImages figures={stepFigures(step, params, anchor, steps)} ratio={ratio} show={show} />}</RemoteState>
        </div>
        <div className="desk-images neighbour">
          {opened != null && <RemoteState state={neighbour}>{(step) => <StepImages figures={stepFigures(step, params, opened, steps)} ratio={ratio} show={show} />}</RemoteState>}
        </div>
        {others.length > 0 && (
        <div className="folder-stack desk-stack" role="tablist" aria-label="这次运行的其余步骤" ref={stack} onKeyDown={onKeyDown}>
          <div className="stack-side earlier">
            {earlier.map((step, position) => (
              <Folder key={step.index} step={step} params={params} opened={step.index === opened} onOpen={setOpened} order={position + 1} />
            ))}
          </div>
          <div className="stack-slot" aria-hidden="true">
            <span>第 {anchor} 步</span>
            <small>当前审阅 · 已取出</small>
          </div>
          <div className="stack-side later">
            {later.map((step, position) => (
              <Folder key={step.index} step={step} params={params} opened={step.index === opened} onOpen={setOpened} order={later.length - position} />
            ))}
          </div>
        </div>
      )}
        <div className="desk-text pinned">
          <RemoteState state={pinned}>{(step) => <StepText key={anchor} data={step} params={params} index={anchor} steps={steps} show={show} />}</RemoteState>
        </div>
        <div className="desk-text neighbour">
          {opened != null && <RemoteState state={neighbour}>{(step) => <StepText key={opened} data={step} params={params} index={opened} steps={steps} show={show} />}</RemoteState>}
        </div>
      </div>
    </section>
  );
}

export function ReviewHistory({ queueId, item, version }) {
  const params = { queue_id: queueId, item_id: item.item_id, queue_sha256: version };
  const remote = useRemote(query("review/trajectory", params), version);
  return <RemoteState state={remote}>{(data) => <EvidenceFolder key={`${item.item_id}:${version}`} data={data} params={params} />}</RemoteState>;
}
