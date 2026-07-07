# Scientific Critique

Use this skill when the user asks for scientific critique, counter-evidence,
believability, over-extrapolation, methodology flaws, statistical pitfalls,
peer-review style assessment, or a devil's-advocate review of biomedical
claims.

## Core Rule

Audit before critique. Formal critique should start from `evidence_graph`
output, not from impressionistic reading. If the user pasted free text and no
evidence graph is available, use `critique_text` only as a heuristic triage
entry point and say that formal critique still requires evidence verification.

## Workflow

1. Receive claims.
   - BioCSSwitch-generated conclusions: use the existing `evidence_graph`
     result when available.
   - User-pasted conclusions: use `critique_text` for quick triage, or first
     decompose into claims and run evidence audit when citations are present.

2. Run structured audit.
   - Use `evidence_graph` to bind each claim to citations, species, sample size,
     experiment type, applicability boundary, conflicts, and counter-evidence.

3. Detect over-extrapolation.
   - Use `critique_conclusion` for each claim.
   - Preserve every `rule_id`, `severity`, `signals`, and `recommendation` in
     the final answer when discussing a finding.

4. Scan methodology.
   - Read the core study or study summary and fill the 10-item checklist.
   - Use `critique_methodology` to validate judgments, detect contradictions,
     compute the quality score, and map findings to GRADE domains.

5. Search for conflicting evidence when useful and allowed.
   - Use `find_conflicting_evidence` for PubMed contrastive search.
   - Treat returned PMIDs as potential conflicts only. Pass them through
     `evidence_verify` before using them as counter-evidence.
   - Use `check_retraction_status` for cited PMIDs when retraction/erratum risk
     matters.

6. Score and report.
   - Use `believability_score` for claim-level stars.
   - Use `critique_full_report` for a full Markdown report.
   - Move major EX/METH risks into `uncertainty_ledger` Conflicts or Missing
     data, and move `upgrade_path` / counter-experiment needs into Next
     experiment.

## Non-Negotiables

- Do not fabricate counter-evidence, PMIDs, DOIs, or trial IDs.
- Do not present critique as a formal peer review or clinical recommendation.
- Explain that star ratings are deterministic weighted arithmetic, not truth.
- If evidence is absent, say what was not verified instead of filling the gap
  with plausible-sounding claims.
- Every critique finding should be reproducible from a rule id plus signals.

## Output Pattern

For concise answers, include:

- Main verdict: one sentence.
- Top risks: 1-3 bullets with `rule_id` or `check_id`.
- Believability: stars plus the main dimension that drove the score.
- Minimum next experiment: one concrete validation step.

For full reviews, use `critique_full_report` and then add a short human summary
of what should change in the manuscript or conclusion wording.

