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
import { MarkToggle } from "./marks.jsx";
import { hasMarks } from "./marks.js";
import {
  FILTERS,
  pickedChoices,
  regionRuleProblems,
  regionRuleText,
  answerSummary,
  draftFromLabel,
  draftFromSuggestion,
  draftProblems,
  draftsEqual,
  askedQuestions,
  confirmPress,
  discardsWork,
  draftStorageKey,
  emptyDraft,
  staleDraftKeys,
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
    ? new Date(value).toLocaleString("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";

const imageSource = (queueId, itemId, role, version) =>
  query("review/image", { queue_id: queueId, item_id: itemId, role, queue_sha256: version });

const STATUS_TEXT = {
  unlabeled: "未标注",
  accepted: "已接受建议",
  corrected: "已修正建议",
  rejected: "已驳回建议",
  labeled: "已标注",
  skipped: "已跳过",
};

/** What an accept, edit, skip or reject does, for the press-again prompt. */
const ACTION_TEXT = {
  accept: "接受建议",
  edit: "用建议替换你的答案",
  skip: "跳过这一条",
  reject: "驳回建议",
};

const marksSource = (queueId, itemId, role, version) =>
  query("review/marks", { queue_id: queueId, item_id: itemId, role, queue_sha256: version });

/** The served action marks of every image of one item, by role; refetched per item. */
function useItemMarks(queueId, itemId, images, version) {
  const [marks, setMarks] = useState({ itemId: null, byRole: {} });
  useEffect(() => {
    if (!itemId) return undefined;
    const controller = new AbortController();
    setMarks({ itemId, byRole: {} });
    for (const image of images) {
      fetch(marksSource(queueId, itemId, image.role, version), { signal: controller.signal })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (data)
            setMarks((old) =>
              old.itemId === itemId ? { itemId, byRole: { ...old.byRole, [image.role]: data } } : old,
            );
        })
        .catch(() => {});
    }
    return () => controller.abort();
  }, [queueId, itemId, version]);
  return marks.itemId === itemId ? marks.byRole : {};
}

/** Whether this browser has opened the guide of a queue before (the guide opens by itself once). */
function guideSeen(queueId) {
  try {
    return window.localStorage.getItem(`review-guide-seen:${queueId}`) === "1";
  } catch {
    return false;
  }
}

function markGuideSeen(queueId) {
  try {
    window.localStorage.setItem(`review-guide-seen:${queueId}`, "1");
  } catch {
    // Without storage the guide simply opens again next time.
  }
}

/** One discrete line per item, in queue order: the queue's spectrum. */
export function ProgressStrip({ states, current = -1, dirty = new Set(), onJump }) {
  return (
    <div
      className={`progress-strip ${onJump ? "interactive" : ""}`}
      role="img"
      aria-label={`${states.length} 条里有 ${states.filter((s) => itemStatus(s) !== "unlabeled").length} 条已有你的标注`}
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
          {status === "unlabeled" ? "还剩" : STATUS_TEXT[status]} {count}
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
          <span className="eyebrow">标注</span>
          <h1>审阅队列</h1>
        </div>
      </div>
      <div className="panel padded review-off">
        <h2>标注没有打开</h2>
        <p>
          这个查看器是以只读方式启动的。要标注审阅队列和运行的步骤，请带上标注目录重新启动。
          标注只写进那个目录，运行记录始终只读。
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
          <span className="eyebrow">标注</span>
          <h1>审阅队列</h1>
          <p>
            标注者：<strong>{annotations.annotator}</strong>。标注会追加保存到{" "}
            <span className="mono">{annotations.directory}</span>。
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
              <Empty title="还没有审阅队列">
                生成队列的工具会把队列文件写到 {annotations.directory}/queues，写好后刷新即可。
                你也可以在任意运行步骤的“标注”页里随手标注。
              </Empty>
            </div>
          )
        }
      </RemoteState>
      <section className="step-notes-list">
        <h2>随手标注</h2>
        <p className="muted small">
          在运行步骤的“标注”页里写的备注和画的框，最新的在前。
        </p>
        <RemoteState state={adhoc}>
          {(data) => (
            <>
              {data.errors.length > 0 && (
                <div className="notice error">
                  labels/_adhoc.jsonl 里有 {data.errors.length} 行读不出来
                  （第一处：第 {data.errors[0].line} 行，{data.errors[0].error}）。
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
                        <span className="muted"> 第 {label.step} 步</span>
                      </span>
                      <span className="step-note-text">
                        {label.note || (
                          <span className="muted">{label.regions.length} 个框</span>
                        )}
                      </span>
                      <span className="muted small">
                        {label.annotator} · {time(label.at)}
                      </span>
                    </a>
                  ))}
                </div>
              ) : (
                <p className="muted small">还没有随手标注。</p>
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
          这个队列打不开：{row.error}
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
          已完成 <strong>{done}</strong> / {progress.total}
          {progress.labeled_by_anyone > progress.labeled &&
            ` · 所有人合计标了 ${progress.labeled_by_anyone} 条`}
        </span>
        {row.label_errors > 0 && (
          <span className="row-error">
            {row.label_errors} 行标注读不出来
          </span>
        )}
      </div>
      <button className="button" onClick={open}>
        {progress.remaining
          ? done
            ? "继续"
            : "开始"
          : "再看一遍"}
        <Icon name="arrow" />
      </button>
    </div>
  );
}

export function ReviewSession({ route, revision, annotations }) {
  const [reloads, setReloads] = useState(0);
  const remote = useRemote(
    query("review/queue", { queue_id: route.queue }),
    `${revision}:${reloads}`,
  );
  return (
    <RemoteState state={remote}>
      {(data) => (
        // One session per queue version: a rewritten queue starts afresh.
        <Session
          key={`${data.queue_id}:${data.sha256}`}
          data={data}
          route={route}
          annotations={annotations}
          onReload={() => setReloads((value) => value + 1)}
        />
      )}
    </RemoteState>
  );
}

/** Drafts this tab kept for the queue version, after dropping other versions' drafts. */
function loadDrafts(queueId, version) {
  try {
    const storage = window.sessionStorage;
    const keys = Array.from({ length: storage.length }, (_, i) => storage.key(i));
    let dropped = 0;
    for (const key of staleDraftKeys(keys, queueId, version)) {
      dropped += Object.keys(JSON.parse(storage.getItem(key) || "{}")).length;
      storage.removeItem(key);
    }
    const drafts = JSON.parse(storage.getItem(draftStorageKey(queueId, version)) || "{}");
    return { drafts, dropped };
  } catch {
    return { drafts: {}, dropped: 0 };
  }
}

function Session({ data, route, annotations, onReload }) {
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

  // Unsaved drafts survive leaving the queue and reloading the tab, for this
  // queue version only: a rewritten queue drops them and says so.
  const draftKey = draftStorageKey(data.queue_id, data.sha256);
  const [initial] = useState(() => loadDrafts(data.queue_id, data.sha256));
  const [drafts, setDrafts] = useState(initial.drafts);
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
  const [guide, setGuide] = useState(() => !guideSeen(data.queue_id));
  const [showMarks, setShowMarks] = useState(true);
  const onRequest = data.suggestion_display?.shown === "on_request";
  const [revealed, setRevealed] = useState(!onRequest);
  const [focused, setFocused] = useState(0);
  const [pending, setPending] = useState(null);
  const note = useRef(null);
  useEffect(() => {
    if (guide) markGuideSeen(data.queue_id);
  }, [guide]);
  useEffect(() => {
    setPending(null);
    setSelected(null);
    setFlipIndex(0);
    setRevealed(!onRequest);
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
  }, [itemId, help, guide]);
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
      probe.src = imageSource(data.queue_id, itemId, image.role, data.sha256);
    }
  }, [itemId]);

  const marks = useItemMarks(data.queue_id, itemId, state?.item.images || [], data.sha256);
  if (!state)
    return (
      <Empty title="这个队列没有条目">
        生成 {data.queue_id} 的工具没有列出要审阅的条目。
      </Empty>
    );
  const item = state.item;
  const suggestion = suggestionHasAnswer(item.suggestion) ? item.suggestion : null;
  // A suggestion shown only on request stays out of sight (and out of the
  // answer buttons) until the person asks for it, so it cannot anchor them.
  const visible = suggestion && revealed ? suggestion : null;
  const suggestionTitle = data.suggestion_display?.title || "建议";
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
    src: imageSource(data.queue_id, item.item_id, image.role, data.sha256),
    marks: hasMarks(marks[image.role]) ? marks[image.role] : null,
    detail:
      image.source.kind === "archive"
        ? `画面帧 · ${image.source.step == null ? "收尾记录" : `第 ${image.source.step} 步`}`
        : `第 ${image.source.step} 步的截图`,
  }));
  const anyMarks = images.some((image) => image.marks);

  const setDraft = (next) => {
    setDrafts((old) => ({ ...old, [itemId]: next }));
    // A reason not to save belongs to the answer it was about; a changed answer drops it.
    setMessage((old) => (old?.kind === "problem" ? null : old));
  };
  const move = (direction) => {
    const target = stepIndex(states, index, direction, filter);
    if (target < 0)
      setMessage({
        kind: "info",
        text: "没有别的条目符合这个筛选。",
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
        labelRequest(data.queue_id, itemId, use, spec, { skipped, version: data.sha256 }),
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
          ? `已跳过：${item.title || item.item_id}。`
          : `已保存：${item.title || item.item_id}${verdict ? `（${STATUS_TEXT[verdict] || verdict}）` : ""}。`,
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
              ? "这个队列的每一条都有你的标注了。换个筛选可以再看一遍。"
              : "这已经是这个筛选里的最后一条。",
        });
    } catch (error) {
      setMessage(
        error.status === 409
          ? {
              kind: "changed",
              text: "这个队列在你打开之后被重新生成了，所以没有保存。请重新加载，标注当前版本。",
            }
          : { kind: "problem", text: error.message },
      );
    } finally {
      setSaving(false);
    }
  }

  // Accept, edit, skip and reject replace the answer. When that would drop
  // unsaved work, the first press only asks for a second one.
  const guarded = (action, replacement, run, prompt) => {
    if (!discardsWork(draft, saved, replacement)) {
      setPending(null);
      return run();
    }
    const result = confirmPress(pending, action, Date.now());
    setPending(result.pending);
    if (result.run) return run();
    setMessage({
      kind: "confirm",
      text: `${prompt}，即可${ACTION_TEXT[action]}，并丢掉你还没保存的答案。`,
    });
  };
  const proposed = { ...draftFromSuggestion(suggestion), note: draft.note };
  // Acting on a hidden suggestion first shows it: nobody accepts what they did not see.
  const shown = (run) => () => {
    if (visible) return run();
    setRevealed(true);
    setMessage({ kind: "info", text: `已显示${suggestionTitle}。看过之后再按一次。` });
  };
  const accept = shown(() =>
    guarded("accept", proposed, () => save({ use: proposed }), "再按一次 A 或点“接受”"),
  );
  const edit = shown(() =>
    guarded("edit", proposed, () => setDraft(proposed), "再按一次 E 或点“编辑”"),
  );
  const skip = () =>
    guarded("skip", { ...emptyDraft(), note: draft.note }, () => save({ skipped: true }), "再按一次 S 或点“跳过”");
  const reject = shown(() =>
    draft.reject
      ? setDraft(toggleReject(draft))
      : guarded("reject", toggleReject(draft), () => setDraft(toggleReject(draft)), "再按一次 R 或点“驳回”"),
  );
  const regions = draft.regions;

  handler.current = (event) => {
    if (help && event.key === "Escape") return setHelp(false);
    if (guide && event.key === "Escape") return setGuide(false);
    const target = event.target;
    // Enter saves, whatever review control has focus; only a link or a
    // disclosure keeps its own Enter.
    if (event.key === "Enter" && target?.closest?.("a, summary") && !isTextTarget(target))
      return;
    const action = keyAction(
      {
        key: event.key,
        code: event.code,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        repeat: event.repeat,
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
        return reject();
      case "save":
        target?.blur?.();
        return save();
      case "skip":
        return skip();
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
      case "revealSuggestion":
        return setRevealed((value) => !value);
      case "marks":
        return setShowMarks((value) => !value);
      case "guide":
        return setGuide((value) => !value);
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
  const verdict = previewVerdict(draft, visible);
  const ruleProblems = draft.reject
    ? []
    : regionRuleProblems(pickedChoices(draft, spec, asked), draft.regions || []);
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
            全部队列
          </button>
          <h1>{data.title}</h1>
          <p className="item-line">
            <span>
              第 {position} 条，共 {states.length} 条
              {item.title && ` · ${item.title}`}
            </span>
            <ItemLinks item={item} />
          </p>
        </div>
        <div className="review-controls">
          <label className="filter-select">
            <span className="sr-only">要逐条查看的条目</span>
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
          <span className="muted small filter-count">筛选出 {inFilter} 条</span>
          <button className="button" onClick={() => move(-1)}>
            <Icon name="back" />
            上一条 <Keycap>K</Keycap>
          </button>
          <button className="button" onClick={() => move(1)}>
            下一条 <Keycap>J</Keycap>
            <Icon name="arrow" />
          </button>
          <button
            className={`button guide-button ${guide ? "on" : ""}`}
            aria-expanded={guide}
            onClick={() => setGuide(!guide)}
          >
            标注说明 <Keycap>I</Keycap>
          </button>
          <button
            className="button icon-button"
            aria-label="键盘快捷键"
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
      {initial.dropped > 0 && (
        <div className="notice">
          你开始编辑后，这个队列被它的工具重新生成了。旧版本里 {initial.dropped}{" "}
          份没保存的草稿已经丢弃；你保存过的标注不受影响。
        </div>
      )}
      {data.removed_items_with_labels > 0 && (
        <p className="muted small">
          有 {data.removed_items_with_labels} 个已经不在这个队列里的条目有标注。它们仍保存在
          labels/{data.queue_id}.jsonl 里，这里不显示。
        </p>
      )}
      {data.label_errors.length > 0 && (
        <div className="notice error">
          labels/{data.queue_id}.jsonl 里有 {data.label_errors.length} 行读不出来，已略过
          （第一处：第 {data.label_errors[0].line} 行，{data.label_errors[0].error}）。
        </div>
      )}
      {help && <ShortcutSheet onClose={() => setHelp(false)} />}
      {guide && (
        <GuideSheet
          data={data}
          spec={spec}
          suggestionTitle={suggestionTitle}
          onClose={() => setGuide(false)}
        />
      )}
      <div className="review-body">
        <section className="review-stage" aria-label="条目">
          <ItemFacts item={item} />
          <div className="stage-tools">
            {multiImage && (
              <div className="segmented" role="group" aria-label="对比图片">
                {COMPARE_MODES.map((mode) => (
                  <button
                    key={mode.value}
                    aria-pressed={compare === mode.value}
                    onClick={() => setCompare(mode.value)}
                  >
                    {mode.text}
                  </button>
                ))}
                <Keycap>C</Keycap>
              </div>
            )}
            <div className="segmented" role="group" aria-label="缩放">
              <button
                aria-label="缩小"
                onClick={() => setZoom(Math.max(0, zoom - 1))}
                disabled={zoom === 0}
              >
                −
              </button>
              <button className="zoom-level" onClick={() => setZoom(0)}>
                {zoomText(ZOOMS[zoom])}
              </button>
              <button
                aria-label="放大"
                onClick={() => setZoom(Math.min(ZOOMS.length - 1, zoom + 1))}
                disabled={zoom === ZOOMS.length - 1}
              >
                +
              </button>
            </div>
            {anyMarks && <MarkToggle show={showMarks} onChange={setShowMarks} />}
            {visible?.regions?.length > 0 && (
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={showSuggested}
                  onChange={(event) => setShowSuggested(event.target.checked)}
                />
                建议的框 <Keycap>G</Keycap>
              </label>
            )}
            {kinds && !(multiImage && compare === "difference") && (
              <span className="muted small stage-hint">
                在图上拖动画框 · 按住 Alt 拖动可在已有框上再画框
              </span>
            )}
          </div>
          <ImageBoard
            key={itemId}
            images={images}
            regions={regions}
            suggested={visible?.regions || []}
            showSuggested={showSuggested}
            showMarks={showMarks}
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
        <aside className="review-rail" aria-label="标注" ref={rail}>
          <Instructions text={data.instructions} onGuide={() => setGuide(true)} />
          {suggestion && (
            <SuggestionCard
              suggestion={visible}
              title={suggestionTitle}
              spec={spec}
              onReveal={() => setRevealed(true)}
              onHide={onRequest ? () => setRevealed(false) : null}
              onAccept={accept}
              onEdit={edit}
              onReject={reject}
              rejected={draft.reject}
              saving={saving}
            />
          )}
          <section className="rail-section your-label">
            <h3>你的标注</h3>
            {draft.reject && (
              <div className="rejection">
                <strong>已驳回建议。</strong>保存时不带替代答案。可以在备注里写原因，
                或再按一次 R 撤销。
              </div>
            )}
            {asked.length > 0 && (
              <QuestionRows
                asked={asked}
                draft={draft}
                focused={focused}
                suggestion={visible}
                onFocus={setFocused}
                onAnswer={(index, key, value) => {
                  const next = toggleAnswer(draft, key, value);
                  setDraft(next);
                  setFocused(nextOpenQuestion(asked, next.answers, index));
                }}
              />
            )}
            {spec.choices.length > 0 && (
              <div className="choices" role="group" aria-label="答案">
                {spec.choices.map((choice, i) => {
                  const on = spec.multi
                    ? draft.choices.includes(choice.value)
                    : draft.choice === choice.value;
                  return (
                    <button
                      key={choice.value}
                      className={`choice ${on ? "on" : ""} ${visible && (visible.choice === choice.value || visible.choices?.includes(choice.value)) ? "suggested" : ""}`}
                      aria-pressed={on}
                      onClick={() => setDraft(toggleChoice(draft, spec, choice.value))}
                    >
                      {i < 9 && <Keycap>{i + 1}</Keycap>}
                      <span className="choice-body">
                        <span className="choice-name">
                          {choice.text || choice.value}
                          {regionRuleText(choice.regions) && (
                            <span className={`rule-tag ${choice.regions}`}>
                              {regionRuleText(choice.regions)}
                            </span>
                          )}
                        </span>
                        {choice.help && <span className="choice-help">{choice.help}</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            {spec.multi && spec.choices.length > 0 && (
              <p className="muted small">可以多选。</p>
            )}
            {ruleProblems.length > 0 && (
              <p className="rule-problem" role="alert">
                {ruleProblems[0]}
              </p>
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
                  备注 <Keycap>N</Keycap>
                </span>
                <textarea
                  ref={note}
                  rows="3"
                  value={draft.note}
                  placeholder="可选。比如：为什么难判断、为什么跳过。"
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
                {saving ? "正在保存…" : dirty || !state.mine ? "保存并下一条" : "下一条"}
                <Keycap>Enter</Keycap>
              </button>
              <button className="button" onClick={skip} disabled={saving}>
                跳过 <Keycap>S</Keycap>
              </button>
            </div>
            <p className="save-status" role="status">
              {message ? (
                <span className={`save-message ${message.kind}`}>
                  {message.text}
                  {message.kind === "changed" && (
                    <>
                      {" "}
                      <button className="text-button" onClick={onReload}>
                        重新加载队列
                      </button>
                    </>
                  )}
                </span>
              ) : ruleProblems.length && !isBlank(draft) ? (
                <span className="save-message problem">现在还不能保存：{ruleProblems[0]}</span>
              ) : verdict && !isBlank(draft) && (dirty || !state.mine) ? (
                <>
                  将保存为 <Badge value={verdict} />
                </>
              ) : visible && !state.mine && isBlank(draft) ? (
                <span className="muted">
                  按 <Keycap>A</Keycap> 接受建议，或者自己选答案。
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
  const facts = item.fields.filter((field) => !field.technical);
  const technical = item.fields.filter((field) => field.technical);
  const list = (fields) =>
    fields.map((field, i) => (
      <div key={`${field.text}-${i}`}>
        <dt>{field.text}</dt>
        <dd>{field.value == null ? "—" : String(field.value)}</dd>
      </div>
    ));
  return (
    <>
      {facts.length > 0 && <dl className="item-facts">{list(facts)}</dl>}
      {technical.length > 0 && (
        <details className="technical-facts">
          <summary>技术细节（{technical.length} 项，一般不用看）</summary>
          <dl className="item-facts">{list(technical)}</dl>
        </details>
      )}
    </>
  );
}

function ItemLinks({ item }) {
  const links = [
    ...(item.run ? [{ text: "打开这一步", run: item.run, step: item.step, own: true }] : []),
    ...item.links,
  ];
  return links.map((link, i) => (
    <a
      key={`${link.run}-${link.step}-${i}`}
      href={runLink(link.run, link.step)}
      target="_blank"
      rel="noreferrer"
      title={`${link.run}${link.step != null ? `，第 ${link.step} 步` : ""}（新标签页）`}
    >
      {link.text}
      {link.step != null && !link.own && ` · 第 ${link.step} 步`} ↗
    </a>
  ));
}

function Instructions({ text, onGuide }) {
  return (
    <section className="instructions open">
      {text && <p>{text}</p>}
      <button className="button guide-open" onClick={onGuide}>
        查看标注说明（每个选项的意思和例子） <Keycap>I</Keycap>
      </button>
    </section>
  );
}

/** Plain guide text: blank lines part paragraphs, lines starting with "- " are list items. */
function GuideText({ text }) {
  const blocks = [];
  for (const line of text.split("\n")) {
    const item = line.startsWith("- ") ? line.slice(2) : null;
    const last = blocks[blocks.length - 1];
    if (item != null) {
      if (last?.list) last.lines.push(item);
      else blocks.push({ list: true, lines: [item] });
    } else if (line.trim()) blocks.push({ list: false, lines: [line] });
  }
  return blocks.map((block, i) =>
    block.list ? (
      <ul key={i}>
        {block.lines.map((line, j) => (
          <li key={j}>{line}</li>
        ))}
      </ul>
    ) : (
      <p key={i}>{block.lines[0]}</p>
    ),
  );
}

function ChoiceDefinitions({ choices }) {
  const explained = choices.filter((choice) => choice.help || choice.regions !== "optional");
  return (
    <>
      {explained.length < choices.length && (
        <p className="muted small">
          选项：{choices.map((choice) => choice.text || choice.value).join(" / ")}
        </p>
      )}
      <dl className="guide-choices">
        {explained.map((choice) => (
          <div key={choice.value}>
            <dt>
              {choice.text || choice.value}
              {regionRuleText(choice.regions) && (
                <span className={`rule-tag ${choice.regions}`}>
                  {regionRuleText(choice.regions)}
                </span>
              )}
            </dt>
            <dd>{choice.help || "—"}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}

/** The queue's labeling guide: what to do, what every answer means, and examples. */
function GuideSheet({ data, spec, suggestionTitle, onClose }) {
  return (
    <div className="guide-sheet" role="dialog" aria-label="标注说明">
      <div className="section-heading">
        <h3>标注说明 · {data.title}</h3>
        <button className="text-button" onClick={onClose}>
          关闭 <Keycap>Esc</Keycap>
        </button>
      </div>
      {data.instructions && <p className="guide-lead">{data.instructions}</p>}
      {spec.questions ? (
        spec.questions.map((question) => (
          <section key={question.key}>
            <h4>{question.text || question.key}</h4>
            {question.help && <p>{question.help}</p>}
            <ChoiceDefinitions choices={question.choices} />
          </section>
        ))
      ) : spec.choices.length ? (
        <section>
          <h4>选项{spec.multi ? "（可以多选）" : "（选一个）"}</h4>
          <ChoiceDefinitions choices={spec.choices} />
        </section>
      ) : null}
      {spec.regions && (
        <section>
          <h4>框</h4>
          <dl className="guide-choices">
            {spec.regions.kinds.map((kind) => (
              <div key={kind.value}>
                <dt>{kind.text || kind.value}</dt>
                <dd>{kind.help || "—"}</dd>
              </div>
            ))}
          </dl>
          <p className="muted small">
            在图上按住鼠标拖动画框；点中框可以移动、拉角调整大小，按 Delete 删除。
          </p>
        </section>
      )}
      {(data.guide || []).map((section, i) => (
        <section key={i}>
          <h4>{section.title}</h4>
          <GuideText text={section.text} />
        </section>
      ))}
      <section>
        <h4>保存、跳过和建议</h4>
        <ul>
          <li>选好答案（和框）后按 Enter 或点“保存并下一条”。</li>
          <li>看不清、拿不准或图没加载出来：按 S 跳过，并在备注里写原因。</li>
          <li>
            “{suggestionTitle}”只供参考
            {data.suggestion_display?.shown === "on_request"
              ? "，默认藏起来，先自己判断，再按 V 对照。"
              : "；可以按 A 接受、按 E 复制过来修改，或按 R 驳回。"}
          </li>
          <li>图上的编号标记是动作的位置，编号和动作列表一致；按 M 可以隐藏或显示。</li>
        </ul>
      </section>
    </div>
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
            {question.help && <p className="question-help">{question.help}</p>}
            <div className="question-choices" role="group" aria-label={question.text || question.key}>
              {question.choices.map((choice, i) => (
                <button
                  key={choice.value}
                  className={`choice compact ${mine === choice.value ? "on" : ""} ${proposed === choice.value ? "suggested" : ""}`}
                  aria-pressed={mine === choice.value}
                  title={choice.help || undefined}
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
            {question.choices.some((choice) => choice.help) && (
              <p className="question-help small">
                {question.choices
                  .filter((choice) => choice.help)
                  .map((choice) => `“${choice.text || choice.value}”：${choice.help}`)
                  .join(" ")}
              </p>
            )}
            {proposed != null && (
              <p className="question-suggestion">
                <span className="suggested-mark">模型的判断</span>{" "}
                {question.choices.find((c) => c.value === proposed)?.text || proposed}
                {reason && <span className="muted">。理由（英文原文）：{reason}</span>}
              </p>
            )}
          </fieldset>
        );
      })}
    </div>
  );
}

function SuggestionCard({
  suggestion,
  title,
  spec,
  onReveal,
  onHide,
  onAccept,
  onEdit,
  onReject,
  rejected,
  saving,
}) {
  if (!suggestion)
    return (
      <section className="rail-section suggestion hidden-suggestion">
        <div className="suggestion-head">
          <h3>{title}</h3>
        </div>
        <p className="muted small">默认不显示，免得影响你的判断。先自己选，再对照。</p>
        <button className="button" onClick={onReveal}>
          查看{title} <Keycap>V</Keycap>
        </button>
      </section>
    );
  return (
    <section className="rail-section suggestion">
      <div className="suggestion-head">
        <h3>{title}</h3>
      </div>
      {suggestion.source && (
        <p className="muted small suggestion-from">
          来源：<span className="suggestion-source">{suggestion.source}</span>
        </p>
      )}
      <p className="suggestion-answer">
        {suggestion.answers && spec.questions
          ? `回答了 ${spec.questions.length} 个问题中的 ${Object.keys(suggestion.answers).length} 个`
          : answerSummary(suggestion, spec)}
      </p>
      {suggestion.answers && spec.questions && (
        <p className="muted small">每题的建议答案标在题目下面。</p>
      )}
      {suggestion.regions?.length > 0 && (
        <p className="muted small">建议的框在图上用虚线画出。</p>
      )}
      {suggestion.rationale && <p className="rationale">{suggestion.rationale}</p>}
      {suggestion.note && <p className="muted small">{suggestion.note}</p>}
      <div className="suggestion-actions">
        <button className="button accept" onClick={onAccept} disabled={saving}>
          接受 <Keycap>A</Keycap>
        </button>
        <button className="button" onClick={onEdit}>
          编辑 <Keycap>E</Keycap>
        </button>
        <button
          className={`button ${rejected ? "reject on" : "reject"}`}
          aria-pressed={rejected}
          onClick={onReject}
        >
          驳回 <Keycap>R</Keycap>
        </button>
        {onHide && (
          <button className="text-button" onClick={onHide}>
            隐藏 <Keycap>V</Keycap>
          </button>
        )}
      </div>
    </section>
  );
}

function KindPicker({ kinds, activeKind, onPick }) {
  return (
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
          onClick={() => onPick(kind.value)}
        >
          <span className="kind-swatch" />
          {kind.text || kind.value}
          {i < 9 && <Keycap>⇧{i + 1}</Keycap>}
        </button>
      ))}
      {kinds.length === 1 && kinds[0].help && (
        <span className="muted small kind-help">{kinds[0].help}</span>
      )}
    </div>
  );
}

function RegionList({ regions, kinds, images, selected, onSelect, onDelete }) {
  if (!regions.length)
    return <p className="muted small region-empty">还没有画框。</p>;
  const kindOf = Object.fromEntries(kinds.map((kind) => [kind.value, kind]));
  const imageText = Object.fromEntries(images.map((image) => [image.role, image.text || image.role]));
  return (
    <ol className="region-list">
      {regions.map((region, i) => (
        <li key={i} className={selected === i ? "selected" : ""}>
          <button className="region-pick" onClick={() => onSelect(i)}>
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
            aria-label={`删除第 ${i + 1} 个框`}
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
  if (!mine) return <p className="muted small saved-line">你还没有标这一条。</p>;
  return (
    <p className="small saved-line">
      {dirty ? "有未保存的修改。" : ""}
      {time(mine.at)} 保存：{mine.skipped ? "已跳过" : answerSummary(mine, spec) || "没有答案"}
      {mine.verdict && (
        <>
          {" "}
          <Badge value={mine.verdict} />
        </>
      )}
      {mine.queue_changed && (
        <span className="warn-text"> 这是针对这个队列的早先版本保存的。</span>
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
          <h3>其他标注者</h3>
          <ul>
            {state.others.map((label) => (
              <li key={label.annotator}>
                <strong>{label.annotator}</strong>{" "}
                {label.skipped ? "已跳过" : answerSummary(label, spec)}
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
          <summary>这一条保存过的全部 {state.history} 个标注</summary>
          <RemoteState state={history}>
            {(data) =>
              data && (
                <ol className="history">
                  {data.labels.map((label, i) => (
                    <li key={i}>
                      {time(label.at)} · {label.annotator} ·{" "}
                      {label.skipped ? "已跳过" : answerSummary(label, spec) || "没有答案"}
                      {label.verdict && ` · ${STATUS_TEXT[label.verdict] || label.verdict}`}
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
    <div className="shortcut-sheet" role="dialog" aria-label="键盘快捷键">
      <div className="section-heading">
        <h3>键盘快捷键</h3>
        <button className="text-button" onClick={onClose}>
          关闭 <Keycap>Esc</Keycap>
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
