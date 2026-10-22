/** Notes and boxes on one run step, outside any review queue. */
import React, { useEffect, useRef, useState } from "react";
import { Icon, Keycap, RemoteState, postJSON, query, useRemote } from "./common.jsx";
import { ImageBoard } from "./board.jsx";
import { sameRegions } from "./boxes.js";
import { isTextTarget, keyAction } from "./keys.js";
import { kindColor, refreshDraft } from "./labels.js";
import { MarkToggle } from "./marks.jsx";
import { hasMarks } from "./marks.js";

const savedDraft = (mine) => ({ regions: mine?.regions || [], note: mine?.note || "" });
const sameStepDraft = (a, b) =>
  sameRegions(a.regions, b.regions) && a.note.trim() === b.note.trim();
const storageKey = (run, step) => `step-label-draft:${run}#${step}`;

function storedDraft(run, step) {
  try {
    const value = window.sessionStorage.getItem(storageKey(run, step));
    return value ? JSON.parse(value) : null;
  } catch {
    return null;
  }
}

function storeDraft(run, step, draft) {
  try {
    if (draft) window.sessionStorage.setItem(storageKey(run, step), JSON.stringify(draft));
    else window.sessionStorage.removeItem(storageKey(run, step));
  } catch {
    // Without storage, unsaved edits last as long as this step stays open.
  }
}

export function StepLabels({ runKey, index, revision, annotations, hasScreenshot }) {
  const [saved, setSaved] = useState(null);
  const state = useRemote(query("review/step", { run: runKey, step: index }), revision);
  useEffect(() => setSaved(null), [runKey, index, revision]);
  return (
    <RemoteState state={state}>
      {(data) => (
        <StepLabelEditor
          key={`${runKey}#${index}`}
          data={saved || data}
          runKey={runKey}
          index={index}
          annotations={annotations}
          hasScreenshot={hasScreenshot}
          onSaved={setSaved}
        />
      )}
    </RemoteState>
  );
}

function StepLabelEditor({ data, runKey, index, annotations, hasScreenshot, onSaved }) {
  const kinds = data.spec.regions.kinds.map((kind, i) => ({ ...kind, color: kindColor(i) }));
  const initial = savedDraft(data.mine);
  // Unsaved edits of this step survive leaving it and reloading the tab.
  const [restored] = useState(() => storedDraft(runKey, index));
  const [draft, setDraft] = useState(restored || initial);
  const [activeKind, setActiveKind] = useState(kinds[0].value);
  const [selected, setSelected] = useState(null);
  const [status, setStatus] = useState(
    restored && !sameStepDraft(restored, initial)
      ? { kind: "info", text: "已恢复你对这一步还没保存的修改。" }
      : null,
  );
  const [saving, setSaving] = useState(false);
  const [showMarks, setShowMarks] = useState(true);
  const marks = useRemote(query("marks", { key: runKey, index }));
  const markData = hasMarks(marks.data) ? marks.data : null;
  // A refresh re-reads the saved label every few seconds: a clean draft
  // follows it, unsaved edits are never replaced.
  const before = useRef(initial);
  useEffect(() => {
    const after = savedDraft(data.mine);
    const result = refreshDraft(draft, before.current, after, sameStepDraft);
    before.current = after;
    if (result.draft !== draft) setDraft(result.draft);
    if (result.conflict)
      setStatus({
        kind: "confirm",
        text: "你在别处保存的这一步的标注变了；你没保存的修改还在，保存后会替换它。",
      });
  }, [data]);
  const dirty = !sameStepDraft(draft, initial);
  useEffect(() => {
    storeDraft(runKey, index, dirty ? draft : null);
    if (!dirty) return;
    const warn = (event) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draft, dirty, runKey, index]);

  async function save(body) {
    setSaving(true);
    try {
      const result = await postJSON("/api/review/step", { run: runKey, step: index, ...body });
      onSaved(result);
      setStatus({ kind: "saved", text: body.regions.length || body.note ? "已保存。" : "已清除。" });
      setSelected(null);
    } catch (error) {
      setStatus({ kind: "problem", text: error.message });
    } finally {
      setSaving(false);
    }
  }

  function onKeyDown(event) {
    const action = keyAction(
      {
        key: event.key,
        code: event.code,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        repeat: event.repeat,
        inText: isTextTarget(event.target),
      },
      { choices: 0, kinds: kinds.length, suggestion: false, selection: selected != null },
    );
    if (!action) return;
    if (action.type === "kind") setActiveKind(kinds[action.index].value);
    else if (action.type === "deleteBox") {
      setDraft({ ...draft, regions: draft.regions.filter((_, i) => i !== selected) });
      setSelected(null);
    } else if (action.type === "escape") setSelected(null);
    else if (action.type === "save" && event.target.tagName === "TEXTAREA") save(draft);
    else return;
    event.preventDefault();
  }

  return (
    <section className="step-labels" onKeyDown={onKeyDown}>
      <p className="muted small">
        标注者：<strong>{annotations.annotator}</strong>。框画在未加标记的浏览器截图上；保存时追加一行到{" "}
        <span className="mono">labels/_adhoc.jsonl</span>，不会写进运行记录。
      </p>
      {hasScreenshot ? (
        <>
          <div className="kinds" role="radiogroup" aria-label="框的类别">
            <span className="kinds-label">框的类别</span>
            {kinds.map((kind, i) => (
              <button
                key={kind.value}
                role="radio"
                aria-checked={activeKind === kind.value}
                className={`kind ${activeKind === kind.value ? "on" : ""}`}
                style={{ "--kind": kind.color }}
                title={kind.help || undefined}
                onClick={() => {
                  setActiveKind(kind.value);
                  if (selected != null)
                    setDraft({
                      ...draft,
                      regions: draft.regions.map((r, j) =>
                        j === selected ? { ...r, kind: kind.value } : r,
                      ),
                    });
                }}
              >
                <span className="kind-swatch" />
                {kind.text}
                <Keycap>⇧{i + 1}</Keycap>
              </button>
            ))}
            {markData && (
              <MarkToggle show={showMarks} onChange={setShowMarks} keycap={false} />
            )}
          </div>
          <ImageBoard
            images={[
              {
                role: "screenshot",
                text: "浏览器状态",
                detail: `第 ${index} 步`,
                src: query("screenshot", { key: runKey, index, view: "browser" }),
                marks: markData,
              },
            ]}
            regions={draft.regions}
            kinds={kinds}
            activeKind={activeKind}
            editable
            selected={selected}
            onSelect={setSelected}
            onChange={(regions) => setDraft({ ...draft, regions })}
            showMarks={showMarks}
          />
        </>
      ) : (
        <p className="muted small">这一步没有截图；你仍然可以写备注。</p>
      )}
      <label className="note-field">
        <span>备注</span>
        <textarea
          rows="3"
          value={draft.note}
          placeholder="看这一步的人应该知道什么？"
          onChange={(event) => setDraft({ ...draft, note: event.target.value })}
        />
      </label>
      <div className="save-row">
        <button className="button primary" disabled={!dirty || saving} onClick={() => save(draft)}>
          {saving ? "正在保存…" : "保存标注"}
          <Keycap>⌘ Enter</Keycap>
        </button>
        {data.mine && (
          <button
            className="button"
            disabled={saving}
            onClick={() => save({ regions: [], note: "" })}
          >
            <Icon name="close" />
            清除我的标注
          </button>
        )}
        {status && <span className={`save-message ${status.kind}`}>{status.text}</span>}
      </div>
      {data.others.length > 0 && (
        <div className="rail-section others">
          <h3>其他标注者</h3>
          <ul>
            {data.others.map((label) => (
              <li key={label.annotator}>
                <strong>{label.annotator}</strong>{" "}
                {label.regions?.length ? `${label.regions.length} 个框` : ""}
                {label.note && <p className="small">{label.note}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data.history.length > 0 && (
        <p className="muted small">
          这一步保存过 {data.history.length} 行标注
          {data.errors ? ` · 文件里有 ${data.errors} 行读不出来` : ""}。
          {data.mine?.at && ` 你的标注保存于 ${new Date(data.mine.at).toLocaleString("zh-CN")}。`}
        </p>
      )}
    </section>
  );
}
