# Clean DOM comparison and decision-model trial

Measured on 2026-10-19. This is an offline experiment, not a runtime judge or a decision
model registrar. Architecture and current code remain authoritative.
For the current skill integration, use [the Jev workflow](jev-workflow.md); the results
below explain its evidence-selection and escalation choices.

## Reusing the capture

The Bilibili/Xiaohongshu recordings used a vision-only Agent but selected
`browser.dom_raw`. Their archived captures include the raw document, layout/paint
snapshot, accessibility tree, document identity, viewport and DPR needed by the
BrowserUse cleaner. No new website request or recording was needed.

Use the source configuration's composed cleaner and serializer, not a second default
instance with potentially different knobs:

1. Read the committed archive entry through `RunTrace`/`ArchiveReader` and decode it with
   `read_dom_document`.
2. Reconstruct `TargetCapture` with `capture_from_document(document["capture"])`.
3. Call `cleaner.clean(capture, viewport, previous_selector_map=..., device_pixel_ratio=...)`;
   previous maps belong to the corresponding tab within that source run.
4. Apply `DOMBudgetPass(capability.dom_budget).apply(state, serializer.serialize)`.
5. Retain the tree/geometry and link destinations alongside `state.serialized_dom`.
   Remove transient serialized backend handles for cross-run textual comparison, including
   handles after shadow-root and scroll markers. Do not erase business IDs or query fields.

Across 39 archived observations, cleaning took about 28 ms median; read/decode + clean +
serialization took about 47 ms median. Median raw JSON was about 1.58 million characters,
versus 5,905 serialized characters. These are one-machine measurements, not guarantees.
No DOMBudget cut occurred; the configured text serializer still has its own per-node
text limit, and cleaning intentionally removes content outside its observation contract.

The restored Xiaohongshu search checkpoint had identical normalized text and link lists
to its source. Line comparison took about 0.5 ms. The missing-results baseline differed.
Text matching alone does not establish equal pixel appearance or coordinate targets.

## Jev route and evaluation

The user selected `jev-1.14.0` on the same DMX gateway as Luna. The documented and
verified endpoint is `https://www.dmxapi.cn/typesafe/v1/systemone`, using `model`,
`state` and `questions`, not Chat Completions. The returned model identity was
`jev-1.14.0`. [DMX's protocol example](https://doc.dmxapi.cn/jev-1.13.0-typesafe-structured-judgment.html)
uses that version despite its older page title. Re-verify routing for later experiments.

Nine prelabelled cases used real DOM pairs plus identity, missing-evidence, modality and
counterfactual-task controls. Labels were excluded from requests. Eight initial requests
returned valid choices, all matching their labels; one Bilibili request exceeded the
context limit. Its supplemental link inventory contained very long advertising URLs.
Keeping the full clean text and omitting link details for that read-text task produced
a valid, correct follow-up. Preserve that as a changed-input follow-up, not an initial
9/9 result. This small set comes from two source tasks and is not a calibrated benchmark.

The model distinguished an empty result page from restored results. The same closed
history panel was acceptable for direct typing but material for clicking a history entry.
It answered insufficient evidence when asked to compare actual colors without pixels.
The direct-typing case had choice confidence 0.61 while a separate equivalence yes/no
question returned 0.26: do not automatically approve from that choice alone or combine
the two numbers as if they were one score. Preserve disagreement for visual/AI review.

Local reproducible inputs and outputs are under `tmp/jev_dom_review_20261019/`:
`clean.py`, `evaluate.py`, `cleaned.index.json`, `evaluation.plan.json`, raw request/response
files, and `delivery.json`. If absent, regenerate from available current-format traces.
No screenshot was sent in this trial; it does not establish current Jev image capability.
The older Jev memo remains dated research, not a reason to register it as a general chat
model or to change the execution loop.
