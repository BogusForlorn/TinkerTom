---
name: handover
description: Prepare a verified TinkerTom task handover between Codex and Claude.
disable-model-invocation: true
---

# Explicit provider handover

This skill is explicit-only. Invoke it only when the user asks for `/handover`.

1. Call the TinkerTom `handover` action `begin` first. If that tool is absent, stop; never write a raw handover file or use another persistence path.
2. Inspect the returned verified facts and in-progress Bead candidates. If exactly one candidate is clearly relevant, select that Bead automatically. If candidates are ambiguous or none exists, continue with a checkpoint-only handover; do not invent or force a Bead selection. Draft a provider-neutral handover containing: objective; confirmed bugs and root causes; decisions; files changed and why; tests already run and results; a verified Git-state reference; unresolved work and next action; scope/security constraints; and mistakes/assumptions.
3. Ask `rubber_duck` once for a consistency review of the draft and verified facts when available. Record any new defects as unresolved; do not fix them. If unavailable, record that the review was skipped.
4. Finalize through the TinkerTom handover tool, supplying the selected Bead only when step 2 identified exactly one clearly relevant candidate; otherwise finalize a checkpoint-only handover. Then stop and tell the user to exit this provider and start the target provider.

Do not edit source, tests, configuration, memory, or this skill. Do not invoke code workers or subagents, run new tests, stage, commit, push, publish, deploy, or touch external state. The handover tool is the sole persistence mechanism.
