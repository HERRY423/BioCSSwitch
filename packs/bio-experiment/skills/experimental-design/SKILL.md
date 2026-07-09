---
name: experimental-design
description: Design executable biomedical experiments from literature gaps or hypotheses, including testability, power, protocol, reagents, cost, preregistration, and failure modes.
---

# Experimental Design

Use this skill when the user asks what experiment to do next, how to test a biological mechanism, how to close a literature gap, or how to turn a hypothesis into an executable protocol.

## Workflow

1. Ground the gap with `bio-lit` and `bio-audit` when citations are available.
2. If the claim is contested, use `bio-critique` or the multi-agent debate arena before selecting the hypothesis.
3. Call `hypothesis_testability_score` before writing a protocol.
4. Call `power_analysis_plan`; do not defer power analysis until after the experiment.
5. Call `agentic_experiment_plan` for the integrated output.
6. If causal edges are produced, persist curated results through `bio-kg`.

## Required Output

Return a structured plan with:

- literature gap and hypothesis
- testability score and blockers
- sample-size/power assumptions
- model system, intervention, controls, primary endpoint, and statistics
- ELN-importable protocol sections
- reagent/equipment checklist and lookup hints
- rough cost and timeline
- preregistration template
- pre-mortem failure modes and stopping rules

Never present generated protocols as already executed. Mark placeholders that need local lab values, quotes, approvals, or pilot data.
