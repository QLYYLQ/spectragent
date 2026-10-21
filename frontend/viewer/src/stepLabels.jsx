/** Notes and boxes on one run step, outside any review queue. */
import React, { useEffect, useRef, useState } from "react";
import { Icon, Keycap, RemoteState, postJSON, query, useRemote } from "./common.jsx";
import { ImageBoard } from "./board.jsx";
import { sameRegions } from "./boxes.js";
import { isTextTarget, keyAction } from "./keys.js";
import { kindColor, refreshDraft } from "./labels.js";

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
      ? { kind: "info", text: "Your unsaved edits to this step were restored." }
      : null,
  );
  const [saving, setSaving] = useState(false);
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
        text: "Your saved label for this step changed elsewhere; your unsaved edits are kept and saving replaces it.",
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
      setStatus({ kind: "saved", text: body.regions.length || body.note ? "Saved." : "Cleared." });
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
        Labeling as <strong>{annotations.annotator}</strong>. Boxes are drawn on
        the clean browser screenshot; saving appends a line to{" "}
        <span className="mono">labels/_adhoc.jsonl</span>, never to the trace.
      </p>
      {hasScreenshot ? (
        <>
          <div className="kinds" role="radiogroup" aria-label="Box kind">
            <span className="kinds-label">Box kind</span>
            {kinds.map((kind, i) => (
              <button
                key={kind.value}
                role="radio"
                aria-checked={activeKind === kind.value}
                className={`kind ${activeKind === kind.value ? "on" : ""}`}
                style={{ "--kind": kind.color }}
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
          </div>
          <ImageBoard
            images={[
              {
                role: "screenshot",
                text: "Browser state",
                detail: `step ${index}`,
                src: query("screenshot", { key: runKey, index, view: "browser" }),
              },
            ]}
            regions={draft.regions}
            kinds={kinds}
            activeKind={activeKind}
            editable
            selected={selected}
            onSelect={setSelected}
            onChange={(regions) => setDraft({ ...draft, regions })}
          />
        </>
      ) : (
        <p className="muted small">This step has no committed screenshot; you can still add a note.</p>
      )}
      <label className="note-field">
        <span>Note</span>
        <textarea
          rows="3"
          value={draft.note}
          placeholder="What should someone looking at this step know?"
          onChange={(event) => setDraft({ ...draft, note: event.target.value })}
        />
      </label>
      <div className="save-row">
        <button className="button primary" disabled={!dirty || saving} onClick={() => save(draft)}>
          {saving ? "Saving…" : "Save label"}
          <Keycap>⌘ Enter</Keycap>
        </button>
        {data.mine && (
          <button
            className="button"
            disabled={saving}
            onClick={() => save({ regions: [], note: "" })}
          >
            <Icon name="close" />
            Clear my label
          </button>
        )}
        {status && <span className={`save-message ${status.kind}`}>{status.text}</span>}
      </div>
      {data.others.length > 0 && (
        <div className="rail-section others">
          <h3>Other annotators</h3>
          <ul>
            {data.others.map((label) => (
              <li key={label.annotator}>
                <strong>{label.annotator}</strong>{" "}
                {label.regions?.length ? `${label.regions.length} boxes` : ""}
                {label.note && <p className="small">{label.note}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data.history.length > 0 && (
        <p className="muted small">
          {data.history.length} saved {data.history.length === 1 ? "line" : "lines"} for this
          step{data.errors ? ` · ${data.errors} unreadable lines in the file` : ""}.
          {data.mine?.at && ` Yours was saved ${new Date(data.mine.at).toLocaleString()}.`}
        </p>
      )}
    </section>
  );
}
