/**
 * Label state for one review item: the person's draft, how it compares
 * with the suggestion, and where each item stands. The store recomputes
 * every verdict; the page only previews it with the same rules.
 */
import { sameRegions } from "./boxes.js";

export function emptyDraft() {
  return { choice: null, choices: [], answers: {}, regions: [], note: "", reject: false };
}

/** The questions an item asks (its own list wins, else each question's applies_to). */
export function askedQuestions(spec, item) {
  if (!spec.questions) return [];
  if (item.questions) return spec.questions.filter((q) => item.questions.includes(q.key));
  return spec.questions.filter(
    (q) => q.applies_to == null || q.applies_to.includes(item.item_id),
  );
}

/** The draft that reproduces a saved label (a skipped one keeps only its note). */
export function draftFromLabel(label) {
  if (!label) return emptyDraft();
  return {
    choice: label.skipped ? null : (label.choice ?? null),
    choices: label.skipped ? [] : [...(label.choices || [])],
    answers: label.skipped ? {} : { ...(label.answers || {}) },
    regions: label.skipped ? [] : (label.regions || []).map(copyRegion),
    note: label.note || "",
    reject: label.verdict === "rejected",
  };
}

export function suggestionHasAnswer(suggestion) {
  return Boolean(
    suggestion &&
      (suggestion.choice != null ||
        suggestion.choices != null ||
        suggestion.answers != null ||
        suggestion.regions != null),
  );
}

/** The suggestion as an editable draft: its answer fields, never its note. */
export function draftFromSuggestion(suggestion) {
  return {
    ...emptyDraft(),
    choice: suggestion?.choice ?? null,
    choices: [...(suggestion?.choices || [])],
    answers: { ...(suggestion?.answers || {}) },
    regions: (suggestion?.regions || []).map(copyRegion),
  };
}

function copyRegion(region) {
  return { image: region.image, kind: region.kind, box: [...region.box] };
}

const sortedChoices = (draft) => [...(draft.choices || [])].sort();
const sortedAnswers = (draft) =>
  JSON.stringify(Object.entries(draft.answers || {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

export function draftsEqual(a, b) {
  return (
    (a.choice ?? null) === (b.choice ?? null) &&
    JSON.stringify(sortedChoices(a)) === JSON.stringify(sortedChoices(b)) &&
    sortedAnswers(a) === sortedAnswers(b) &&
    sameRegions(a.regions, b.regions) &&
    (a.note || "").trim() === (b.note || "").trim() &&
    Boolean(a.reject) === Boolean(b.reject)
  );
}

export function isBlank(draft) {
  return (
    draft.choice == null &&
    !draft.choices?.length &&
    !Object.keys(draft.answers || {}).length &&
    !draft.regions?.length &&
    !draft.note?.trim() &&
    !draft.reject
  );
}

/** Same rules as spectragent.annotation.compute_verdict. */
export function previewVerdict(draft, suggestion, { skipped = false } = {}) {
  if (!suggestionHasAnswer(suggestion) || skipped) return null;
  if (draft.reject) return "rejected";
  let same = true;
  if (suggestion.choice != null) same &&= draft.choice === suggestion.choice;
  if (suggestion.choices != null)
    same &&=
      JSON.stringify([...new Set(draft.choices || [])].sort()) ===
      JSON.stringify([...new Set(suggestion.choices)].sort());
  if (suggestion.answers != null)
    same &&= Object.entries(suggestion.answers).every(
      ([key, value]) => (draft.answers || {})[key] === value,
    );
  if (suggestion.regions != null)
    same &&= sameRegions(draft.regions, suggestion.regions);
  return same ? "accepted" : "corrected";
}

/** The choices the draft picks: its one choice, its several, or each question's answer. */
export function pickedChoices(draft, spec, asked = []) {
  if (spec.questions)
    return asked
      .map((question) =>
        question.choices.find((choice) => choice.value === (draft.answers || {})[question.key]),
      )
      .filter(Boolean);
  if (spec.multi) return spec.choices.filter((choice) => (draft.choices || []).includes(choice.value));
  return spec.choices.filter((choice) => choice.value === draft.choice);
}

const named = (options) => options.map((option) => `“${option.text || option.value}”`).join("、");

/** Why the boxes break a picked choice's box rule; the same words as the store's. */
export function regionRuleProblems(picked, regions = []) {
  const forbidding = picked.filter((option) => option.regions === "forbidden");
  const requiring = picked.filter((option) => option.regions === "required");
  if (forbidding.length && requiring.length)
    return [`${named(requiring)}要画框，${named(forbidding)}不能画框，不能同时选`];
  if (forbidding.length && regions.length)
    return [`选了${named(forbidding)}就不能画框：请删掉框，或者换一个选项`];
  if (requiring.length && !regions.length) return [`选了${named(requiring)}就至少要画一个框`];
  return [];
}

/** What a choice's box rule asks, as a short tag; "" when boxes are optional. */
export function regionRuleText(rule) {
  return rule === "required" ? "必须画框" : rule === "forbidden" ? "不能画框" : "";
}

/** Why the draft cannot be saved as an answer yet; [] when it can. */
export function draftProblems(draft, spec, asked = []) {
  if (draft.reject) return [];
  const problems = [];
  if (spec.questions) {
    const open = asked.filter((question) => !(draft.answers || {})[question.key]);
    if (open.length)
      problems.push(
        `请回答每个问题（已答 ${asked.length - open.length}/${asked.length}），或者驳回建议、跳过。`,
      );
  } else if (!spec.multi && spec.choices.length && draft.choice == null)
    problems.push("请选一个答案，或者驳回建议、跳过。");
  problems.push(...regionRuleProblems(pickedChoices(draft, spec, asked), draft.regions || []));
  if (spec.regions?.image_roles && (draft.regions || []).some(region => !spec.regions.image_roles.includes(region.image)))
    problems.push("有标注框画在辅助图片上，请删除后在指定的标注图上重新画框。");
  if (isBlank(draft) && (spec.choices.length || spec.regions || spec.questions))
    problems.push("还没有可以保存的内容：请选择答案、画框，或者跳过。");
  return problems;
}

export function toggleAnswer(draft, key, value) {
  const answers = { ...(draft.answers || {}) };
  if (answers[key] === value) delete answers[key];
  else answers[key] = value;
  return { ...draft, answers, reject: false };
}

/** The question to highlight after answering `index`: the next unanswered one, else stay. */
export function nextOpenQuestion(asked, answers, index) {
  for (let offset = 1; offset <= asked.length; offset += 1) {
    const candidate = (index + offset) % asked.length;
    if (!(answers || {})[asked[candidate].key]) return candidate;
  }
  return index;
}

export function toggleChoice(draft, spec, value) {
  if (spec.multi) {
    const choices = draft.choices.includes(value)
      ? draft.choices.filter((choice) => choice !== value)
      : [...draft.choices, value];
    return { ...draft, choices, reject: false };
  }
  return { ...draft, choice: draft.choice === value ? null : value, reject: false };
}

/** Rejecting clears the answer: a rejected suggestion carries no replacement. */
export function toggleReject(draft) {
  return draft.reject
    ? { ...draft, reject: false }
    : { ...draft, choice: null, choices: [], answers: {}, regions: [], reject: true };
}

/** The body of a label save; `version` is the sha256 of the queue the person loaded. */
export function labelRequest(queueId, itemId, draft, spec, { skipped = false, version } = {}) {
  const answered = !skipped && !draft.reject;
  return {
    queue_id: queueId,
    queue_sha256: version,
    item_id: itemId,
    choice: answered && !spec.multi && !spec.questions ? draft.choice : null,
    choices: answered && spec.multi && !spec.questions ? draft.choices : null,
    answers: answered && spec.questions ? draft.answers : null,
    regions: answered ? draft.regions : [],
    note: draft.note || "",
    skipped,
    reject: !skipped && draft.reject,
  };
}

/** Where an item stands for this annotator. */
export function itemStatus(state) {
  const mine = state.mine;
  if (!mine) return "unlabeled";
  if (mine.skipped) return "skipped";
  return mine.verdict || "labeled";
}

export const FILTERS = [
  { value: "unlabeled", text: "我还没标的" },
  { value: "all", text: "全部条目" },
  { value: "mine", text: "我标过的" },
  { value: "skipped", text: "我跳过的" },
  { value: "disagree", text: "和建议不一致的" },
];

export function matchesFilter(state, filter) {
  const status = itemStatus(state);
  if (filter === "unlabeled") return status === "unlabeled";
  if (filter === "mine") return status !== "unlabeled" && status !== "skipped";
  if (filter === "skipped") return status === "skipped";
  if (filter === "disagree")
    return [state.mine, ...(state.others || [])].some(
      (label) => label && ["corrected", "rejected"].includes(label.verdict),
    );
  return true;
}

/**
 * The next item in queue order that passes the filter, scanning from (not
 * including) `index`; wraps once. -1 when no other item passes.
 */
export function stepIndex(states, index, direction, filter) {
  const count = states.length;
  for (let offset = 1; offset < count; offset += 1) {
    const candidate = (index + direction * offset + count * count) % count;
    if (matchesFilter(states[candidate], filter)) return candidate;
  }
  return -1;
}

/** The item a queue opens at: the first one this filter still asks about. */
export function openingIndex(states, filter) {
  const index = states.findIndex((state) => matchesFilter(state, filter));
  return index < 0 ? 0 : index;
}

/** The brand's six spectral lines, in order; region kinds take them in turn. */
export const KIND_COLORS = [
  "#007f96",
  "#6753b8",
  "#168052",
  "#c26400",
  "#d14343",
  "#9a7800",
];

export function kindColor(index) {
  return KIND_COLORS[((index % KIND_COLORS.length) + KIND_COLORS.length) % KIND_COLORS.length];
}

export function choiceText(spec, value) {
  return spec.choices.find((choice) => choice.value === value)?.text || value;
}

export function kindText(spec, value) {
  return spec.regions?.kinds.find((kind) => kind.value === value)?.text || value;
}

/** One line saying what a label or suggestion answers. */
export function answerSummary(answer, spec) {
  const parts = [];
  if (answer.choice != null) parts.push(choiceText(spec, answer.choice));
  if (answer.answers != null && spec.questions)
    parts.push(
      spec.questions
        .filter((question) => question.key in answer.answers)
        .map((question) => choiceText(question, answer.answers[question.key]))
        .join(" / ") || "没有回答",
    );
  if (answer.choices != null)
    parts.push(
      answer.choices.length
        ? answer.choices.map((value) => choiceText(spec, value)).join(", ")
        : "没有选择",
    );
  if (answer.regions != null && answer.regions.length) parts.push(`${answer.regions.length} 个框`);
  else if (answer.regions != null && spec.regions) parts.push("没有框");
  return parts.join(" · ");
}

/** The answer a draft gives, without its note (which every action keeps). */
function answerOf(draft) {
  return { ...draft, note: "" };
}

/**
 * Whether replacing `draft` with `replacement` (an accept, a skip, an edit or a
 * reject) would throw away unsaved work: the draft differs from what is saved
 * and its answer differs from the replacement's.
 */
export function discardsWork(draft, saved, replacement) {
  const answer = answerOf(draft);
  return (
    !draftsEqual(draft, saved) &&
    !isBlank({ ...answer, reject: false }) &&
    !draftsEqual(answer, answerOf(replacement))
  );
}

/** Two presses of the same action within `window` ms confirm it. */
export const CONFIRM_MS = 1500;

export function confirmPress(pending, action, now, window = CONFIRM_MS) {
  if (pending && pending.action === action && now - pending.at <= window)
    return { run: true, pending: null };
  return { run: false, pending: { action, at: now } };
}

/** Where unsaved drafts of one queue version are kept for the tab. */
export function draftStorageKey(queueId, version) {
  return `review-drafts:${queueId}:${version}`;
}

/** Stored draft keys of the same queue made for another version of it. */
export function staleDraftKeys(keys, queueId, version) {
  const prefix = `review-drafts:${queueId}:`;
  return keys.filter((key) => key.startsWith(prefix) && key !== draftStorageKey(queueId, version));
}

/**
 * The draft to show after the saved label is read again. A clean draft
 * follows the saved label; edits the person has not saved are kept, and
 * `conflict` says the saved label changed underneath them.
 */
export function refreshDraft(draft, before, after, equal) {
  if (equal(before, after)) return { draft, conflict: false };
  if (equal(draft, before) || equal(draft, after)) return { draft: after, conflict: false };
  return { draft, conflict: true };
}
