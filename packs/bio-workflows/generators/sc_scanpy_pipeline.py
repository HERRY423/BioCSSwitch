#!/usr/bin/env python3
"""Generate a stage-gated Scanpy workflow for droplet scRNA-seq.

The generated script is intentionally conservative. It verifies raw counts,
audits the experimental design, applies sample-aware robust QC, optionally runs
doublet detection per capture library, preserves unintegrated representations,
quantifies integration effects, measures clustering stability, and emits
machine-readable readiness/provenance reports.

It does not perform reference annotation, condition-level differential
expression, ambient-RNA correction, or causal interpretation. Those require
additional data and explicit downstream designs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from string import Template
from typing import Any


GENERATOR_VERSION = "2.0.0"


PIPELINE_TEMPLATE = Template(r'''#!/usr/bin/env python3
"""Generated BioCSSwitch stage-gated scRNA-seq workflow."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import scanpy as sc
from scipy import sparse
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.neighbors import NearestNeighbors

INPUT_H5AD = Path($h5ad)
OUTDIR = Path($outdir)
ORGANISM = $organism
TISSUE = $tissue
ANALYSIS_GOALS = $analysis_goals
SAMPLE_KEY = $sample_key
CONDITION_KEY = $condition_key
BATCH_KEY = $batch_key
DOUBLET_KEY = $doublet_key
SEED = $seed
MIN_GENES = $min_genes
MIN_CELLS = $min_cells
N_MADS = $n_mads
MAX_PCT_MT = $max_pct_mt
N_TOP_GENES = $n_top_genes
N_PCS_REQUESTED = $n_pcs
N_NEIGHBORS = $n_neighbors
RESOLUTION = $resolution
RESOLUTION_GRID = $resolution_grid
STABILITY_SEEDS = $stability_seeds
EXPECTED_DOUBLET_RATE = $expected_doublet_rate
INCLUDE_DOUBLET = $include_doublet
INTEGRATION_METHOD = $integration_method
RECIPE_HASH = $recipe_hash
GENERATOR_VERSION = $generator_version

np.random.seed(SEED)
OUTDIR.mkdir(parents=True, exist_ok=True)
sc.settings.figdir = OUTDIR / "figures"
sc.settings.figdir.mkdir(parents=True, exist_ok=True)


def write_json(name, value):
    (OUTDIR / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def count_like(matrix, sample_size=100000):
    values = matrix.data if sparse.issparse(matrix) else np.asarray(matrix).ravel()
    if values.size == 0:
        return False
    if values.size > sample_size:
        rng = np.random.default_rng(SEED)
        values = rng.choice(values, sample_size, replace=False)
    values = np.asarray(values)
    return bool(
        np.isfinite(values).all()
        and np.min(values) >= 0
        and np.allclose(values, np.round(values), atol=1e-6)
    )


def robust_bounds(values, n_mads=N_MADS):
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {
            "median": None, "mad": None, "lower": None, "upper": None,
            "method": "unavailable",
        }
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    scale = 1.4826 * mad
    if scale == 0:
        return {
            "median": median,
            "mad": mad,
            "lower": float(np.quantile(finite, 0.01)),
            "upper": float(np.quantile(finite, 0.99)),
            "method": "1st-99th percentile fallback because MAD=0",
        }
    return {
        "median": median,
        "mad": mad,
        "lower": median - n_mads * scale,
        "upper": median + n_mads * scale,
        "method": f"median +/- {n_mads} scaled MAD",
    }


def safe_silhouette(embedding, labels, max_cells=10000):
    labels = np.asarray(labels).astype(str)
    if len(np.unique(labels)) < 2 or len(labels) < 20:
        return None
    counts = pd.Series(labels).value_counts()
    if counts.min() < 2:
        return None
    idx = np.arange(len(labels))
    if len(idx) > max_cells:
        idx = np.random.default_rng(SEED).choice(idx, max_cells, replace=False)
    try:
        return float(silhouette_score(np.asarray(embedding)[idx], labels[idx]))
    except ValueError:
        return None


def batch_mixing_entropy(embedding, labels, n_neighbors=30, max_cells=10000):
    labels = np.asarray(labels).astype(str)
    unique = np.unique(labels)
    if len(unique) < 2 or len(labels) < 20:
        return None
    idx = np.arange(len(labels))
    if len(idx) > max_cells:
        idx = np.random.default_rng(SEED).choice(idx, max_cells, replace=False)
    x = np.asarray(embedding)[idx]
    y = labels[idx]
    k = min(n_neighbors + 1, len(idx))
    neighbor_idx = NearestNeighbors(n_neighbors=k).fit(x).kneighbors(
        x, return_distance=False
    )
    entropies = []
    for row, own in zip(neighbor_idx, np.arange(len(idx))):
        row = row[row != own][:n_neighbors]
        probabilities = pd.Series(y[row]).value_counts(normalize=True).to_numpy()
        entropy = -float(np.sum(probabilities * np.log(probabilities + 1e-12)))
        entropies.append(entropy / np.log(len(unique)))
    return float(np.mean(entropies))


def embedding_audit(embedding, obs):
    result = {}
    if BATCH_KEY:
        result["batch_silhouette"] = safe_silhouette(embedding, obs[BATCH_KEY])
        result["batch_mixing_entropy"] = batch_mixing_entropy(embedding, obs[BATCH_KEY])
    if CONDITION_KEY:
        result["condition_silhouette"] = safe_silhouette(embedding, obs[CONDITION_KEY])
    return result


if not INPUT_H5AD.is_file():
    raise SystemExit(f"Input H5AD not found: {INPUT_H5AD}")

input_sha256 = sha256_file(INPUT_H5AD)
adata = sc.read_h5ad(INPUT_H5AD)
if adata.n_obs == 0 or adata.n_vars == 0:
    raise SystemExit("AnnData has zero cells or zero genes")
if not adata.obs_names.is_unique:
    raise SystemExit("Cell barcodes are not unique; disambiguate them per capture library")
if not adata.var_names.is_unique:
    raise SystemExit("Gene identifiers are not unique; resolve duplicates before analysis")

required_keys = [key for key in (SAMPLE_KEY, CONDITION_KEY, BATCH_KEY, DOUBLET_KEY) if key]
missing_keys = sorted(set(required_keys) - set(adata.obs.columns))
if missing_keys:
    raise SystemExit(f"Missing required obs metadata keys: {missing_keys}")

if "counts" not in adata.layers:
    if not count_like(adata.X):
        raise SystemExit(
            "Neither layers['counts'] nor a count-like X was found. "
            "Do not reconstruct raw counts from log-normalized values."
        )
    adata.layers["counts"] = adata.X.copy()
elif not count_like(adata.layers["counts"]):
    raise SystemExit("layers['counts'] is not non-negative integer-like raw counts")

design_audit = {
    "sample_key": SAMPLE_KEY or None,
    "condition_key": CONDITION_KEY or None,
    "batch_key": BATCH_KEY or None,
    "n_cells": int(adata.n_obs),
    "n_genes": int(adata.n_vars),
    "warnings": [],
}
if SAMPLE_KEY:
    design_audit["n_biological_samples"] = int(adata.obs[SAMPLE_KEY].nunique())
if SAMPLE_KEY and CONDITION_KEY:
    mapping = adata.obs[[SAMPLE_KEY, CONDITION_KEY]].drop_duplicates()
    condition_per_sample = mapping.groupby(SAMPLE_KEY, observed=True)[CONDITION_KEY].nunique()
    if int(condition_per_sample.max()) > 1:
        raise SystemExit("A biological sample maps to multiple conditions; repair sample metadata")
    reps = mapping.groupby(CONDITION_KEY, observed=True)[SAMPLE_KEY].nunique().to_dict()
    design_audit["biological_replicates_per_condition"] = {str(k): int(v) for k, v in reps.items()}
    if min(reps.values(), default=0) < 3:
        design_audit["warnings"].append(
            "Fewer than three biological replicates in at least one condition; "
            "condition-level differential expression is exploratory."
        )
if BATCH_KEY and CONDITION_KEY:
    cross = pd.crosstab(adata.obs[CONDITION_KEY], adata.obs[BATCH_KEY])
    cross.to_csv(OUTDIR / "design_condition_by_batch.tsv", sep="\t")
    batches_per_condition = (cross > 0).sum(axis=1)
    conditions_per_batch = (cross > 0).sum(axis=0)
    if int(batches_per_condition.min()) == 1 and int(conditions_per_batch.max()) == 1:
        design_audit["warnings"].append(
            "Condition appears perfectly confounded with batch; integration cannot identify "
            "which signal is technical versus biological."
        )
write_json("design_audit.json", design_audit)

# QC is calculated before filtering and thresholds are estimated per biological
# sample/capture when available. This preserves a complete exclusion audit.
names = adata.var_names.astype(str)
upper_names = names.str.upper()
if ORGANISM == "human":
    adata.var["mt"] = upper_names.str.startswith("MT-")
    adata.var["ribo"] = upper_names.str.startswith(("RPS", "RPL"))
    adata.var["hb"] = upper_names.str.match(r"^HB(?!P)")
else:
    adata.var["mt"] = names.str.startswith(("mt-", "Mt-"))
    adata.var["ribo"] = names.str.startswith(("Rps", "Rpl"))
    adata.var["hb"] = names.str.match(r"^Hb(?!p)")
sc.pp.calculate_qc_metrics(
    adata, qc_vars=["mt", "ribo", "hb"], percent_top=[20, 50], log1p=True, inplace=True
)
qc_columns = [
    "total_counts", "n_genes_by_counts", "pct_counts_mt", "pct_counts_ribo",
    "pct_counts_hb", "pct_counts_in_top_20_genes", "pct_counts_in_top_50_genes",
    "log1p_total_counts", "log1p_n_genes_by_counts",
]
available_qc_columns = [c for c in qc_columns if c in adata.obs]
adata.obs[available_qc_columns].to_csv(OUTDIR / "qc_metrics_before_filter.tsv", sep="\t")

group_key = SAMPLE_KEY or DOUBLET_KEY or BATCH_KEY
groups = (
    adata.obs.groupby(group_key, observed=True).indices
    if group_key
    else {"__all__": np.arange(adata.n_obs)}
)
adata.obs["qc_low_counts"] = False
adata.obs["qc_high_counts"] = False
adata.obs["qc_low_genes"] = False
adata.obs["qc_high_genes"] = False
adata.obs["qc_high_mt"] = False
threshold_rows = []
for group, positions in groups.items():
    group_obs = adata.obs.iloc[np.asarray(positions)]
    count_bounds = robust_bounds(group_obs["log1p_total_counts"])
    gene_bounds = robust_bounds(group_obs["log1p_n_genes_by_counts"])
    mt_bounds = robust_bounds(group_obs["pct_counts_mt"])
    mt_upper = mt_bounds["upper"]
    if MAX_PCT_MT is not None:
        mt_upper = min(mt_upper, MAX_PCT_MT)
    obs_names = group_obs.index
    adata.obs.loc[obs_names, "qc_low_counts"] = (
        group_obs["log1p_total_counts"] < count_bounds["lower"]
    ).to_numpy()
    adata.obs.loc[obs_names, "qc_high_counts"] = (
        group_obs["log1p_total_counts"] > count_bounds["upper"]
    ).to_numpy()
    adata.obs.loc[obs_names, "qc_low_genes"] = (
        (group_obs["log1p_n_genes_by_counts"] < gene_bounds["lower"])
        | (group_obs["n_genes_by_counts"] < MIN_GENES)
    ).to_numpy()
    adata.obs.loc[obs_names, "qc_high_genes"] = (
        group_obs["log1p_n_genes_by_counts"] > gene_bounds["upper"]
    ).to_numpy()
    adata.obs.loc[obs_names, "qc_high_mt"] = (
        group_obs["pct_counts_mt"] > mt_upper
    ).to_numpy()
    threshold_rows.append({
        "group": str(group),
        "n_cells": int(len(positions)),
        "counts_log_lower": count_bounds["lower"],
        "counts_log_upper": count_bounds["upper"],
        "genes_log_lower": gene_bounds["lower"],
        "genes_log_upper": gene_bounds["upper"],
        "pct_mt_upper": mt_upper,
        "counts_method": count_bounds["method"],
        "genes_method": gene_bounds["method"],
        "mt_method": mt_bounds["method"],
    })

qc_flags = ["qc_low_counts", "qc_high_counts", "qc_low_genes", "qc_high_genes", "qc_high_mt"]
adata.obs["qc_fail"] = adata.obs[qc_flags].any(axis=1)
adata.obs["qc_fail_reasons"] = adata.obs[qc_flags].apply(
    lambda row: ";".join(name.removeprefix("qc_") for name, value in row.items() if value),
    axis=1,
)
pd.DataFrame(threshold_rows).to_csv(OUTDIR / "qc_thresholds_by_group.tsv", sep="\t", index=False)
adata.obs[available_qc_columns + qc_flags + ["qc_fail", "qc_fail_reasons"]].to_csv(
    OUTDIR / "qc_cell_decisions.tsv", sep="\t"
)
n_before_qc = int(adata.n_obs)
adata = adata[~adata.obs["qc_fail"]].copy()
sc.pp.filter_genes(adata, min_cells=MIN_CELLS)
n_after_qc = int(adata.n_obs)

$doublet_block

# Preserve full log-normalized gene space for marker inspection while all
# inferential count models continue to use layers['counts'].
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
adata.raw = adata.copy()
if BATCH_KEY:
    sc.pp.highly_variable_genes(
        adata, n_top_genes=N_TOP_GENES, flavor="seurat_v3",
        layer="counts", batch_key=BATCH_KEY
    )
else:
    sc.pp.highly_variable_genes(
        adata, n_top_genes=N_TOP_GENES, flavor="seurat_v3", layer="counts"
    )
if int(adata.var["highly_variable"].sum()) < 100:
    raise SystemExit("Fewer than 100 highly variable genes remain; review QC and input counts")
adata = adata[:, adata.var["highly_variable"]].copy()
sc.pp.scale(adata, max_value=10)
n_pcs = min(N_PCS_REQUESTED, adata.n_obs - 1, adata.n_vars - 1)
if n_pcs < 2:
    raise SystemExit("Too few cells/genes for PCA after QC")
sc.tl.pca(adata, n_comps=n_pcs, svd_solver="arpack", random_state=SEED)
adata.obsm["X_pca_unintegrated"] = adata.obsm["X_pca"].copy()
representation = "X_pca_unintegrated"
integration_audit = {
    "method": INTEGRATION_METHOD,
    "before": embedding_audit(adata.obsm["X_pca_unintegrated"], adata.obs),
    "after": None,
    "status": "not_requested",
    "warnings": [],
}

$integration_block

write_json("integration_audit.json", integration_audit)
sc.pp.neighbors(
    adata, n_neighbors=min(N_NEIGHBORS, adata.n_obs - 1),
    n_pcs=n_pcs, use_rep=representation, random_state=SEED
)
sc.tl.umap(adata, random_state=SEED)

# The requested resolution is the reporting key. Alternate random seeds and a
# resolution grid are retained as a stability audit, not silently optimized.
sc.tl.leiden(adata, resolution=RESOLUTION, key_added="leiden", random_state=SEED)
primary_labels = adata.obs["leiden"].astype(str).to_numpy()
seed_rows = []
for offset in range(1, STABILITY_SEEDS):
    key = f"leiden_seed_{SEED + offset}"
    sc.tl.leiden(
        adata, resolution=RESOLUTION, key_added=key, random_state=SEED + offset
    )
    seed_rows.append({
        "seed": SEED + offset,
        "adjusted_rand_index_vs_primary": float(
            adjusted_rand_score(primary_labels, adata.obs[key].astype(str))
        ),
        "n_clusters": int(adata.obs[key].nunique()),
    })
resolution_rows = []
for value in RESOLUTION_GRID:
    key = "leiden_resolution_" + str(value).replace(".", "_")
    sc.tl.leiden(adata, resolution=float(value), key_added=key, random_state=SEED)
    resolution_rows.append({
        "resolution": float(value),
        "n_clusters": int(adata.obs[key].nunique()),
        "adjusted_rand_index_vs_selected": float(
            adjusted_rand_score(primary_labels, adata.obs[key].astype(str))
        ),
    })
pd.DataFrame(seed_rows).to_csv(OUTDIR / "cluster_seed_stability.tsv", sep="\t", index=False)
pd.DataFrame(resolution_rows).to_csv(
    OUTDIR / "cluster_resolution_sensitivity.tsv", sep="\t", index=False
)

if SAMPLE_KEY:
    support = pd.crosstab(adata.obs["leiden"], adata.obs[SAMPLE_KEY])
    support["samples_detected"] = (support > 0).sum(axis=1)
    support["minimum_cells_in_detected_sample"] = support.replace(0, np.nan).min(axis=1)
    support.to_csv(OUTDIR / "cluster_sample_support.tsv", sep="\t")

if adata.obs["leiden"].nunique() >= 2:
    sc.tl.rank_genes_groups(
        adata, groupby="leiden", method="wilcoxon", pts=True, use_raw=True,
        tie_correct=True, corr_method="benjamini-hochberg"
    )
    markers = sc.get.rank_genes_groups_df(adata, group=None)
    markers.to_csv(OUTDIR / "cluster_markers_exploratory.tsv", sep="\t", index=False)
else:
    pd.DataFrame([{
        "status": "skipped", "reason": "fewer_than_two_clusters"
    }]).to_csv(OUTDIR / "cluster_markers_exploratory.tsv", sep="\t", index=False)
sc.pl.umap(
    adata, color=["leiden", "n_genes_by_counts", "pct_counts_mt"],
    show=False, save="_clusters_qc.png"
)
if adata.obs["leiden"].nunique() >= 2:
    sc.pl.rank_genes_groups_dotplot(
        adata, n_genes=5, use_raw=True, show=False, save="_cluster_markers.png"
    )

mean_seed_ari = (
    float(pd.DataFrame(seed_rows)["adjusted_rand_index_vs_primary"].mean())
    if seed_rows else None
)
replicates = design_audit.get("biological_replicates_per_condition", {})
pseudobulk_ready = bool(replicates) and min(replicates.values()) >= 3
limitations = [
    "Cluster markers are descriptive rankings, not condition-level differential expression.",
    "Reference/consensus annotation was not run; retain an explicit unknown/ambiguous label.",
    "Trajectory, communication, and foundation-model outputs are hypotheses unless independently validated.",
]
if INTEGRATION_METHOD != "none":
    limitations.append(
        "Integrated embeddings require biological-conservation review; corrected coordinates are not corrected counts."
    )
readiness = {
    "raw_counts_verified": True,
    "design_audit_passed": not design_audit["warnings"],
    "pseudobulk_condition_de_ready": pseudobulk_ready,
    "cluster_seed_stability_mean_ari": mean_seed_ari,
    "cluster_stability_review": (
        "pass" if mean_seed_ari is not None and mean_seed_ari >= 0.8 else "review_required"
    ),
    "integration_status": integration_audit["status"],
    "cell_type_annotation_status": "not_run",
    "limitations": limitations,
}
write_json("analysis_readiness.json", readiness)

provenance = {
    "schema": "biocsswitch/sc-scanpy-pipeline/2",
    "recipe_hash": RECIPE_HASH,
    "generator_version": GENERATOR_VERSION,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "input": {"path": str(INPUT_H5AD), "sha256": input_sha256},
    "context": {
        "organism": ORGANISM, "tissue": TISSUE, "analysis_goals": ANALYSIS_GOALS
    },
    "keys": {
        "sample": SAMPLE_KEY or None, "condition": CONDITION_KEY or None,
        "batch": BATCH_KEY or None, "doublet": DOUBLET_KEY or None,
    },
    "parameters": {
        "min_genes": MIN_GENES, "min_cells": MIN_CELLS, "n_mads": N_MADS,
        "max_pct_mt": MAX_PCT_MT, "n_top_genes": N_TOP_GENES,
        "n_pcs": n_pcs, "n_neighbors": N_NEIGHBORS, "resolution": RESOLUTION,
        "resolution_grid": RESOLUTION_GRID, "stability_seeds": STABILITY_SEEDS,
        "expected_doublet_rate": EXPECTED_DOUBLET_RATE,
        "integration_method": INTEGRATION_METHOD, "seed": SEED,
    },
    "cell_counts": {
        "input": n_before_qc, "after_qc": n_after_qc,
        "final": int(adata.n_obs),
    },
    "software": {
        name: package_version(name)
        for name in ("anndata", "scanpy", "numpy", "pandas", "scikit-learn",
                     "scrublet", "harmonypy", "leidenalg")
    },
    "reports": {
        "design": "design_audit.json",
        "qc_thresholds": "qc_thresholds_by_group.tsv",
        "integration": "integration_audit.json",
        "readiness": "analysis_readiness.json",
    },
}
write_json("provenance.json", provenance)
adata.uns["biocsswitch_pipeline"] = provenance
# Backward-compatible key for consumers of the original generator.
adata.uns["csswitch_scanpy_pipeline"] = provenance
adata.write_h5ad(OUTDIR / "processed.h5ad")
print(json.dumps(readiness, ensure_ascii=False, indent=2))
''')


DOUBLET_TEMPLATE = Template(r'''# Doublets are estimated within capture libraries to avoid learning
# library-to-library shifts as doublet signal.
import scrublet as scr

doublet_group_key = DOUBLET_KEY or SAMPLE_KEY or BATCH_KEY
doublet_groups = (
    adata.obs.groupby(doublet_group_key, observed=True).indices
    if doublet_group_key
    else {"__all__": np.arange(adata.n_obs)}
)
adata.obs["doublet_score"] = np.nan
adata.obs["predicted_doublet"] = False
doublet_rows = []
for group, positions in doublet_groups.items():
    positions = np.asarray(positions)
    if len(positions) < 100:
        doublet_rows.append({
            "group": str(group), "n_cells": int(len(positions)),
            "status": "skipped_lt_100_cells",
        })
        continue
    counts = adata.layers["counts"][positions]
    scrub = scr.Scrublet(
        counts, expected_doublet_rate=EXPECTED_DOUBLET_RATE,
        random_state=SEED
    )
    scores, predictions = scrub.scrub_doublets(verbose=False)
    obs_names = adata.obs_names[positions]
    adata.obs.loc[obs_names, "doublet_score"] = scores
    adata.obs.loc[obs_names, "predicted_doublet"] = predictions
    doublet_rows.append({
        "group": str(group), "n_cells": int(len(positions)), "status": "run",
        "predicted_doublets": int(np.sum(predictions)),
        "predicted_fraction": float(np.mean(predictions)),
    })
adata.obs[["doublet_score", "predicted_doublet"]].to_csv(
    OUTDIR / "doublet_cell_decisions.tsv", sep="\t"
)
pd.DataFrame(doublet_rows).to_csv(OUTDIR / "doublet_summary_by_group.tsv", sep="\t", index=False)
adata = adata[~adata.obs["predicted_doublet"]].copy()''')


HARMONY_TEMPLATE = Template(r'''# Integration is never allowed to overwrite raw/log expression or the
# unintegrated PCA. The report quantifies batch mixing and condition geometry.
import scanpy.external as sce

if not BATCH_KEY:
    raise SystemExit("Harmony requested without --batch-key")
if adata.obs[BATCH_KEY].nunique() < 2:
    integration_audit["status"] = "skipped_single_batch"
else:
    sce.pp.harmony_integrate(adata, key=BATCH_KEY, basis="X_pca", adjusted_basis="X_pca_harmony")
    representation = "X_pca_harmony"
    integration_audit["after"] = embedding_audit(adata.obsm[representation], adata.obs)
    before = integration_audit["before"]
    after = integration_audit["after"]
    entropy_ok = (
        before.get("batch_mixing_entropy") is None
        or after.get("batch_mixing_entropy") is None
        or after["batch_mixing_entropy"] >= before["batch_mixing_entropy"] - 0.01
    )
    silhouette_ok = (
        before.get("batch_silhouette") is None
        or after.get("batch_silhouette") is None
        or abs(after["batch_silhouette"]) <= abs(before["batch_silhouette"]) + 0.01
    )
    condition_drop = None
    if before.get("condition_silhouette") is not None and after.get("condition_silhouette") is not None:
        condition_drop = before["condition_silhouette"] - after["condition_silhouette"]
        integration_audit["condition_silhouette_drop"] = condition_drop
    if condition_drop is not None and condition_drop > 0.05:
        integration_audit["warnings"].append(
            "Condition separation dropped materially after integration; inspect for over-correction."
        )
    integration_audit["status"] = (
        "metrics_pass_but_biology_review_required"
        if entropy_ok and silhouette_ok and not integration_audit["warnings"]
        else "review_required"
    )''')


def _canonical_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a stage-gated, auditable Scanpy scRNA-seq workflow."
    )
    parser.add_argument("--h5ad", required=True, help="Input AnnData .h5ad")
    parser.add_argument("--organism", choices=["human", "mouse"], default="human")
    parser.add_argument("--tissue", default="unknown")
    parser.add_argument("--analysis-goals", nargs="+", default=["clustering", "marker"])
    parser.add_argument("--sample-key", default="", help="Biological replicate/donor key")
    parser.add_argument("--condition-key", default="", help="Experimental condition key")
    parser.add_argument("--batch-key", default="", help="Technical batch key")
    parser.add_argument(
        "--doublet-key", default="",
        help="Capture-library key for per-library doublet detection; falls back to sample/batch",
    )
    parser.add_argument("--out", type=Path, required=True, help="Generated Python script")
    parser.add_argument("--outdir", default="scanpy_out")
    parser.add_argument("--min-genes", type=int, default=200)
    parser.add_argument("--min-cells", type=int, default=3)
    parser.add_argument("--n-mads", type=float, default=5.0)
    parser.add_argument(
        "--max-pct-mt", type=float, default=None,
        help="Optional hard ceiling combined with adaptive per-group mitochondrial threshold",
    )
    parser.add_argument("--n-top-genes", type=int, default=3000)
    parser.add_argument("--n-pcs", type=int, default=50)
    parser.add_argument("--n-neighbors", type=int, default=15)
    parser.add_argument("--resolution", type=float, default=0.8)
    parser.add_argument(
        "--resolution-grid", type=float, nargs="+", default=[0.4, 0.8, 1.2]
    )
    parser.add_argument("--stability-seeds", type=int, default=3)
    parser.add_argument("--include-doublet", action="store_true")
    parser.add_argument("--expected-doublet-rate", type=float, default=0.06)
    parser.add_argument(
        "--integration-method", choices=["none", "harmony"], default=None,
        help="Default: harmony when --batch-key is provided, otherwise none",
    )
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.min_genes < 1 or args.min_cells < 1:
        raise SystemExit("--min-genes and --min-cells must be positive")
    if args.n_mads <= 0 or args.n_pcs < 2 or args.n_neighbors < 2:
        raise SystemExit("--n-mads must be >0 and PCA/neighbors parameters must be >=2")
    if args.stability_seeds < 1:
        raise SystemExit("--stability-seeds must be >=1")
    if not 0 < args.expected_doublet_rate < 1:
        raise SystemExit("--expected-doublet-rate must be between 0 and 1")
    if args.max_pct_mt is not None and not 0 <= args.max_pct_mt <= 100:
        raise SystemExit("--max-pct-mt must be between 0 and 100")
    if args.condition_key and not args.sample_key:
        raise SystemExit(
            "--condition-key requires --sample-key so cells are not treated as biological replicates"
        )
    integration_method = args.integration_method or ("harmony" if args.batch_key else "none")
    if integration_method != "none" and not args.batch_key:
        raise SystemExit("Integration requires --batch-key")

    params = {
        "generator_version": GENERATOR_VERSION,
        "h5ad": args.h5ad,
        "organism": args.organism,
        "tissue": args.tissue,
        "analysis_goals": args.analysis_goals,
        "sample_key": args.sample_key,
        "condition_key": args.condition_key,
        "batch_key": args.batch_key,
        "doublet_key": args.doublet_key,
        "outdir": args.outdir,
        "min_genes": args.min_genes,
        "min_cells": args.min_cells,
        "n_mads": args.n_mads,
        "max_pct_mt": args.max_pct_mt,
        "n_top_genes": args.n_top_genes,
        "n_pcs": args.n_pcs,
        "n_neighbors": args.n_neighbors,
        "resolution": args.resolution,
        "resolution_grid": args.resolution_grid,
        "stability_seeds": args.stability_seeds,
        "include_doublet": args.include_doublet,
        "expected_doublet_rate": args.expected_doublet_rate,
        "integration_method": integration_method,
        "seed": args.seed,
    }
    recipe_hash = _canonical_hash(params)
    content = PIPELINE_TEMPLATE.substitute(
        h5ad=_json(args.h5ad),
        outdir=_json(args.outdir),
        organism=_json(args.organism),
        tissue=_json(args.tissue),
        analysis_goals=_json(args.analysis_goals),
        sample_key=_json(args.sample_key),
        condition_key=_json(args.condition_key),
        batch_key=_json(args.batch_key),
        doublet_key=_json(args.doublet_key),
        seed=args.seed,
        min_genes=args.min_genes,
        min_cells=args.min_cells,
        n_mads=args.n_mads,
        max_pct_mt="None" if args.max_pct_mt is None else args.max_pct_mt,
        n_top_genes=args.n_top_genes,
        n_pcs=args.n_pcs,
        n_neighbors=args.n_neighbors,
        resolution=args.resolution,
        resolution_grid=repr(args.resolution_grid),
        stability_seeds=args.stability_seeds,
        expected_doublet_rate=args.expected_doublet_rate,
        include_doublet=repr(args.include_doublet),
        integration_method=_json(integration_method),
        recipe_hash=_json(recipe_hash),
        generator_version=_json(GENERATOR_VERSION),
        doublet_block=DOUBLET_TEMPLATE.substitute() if args.include_doublet else (
            'adata.obs["predicted_doublet"] = False\n'
            'pd.DataFrame([{"status": "not_requested"}]).to_csv(\n'
            '    OUTDIR / "doublet_summary_by_group.tsv", sep="\\t", index=False\n'
            ')'
        ),
        integration_block=HARMONY_TEMPLATE.substitute() if integration_method == "harmony" else "",
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8", newline="\n")
    manifest = {
        "schema": "biocsswitch/sc-scanpy-generator-manifest/2",
        "recipe_hash": recipe_hash,
        "generator_version": GENERATOR_VERSION,
        "script": str(args.out),
        "params": params,
        "required_packages": [
            "scanpy", "anndata", "numpy", "pandas", "scipy", "scikit-learn",
            "leidenalg",
        ] + (["scrublet"] if args.include_doublet else [])
          + (["harmonypy"] if integration_method == "harmony" else []),
        "upstream_requirements": [
            "raw UMI counts in layers['counts'] or X",
            "ambient-RNA correction upstream when raw/filtered droplet matrices are available",
            "sample/donor metadata for condition-level inference",
        ],
    }
    manifest_path = args.out.with_suffix(args.out.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[sc_scanpy_pipeline] generated: {args.out}")
    print(f"[sc_scanpy_pipeline] manifest:  {manifest_path}")
    print(f"[sc_scanpy_pipeline] recipe:    {recipe_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
