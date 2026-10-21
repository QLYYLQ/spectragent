import assert from "node:assert/strict";
import test from "node:test";
import {
  confirmPress,
  discardsWork,
  draftStorageKey,
  refreshDraft,
  staleDraftKeys,
  answerSummary,
  askedQuestions,
  nextOpenQuestion,
  toggleAnswer,
  draftFromLabel,
  draftFromSuggestion,
  draftProblems,
  draftsEqual,
  emptyDraft,
  itemStatus,
  kindColor,
  labelRequest,
  matchesFilter,
  openingIndex,
  previewVerdict,
  stepIndex,
  toggleChoice,
  toggleReject,
} from "./labels.js";

const single = {
  choices: [
    { value: "yes", text: "Reasonable" },
    { value: "no", text: "Unreasonable" },
  ],
  multi: false,
  regions: null,
  note: true,
};
const boxes = {
  choices: [{ value: "read", text: "Read a value" }],
  multi: true,
  regions: { kinds: [{ value: "evidence", text: "Evidence" }] },
  note: true,
};
const region = { image: "before", kind: "evidence", box: [1, 2, 30, 20] };

test("the verdict preview follows the store's rules", () => {
  const suggestion = { choice: "yes", source: "model" };
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "yes" }, suggestion), "accepted");
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "no" }, suggestion), "corrected");
  assert.equal(previewVerdict(toggleReject(emptyDraft()), suggestion), "rejected");
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "no" }, suggestion, { skipped: true }), null);
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "no" }, null), null);
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "no" }, { rationale: "only text" }), null);
});

test("only the fields a suggestion sets are compared, notes never", () => {
  const suggestion = { choices: ["read"], regions: [region] };
  const draft = { ...draftFromSuggestion(suggestion), note: "looked twice" };
  assert.equal(previewVerdict(draft, suggestion), "accepted");
  assert.equal(previewVerdict({ ...draft, regions: [region, region] }, suggestion), "corrected");
  assert.equal(previewVerdict({ ...emptyDraft(), choice: "yes", regions: [region] }, { choice: "yes" }), "accepted");
});

test("copying a suggestion never shares its boxes", () => {
  const suggestion = { regions: [region], note: "model note" };
  const draft = draftFromSuggestion(suggestion);
  draft.regions[0].box[0] = 99;
  assert.equal(region.box[0], 1);
  assert.equal(draft.note, "");
});

test("a saved label becomes the same draft", () => {
  const label = { choice: null, choices: ["read"], regions: [region], note: "x", verdict: "corrected", skipped: false };
  const draft = draftFromLabel(label);
  assert.ok(draftsEqual(draft, { ...emptyDraft(), choices: ["read"], regions: [region], note: "x " }));
  assert.equal(draftFromLabel({ ...label, verdict: "rejected", choices: null, regions: [] }).reject, true);
  assert.deepEqual(draftFromLabel({ ...label, skipped: true }).regions, []);
});

test("choices toggle and rejecting clears the answer", () => {
  let draft = toggleChoice(emptyDraft(), single, "yes");
  assert.equal(draft.choice, "yes");
  assert.equal(toggleChoice(draft, single, "yes").choice, null);
  draft = toggleChoice(toggleChoice(emptyDraft(), boxes, "read"), boxes, "read");
  assert.deepEqual(draft.choices, []);
  const rejected = toggleReject({ ...emptyDraft(), choice: "yes", regions: [region], note: "n" });
  assert.deepEqual([rejected.choice, rejected.regions, rejected.note, rejected.reject], [null, [], "n", true]);
  assert.equal(toggleChoice(rejected, single, "no").reject, false);
});

test("a single-choice queue needs a choice, a blank label is never saved by accident", () => {
  assert.equal(draftProblems(emptyDraft(), single).length, 2);
  assert.deepEqual(draftProblems({ ...emptyDraft(), choice: "yes" }, single), []);
  assert.deepEqual(draftProblems(toggleReject(emptyDraft()), single), []);
  assert.equal(draftProblems(emptyDraft(), boxes).length, 1);
  assert.deepEqual(draftProblems({ ...emptyDraft(), regions: [region] }, boxes), []);
});

test("the request carries one answer field and never an answer with skip or reject", () => {
  const draft = { ...emptyDraft(), choice: "yes", note: "ok" };
  assert.deepEqual(labelRequest("q", "i", draft, single, { version: "abc" }), {
    queue_id: "q",
    queue_sha256: "abc",
    item_id: "i",
    choice: "yes",
    choices: null,
    answers: null,
    regions: [],
    note: "ok",
    skipped: false,
    reject: false,
  });
  const skipped = labelRequest("q", "i", draft, single, { skipped: true });
  assert.equal(skipped.choice, null);
  assert.equal(skipped.skipped, true);
  const multi = labelRequest("q", "i", { ...emptyDraft(), choices: ["read"], regions: [region] }, boxes);
  assert.deepEqual([multi.choice, multi.choices, multi.regions.length], [null, ["read"], 1]);
  const rejected = labelRequest("q", "i", toggleReject(draft), single);
  assert.deepEqual([rejected.choice, rejected.reject], [null, true]);
});

const states = [
  { mine: null, others: [] },
  { mine: { verdict: "accepted", skipped: false }, others: [] },
  { mine: { verdict: null, skipped: true }, others: [{ verdict: "corrected" }] },
  { mine: null, others: [] },
  { mine: { verdict: "rejected", skipped: false }, others: [] },
];

test("item status and filters", () => {
  assert.deepEqual(states.map(itemStatus), ["unlabeled", "accepted", "skipped", "unlabeled", "rejected"]);
  assert.deepEqual(
    states.map((state) => matchesFilter(state, "disagree")),
    [false, false, true, false, true],
  );
  assert.deepEqual(
    states.map((state) => matchesFilter(state, "mine")),
    [false, true, false, false, true],
  );
  assert.equal(itemStatus({ mine: { verdict: null, skipped: false } }), "labeled");
});

test("next and previous follow the filter, wrap once, and skip the current item", () => {
  assert.equal(stepIndex(states, 0, 1, "unlabeled"), 3);
  assert.equal(stepIndex(states, 3, 1, "unlabeled"), 0);
  assert.equal(stepIndex(states, 0, -1, "all"), 4);
  assert.equal(stepIndex(states, 1, 1, "skipped"), 2);
  assert.equal(stepIndex(states, 2, 1, "skipped"), -1);
  assert.equal(openingIndex(states, "unlabeled"), 0);
  assert.equal(openingIndex([states[1]], "unlabeled"), 0);
});

test("summaries name choices by their text", () => {
  assert.equal(answerSummary({ choice: "yes" }, single), "Reasonable");
  assert.equal(answerSummary({ choices: ["read"], regions: [region, region] }, boxes), "Read a value · 2 boxes");
  assert.equal(answerSummary({ choices: [], regions: [] }, boxes), "No choice · No boxes");
});

test("kind colors cycle through the six spectral lines", () => {
  assert.equal(kindColor(0), kindColor(6));
  assert.notEqual(kindColor(0), kindColor(1));
});

const yesNo = [
  { value: "yes", text: "Yes" },
  { value: "no", text: "No" },
  { value: "uncertain", text: "Cannot tell" },
];
const questions = {
  choices: [],
  multi: false,
  questions: [
    { key: "same_intent", text: "Same goal?", choices: yesNo, applies_to: null },
    { key: "reasonable", text: "Reasonable?", choices: yesNo, applies_to: null },
    { key: "executed", text: "Executed?", choices: yesNo, applies_to: ["b"] },
  ],
  regions: null,
  note: true,
};

test("an item asks its own questions, else those that apply to it", () => {
  const keys = (item) => askedQuestions(questions, item).map((q) => q.key);
  assert.deepEqual(keys({ item_id: "a" }), ["same_intent", "reasonable"]);
  assert.deepEqual(keys({ item_id: "b" }), ["same_intent", "reasonable", "executed"]);
  assert.deepEqual(keys({ item_id: "b", questions: ["executed"] }), ["executed"]);
  assert.deepEqual(askedQuestions(single, { item_id: "a" }), []);
});

test("answers toggle per question and every asked question must be answered", () => {
  const asked = askedQuestions(questions, { item_id: "a" });
  let draft = toggleAnswer(emptyDraft(), "same_intent", "yes");
  assert.match(draftProblems(draft, questions, asked)[0], /1 of 2 answered/);
  draft = toggleAnswer(draft, "reasonable", "no");
  assert.deepEqual(draftProblems(draft, questions, asked), []);
  assert.deepEqual(toggleAnswer(draft, "reasonable", "no").answers, { same_intent: "yes" });
  assert.deepEqual(toggleReject(draft).answers, {});
});

test("question verdicts compare every suggested answer", () => {
  const suggestion = { answers: { same_intent: "yes", reasonable: "no" } };
  const draft = { ...emptyDraft(), answers: { reasonable: "no", same_intent: "yes" } };
  assert.equal(previewVerdict(draft, suggestion), "accepted");
  assert.equal(previewVerdict({ ...draft, answers: { ...draft.answers, reasonable: "uncertain" } }, suggestion), "corrected");
  assert.equal(previewVerdict(draftFromSuggestion(suggestion), suggestion), "accepted");
  assert.ok(draftsEqual(draft, draftFromSuggestion(suggestion)));
});

test("a question request sends answers and never choices", () => {
  const draft = { ...emptyDraft(), answers: { same_intent: "yes" } };
  const body = labelRequest("q", "i", draft, questions);
  assert.deepEqual([body.choice, body.choices, body.answers], [null, null, { same_intent: "yes" }]);
  assert.equal(labelRequest("q", "i", draft, questions, { skipped: true }).answers, null);
  assert.equal(labelRequest("q", "i", { ...emptyDraft(), choice: "yes" }, single).answers, null);
});

test("the highlight moves to the next unanswered question", () => {
  const asked = questions.questions;
  assert.equal(nextOpenQuestion(asked, { same_intent: "yes" }, 0), 1);
  assert.equal(nextOpenQuestion(asked, { same_intent: "yes", reasonable: "no" }, 1), 2);
  assert.equal(nextOpenQuestion(asked, { reasonable: "no", executed: "no" }, 2), 0);
  assert.equal(nextOpenQuestion(asked, { same_intent: "y", reasonable: "n", executed: "n" }, 1), 1);
});

test("question answers summarize in question order", () => {
  assert.equal(answerSummary({ answers: { reasonable: "no", same_intent: "yes" } }, questions), "Yes / No");
});

test("an action that would replace unsaved work needs a second press", () => {
  const saved = emptyDraft();
  const mine = { ...emptyDraft(), choice: "no", note: "why" };
  const suggestion = draftFromSuggestion({ choice: "yes" });
  assert.equal(discardsWork(mine, saved, suggestion), true);
  assert.equal(discardsWork(saved, saved, suggestion), false, "a clean draft loses nothing");
  assert.equal(discardsWork({ ...emptyDraft(), choice: "yes", note: "n" }, saved, suggestion), false);
  assert.equal(discardsWork(mine, saved, emptyDraft()), true, "a skip drops the answer");
  assert.equal(discardsWork({ ...emptyDraft(), note: "only a note" }, saved, emptyDraft()), false);
});

test("a second press of the same action inside the window confirms it", () => {
  let state = confirmPress(null, "skip", 1000);
  assert.deepEqual(state, { run: false, pending: { action: "skip", at: 1000 } });
  assert.equal(confirmPress(state.pending, "skip", 2400).run, true);
  assert.equal(confirmPress(state.pending, "skip", 2600).run, false, "too late");
  assert.equal(confirmPress(state.pending, "accept", 1100).run, false, "another action");
});

test("drafts are stored per queue version and other versions are found", () => {
  const keys = [
    draftStorageKey("q", "v1"),
    draftStorageKey("q", "v2"),
    draftStorageKey("other", "v1"),
    "unrelated",
  ];
  assert.deepEqual(staleDraftKeys(keys, "q", "v2"), ["review-drafts:q:v1"]);
  assert.deepEqual(staleDraftKeys(keys, "q-longer", "v2"), []);
});

test("a refreshed saved label replaces a clean draft but never unsaved edits", () => {
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const before = { note: "old" };
  const after = { note: "new" };
  assert.deepEqual(refreshDraft(before, before, before, same), { draft: before, conflict: false });
  assert.deepEqual(refreshDraft({ note: "typing" }, before, before, same), { draft: { note: "typing" }, conflict: false });
  assert.deepEqual(refreshDraft(before, before, after, same), { draft: after, conflict: false });
  assert.deepEqual(refreshDraft({ note: "typing" }, before, after, same), { draft: { note: "typing" }, conflict: true });
  assert.deepEqual(refreshDraft(after, before, after, same), { draft: after, conflict: false }, "what was just saved");
});
