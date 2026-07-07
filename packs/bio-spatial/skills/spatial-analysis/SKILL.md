---
name: spatial-analysis
description: Use for spatial transcriptomics, Visium, Visium HD, Xenium, CosMx, MERFISH, Slide-seq, spatial deconvolution, rare spatial cell detection, spatial foundation models, scGPT-Spatial, Nicheformer, CELLama, STORM, IPF spatial niches, KRT17 epithelial states and orthogonal spatial validation. Do not use for ordinary dissociated scRNA-seq only; route that to sc-analysis / single-cell-prep.
---

# Spatial Transcriptomics Analysis

This skill coordinates spatial transcriptomics recipe generation. It does not run heavy spatial analysis or claim biological results. It produces platform-aware recipes, scripts/skeletons, provenance fields and uncertainty boundaries.

## Workflow

1. Clarify platform and data shape: Visium, Visium HD, Xenium, CosMx, MERFISH, Slide-seq; matched histology; segmentation source; coordinate key; raw counts; panel genes; donors and sample groups.
2. Call `spatial_platform_matrix` before cross-platform interpretation or platform selection.
3. Call `spatial_preprocess_recipe` to generate platform-specific QC, spatial graph and provenance steps.
4. For cell-type mixture or reference transfer, call `spatial_deconvolution_recipe`. For rare populations, require `marker_score` as a baseline even when a complex method is also used.
5. For rare cell detection, call `spatial_rare_cell_recipe`; require negative controls, donor-level replication and an orthogonal check before strong claims.
6. For spatial foundation models, call `spatial_scfm_model_matrix` and `spatial_scfm_plan`; always pair the foundation model with simpler expression-only and marker/deconvolution baselines.
7. For IPF/KRT17 questions, call `ipf_krt17_spatial_validation_recipe` and state what is validation, what is association and what remains mechanistic hypothesis.
8. Close with an uncertainty-first summary if the answer contains biological or translational claims.

## Do

- Keep platform, bin size, panel, segmentation and coordinate provenance explicit.
- Separate discovery platforms from validation platforms.
- Use sample/donor replication for claims; do not treat neighboring spots/cells as biological replicates.
- Audit neighbor contamination and segmentation quality for imaging platforms.
- Preserve raw counts and panel metadata.

## Don't

- Do not pool platforms without platform-stratified QC and covariates.
- Do not claim a rare cell type from one deconvolution method alone.
- Do not treat spatial foundation-model embeddings as self-explaining biological evidence.
- Do not infer cell-state origin or mechanics from static spatial co-localization alone.

## Handoffs

- Dissociated scRNA-seq QC, doublet, batch and annotation: `single-cell-prep`.
- scFM provenance and embedding quality for non-spatial models: `scfm-embed`.
- DEG, trajectory, communication and enrichment after annotation: `sc-downstream-analysis`.
- Literature or medical claims: `evidence-audit` and `uncertainty-first`.
