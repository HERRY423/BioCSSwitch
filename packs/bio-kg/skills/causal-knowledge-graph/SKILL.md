---
name: causal-knowledge-graph
description: Persist and query biomedical causal triples from BioCSSwitch conversations, literature checks, and debate outputs.
---

# Causal Knowledge Graph

Use this skill when a biomedical answer produces causal claims that should be remembered, checked for conflicts, or converted into new hypotheses.

## Workflow

1. Extract candidate triples with `kg_extract_triples`.
2. Curate entity names, context, evidence IDs, experiment type, model system, and confidence before persistence.
3. Persist curated triples with `kg_add_triples`.
4. Before asserting a causal direction, run `kg_conflict_scan`.
5. For mechanistic explanations, run `kg_causal_paths` and treat paths as hypotheses unless every edge is strongly supported.
6. For next-step planning, run `kg_gap_analysis` and hand high-priority gaps to `bio-experiment.agentic_experiment_plan`.

## Output Standard

Each causal edge should include subject, relation, object, evidence, direction, context, experiment type, model system, confidence, timestamp, and source. Do not store PHI. If a claim has no PMID, DOI, NCT, or local evidence record, mark it as a gap rather than established knowledge.
