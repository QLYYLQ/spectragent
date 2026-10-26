# Lookahead collection rounds (dated evidence)

How to run a round is in `lookahead/README.md` ("A collection round") with the shipped
`configs/lookahead/` files. This file keeps what the rounds measured, so a session without
the earlier conversations knows why the settings are what they are. Every raw recording and
fork of these rounds was deleted on 2026-10-25; only text reports remain locally
(`tmp/lookahead_*/REPORT.md`), so none of them can be replayed or labelled again.

## What the data is for

The lookahead-observation model predicts where on the next screen the information for the
next decision will appear. A round records teacher runs live, then branches them offline:
the same state gets several new teacher decisions (same task, or a variant task on the same
page), which are judged by rules and a model and reviewed by people in the viewer. By tokens a
fork's decision costs about as much as a recorded one (about 14k input tokens each, both
carry the whole history); forks are worth it because they give several decisions on an
identical state, can change the task or the model, and do not depend on the live site.

## Rounds

Same 15 LexBench L1 tasks each time (5 Chinese sites x 3), teacher ui_tars with
deepseek-v4.1-flash-1017 (reasoning high). "Judgeable" means the fork's checkpoint restored,
so its decision can be judged.

| Round | What changed before it | Judgeable forks |
|---|---|---|
| 2026-10-21 | first round; Ctrip recordings were anti-bot block pages and Ctrip was dropped | 51 of 318 restored |
| 2026-10-22 | teacher in `screenshot_pixels` (the model mixed the 0-1000 grid with pixels; 0 of 31 mixed afterwards); typing refuses without a focused field | 55/274 (20%) |
| 2026-10-23 | recording, viewer and replay fixes | 74/304 (24%); Bilibili fell to 2/52 because the shared clock changed signed request parameters |
| 2026-10-23, freeze | `background_tabs: freeze`; shared-clock site rules | 137/334 (41%) |
| 2026-10-24 | media-area rules (video pages), rules reading nested fields, page-URL parameters (`vt`), full request-body keys in the replay log | 229/330 (69%) |
| 2026-10-24, rules rerun | same recordings, more site rules (iQIYI play page, IGN media areas, East Money, Bilibili space list) | 246/330 (75%) |

2026-10-24 per site (judgeable/forks): Tonghuashun 58/68, iQIYI 37/72, Bilibili 52/56, IGN
42/64, Tencent Video 40/70 (0 the round before; the media-area rules for its play pages made
the difference). Forks: 210 same task, 60 variant task, 60 variant task without source
thoughts. Tokens for the round: 11.68M (recording 5.8M, same-task forks 2.95M, variant forks
1.97M, variant generation 0.06M, judging 0.89M).

Other 2026-10-24 measurements:

- Screen stability, 409 observations: 47% changed within about 1 s of capture; of 100 with
  playing media, 99 changed on the media (hence the stability review's media-area option).
- Settle before capture: 63% settled, 34% capped with only the DOM still changing (players,
  carousels, tickers), 3% capped before the main document loaded. The owner ruled on
  2026-10-25 that only the last counts as not loaded (`enhance.md`).
- Model judge: same-task forks 119/137 reasonable, 64 executed as intended; variant forks
  83/92 reasonable, 60 as intended. Rules and model agreed on fork intent 58% of the time
  where both decided (39 of the disagreements: rules "different", model "same intent").

## Failures left after 2026-10-24, and what followed

- Forks act 3 to 35 s ahead of the recording, so page timers differ: `pace:
  recorded_timeline` was added on 2026-10-25, not yet compared with `recorded_llm_latency`.
- Recordings that caught pages still loading on a slow live network: unloaded checkpoints and
  samples are now left out, the teacher is told plainly, and the export can drop waits
  (2026-10-25); the next round records on a fast network.
- 23 forks lost because the teacher left the site (ads, another video site's search). Open:
  a report-only health flag, or stopping it while recording.
- 15 iQIYI restorations stopped at an earlier page of the prefix (a navigation that did not
  happen offline). Open.
- The network archive's 5 s flush budget was too short on a heavy page with 8 forks in
  parallel (IGN task 155: 3 incomplete fork records). Open.
- The teacher pressed browser shortcuts that never reached the browser: `send_keys` now runs
  the browser's own Back/Forward/Reload/Close-tab/tab-switch shortcuts (2026-10-24).

The 2026-10-24/25 changes altered the teacher's system prompt, so older recordings cannot be
branched with `history_source: source`; the next round records on current `main`.
