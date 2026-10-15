# Human–code-agent developer experience

This reference separates durable product intent, implemented tooling and explicitly
deferred ideas. Verify the dated implementation facts against current architecture/code.

## Why AF uses Conscribe deeply

User rationale, reaffirmed 2026-09-23:

- Human researchers usually choose experiment variants by editing YAML. They should get
  generated schemas, completion, documentation, and startup validation instead of
  memorizing a stringly typed configuration language.
- Substantial code changes are increasingly delegated to coding agents. Each extension
  axis therefore needs a local owner and an explicit entry point so an agent can add an
  Agent, Loop, Capability, Action, or strategy without editing unrelated central
  switches.
- `__wiring__` and registrar metaclasses concentrate complexity in the framework
  toolchain in exchange for small, bounded modification surfaces. Repository skills add
  the procedural map: where to edit, which invariants matter, and how to verify the
  result.

The intended collaboration loop is:

1. A human states research intent.
2. A coding agent changes one bounded extension surface.
3. Conscribe derives and validates the legal configuration space.
4. A human selects experiments through schema-backed YAML.
5. AF rejects incompatible composition before spending run time or model calls.

## Configuration explanation: implemented surface

The user approved the original config-explainer direction on 2026-09-23. Do not keep
that whole direction labelled unimplemented: the current command is
`spectragent config explain CONFIG --json`, implemented in `cli/app.py` and
`cli/configuration.py` and documented in architecture §4.17. It already exists in the
accepted `a6a36d0` code; the richer provenance below was added later.

The current command composes and prepares without launching an environment or model.
It reports component instance/implementation identities, resolved redacted config,
effective action/fragment/archive negotiation and descriptors, AOP intercepts,
fingerprints and generated-config freshness. It has human and JSON output. CLI tests
verify the no-launch and secret-redaction boundaries.

The 2026-10-16 framework expansion delivered the richer field/MRO/wiring provenance.
Published Conscribe 2.1.0 supplies public declaration evidence; AF joins it with explicit/
default input origins and actual Composer injection evidence. These are separate facts,
not a graph guessed from instance fields. Structured failures retain their stage and
public contract details. See architecture §4.17 and the current handoff indexed in
`unfinished-work.md` for evidence and limits.

Keep read-only preparation, trace-equivalent redaction, machine-readable output and
actionable errors as constraints. Templates below author ordinary executable YAML;
explain remains a redacted report.

## Commented YAML template assembly: implemented after the original deferral

On 2026-10-15 the user preferred explicit configuration even where Conscribe can supply
defaults, with BrowserUse/ReAct, token recording and no replay as their example. They
initially deferred template work during the loop-layering discussion. The later
authorized framework expansion delivered `spectragent config template`: selected
components, receptors and default actions expand with schema comments/constraints,
missing required values are named, and null/empty/subset semantics are preserved.
Selection or `--from CONFIG` produces ordinary YAML through the public catalog and
same Composer, without starting environment/model or opening task replay evidence.
Architecture §4.17 defines the CLI; it is no longer an active deferral.

## Implemented recording and execution decisions

The 2026-10-15 discussion was implemented and accepted at `a6a36d0`; its dated
handoff is `tmp/flywheel/followup/implementation/README.md`. The current framework
expansion is indexed in `unfinished-work.md`. The earlier
`review_changes/README.md` remains the requirements index, not an unfinished-work queue.

Preserve these user rulings in future changes:

- Instrumentation is optional. Without statistics, usage and model-call counts are
  `None`, not zero; provider-missing usage differs from measured zero and known subtotals.
- Lightweight/full describes **persisted content scope**, never an inferred requirement
  for an in-memory-only mode. The implemented `RecordingPlan` independently selects
  metrics, errors, trajectory and observations; archive remains explicitly configured.
- One recording component collects the call ledger, including retries, failed replies
  with usage and helper calls. Accounting belongs outside both ReAct and LoopBase.
- Recording/WAL failures cannot choose whether the Agent continues or replace execution
  errors, cancellation, veto or results. Buffer, retry and cleanup budgets are configurable;
  a recorder-controlled stop-versus-continue policy is not.
- The user's phrase "error time" referred to an actual execution error event, such as
  an action failing at step 5, not a new error-count/failure-duration metric.
- Runtime measures execution time; the task driver measures full task time. Step execution
  and its recording overhead keep their own identity. Agent owns prompt content/placement.

The old questions about the concrete recording field set, accounting ownership and
unconditional stop-on-hole behavior are closed. Discuss related new design questions
by meaningful part rather than making the user reconfirm every settled review item.

## Framework-wide public/private API review: completed after the original request

On 2026-10-15 the user requested a future file-by-file review of private module
variables, helper classes, instance/class attributes, properties, and methods. They
had then inspected only Agent classes and suspected the same excessive `_` use
elsewhere. BrowserUse's system-prompt text and its private prompt-building helpers are
examples to examine, not the limit of the task.

The later authorized framework expansion completed this review, including methods,
properties and helper classes. `unfinished-work.md` indexes baseline, integration and
cursor module coverage separately; the independent reviewer did not repeat every
baseline inspection. The earlier `a6a36d0` work alone had only audited touched contracts.

Keep judging each member's responsibility and intended consumers; define or change
the public Protocol/API where researchers, callers, or extension authors need access. Distinguish
public read-only state, configuration/extension points, and actual implementation
details. Do not reduce the task to mechanically deleting underscores or treating every
private member as wrong. Existing public snapshots must be checked for whether they
meet the intended use, rather than assumed sufficient. The user explicitly added
private methods to the scope. The completed audit is not an automatic request to repeat
it or a claim about future modules. The user reaffirmed semantic layer ownership and
strict private/public reasoning; do not turn this reminder into mechanical underscore
removal.
