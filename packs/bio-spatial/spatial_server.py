#!/usr/bin/env python3
"""Spatial transcriptomics recipe MCP (bio-spatial).

The tools here follow the BioCSSwitch rule: do not run heavy analysis in the
MCP subprocess. They produce deterministic recipe hashes, scripts/skeletons,
and provenance templates that the user runs locally.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import provenance as prov  # noqa: E402
from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-spatial", "0.1.0")


_PLATFORMS: Dict[str, Dict[str, Any]] = {
    "visium": {
        "assay_class": "spot-based whole transcriptome",
        "resolution": "multi-cell spots",
        "strengths": ["whole-transcriptome coverage", "mature ecosystem", "histology alignment"],
        "failure_modes": ["mixed-cell spots", "lower single-cell specificity", "spot-level deconvolution required"],
        "best_for": ["broad tissue architecture", "discovery scans", "paired histology studies"],
    },
    "visium_hd": {
        "assay_class": "high-resolution whole transcriptome",
        "resolution": "near-cellular bins",
        "strengths": ["whole-transcriptome coverage", "near-cellular spatial grid"],
        "failure_modes": ["sparsity", "bin-size sensitivity", "newer computational defaults"],
        "best_for": ["rare-region screening", "whole-transcriptome niche discovery"],
    },
    "xenium": {
        "assay_class": "imaging-based targeted panel",
        "resolution": "single-cell / subcellular molecules",
        "strengths": ["low-background targeted signal", "cell segmentation", "molecule-level coordinates"],
        "failure_modes": ["panel-limited genes", "segmentation bias", "neighbor contamination"],
        "best_for": ["orthogonal validation", "rare cell localization", "niche marker checks"],
    },
    "cosmx": {
        "assay_class": "imaging-based targeted panel",
        "resolution": "single-cell / subcellular molecules",
        "strengths": ["large targeted panels", "cell segmentation", "subcellular molecule coordinates"],
        "failure_modes": ["background/specificity must be audited", "segmentation-sensitive counts"],
        "best_for": ["targeted spatial validation", "immune/tumor microenvironment panels"],
    },
    "merfish": {
        "assay_class": "multiplexed imaging",
        "resolution": "single-cell / subcellular molecules",
        "strengths": ["high molecule localization precision", "custom gene panels", "3D-capable designs"],
        "failure_modes": ["panel design bias", "segmentation and decoding QC dominate"],
        "best_for": ["3D/subcellular atlas work", "mechanistic niche mapping"],
    },
    "slideseq": {
        "assay_class": "bead-based high-resolution capture",
        "resolution": "near-cellular bead coordinates",
        "strengths": ["high spatial density", "whole-transcriptome-like discovery"],
        "failure_modes": ["bead registration", "capture efficiency variation", "coordinate QC"],
        "best_for": ["fine-grained tissue gradients", "discovery when imaging panels are too narrow"],
    },
}


_SPATIAL_MODELS: Dict[str, Dict[str, Any]] = {
    "scgpt_spatial": {
        "family": "spatial expression foundation model",
        "inputs": ["gene expression", "platform/protocol metadata", "spatial profiles"],
        "use_when": ["zero-shot or fine-tuned spatial cell representation", "protocol-aware benchmarking"],
        "baseline": "scVI plus platform-stratified marker scoring",
        "script_status": "skeleton; verify current official API before running",
    },
    "nicheformer": {
        "family": "joint dissociated + spatial representation model",
        "inputs": ["single-cell expression", "spatial expression", "cell/niche context"],
        "use_when": ["joint scRNA-seq and spatial reference mapping", "niche-aware embeddings"],
        "baseline": "cell2location/RCTD plus scVI",
        "script_status": "skeleton; verify current official API before running",
    },
    "cellama": {
        "family": "cell sentence / metadata-aware representation",
        "inputs": ["expression summary", "metadata", "optional niche context"],
        "use_when": ["zero-shot reference mapping", "metadata-rich atlas harmonization"],
        "baseline": "CellTypist/SingleR plus marker scoring",
        "script_status": "skeleton; verify current official API before running",
    },
    "storm": {
        "family": "histology + spatial multimodal model",
        "inputs": ["spatial expression", "H&E image tiles"],
        "use_when": ["histology-aligned prediction", "therapy-response style multimodal studies"],
        "baseline": "squidpy image features plus expression-only model",
        "script_status": "skeleton; verify current official API before running",
    },
    "novae": {
        "family": "graph spatial representation",
        "inputs": ["spatial graph", "expression"],
        "use_when": ["neighborhood graph embeddings", "spatial domain discovery"],
        "baseline": "squidpy neighbors plus Leiden/PAGA",
        "script_status": "skeleton; verify current official API before running",
    },
    "past": {
        "family": "pathology-fused spatial model",
        "inputs": ["spatial expression", "histology features"],
        "use_when": ["pathology-aware spatial embedding", "histology-expression fusion"],
        "baseline": "expression-only embedding plus image-feature ablation",
        "script_status": "skeleton; verify current official API before running",
    },
}


def _recipe_hash(tool: str, params: Dict[str, Any]) -> str:
    return prov.content_hash({"tool": tool, "params": params})


def _provenance(tool: str, recipe_hash: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "bio-spatial/recipe-provenance/1",
        "tool": tool,
        "recipe_hash": recipe_hash,
        "input": {
            "anndata_fingerprint": "<FILL from bio-singlecell>",
            "anndata_sha256": "<FILL true content hash>",
            "spatial_coordinate_hash": "<FILL if coordinate table is separate>",
            "image_sha256": "<FILL if histology/image is used>",
        },
        "params": params,
        "run": {"created_at": "<FILL ISO8601>", "executed_by_user": True},
    }


def _platform_names(platforms: Optional[List[str]]) -> List[str]:
    if not platforms:
        return sorted(_PLATFORMS)
    return [p.lower() for p in platforms if p.lower() in _PLATFORMS]


@server.tool(
    "spatial_platform_matrix",
    "Return a platform-aware spatial transcriptomics comparison matrix and QC decision guide. Use before mixing "
    "Visium/Visium HD/Xenium/CosMx/MERFISH data or making rare-cell claims.",
    {
        "type": "object",
        "properties": {
            "platforms": {"type": "array", "items": {"type": "string"}},
            "tissue": {"type": "string", "default": "generic"},
            "goal": {"type": "string", "default": "platform_selection"},
        },
    },
)
def spatial_platform_matrix(platforms: Optional[List[str]] = None, tissue: str = "generic", goal: str = "platform_selection"):
    chosen = _platform_names(platforms)
    rows = []
    for p in chosen:
        item = dict(_PLATFORMS[p])
        item["platform"] = p
        rows.append(item)
    return {
        "platforms": rows,
        "goal": goal,
        "tissue": tissue,
        "decision_rules": [
            "Use whole-transcriptome spot/bin platforms for discovery; use imaging platforms for targeted orthogonal validation.",
            "Keep platform as an explicit covariate; do not pool Visium, Xenium and CosMx counts without platform-stratified QC.",
            "For rare cells, require a marker-score baseline and at least one orthogonal check before claiming a new population.",
            "Audit segmentation quality and neighbor contamination for imaging platforms before interpreting niche enrichment.",
            "For Visium HD, report bin size and sparsity diagnostics because conclusions can change with binning choices.",
        ],
        "matched_section_design": {
            "minimum": ["adjacent sections", "shared tissue landmarks", "shared marker panel or marker genes"],
            "strong": ["matched H&E", "matched scRNA-seq reference", "platform-specific negative controls", "replicate donors"],
        },
    }


@server.tool(
    "spatial_preprocess_recipe",
    "Generate a platform-aware spatial preprocessing recipe. Covers Visium/Visium HD binning, Xenium/CosMx segmentation "
    "QC, molecule contamination checks, squidpy neighborhoods and provenance.",
    {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": ["visium", "visium_hd", "xenium", "cosmx", "merfish", "slideseq"], "default": "visium"},
            "has_matched_histology": {"type": "boolean", "default": True},
            "segmentation_source": {"type": "string"},
            "coordinate_key": {"type": "string", "default": "spatial"},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def spatial_preprocess_recipe(
    platform: str = "visium",
    has_matched_histology: bool = True,
    segmentation_source: Optional[str] = None,
    coordinate_key: str = "spatial",
    seed: int = 0,
):
    platform = platform.lower()
    params = {
        "platform": platform,
        "has_matched_histology": has_matched_histology,
        "segmentation_source": segmentation_source,
        "coordinate_key": coordinate_key,
        "seed": seed,
    }
    recipe_hash = _recipe_hash("spatial_preprocess_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": _render_preprocess_script(platform, coordinate_key, has_matched_histology, segmentation_source, seed),
        "qc_checks": _preprocess_qc_checks(platform, has_matched_histology),
        "provenance_skeleton": _provenance("spatial_preprocess_recipe", recipe_hash, params),
        "warnings": [
            "Do not interpret spatial domains until coordinate and image alignment are recorded in provenance.",
            "Do not erase raw counts; keep raw/counts layers for deconvolution and downstream model baselines.",
        ],
    }


def _preprocess_qc_checks(platform: str, has_image: bool) -> List[str]:
    checks = ["coordinate completeness", "raw-count layer present", "per-spot/cell UMI and gene-count distributions"]
    if has_image:
        checks.append("histology/image registration audit")
    if platform in {"xenium", "cosmx", "merfish"}:
        checks.extend(["segmentation area/nucleus overlap", "negative probe/control probe background", "neighbor contamination audit"])
    if platform == "visium_hd":
        checks.extend(["bin-size sensitivity", "sparsity by bin", "aggregate-to-cell-radius comparison"])
    if platform == "slideseq":
        checks.extend(["bead registration", "bead density", "coordinate outlier removal"])
    return checks


def _render_preprocess_script(platform: str, coordinate_key: str, has_image: bool, segmentation_source: Optional[str], seed: int) -> str:
    if platform == "visium":
        loader = 'adata = sq.read.visium("SPACERANGER_OUT_DIR")'
    elif platform == "visium_hd":
        loader = 'adata = sc.read_h5ad("visium_hd_bins.h5ad")  # include bin_size in adata.uns["spatial_qc"]'
    else:
        loader = f'adata = sc.read_h5ad("{platform}_cell_feature_matrix.h5ad")  # must include adata.obsm["{coordinate_key}"]'
    seg_block = ""
    if platform in {"xenium", "cosmx", "merfish"}:
        seg_block = f'''
# Imaging-platform segmentation and background audit.
adata.obs["segmentation_source"] = {segmentation_source!r}
for col in ["cell_area", "nucleus_area", "negative_probe_count", "control_probe_count"]:
    if col not in adata.obs:
        adata.obs[col] = np.nan
adata.obs[["cell_area", "nucleus_area", "negative_probe_count", "control_probe_count"]].to_csv("segmentation_background_qc.tsv", sep="\\t")
# TODO: inspect molecule-to-cell assignment and neighbor contamination before rare-cell claims.
'''
    image_note = "# Matched histology is expected; record image_sha256 and registration method.\n" if has_image else "# No matched histology declared; image-derived claims are disabled.\n"
    return f'''# bio-spatial.spatial_preprocess_recipe
import json
import numpy as np
import scanpy as sc
import squidpy as sq

np.random.seed({seed})
{loader}
{image_note}assert "{coordinate_key}" in adata.obsm or "spatial" in adata.obsm, "missing spatial coordinates"
if "counts" not in adata.layers:
    adata.layers["counts"] = adata.X.copy()
sc.pp.filter_genes(adata, min_cells=3)
sc.pp.calculate_qc_metrics(adata, inplace=True)
adata.obs[["total_counts", "n_genes_by_counts"]].to_csv("spatial_qc_cell_or_spot_metrics.tsv", sep="\\t")
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
sc.pp.highly_variable_genes(adata, n_top_genes=3000, flavor="seurat_v3", layer="counts")
sc.tl.pca(adata)
sc.pp.neighbors(adata)
sc.tl.leiden(adata, key_added="spatial_leiden")
sq.gr.spatial_neighbors(adata, coord_type="generic")
sq.gr.spatial_autocorr(adata, mode="moran")
sq.gr.nhood_enrichment(adata, cluster_key="spatial_leiden")
{seg_block}
provenance = {{
  "platform": "{platform}",
  "coordinate_key": "{coordinate_key}",
  "has_matched_histology": {has_image!r},
  "seed": {seed}
}}
json.dump(provenance, open("spatial_preprocess_provenance_stub.json", "w", encoding="utf-8"), indent=2)
adata.write_h5ad("spatial_preprocessed.h5ad")
'''


@server.tool(
    "spatial_deconvolution_recipe",
    "Generate spatial deconvolution or reference-mapping recipes. Auto mode uses marker scoring for rare-cell-heavy "
    "questions, otherwise cell2location for Visium-like data and RCTD for imaging/spot transfer checks.",
    {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["auto", "marker_score", "cell2location", "rctd", "tangram", "stereoscope"], "default": "auto"},
            "platform": {"type": "string", "enum": ["visium", "visium_hd", "xenium", "cosmx", "merfish", "slideseq"], "default": "visium"},
            "reference_modality": {"type": "string", "default": "scRNA-seq"},
            "rare_cell_expected": {"type": "boolean", "default": False},
            "celltype_key": {"type": "string", "default": "cell_type"},
            "sample_key": {"type": "string", "default": "sample_id"},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def spatial_deconvolution_recipe(
    method: str = "auto",
    platform: str = "visium",
    reference_modality: str = "scRNA-seq",
    rare_cell_expected: bool = False,
    celltype_key: str = "cell_type",
    sample_key: str = "sample_id",
    seed: int = 0,
):
    platform = platform.lower()
    chosen = _choose_deconv_method(method, platform, rare_cell_expected)
    params = {
        "method": chosen,
        "platform": platform,
        "reference_modality": reference_modality,
        "rare_cell_expected": rare_cell_expected,
        "celltype_key": celltype_key,
        "sample_key": sample_key,
        "seed": seed,
    }
    recipe_hash = _recipe_hash("spatial_deconvolution_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "recommended_method": chosen,
        "script": _render_deconv_script(chosen, platform, celltype_key, sample_key, seed),
        "method_guidance": {
            "marker_score": "Mandatory baseline for rare populations and targeted panels; robust but marker-list dependent.",
            "cell2location": "Strong for Visium-like spot/bin deconvolution when a matched scRNA-seq reference exists.",
            "rctd": "Useful for reference transfer and doublet/mixed-spot modeling; good orthogonal check.",
            "tangram": "Maps scRNA-seq cells onto space; sensitive to reference and gene overlap.",
            "stereoscope": "Probabilistic spot deconvolution; useful baseline for whole-transcriptome spots.",
        },
        "rare_cell_guardrails": [
            "Report agreement between deconvolution and marker-score baseline; do not rely on one complex model alone.",
            "Use donor/sample-level replication; do not count spots/cells as independent biological replicates.",
            "For rare cell types, require manual marker audit and a negative-control marker set.",
        ],
        "provenance_skeleton": _provenance("spatial_deconvolution_recipe", recipe_hash, params),
    }


def _choose_deconv_method(method: str, platform: str, rare: bool) -> str:
    if method != "auto":
        return method
    if rare:
        return "marker_score"
    if platform in {"visium", "visium_hd", "slideseq"}:
        return "cell2location"
    return "rctd"


def _render_deconv_script(method: str, platform: str, celltype_key: str, sample_key: str, seed: int) -> str:
    if method == "marker_score":
        return f'''# Marker-score baseline for spatial deconvolution / rare-cell checks.
import json
import numpy as np
import scanpy as sc
import squidpy as sq

np.random.seed({seed})
adata = sc.read_h5ad("spatial_preprocessed.h5ad")
markers = json.load(open("celltype_marker_genes.json", encoding="utf-8"))
for name, genes in markers.items():
    genes = [g for g in genes if g in adata.var_names]
    if genes:
        sc.tl.score_genes(adata, genes, score_name=f"score_{{name}}")
score_cols = [c for c in adata.obs.columns if c.startswith("score_")]
adata.obs[score_cols].to_csv("spatial_marker_scores.tsv", sep="\\t")
sq.gr.nhood_enrichment(adata, cluster_key="spatial_leiden")
adata.write_h5ad("spatial_marker_scored.h5ad")
'''
    if method == "cell2location":
        return f'''# cell2location recipe skeleton for {platform}; fill current API details locally.
raise SystemExit("SKELETON: install/pin cell2location, fill TODOs, verify current API, then remove this guard")
import numpy as np
import scanpy as sc
np.random.seed({seed})
sp = sc.read_h5ad("spatial_preprocessed.h5ad")
ref = sc.read_h5ad("matched_scrna_reference.h5ad")
assert "{celltype_key}" in ref.obs, "reference needs cell-type labels"
assert "counts" in sp.layers and "counts" in ref.layers, "cell2location requires raw counts"
# TODO: train reference regression model, export cell-type signatures.
# TODO: train cell2location spatial model and write abundance estimates.
'''
    if method == "rctd":
        return f'''# RCTD / spacexr recipe skeleton.
suppressPackageStartupMessages({{
  library(spacexr)
  library(zellkonverter)
}})
set.seed({seed})
sp <- readH5AD("spatial_preprocessed.h5ad")
ref <- readH5AD("matched_scrna_reference.h5ad")
# TODO: construct SpatialRNA and Reference objects using {celltype_key} and {sample_key}.
# TODO: run RCTD and export weights for each spot/cell/bin.
'''
    if method == "tangram":
        return f'''# Tangram mapping recipe skeleton.
raise SystemExit("SKELETON: install/pin tangram-sc, fill TODOs, verify current API, then remove this guard")
import numpy as np
import scanpy as sc
np.random.seed({seed})
sp = sc.read_h5ad("spatial_preprocessed.h5ad")
ref = sc.read_h5ad("matched_scrna_reference.h5ad")
# TODO: select shared genes, preprocess ref/spatial, run tangram.map_cells_to_space.
'''
    return f'''# Stereoscope recipe skeleton.
raise SystemExit("SKELETON: install/pin stereoscope, fill TODOs, verify current API, then remove this guard")
# TODO: export counts and labels from matched_scrna_reference.h5ad, train reference model, deconvolve spatial counts.
'''


@server.tool(
    "spatial_rare_cell_recipe",
    "Generate a rare-cell spatial validation recipe with marker scoring, negative controls, neighborhood enrichment, "
    "and simulation/stress-test hooks.",
    {
        "type": "object",
        "properties": {
            "rare_population": {"type": "string", "default": "rare epithelial state"},
            "marker_genes": {"type": "array", "items": {"type": "string"}},
            "platform": {"type": "string", "enum": ["visium", "visium_hd", "xenium", "cosmx", "merfish", "slideseq"], "default": "xenium"},
            "min_spots_or_cells": {"type": "integer", "default": 25},
            "validation_mode": {"type": "string", "enum": ["marker_only", "orthogonal", "simulation_stress"], "default": "orthogonal"},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def spatial_rare_cell_recipe(
    rare_population: str = "rare epithelial state",
    marker_genes: Optional[List[str]] = None,
    platform: str = "xenium",
    min_spots_or_cells: int = 25,
    validation_mode: str = "orthogonal",
    seed: int = 0,
):
    markers = marker_genes or _default_rare_markers(rare_population)
    params = {
        "rare_population": rare_population,
        "marker_genes": markers,
        "platform": platform,
        "min_spots_or_cells": min_spots_or_cells,
        "validation_mode": validation_mode,
        "seed": seed,
    }
    recipe_hash = _recipe_hash("spatial_rare_cell_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": _render_rare_cell_script(rare_population, markers, min_spots_or_cells, seed),
        "decision_thresholds": {
            "minimum_detected_units": min_spots_or_cells,
            "required_evidence": ["marker score above background", "spatial clustering or niche enrichment", "negative-control marker set", "sample-level replication"],
            "orthogonal_mode_adds": ["matched scRNA-seq reference", "second spatial platform or imaging/protein validation"],
        },
        "stress_tests": [
            "Shuffle marker labels and rerun score distribution.",
            "Use decoy markers matched for expression level.",
            "Downsample counts to test stability.",
            "Repeat analysis stratified by sample/donor and platform.",
        ],
        "provenance_skeleton": _provenance("spatial_rare_cell_recipe", recipe_hash, params),
    }


def _default_rare_markers(population: str) -> List[str]:
    p = population.lower()
    if "krt17" in p or "basaloid" in p or "ipf" in p:
        return ["KRT17", "KRT5", "KRT8", "EPCAM", "KRT14", "SPP1", "COL1A1"]
    if "immune" in p:
        return ["PTPRC", "CD3D", "NKG7", "MS4A1", "LYZ"]
    return ["EPCAM", "PTPRC", "COL1A1", "PECAM1"]


def _render_rare_cell_script(population: str, markers: List[str], min_units: int, seed: int) -> str:
    return f'''# Rare-cell spatial validation recipe: {population}
import json
import numpy as np
import scanpy as sc
import squidpy as sq

np.random.seed({seed})
adata = sc.read_h5ad("spatial_preprocessed.h5ad")
markers = {markers!r}
present = [g for g in markers if g in adata.var_names]
assert len(present) >= max(2, len(markers) // 3), "too few marker genes present for a defensible rare-cell score"
sc.tl.score_genes(adata, present, score_name="rare_population_score")
threshold = adata.obs["rare_population_score"].quantile(0.95)
adata.obs["rare_population_candidate"] = adata.obs["rare_population_score"] >= threshold
assert int(adata.obs["rare_population_candidate"].sum()) >= {min_units}, "candidate count below predeclared minimum; report as underpowered"
sq.gr.spatial_neighbors(adata)
sq.gr.nhood_enrichment(adata, cluster_key="rare_population_candidate")
adata.obs[["rare_population_score", "rare_population_candidate"]].to_csv("rare_population_marker_score.tsv", sep="\\t")
json.dump({{"population": {population!r}, "markers": present, "threshold_quantile": 0.95}}, open("rare_population_recipe_meta.json", "w"), indent=2)
adata.write_h5ad("spatial_rare_population_scored.h5ad")
'''


@server.tool(
    "spatial_scfm_model_matrix",
    "Return spatial foundation-model and baseline matrix. Use before choosing scGPT-Spatial, Nicheformer, CELLama, "
    "STORM, Novae or PAST-style model skeletons.",
    {
        "type": "object",
        "properties": {"model": {"type": "string"}},
    },
)
def spatial_scfm_model_matrix(model: Optional[str] = None):
    if model:
        key = model.lower().replace("-", "_")
        return _SPATIAL_MODELS.get(key) or {"error": f"unknown spatial model: {model}", "available": sorted(_SPATIAL_MODELS)}
    return {
        "models": _SPATIAL_MODELS,
        "baseline_contract": [
            "Every spatial foundation-model run must include an expression-only baseline and a marker/deconvolution baseline.",
            "Record platform, panel/binning, coordinate preprocessing hash and image-feature provenance separately.",
            "Never compare UMAP aesthetics only; report batch/platform mixing and biology conservation metrics.",
        ],
    }


@server.tool(
    "spatial_scfm_plan",
    "Generate a NOT-RUNNABLE spatial foundation-model skeleton plus provenance fields for scGPT-Spatial, Nicheformer, "
    "CELLama, STORM, Novae or PAST-style workflows.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string", "enum": ["scgpt_spatial", "nicheformer", "cellama", "storm", "novae", "past"]},
            "platform": {"type": "string", "enum": ["visium", "visium_hd", "xenium", "cosmx", "merfish", "slideseq"], "default": "visium"},
            "task_type": {"type": "string", "enum": ["embedding", "reference_mapping", "niche_prediction", "histology_fusion"], "default": "embedding"},
            "anndata_sha256": {"type": "string"},
            "spatial_recipe_hash": {"type": "string"},
            "output_layer": {"type": "string"},
            "seed": {"type": "integer", "default": 0},
        },
        "required": ["model"],
    },
)
def spatial_scfm_plan(
    model: str,
    platform: str = "visium",
    task_type: str = "embedding",
    anndata_sha256: Optional[str] = None,
    spatial_recipe_hash: Optional[str] = None,
    output_layer: Optional[str] = None,
    seed: int = 0,
):
    key = model.lower()
    meta = _SPATIAL_MODELS.get(key)
    if not meta:
        return {"error": f"unknown spatial model: {model}", "available": sorted(_SPATIAL_MODELS)}
    out_layer = output_layer or f"X_{key}"
    params = {
        "model": key,
        "platform": platform,
        "task_type": task_type,
        "anndata_sha256": anndata_sha256,
        "spatial_recipe_hash": spatial_recipe_hash,
        "output_layer": out_layer,
        "seed": seed,
    }
    plan_hash = _recipe_hash("spatial_scfm_plan", params)
    gaps = []
    if not anndata_sha256:
        gaps.append("missing anndata_sha256 from true content hash snippet")
    if not spatial_recipe_hash:
        gaps.append("missing spatial_recipe_hash from spatial_preprocess_recipe")
    return {
        "plan_hash": plan_hash,
        "artifact_type": "skeleton",
        "runnable": False,
        "model": meta,
        "params": params,
        "script": _render_spatial_scfm_script(key, platform, task_type, out_layer, seed),
        "gaps": gaps,
        "provenance_skeleton": {
            "schema": "bio-spatial/foundation-model-provenance/1",
            "plan_hash": plan_hash,
            "model": {"name": key, "version": "<FILL>", "checkpoint": "<FILL>", "commit": "<FILL>"},
            "input": {"anndata_sha256": anndata_sha256 or "<FILL>", "platform": platform, "spatial_recipe_hash": spatial_recipe_hash or "<FILL>"},
            "embedding": {"output_layer": out_layer, "output_sha256": "<FILL>", "n_dims": "<FILL>"},
            "baseline": {"expression_only": "<FILL>", "marker_or_deconvolution": "<FILL>"},
            "run": {"seed": seed, "created_at": "<FILL ISO8601>", "device": "<FILL>"},
        },
        "warnings": [
            "This is a skeleton, not a runnable script. Verify the current official API and remove SystemExit only after filling TODOs.",
            "Spatial foundation-model claims require platform-stratified quality metrics and a simpler baseline.",
        ],
    }


def _render_spatial_scfm_script(model: str, platform: str, task_type: str, out_layer: str, seed: int) -> str:
    return f'''# {"=" * 68}
# SKELETON - NOT RUNNABLE AS-IS
# Spatial foundation-model APIs and checkpoints change quickly. Pin versions,
# fill TODOs, verify the current official API, then remove this guard.
# {"=" * 68}
raise SystemExit("SKELETON: fill TODOs, pin versions, verify official API, then remove this guard")

import hashlib
import json
import numpy as np
import scanpy as sc

np.random.seed({seed})
adata = sc.read_h5ad("spatial_preprocessed.h5ad")
assert "spatial" in adata.obsm or any(k.endswith("spatial") for k in adata.obsm.keys()), "missing spatial coordinates"

# TODO {model}: load checkpoint and run task={task_type} for platform={platform}.
# TODO: preserve protocol/platform metadata and spatial graph inputs.
# TODO: write embedding to adata.obsm["{out_layer}"].

E = np.ascontiguousarray(adata.obsm["{out_layer}"])
print("embedding_dim:", E.shape[1])
print("output_sha256:sha256:" + hashlib.sha256(E.tobytes()).hexdigest())
json.dump({{"model": "{model}", "platform": "{platform}", "task_type": "{task_type}"}}, open("spatial_scfm_run_stub.json", "w"), indent=2)
adata.write_h5ad("spatial_fm_embedded.h5ad")
'''


@server.tool(
    "ipf_krt17_spatial_validation_recipe",
    "Generate an IPF-focused spatial validation recipe for KRT17/KRT5-low aberrant epithelial states, SPP1 macrophage "
    "niches, fibrotic ECM neighborhoods and orthogonal Xenium/Visium HD-style checks.",
    {
        "type": "object",
        "properties": {
            "platforms": {"type": "array", "items": {"type": "string"}},
            "epithelial_markers": {"type": "array", "items": {"type": "string"}},
            "niche_markers": {"type": "array", "items": {"type": "string"}},
            "sample_groups": {"type": "array", "items": {"type": "string"}},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def ipf_krt17_spatial_validation_recipe(
    platforms: Optional[List[str]] = None,
    epithelial_markers: Optional[List[str]] = None,
    niche_markers: Optional[List[str]] = None,
    sample_groups: Optional[List[str]] = None,
    seed: int = 0,
):
    plats = _platform_names(platforms or ["xenium", "visium_hd"])
    epi = epithelial_markers or ["KRT17", "KRT5", "KRT8", "EPCAM", "KRT14", "TP63"]
    niche = niche_markers or ["SPP1", "TGFB1", "COL1A1", "COL3A1", "ACTA2", "APOE"]
    groups = sample_groups or ["control_lung", "ipf_early_or_normal_appearing", "ipf_fibrotic"]
    params = {"platforms": plats, "epithelial_markers": epi, "niche_markers": niche, "sample_groups": groups, "seed": seed}
    recipe_hash = _recipe_hash("ipf_krt17_spatial_validation_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "validation_arms": [
            "KRT17 epithelial marker score with KRT5-low/KRT17-high stratification.",
            "SPP1 macrophage and fibroblast/ECM niche co-localization.",
            "TGF-beta / APOE / TP53-associated program scoring as hypothesis-generating context.",
            "Matched scRNA-seq or atlas reference mapping; do not infer origin from spatial data alone.",
            "Orthogonal platform check: targeted imaging for specificity plus Visium HD/whole-transcriptome bins for discovery.",
            "Morphology-aware check: compare normal-appearing alveoli, airway-adjacent and fibrotic regions.",
        ],
        "script": _render_ipf_script(epi, niche, groups, seed),
        "reporting_contract": [
            "State platform, panel genes, segmentation QC and bin size before biological conclusions.",
            "Report per-donor effects and avoid treating neighboring cells/spots as independent replicates.",
            "Phrase KRT17 state origin/mechanics as a hypothesis unless supported by perturbation or lineage evidence.",
        ],
        "provenance_skeleton": _provenance("ipf_krt17_spatial_validation_recipe", recipe_hash, params),
    }


def _render_ipf_script(epi: List[str], niche: List[str], groups: List[str], seed: int) -> str:
    return f'''# IPF KRT17 spatial validation recipe.
import json
import numpy as np
import scanpy as sc
import squidpy as sq

np.random.seed({seed})
adata = sc.read_h5ad("spatial_preprocessed.h5ad")
groups = {groups!r}
for required in ["sample_id", "group"]:
    assert required in adata.obs, f"missing {{required}} metadata"

def score(name, genes):
    present = [g for g in genes if g in adata.var_names]
    assert len(present) >= 2, f"too few genes present for {{name}}"
    sc.tl.score_genes(adata, present, score_name=name)
    return present

epi = score("KRT17_epithelial_state_score", {epi!r})
niche = score("SPP1_fibrotic_niche_score", {niche!r})
adata.obs["KRT17_candidate"] = adata.obs["KRT17_epithelial_state_score"] >= adata.obs["KRT17_epithelial_state_score"].quantile(0.95)
sq.gr.spatial_neighbors(adata)
sq.gr.nhood_enrichment(adata, cluster_key="KRT17_candidate")
summary = adata.obs.groupby(["sample_id", "group"], observed=True)[["KRT17_epithelial_state_score", "SPP1_fibrotic_niche_score", "KRT17_candidate"]].mean()
summary.to_csv("ipf_krt17_spatial_scores_by_sample.tsv", sep="\\t")
json.dump({{"epithelial_markers_present": epi, "niche_markers_present": niche, "groups": groups}}, open("ipf_krt17_recipe_meta.json", "w"), indent=2)
adata.write_h5ad("ipf_krt17_spatial_scored.h5ad")
'''


if __name__ == "__main__":
    server.run()
