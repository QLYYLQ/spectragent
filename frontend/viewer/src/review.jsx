/** Review mode: the queues tools wrote, and the screen a person labels them on. */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Empty,
  Icon,
  Keycap,
  RemoteState,
  navigate,
  postJSON,
  query,
  replaceRoute,
  runRoute,
  useRemote,
} from "./common.jsx";
import { COMPARE_MODES, ImageBoard, ZOOMS, zoomText } from "./board.jsx";
import "./review.css";
import { moveBox } from "./boxes.js";
import { SHORTCUTS, isTextTarget, keyAction } from "./keys.js";
import {
  FILTERS,
  answerSummary,
  draftFromLabel,
  draftFromSuggestion,
  draftProblems,
  draftsEqual,
  askedQuestions,
  isBlank,
  nextOpenQuestion,
  toggleAnswer,
  itemStatus,
  kindColor,
  labelRequest,
  matchesFilter,
  openingIndex,
  previewVerdict,
  stepIndex,
  suggestionHasAnswer,
  toggleChoice,
  toggleReject,
} from "./labels.js";

const runLink = (run, step) =>
  `#${new URLSearchParams(
    runRoute(run, step == null ? {} : { tab: "trajectory", step }),
  )}`;

const time = (value) =>
  value
    ? new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";

const imageSource = (queueId, itemId, role) =>
  query("review/image", { queue_id: queueId, item_id: itemId, role });

const STATUS_TEXT = {
  unlabeled: "Not labeled",
  accepted: "Accepted",
  corrected: "Corrected",
  rejected: "Rejected",
  labeled: "Labeled",
  skipped: "Skipped",
};

/** One discrete line per item, in queue order: the queue's spectrum. */
export function ProgressStrip({ states, current = -1, dirty = new Set(), onJump }) {
  return (
    <div
      className={`progress-strip ${onJump ? "interactive" : ""}`}
      role="img"
      aria-label={`${states.filter((s) => itemStatus(s) !== "unlabeled").length} of ${states.length} items have your label`}
      onClick={(event) => {
        const tick = event.target.closest("[data-index]");
        if (tick && onJump) onJump(Number(tick.dataset.index));
      }}
    >
      {states.map((state, index) => {
        const status = itemStatus(state);
        return (
          <span
            key={state.item.item_id}
            data-index={index}
            className={`tick ${status} ${index === current ? "current" : ""} ${dirty.has(state.item.item_id) ? "draft" : ""}`}
            title={`${index + 1}. ${state.item.title || state.item.item_id} · ${STATUS_TEXT[status]}`}
          />
        );
      })}
    </div>
  );
}

function ProgressLegend({ progress }) {
  const parts = [
    ["accepted", progress.accepted],
    ["corrected", progress.corrected],
    ["rejected", progress.rejected],
    ["skipped", progress.skipped],
    ["unlabeled", progress.remaining],
  ].filter(([, count]) => count);
  const other =
    progress.labeled - progress.accepted - progress.corrected - progress.rejected;
  if (other) parts.splice(3, 0, ["labeled", other]);
  return (
    <ul className="progress-legend">
      {parts.map(([status, count]) => (
        <li key={status}>
          <span className={`swatch ${status}`} />
          {count} {status === "unlabeled" ? "left" : STATUS_TEXT[status].toLowerCase()}
        </li>
      ))}
    </ul>
  );
}

export function ReviewDisabled({ root }) {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Labeling</span>
          <h1>Review queues</h1>
        </div>
      </div>
      <div className="panel padded review-off">
        <h2>Labeling is off</h2>
        <p>
          This viewer was started read-only. To label review queues and run
          steps, start it with an annotation directory. Labels are written
          there and nowhere else; traces stay read-only.
        </p>
        <pre className="command">
          spectragent view --root {root} --annotations DIR
        </pre>
      </div>
    </>
  );
}

export function ReviewHome({ revision, annotations }) {
  const state = useRemote("/api/review/queues", revision);
  const adhoc = useRemote("/api/review/adhoc", revision);
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Labeling</span>
          <h1>Review queues</h1>
          <p>
            Label as <strong>{annotations.annotator}</strong>. Labels are
            appended to <span className="mono">{annotations.directory}</span>.
          </p>
        </div>
      </div>
      <RemoteState state={state}>
        {(data) =>
          data.queues.length ? (
            <div className="panel queue-list">
              {data.queues.map((row) => (
                <QueueRow key={row.queue_id} row={row} />
              ))}
            </div>
          ) : (
            <div className="panel">
              <Empty title="No review queues yet">
                Tools write queue files into {annotations.directory}/queues.
                Refresh once one is written. You can still label any run step
                from its Labels tab.
              </Empty>
            </div>
          )
        }
      </RemoteState>
      <section className="step-notes-list">
        <h2>Step labels</h2>
        <p className="muted small">
          Notes and boxes added from a run step&apos;s Labels tab, newest first.
        </p>
        <RemoteState state={adhoc}>
          {(data) => (
            <>
              {data.errors.length > 0 && (
                <div className="notice error">
                  {data.errors.length} unreadable lines in labels/_adhoc.jsonl
                  (first: line {data.errors[0].line}, {data.errors[0].error}).
                </div>
              )}
              {data.labels.length ? (
                <div className="panel">
                  {data.labels.map((label) => (
                    <a
                      key={`${label.run}#${label.step}#${label.annotator}`}
                      className="step-note-row"
                      href={runLink(label.run, label.step)}
                    >
                      <span className="step-note-target">
                        <strong>{label.run}</strong>
                        <span className="muted"> step {label.step}</span>
                      </span>
                      <span className="step-note-text">
                        {label.note || (
                          <span className="muted">
                            {label.regions.length} box
                            {label.regions.length === 1 ? "" : "es"}
                          </span>
                        )}
                      </span>
                      <span className="muted small">
                        {label.annotator} · {time(label.at)}
                      </span>
                    </a>
                  ))}
                </div>
              ) : (
                <p className="muted small">No step labels yet.</p>
              )}
            </>
          )}
        </RemoteState>
      </section>
    </>
  );
}

function QueueRow({ row }) {
  const open = () => navigate({ view: "review", queue: row.queue_id });
  if (row.error)
    return (
      <div className="queue-row broken">
        <div className="queue-name">
          <strong>{row.queue_id}</strong>
          <span className="mono small muted">{row.file}</span>
        </div>
        <p className="row-error queue-error">
          This queue cannot be opened: {row.error}
        </p>
      </div>
    );
  const progress = row.progress;
  const done = progress.total - progress.remaining;
  const width = (count) =>
    `${progress.total ? (count / progress.total) * 100 : 0}%`;
  return (
    <div className="queue-row">
      <div className="queue-name">
        <button className="run-link" onClick={open}>
          {row.title}
        </button>
        <span className="mono small muted">{row.queue_id}</span>
      </div>
      <div className="queue-progress">
        <div className="queue-bar" aria-hidden="true">
          {["accepted", "corrected", "rejected"].map((status) => (
            <span
              key={status}
              className={status}
              style={{ width: width(progress[status]) }}
            />
          ))}
          <span
            className="labeled"
            style={{
              width: width(
                progress.labeled -
                  progress.accepted -
                  progress.corrected -
                  progress.rejected,
              ),
            }}
          />
          <span className="skipped" style={{ width: width(progress.skipped) }} />
        </div>
        <span className="small">
          <strong>{done}</strong> of {progress.total} done
          {progress.labeled_by_anyone > progress.labeled &&
            ` · ${progress.labeled_by_anyone} labeled by anyone`}
        </span>
        {row.label_errors > 0 && (
          <span className="row-error">
            {row.label_errors} unreadable label lines
          </span>
        )}
      </div>
      <button className="button" onClick={open}>
        {progress.remaining
          ? done
            ? "Continue"
            : "Start"
          : "Review again"}
        <Icon name="arrow" />
      </button>
    </div>
  );
}

export function ReviewSession({ route, revision, annotations }) {
  const remote = useRemote(query("review/queue", { queue_id: route.queue }), revision);
  return (
    <RemoteState state={remote}>
      {(data) => (
        <Session
          key={data.queue_id}
          data={data}
          route={route}
          annotations={annotations}
        />
      )}
    </RemoteState>
  );
}

function Session({ data, route, annotations }) {
  const spec = data.label;
  const [states, setStates] = useState(data.items);
  const [progress, setProgress] = useState(data.progress);
  useEffect(() => {
    setStates(data.items);
    setProgress(data.progress);
  }, [data]);
  const filter = route.filter || (data.progress.remaining ? "unlabeled" : "all");
  const found = states.findIndex((state) => state.item.item_id === route.item);
  const index = found >= 0 ? found : openingIndex(states, filter);
  const state = states[index];
  const itemId = state?.item.item_id;
  const go = (target, nextFilter = filter) =>
    replaceRoute({
      view: "review",
      queue: data.queue_id,
      item: states[target].item.item_id,
      filter: nextFilter,
    });
  useEffect(() => {
    if (state && route.item !== itemId) go(index);
  }, [route.item, itemId]);

  // Unsaved drafts survive leaving the queue and reloading the tab.
  const draftKey = `review-drafts:${data.queue_id}:${data.sha256}`;
  const [drafts, setDrafts] = useState(() => {
    try {
      return JSON.parse(window.sessionStorage.getItem(draftKey) || "{}");
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      if (Object.keys(drafts).length)
        window.sessionStorage.setItem(draftKey, JSON.stringify(drafts));
      else window.sessionStorage.removeItem(draftKey);
    } catch {
      // Storage may be unavailable (private windows); drafts then last for this page.
    }
  }, [drafts, draftKey]);
  const saved = draftFromLabel(state?.mine);
  const draft = drafts[itemId] ?? saved;
  const dirty = !draftsEqual(draft, saved);
  const dirtyItems = useMemo(
    () =>
      new Set(
        Object.entries(drafts)
          .filter(([id, value]) => {
            const owner = states.find((s) => s.item.item_id === id);
            return owner && !draftsEqual(value, draftFromLabel(owner.mine));
          })
          .map(([id]) => id),
      ),
    [drafts, states],
  );
  useEffect(() => {
    if (!dirtyItems.size) return;
    const warn = (event) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyItems.size]);

  const kinds = spec.regions
    ? spec.regions.kinds.map((kind, i) => ({ ...kind, color: kindColor(i) }))
    : null;
  const [activeKind, setActiveKind] = useState(kinds?.[0]?.value ?? null);
  const [selected, setSelected] = useState(null);
  const [showSuggested, setShowSuggested] = useState(true);
  const [compare, setCompare] = useState("side");
  const [flipIndex, setFlipIndex] = useState(0);
  const [zoom, setZoom] = useState(0);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const [help, setHelp] = useState(false);
  const [focused, setFocused] = useState(0);
  const note = useRef(null);
  useEffect(() => {
    setSelected(null);
    setFlipIndex(0);
    setMessage((old) => (old?.kind === "saved" ? old : null));
    if (state) {
      const asked = askedQuestions(spec, state.item);
      const open = asked.findIndex((q) => !(state.mine?.answers || {})[q.key]);
      setFocused(open < 0 ? 0 : open);
    }
  }, [itemId]);

  // The rail scrolls on its own; keep its bottom (and the save dock) on screen.
  const rail = useRef(null);
  useEffect(() => {
    const fit = () => {
      if (!rail.current) return;
      // Stacked under the images (narrow windows) the rail is part of the page.
      if (getComputedStyle(rail.current).position !== "sticky") {
        rail.current.style.maxHeight = "";
        return;
      }
      const top = Math.max(12, rail.current.getBoundingClientRect().top);
      rail.current.style.maxHeight = `${Math.max(320, window.innerHeight - top - 12)}px`;
    };
    fit();
    window.addEventListener("resize", fit);
    window.addEventListener("scroll", fit, { passive: true });
    return () => {
      window.removeEventListener("resize", fit);
      window.removeEventListener("scroll", fit);
    };
  }, [itemId, help]);
  // Keep the highlighted question visible above the save dock, scrolling the
  // rail only (never the page, so the images stay where they are).
  useEffect(() => {
    const box = rail.current;
    const question = box?.querySelector(".question.active");
    if (!question || box.scrollHeight <= box.clientHeight) return;
    const outer = box.getBoundingClientRect();
    const inner = question.getBoundingClientRect();
    const dock = box.querySelector(".save-dock")?.offsetHeight || 0;
    if (inner.bottom > outer.bottom - dock) box.scrollTop += inner.bottom - (outer.bottom - dock) + 8;
    else if (inner.top < outer.top) box.scrollTop -= outer.top - inner.top + 8;
  }, [focused, itemId]);
  const handler = useRef(() => {});
  useEffect(() => {
    const listener = (event) => handler.current(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
  // Natural sizes of the item's images, for nudging boxes by keyboard.
  const imageSizes = useRef({});
  useEffect(() => {
    imageSizes.current = {};
    for (const image of state?.item.images || []) {
      const probe = new Image();
      probe.onload = () => {
        imageSizes.current[image.role] = {
          width: probe.naturalWidth,
          height: probe.naturalHeight,
        };
      };
      probe.src = imageSource(data.queue_id, itemId, image.role);
    }
  }, [itemId]);

  if (!state)
    return (
      <Empty title="This queue has no items">
        The tool that wrote {data.queue_id} listed no items to review.
      </Empty>
    );
  const item = state.item;
  const suggestion = suggestionHasAnswer(item.suggestion) ? item.suggestion : null;
  const asked = askedQuestions(spec, item);
  const current = asked[Math.min(focused, asked.length - 1)];
  const answer = (index) => {
    const question = asked[focused];
    if (!question || !question.choices[index]) return;
    const next = toggleAnswer(draft, question.key, question.choices[index].value);
    setDraft(next);
    setFocused(nextOpenQuestion(asked, next.answers, focused));
  };
  const images = item.images.map((image) => ({
    role: image.role,
    text: image.text,
    src: imageSource(data.queue_id, item.item_id, image.role),
    detail:
      image.source.kind === "archive"
        ? `${image.source.archiver} · ${image.source.step == null ? "trailing" : `step ${image.source.step}`}`
        : `step ${image.source.step}`,
  }));

  const setDraft = (next) => setDrafts((old) => ({ ...old, [itemId]: next }));
  const move = (direction) => {
    const target = stepIndex(states, index, direction, filter);
    if (target < 0)
      setMessage({
        kind: "info",
        text: "No other item matches this filter.",
      });
    else go(target);
  };

  async function save({ skipped = false, use = draft } = {}) {
    if (saving) return;
    if (!skipped && state.mine && !state.mine.skipped && draftsEqual(use, saved))
      return move(1);
    const problems = skipped ? [] : draftProblems(use, spec, asked);
    if (problems.length) {
      setMessage({ kind: "problem", text: problems[0] });
      return;
    }
    setSaving(true);
    try {
      const response = await postJSON(
        "/api/review/label",
        labelRequest(data.queue_id, itemId, use, spec, { skipped }),
      );
      const next = states.map((old, i) => (i === index ? response.item : old));
      setStates(next);
      setProgress(response.progress);
      setDrafts((old) => {
        const copy = { ...old };
        delete copy[itemId];
        return copy;
      });
      const verdict = response.label.verdict;
      setMessage({
        kind: "saved",
        text: skipped
          ? `Skipped ${item.title || item.item_id}.`
          : `Saved ${item.title || item.item_id}${verdict ? ` as ${verdict}` : ""}.`,
      });
      const target = stepIndex(next, index, 1, filter);
      if (target >= 0)
        replaceRoute({
          view: "review",
          queue: data.queue_id,
          item: next[target].item.item_id,
          filter,
        });
      else
        setMessage({
          kind: "done",
          text:
            filter === "unlabeled"
              ? "Every item in this queue has your label. Change the filter to look at them again."
              : "That was the last item in this filter.",
        });
    } catch (error) {
      setMessage({ kind: "problem", text: error.message });
    } finally {
      setSaving(false);
    }
  }

  const accept = () =>
    save({ use: { ...draftFromSuggestion(suggestion), note: draft.note } });
  const edit = () => setDraft({ ...draftFromSuggestion(suggestion), note: draft.note });
  const regions = draft.regions;

  handler.current = (event) => {
    if (help && event.key === "Escape") return setHelp(false);
    const target = event.target;
    if (
      event.key === "Enter" &&
      target?.closest?.("a, summary, button:not([data-answer])") &&
      !isTextTarget(target)
    )
      return;
    const action = keyAction(
      {
        key: event.key,
        code: event.code,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        inText: isTextTarget(target),
      },
      {
        choices: current ? current.choices.length : spec.choices.length,
        kinds: kinds?.length || 0,
        suggestion: Boolean(suggestion),
        selection: selected != null && regions[selected] != null,
        questions: asked.length,
      },
    );
    if (!action) return;
    event.preventDefault();
    switch (action.type) {
      case "choice":
        if (asked.length) return answer(action.index);
        return setDraft(toggleChoice(draft, spec, spec.choices[action.index].value));
      case "question":
        return setFocused((value) =>
          Math.max(0, Math.min(asked.length - 1, value + action.step)),
        );
      case "kind": {
        const value = kinds[action.index].value;
        setActiveKind(value);
        if (selected != null && regions[selected])
          setDraft({
            ...draft,
            regions: regions.map((r, i) => (i === selected ? { ...r, kind: value } : r)),
          });
        return;
      }
      case "accept":
        return accept();
      case "edit":
        return edit();
      case "reject":
        return setDraft(toggleReject(draft));
      case "save":
        target?.blur?.();
        return save();
      case "skip":
        return save({ skipped: true });
      case "next":
        return move(1);
      case "previous":
        return move(-1);
      case "note":
        return note.current?.focus();
      case "leaveText":
        return target.blur();
      case "deleteBox":
        setDraft({ ...draft, regions: regions.filter((_, i) => i !== selected) });
        return setSelected(null);
      case "nudge": {
        const image = item.images.find((i) => i.role === regions[selected].image);
        const size = imageSizes.current[image?.role];
        if (!size) return;
        return setDraft({
          ...draft,
          regions: regions.map((r, i) =>
            i === selected ? { ...r, box: moveBox(r.box, action.dx, action.dy, size) } : r,
          ),
        });
      }
      case "escape":
        return setSelected(null);
      case "toggleSuggestion":
        return setShowSuggested((value) => !value);
      case "compare":
        return setCompare((mode) => {
          const order = COMPARE_MODES.map((m) => m.value);
          return order[(order.indexOf(mode) + 1) % order.length];
        });
      case "flip":
        return setFlipIndex((value) => value + 1);
      case "zoom":
        return setZoom((value) =>
          action.step === 0
            ? 0
            : Math.max(0, Math.min(ZOOMS.length - 1, value + action.step)),
        );
      case "help":
        return setHelp((value) => !value);
      default:
        return undefined;
    }
  };
  const verdict = previewVerdict(draft, suggestion);
  const position = index + 1;
  const inFilter = states.filter((s) => matchesFilter(s, filter)).length;
  const multiImage = images.length >= 2;

  return (
    <div className="review">
      <header className="review-head">
        <div className="review-title">
          <button
            className="back-link"
            onClick={() => navigate({ view: "review" })}
          >
            <Icon name="back" />
            All queues
          </button>
          <h1>{data.title}</h1>
          <p className="item-line">
            <span>
              Item {position} of {states.length}
              {item.title && ` · ${item.title}`}
            </span>
            <ItemLinks item={item} />
          </p>
        </div>
        <div className="review-controls">
          <label className="filter-select">
            <span className="sr-only">Items to step through</span>
            <select
              value={filter}
              onChange={(event) =>
                replaceRoute({
                  view: "review",
                  queue: data.queue_id,
                  item: itemId,
                  filter: event.target.value,
                })
              }
            >
              {FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.text}
                </option>
              ))}
            </select>
          </label>
          <span className="muted small filter-count">{inFilter} in filter</span>
          <button className="button" onClick={() => move(-1)}>
            <Icon name="back" />
            Previous <Keycap>K</Keycap>
          </button>
          <button className="button" onClick={() => move(1)}>
            Next <Keycap>J</Keycap>
            <Icon name="arrow" />
          </button>
          <button
            className="button icon-button"
            aria-label="Keyboard shortcuts"
            aria-expanded={help}
            onClick={() => setHelp(!help)}
          >
            <Icon name="keyboard" />
            <Keycap>?</Keycap>
          </button>
        </div>
      </header>
      <ProgressStrip
        states={states}
        current={index}
        dirty={dirtyItems}
        onJump={(target) => go(target)}
      />
      <ProgressLegend progress={progress} />
      {data.label_errors.length > 0 && (
        <div className="notice error">
          {data.label_errors.length} lines of labels/{data.queue_id}.jsonl
          cannot be read and are left out (first: line{" "}
          {data.label_errors[0].line}, {data.label_errors[0].error}).
        </div>
      )}
      {help && <ShortcutSheet onClose={() => setHelp(false)} />}
      <div className="review-body">
        <section className="review-stage" aria-label="Item">
          <ItemFacts item={item} />
          <div className="stage-tools">
            {multiImage && (
              <div className="segmented" role="group" aria-label="Compare images">
                {COMPARE_MODES.map((mode) => (
                  <button
                    key={mode.value}
                    data-answer
                    aria-pressed={compare === mode.value}
                    onClick={() => setCompare(mode.value)}
                  >
                    {mode.text}
                  </button>
                ))}
                <Keycap>C</Keycap>
              </div>
            )}
            <div className="segmented" role="group" aria-label="Zoom">
              <button
                aria-label="Zoom out"
                onClick={() => setZoom(Math.max(0, zoom - 1))}
                disabled={zoom === 0}
              >
                −
              </button>
              <button className="zoom-level" onClick={() => setZoom(0)}>
                {zoomText(ZOOMS[zoom])}
              </button>
              <button
                aria-label="Zoom in"
                onClick={() => setZoom(Math.min(ZOOMS.length - 1, zoom + 1))}
                disabled={zoom === ZOOMS.length - 1}
              >
                +
              </button>
            </div>
            {suggestion?.regions?.length > 0 && (
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={showSuggested}
                  onChange={(event) => setShowSuggested(event.target.checked)}
                />
                Suggested boxes <Keycap>G</Keycap>
              </label>
            )}
            {kinds && !(multiImage && compare === "difference") && (
              <span className="muted small stage-hint">
                Drag on an image to draw · Alt-drag to draw over a box
              </span>
            )}
          </div>
          <ImageBoard
            images={images}
            regions={regions}
            suggested={suggestion?.regions || []}
            showSuggested={showSuggested}
            kinds={kinds}
            activeKind={activeKind}
            editable={Boolean(kinds) && !draft.reject}
            selected={selected}
            onSelect={setSelected}
            onChange={(next) => setDraft({ ...draft, regions: next, reject: false })}
            compare={compare}
            flipIndex={flipIndex}
            zoom={ZOOMS[zoom]}
            fit="below"
          />
        </section>
        <aside className="review-rail" aria-label="Label" ref={rail}>
          {data.instructions && <Instructions text={data.instructions} />}
          {suggestion && (
            <SuggestionCard
              suggestion={suggestion}
              spec={spec}
              onAccept={accept}
              onEdit={edit}
              onReject={() => setDraft(toggleReject(draft))}
              rejected={draft.reject}
              saving={saving}
            />
          )}
          <section className="rail-section your-label">
            <h3>Your label</h3>
            {draft.reject && (
              <div className="rejection">
                <strong>Suggestion rejected.</strong> Saved without a
                replacement answer. Add a note to say why, or press R again to
                undo.
              </div>
            )}
            {asked.length > 0 && (
              <QuestionRows
                asked={asked}
                draft={draft}
                focused={focused}
                suggestion={suggestion}
                onFocus={setFocused}
                onAnswer={(index, key, value) => {
                  const next = toggleAnswer(draft, key, value);
                  setDraft(next);
                  setFocused(nextOpenQuestion(asked, next.answers, index));
                }}
              />
            )}
            {spec.choices.length > 0 && (
              <div className="choices" role="group" aria-label="Answer">
                {spec.choices.map((choice, i) => {
                  const on = spec.multi
                    ? draft.choices.includes(choice.value)
                    : draft.choice === choice.value;
                  return (
                    <button
                      key={choice.value}
                      data-answer
                      className={`choice ${on ? "on" : ""} ${suggestion && (suggestion.choice === choice.value || suggestion.choices?.includes(choice.value)) ? "suggested" : ""}`}
                      aria-pressed={on}
                      onClick={() => setDraft(toggleChoice(draft, spec, choice.value))}
                    >
                      {i < 9 && <Keycap>{i + 1}</Keycap>}
                      <span>{choice.text || choice.value}</span>
                    </button>
                  );
                })}
              </div>
            )}
            {spec.multi && spec.choices.length > 0 && (
              <p className="muted small">Choose any that apply.</p>
            )}
            {kinds && (
              <KindPicker
                kinds={kinds}
                activeKind={activeKind}
                onPick={(value) => {
                  setActiveKind(value);
                  if (selected != null && regions[selected])
                    setDraft({
                      ...draft,
                      regions: regions.map((r, i) =>
                        i === selected ? { ...r, kind: value } : r,
                      ),
                    });
                }}
              />
            )}
            {kinds && (
              <RegionList
                regions={regions}
                kinds={kinds}
                images={item.images}
                selected={selected}
                onSelect={setSelected}
                onDelete={(target) => {
                  setDraft({ ...draft, regions: regions.filter((_, i) => i !== target) });
                  setSelected(null);
                }}
              />
            )}
            {spec.note && (
              <label className="note-field">
                <span>
                  Note <Keycap>N</Keycap>
                </span>
                <textarea
                  ref={note}
                  rows="3"
                  value={draft.note}
                  placeholder="Optional. What made this hard to decide?"
                  onChange={(event) => setDraft({ ...draft, note: event.target.value })}
                />
              </label>
            )}
            <SavedLabel state={state} spec={spec} dirty={dirty} />
          </section>
          <OtherLabels state={state} spec={spec} queueId={data.queue_id} />
          <div className="save-dock">
            <div className="save-row">
              <button
                className="button primary"
                onClick={() => save()}
                disabled={saving}
              >
                {saving ? "Saving…" : dirty || !state.mine ? "Save and next" : "Next"}
                <Keycap>Enter</Keycap>
              </button>
              <button
                className="button"
                onClick={() => save({ skipped: true })}
                disabled={saving}
              >
                Skip <Keycap>S</Keycap>
              </button>
            </div>
            <p className="save-status" role="status">
              {message ? (
                <span className={`save-message ${message.kind}`}>{message.text}</span>
              ) : verdict && !isBlank(draft) && (dirty || !state.mine) ? (
                <>
                  Saves as <Badge value={verdict} />
                </>
              ) : suggestion && !state.mine && isBlank(draft) ? (
                <span className="muted">
                  Press <Keycap>A</Keycap> to accept the suggestion, or choose your own answer.
                </span>
              ) : null}
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

function ItemFacts({ item }) {
  if (!item.fields.length) return null;
  return (
    <dl className="item-facts">
      {item.fields.map((field, i) => (
        <div key={`${field.text}-${i}`}>
          <dt>{field.text}</dt>
          <dd>{field.value == null ? "—" : String(field.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ItemLinks({ item }) {
  const links = [
    ...(item.run ? [{ text: "Open step", run: item.run, step: item.step }] : []),
    ...item.links,
  ];
  return links.map((link, i) => (
    <a
      key={`${link.run}-${link.step}-${i}`}
      href={runLink(link.run, link.step)}
      target="_blank"
      rel="noreferrer"
      title={`${link.run}${link.step != null ? `, step ${link.step}` : ""} (new tab)`}
    >
      {link.text}
      {link.step != null && link.text !== "Open step" && ` · ${link.step}`} ↗
    </a>
  ));
}

function Instructions({ text }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 140;
  return (
    <section className={`instructions ${open || !long ? "open" : ""}`}>
      <p>{text}</p>
      {long && (
        <button className="text-button" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show all instructions"}
        </button>
      )}
    </section>
  );
}

/** One row of answers per question; number keys answer the highlighted row. */
function QuestionRows({ asked, draft, focused, suggestion, onFocus, onAnswer }) {
  return (
    <div className="questions">
      {asked.map((question, index) => {
        const mine = (draft.answers || {})[question.key];
        const proposed = suggestion?.answers?.[question.key];
        const reason = suggestion?.reasons?.[question.key];
        const active = index === focused;
        return (
          <fieldset
            key={question.key}
            className={`question ${active ? "active" : ""} ${mine ? "answered" : ""}`}
            onFocus={() => onFocus(index)}
            onClick={() => onFocus(index)}
          >
            <legend>
              <span className="question-number">{index + 1}</span>
              {question.text || question.key}
            </legend>
            <div className="question-choices" role="group" aria-label={question.text || question.key}>
              {question.choices.map((choice, i) => (
                <button
                  key={choice.value}
                  data-answer
                  className={`choice compact ${mine === choice.value ? "on" : ""} ${proposed === choice.value ? "suggested" : ""}`}
                  aria-pressed={mine === choice.value}
                  onClick={(event) => {
                    event.stopPropagation();
                    onAnswer(index, question.key, choice.value);
                  }}
                >
                  {active && i < 9 && <Keycap>{i + 1}</Keycap>}
                  <span>{choice.text || choice.value}</span>
                </button>
              ))}
            </div>
            {proposed != null && (
              <p className="question-suggestion">
                <span className="suggested-mark">Suggested</span>{" "}
                {question.choices.find((c) => c.value === proposed)?.text || proposed}
                {reason && <span className="muted">. {reason}</span>}
              </p>
            )}
          </fieldset>
        );
      })}
    </div>
  );
}

function SuggestionCard({ suggestion, spec, onAccept, onEdit, onReject, rejected, saving }) {
  return (
    <section className="rail-section suggestion">
      <div className="suggestion-head">
        <h3>Suggestion</h3>
        {suggestion.source && (
          <span className="suggestion-source" title="Where the suggestion came from">
            {suggestion.source}
          </span>
        )}
      </div>
      <p className="suggestion-answer">
        {suggestion.answers && spec.questions
          ? `Answers ${Object.keys(suggestion.answers).length} of ${spec.questions.length} questions`
          : answerSummary(suggestion, spec)}
      </p>
      {suggestion.answers && spec.questions && (
        <p className="muted small">Each suggested answer is marked under its question.</p>
      )}
      {suggestion.regions?.length > 0 && (
        <p className="muted small">Dashed boxes on the images.</p>
      )}
      {suggestion.rationale && <p className="rationale">{suggestion.rationale}</p>}
      {suggestion.note && <p className="muted small">{suggestion.note}</p>}
      <div className="suggestion-actions">
        <button className="button accept" onClick={onAccept} disabled={saving}>
          Accept <Keycap>A</Keycap>
        </button>
        <button className="button" onClick={onEdit}>
          Edit <Keycap>E</Keycap>
        </button>
        <button
          className={`button ${rejected ? "reject on" : "reject"}`}
          aria-pressed={rejected}
          onClick={onReject}
        >
          Reject <Keycap>R</Keycap>
        </button>
      </div>
    </section>
  );
}

function KindPicker({ kinds, activeKind, onPick }) {
  return (
    <div className="kinds" role="radiogroup" aria-label="Box kind">
      <span className="kinds-label">Box kind</span>
      {kinds.map((kind, i) => (
        <button
          key={kind.value}
          data-answer
          role="radio"
          aria-checked={activeKind === kind.value}
          className={`kind ${activeKind === kind.value ? "on" : ""}`}
          style={{ "--kind": kind.color }}
          onClick={() => onPick(kind.value)}
        >
          <span className="kind-swatch" />
          {kind.text || kind.value}
          {i < 9 && <Keycap>⇧{i + 1}</Keycap>}
        </button>
      ))}
    </div>
  );
}

function RegionList({ regions, kinds, images, selected, onSelect, onDelete }) {
  if (!regions.length)
    return <p className="muted small region-empty">No boxes drawn.</p>;
  const kindOf = Object.fromEntries(kinds.map((kind) => [kind.value, kind]));
  const imageText = Object.fromEntries(images.map((image) => [image.role, image.text || image.role]));
  return (
    <ol className="region-list">
      {regions.map((region, i) => (
        <li key={i} className={selected === i ? "selected" : ""}>
          <button className="region-pick" data-answer onClick={() => onSelect(i)}>
            <span className="kind-swatch" style={{ "--kind": kindOf[region.kind]?.color }} />
            <span>{kindOf[region.kind]?.text || region.kind}</span>
            <span className="muted small">
              {imageText[region.image] || region.image} ·{" "}
              {Math.round(region.box[2] - region.box[0])}×
              {Math.round(region.box[3] - region.box[1])}
            </span>
          </button>
          <button
            className="region-delete"
            aria-label={`Delete box ${i + 1}`}
            onClick={() => onDelete(i)}
          >
            <Icon name="close" width="14" height="14" />
          </button>
        </li>
      ))}
    </ol>
  );
}

function SavedLabel({ state, spec, dirty }) {
  const mine = state.mine;
  if (!mine) return <p className="muted small saved-line">Not labeled by you yet.</p>;
  return (
    <p className="small saved-line">
      {dirty ? "Unsaved changes. " : ""}
      Saved {time(mine.at)}:{" "}
      {mine.skipped ? "skipped" : answerSummary(mine, spec) || "no answer"}
      {mine.verdict && (
        <>
          {" "}
          <Badge value={mine.verdict} />
        </>
      )}
      {mine.queue_changed && (
        <span className="warn-text">
          {" "}
          Saved against an earlier version of this queue.
        </span>
      )}
    </p>
  );
}

function OtherLabels({ state, spec, queueId }) {
  const [open, setOpen] = useState(false);
  const history = useRemote(
    open
      ? query("review/history", { queue_id: queueId, item_id: state.item.item_id })
      : null,
    state.history,
  );
  if (!state.others.length && state.history <= 1) return null;
  return (
    <section className="rail-section others">
      {state.others.length > 0 && (
        <>
          <h3>Other annotators</h3>
          <ul>
            {state.others.map((label) => (
              <li key={label.annotator}>
                <strong>{label.annotator}</strong>{" "}
                {label.skipped ? "skipped" : answerSummary(label, spec)}
                {label.verdict && (
                  <>
                    {" "}
                    <Badge value={label.verdict} />
                  </>
                )}
                {label.note && <p className="muted small">{label.note}</p>}
              </li>
            ))}
          </ul>
        </>
      )}
      {state.history > 1 && (
        <details onToggle={(event) => setOpen(event.currentTarget.open)}>
          <summary>All {state.history} saved labels for this item</summary>
          <RemoteState state={history}>
            {(data) =>
              data && (
                <ol className="history">
                  {data.labels.map((label, i) => (
                    <li key={i}>
                      {time(label.at)} · {label.annotator} ·{" "}
                      {label.skipped ? "skipped" : answerSummary(label, spec) || "no answer"}
                      {label.verdict && ` · ${label.verdict}`}
                    </li>
                  ))}
                </ol>
              )
            }
          </RemoteState>
        </details>
      )}
    </section>
  );
}

function ShortcutSheet({ onClose }) {
  return (
    <div className="shortcut-sheet" role="dialog" aria-label="Keyboard shortcuts">
      <div className="section-heading">
        <h3>Keyboard</h3>
        <button className="text-button" onClick={onClose}>
          Close <Keycap>Esc</Keycap>
        </button>
      </div>
      <dl>
        {SHORTCUTS.map((shortcut) => (
          <div key={shortcut.text}>
            <dt>
              {shortcut.keys.map((key, i) =>
                key === "…" ? (
                  <span key={i}>–</span>
                ) : (
                  <Keycap key={i}>{key}</Keycap>
                ),
              )}
            </dt>
            <dd>{shortcut.text}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
