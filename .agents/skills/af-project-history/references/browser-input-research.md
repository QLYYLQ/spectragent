# Browser input and anti-bot research

These are measured snapshots from the September 2026 input-humanization round. The
current design contract is `docs/architecture_design.md` §4.13; implementation details
must be checked against current code.

## Anti-bot result: cursor realism was not the dominant variable

- reCAPTCHA v3 returned 0.9 for every arm, including instant teleport and a deliberately
  weak control. That key and traffic level were not discriminating.
- A real v2 key escalated every tested episode to an image grid, including the most
  human-like arm. Environment and reputation dominated cursor kinematics.
- A warm logged-in Xiaohongshu profile completed a single episode cleanly. This is a
  useful smoke result, not broad evidence of bypass robustness.

The accepted conclusion was narrow: a humanized input layer may be necessary for
realism, but it is nowhere near sufficient for anti-bot success.

## Trajectory comparison

Against 829 human click paths and nine measured features, the round observed:

- `learned`: 7/9 features in range;
- fitted WindMouse: 3/9;
- default WindMouse: 2/9;
- instant: 1/9.

WindMouse's main shape error was late acceleration: peak speed near 85–94% of the
path versus about 35% for the human sample. Fitting Fitts-law duration did not repair
that geometry. Two shared AF tells were unrealistically short click holds and aiming
at the exact element center.

## Learned strategy decision

The selected v2 export was an autoregressive MDN-RNN with a stop head, not the earlier
GAN. The user chose the public strategy name `learned`; the model sub-kind belongs in
the export's `meta.json`, not in YAML. No alias for the old `gan` key was wanted.

Model metadata is authoritative for sampling rate and normalization. A contradictory
runtime configuration should fail rather than silently reinterpret weights.

## Input-layer invariants that motivated the implementation

- Input controllers are direct calls, not nested agent-cdp watchdog events; awaiting a
  sub-event inside the single-consumer watchdog loop can deadlock.
- A trajectory's timing is an absolute schedule. When transport falls behind, drop
  intermediate points rather than stretch wall time; preserve the final endpoint and
  exact wheel delta.
- Repeated waypoints represent dwell and consume time without generating fake motion.
- The `instant` strategy remains the non-humanized baseline; the strategy itself is the
  switch, so there must not be a second `humanize_mouse` boolean.

The old memory said typing had no per-keystroke timing. That was true during the round
but is false now; current `HumanKeyboard` and typing-rhythm tests are authoritative.

Historical evidence lives in `HANDOFF_260916_findings.md`; external harness reports
are local research artifacts and may not exist on another checkout.
