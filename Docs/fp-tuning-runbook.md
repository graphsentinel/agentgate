# False-Positive Tuning Runbook (NFR-3)

How to take a rare-but-legitimate decision chain that the baseline flags and tune it
out **without disabling detection** — the "one we got wrong" beat in the talk.

## Symptom

A chain you know is legitimate shows up in OTel with
`gen_ai.agent.gate.action != log` and a `baseline_deviation` event whose
`score.value` is above your comfort line. In `block` mode the agent gets a 403 it
shouldn't.

## The three knobs

| Knob | Field | Effect | When |
|---|---|---|---|
| **Window** | `baseline.window` | more runs in the rolling baseline → rarer-but-real chains become "seen" | the chain is legitimate and recurring |
| **Threshold** | `detection.threshold` | higher raw-z bar before a call counts as drift | borderline numeric/risk drift |
| **dryRun source** | `baseline.sources: [..., dryRun]` | feed golden chains so the baseline learns them up front | known-good chains that are simply rare |

## Procedure

1. **Stay in shadow.** Set `action: log`. Nothing is blocked while you tune.
2. **Reproduce.** Run the legitimate chain; confirm the `baseline_deviation` event and
   note `gen_ai.agent.computed.anomaly.kind`:
   - `baseline_mismatch` / `scope_creep` → the tool/scope was genuinely never seen →
     add it via a `dryRun` golden chain or widen the data window.
   - `blocked_transition` → the order is legitimate but rare → fold a few real runs
     (raise `window`) so the n-gram learns the transition.
   - `arg_schema_novel` → a new but valid argument shape → fold an example so the
     schema hash is known.
   - numeric `risk_escalation` → raise `threshold` a notch.
3. **Re-measure.** `make eval` over a dataset that includes the chain; watch the
   false-positive rate drop while recall holds.
4. **Promote.** Only when FP is acceptable, flip `action: block`.

## Anti-patterns

- **Don't** raise `threshold` so high that real drift slips through — check recall in
  the same `make eval`.
- **Don't** add wildcards to the baseline; add the specific golden chain.
- **Don't** tune in `block` mode on a live agent — tune in `log`, then promote.

## Quick reference

```yaml
baseline:
  sources: [approvedTraces, successfulRuns, dryRun]   # dryRun = teach golden chains
  window: 100                                         # raise to absorb rare-but-real
detection:
  threshold: 3.5                                      # raise a notch for numeric noise
action: log                                           # tune here; promote to block after
```
