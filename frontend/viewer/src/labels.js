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

/** Why the draft cannot be saved as an answer yet; [] when it can. */
export function draftProblems(draft, spec, asked = []) {
  if (draft.reject) return [];
  const problems = [];
  if (spec.questions) {
    const open = asked.filter((question) => !(draft.answers || {})[question.key]);
    if (open.length)
      problems.push(
        `Answer every question (${asked.length - open.length} of ${asked.length} answered), reject the suggestion, or skip.`,
      );
  } else if (!spec.multi && spec.choices.length && draft.choice == null)
    problems.push("Choose one answer, reject the suggestion, or skip.");
  if (isBlank(draft) && (spec.choices.length || spec.regions || spec.questions))
    problems.push("Nothing to save yet. Choose, draw a box, or skip.");
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

export function labelRequest(queueId, itemId, draft, spec, { skipped = false } = {}) {
  const answered = !skipped && !draft.reject;
  return {
    queue_id: queueId,
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
  { value: "unlabeled", text: "Not labeled by me" },
  { value: "all", text: "All items" },
  { value: "mine", text: "Labeled by me" },
  { value: "skipped", text: "Skipped by me" },
  { value: "disagree", text: "Disagrees with the suggestion" },
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
        .join(" / ") || "No answers",
    );
  if (answer.choices != null)
    parts.push(
      answer.choices.length
        ? answer.choices.map((value) => choiceText(spec, value)).join(", ")
        : "No choice",
    );
  if (answer.regions != null && answer.regions.length)
    parts.push(
      `${answer.regions.length} ${answer.regions.length === 1 ? "box" : "boxes"}`,
    );
  else if (answer.regions != null && spec.regions) parts.push("No boxes");
  return parts.join(" · ");
}
