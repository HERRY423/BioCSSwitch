#!/usr/bin/env python3
"""单细胞数据处理与配方 MCP（bio-singlecell）。

定位：**给 scRNA-seq / scFM 下游分析准备可复现输入层**，不是替用户跑分析。
工具只产出确定性配方、脚本和 provenance 骨架，实际 scanpy / scvi-tools / R 计算在用户机器上跑。

工具：
  anndata_fingerprint   — AnnData 描述符指纹 + 真·内容哈希 snippet
  sc_qc_thresholds      — MAD-based QC 阈值建议
  sc_preprocess_recipe  — scanpy / Geneformer / scGPT 预处理配方
  sc_doublet_recipe     — Scrublet / scDblFinder doublet 检测配方
  sc_batch_recipe       — Harmony / scVI / BBKNN / Scanorama batch 整合配方
  sc_geneid_convert     — symbol / Ensembl / Entrez ID 转换指南与脚本
  sc_celltype_recipe    — CellTypist / SingleR / marker-based 细胞注释配方
  sc_multimodal_recipe  — CITE-seq / Multiome 预处理配方
  sc_spatial_recipe     — Visium / MERFISH / Slide-seq 空间转录组预处理配方
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import provenance as prov  # noqa: E402
from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-singlecell", "0.3.1")


def _recipe_hash(tool: str, params: Dict[str, Any]) -> str:
    return prov.content_hash({"tool": tool, "params": params})


def _provenance_skeleton(tool: str, recipe_hash: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    skel = {
        "schema": "bio-singlecell/recipe-provenance/1",
        "tool": tool,
        "recipe_hash": recipe_hash,
        "input": {
            "anndata_fingerprint": "<FILL: anndata_fingerprint>",
            "anndata_sha256": "<FILL: true content hash>",
        },
        "run": {
            "created_at": "<FILL: ISO8601>",
            "seed": "<FILL>",
            "executed_by_user": True,
        },
    }
    if extra:
        skel.update(extra)
    return skel


@server.tool(
    "anndata_fingerprint",
    "Fingerprint an AnnData dataset for reproducibility. Takes a DESCRIPTOR (metadata only: n_obs, "
    "n_var, var_id_type, obs/var keys, layers, optional X_checksum) and returns a stable, auditable "
    "fingerprint hash — plus a Python snippet the user runs locally to compute the TRUE content hash.",
    {
        "type": "object",
        "properties": {
            "descriptor": {
                "type": "object",
                "description": "AnnData metadata: n_obs, n_var, var_id_type, obs_keys, var_keys, layers, X_dtype, X_checksum, assay, organism, raw_present.",
            },
            "hash_layer": {
                "type": "string",
                "description": "Which layer the true-hash snippet should hash (default: X).",
            },
        },
        "required": ["descriptor"],
    },
)
def anndata_fingerprint(descriptor: Dict[str, Any], hash_layer: Optional[str] = None):
    fp = prov.hash_descriptor(descriptor)
    warns: List[str] = []
    if not descriptor.get("var_id_type"):
        warns.append("未声明 var_id_type（ensembl / symbol / entrez）——scFM 与下游基因集分析都对基因 ID 类型敏感")
    if not descriptor.get("X_checksum"):
        warns.append("descriptor 未含 X_checksum：当前指纹只覆盖元数据。跑 true_content_hash_snippet 算真·内容哈希后再指纹一次")
    return {
        "fingerprint": fp,
        "descriptor_used": {
            k: descriptor.get(k)
            for k in ("n_obs", "n_var", "var_id_type", "layers", "assay", "organism", "raw_present")
        },
        "true_content_hash_snippet": prov.anndata_hash_snippet(hash_layer),
        "warnings": warns,
        "note": "fingerprint 是元数据级可核对代理；真·内容哈希请用 snippet 在本地算。两者都记进 provenance。",
    }


_DEFAULT_STEPS = [
    {"op": "filter_cells", "min_genes": 200},
    {"op": "filter_genes", "min_cells": 3},
    {"op": "normalize_total", "target_sum": 1e4},
    {"op": "log1p"},
    {"op": "highly_variable_genes", "n_top_genes": 2000, "flavor": "seurat_v3"},
]


@server.tool(
    "sc_preprocess_recipe",
    "Produce a deterministic scanpy preprocessing recipe (params + generated Python script + provenance hash). "
    "Pass target_model for model-appropriate defaults: Geneformer uses rank-value tokenization; scGPT uses HVG + binning.",
    {
        "type": "object",
        "properties": {
            "target_model": {"type": "string", "enum": ["geneformer", "scgpt", "generic"], "default": "generic"},
            "overrides": {"type": "object", "description": "Per-op param overrides."},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_preprocess_recipe(target_model: str = "generic", overrides: Optional[Dict[str, Any]] = None, seed: int = 0):
    steps = [dict(s) for s in _DEFAULT_STEPS]
    notes: List[str] = []
    if target_model == "geneformer":
        steps = [
            {"op": "filter_cells", "min_genes": 200},
            {"op": "filter_genes", "min_cells": 3},
            {"op": "require_ensembl_ids"},
            {"op": "note", "text": "Geneformer 走 rank-value tokenization，不做 normalize/log/HVG；需 Ensembl ID。"},
        ]
        notes.append("Geneformer：跳过 log1p / HVG；基因必须是 Ensembl ID。")
    elif target_model == "scgpt":
        steps = [
            {"op": "filter_cells", "min_genes": 200},
            {"op": "filter_genes", "min_cells": 3},
            {"op": "normalize_total", "target_sum": 1e4},
            {"op": "log1p"},
            {"op": "highly_variable_genes", "n_top_genes": 1200, "flavor": "seurat_v3"},
            {"op": "value_binning", "n_bins": 51},
        ]
        notes.append("scGPT：HVG(~1200) + value binning(51)；基因用 symbol。")
    if overrides:
        for s in steps:
            if s.get("op") in overrides:
                s.update(overrides[s["op"]])
    params = {"target_model": target_model, "seed": seed, "steps": steps}
    recipe_hash = _recipe_hash("sc_preprocess_recipe", params)
    script = _render_scanpy_script(params, recipe_hash)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": script,
        "provenance_skeleton": _provenance_skeleton("sc_preprocess_recipe", recipe_hash, {"preprocessing": params}),
        "notes": notes,
        "note": "把 recipe_hash 记进 scFM provenance.preprocessing_hash；脚本在用户机器上跑，产物落本地。",
    }


def _render_scanpy_script(params: Dict[str, Any], recipe_hash: str) -> str:
    needs_counts_layer = any(s.get("op") in {"normalize_total", "highly_variable_genes"} for s in params["steps"])
    lines = [
        "# 由 bio-singlecell.sc_preprocess_recipe 生成 —— 确定性预处理，请勿手改参数",
        f"# recipe params hash: {recipe_hash}",
        "import scanpy as sc, numpy as np",
        f"np.random.seed({params.get('seed', 0)})",
        'adata = sc.read_h5ad("YOUR_FILE.h5ad")',
    ]
    if needs_counts_layer:
        lines.extend([
            "# seurat_v3 HVG、Scrublet、scVI 等步骤需要 raw counts；若 X 已经是 log/normalized，请先提供 layers['counts']。",
            "if 'counts' not in adata.layers:",
            "    adata.layers['counts'] = adata.X.copy()",
        ])
    for s in params["steps"]:
        op = s.get("op")
        if op == "filter_cells":
            lines.append(f"sc.pp.filter_cells(adata, min_genes={s.get('min_genes', 200)})")
        elif op == "filter_genes":
            lines.append(f"sc.pp.filter_genes(adata, min_cells={s.get('min_cells', 3)})")
        elif op == "normalize_total":
            lines.append(f"sc.pp.normalize_total(adata, target_sum={s.get('target_sum', 1e4)})")
        elif op == "log1p":
            lines.append("sc.pp.log1p(adata)")
        elif op == "highly_variable_genes":
            flavor = s.get("flavor", "seurat_v3")
            layer_arg = ", layer='counts'" if flavor == "seurat_v3" else ""
            lines.append(f"sc.pp.highly_variable_genes(adata, n_top_genes={s.get('n_top_genes', 2000)}, flavor='{flavor}'{layer_arg})")
        elif op == "value_binning":
            lines.append(f"# scGPT value binning into {s.get('n_bins', 51)} bins（见 scGPT 预处理）")
        elif op == "require_ensembl_ids":
            lines.append("assert adata.var_names.str.startswith('ENSG').mean() > 0.9, 'Geneformer 需要 Ensembl 基因 ID'")
        elif op == "note":
            lines.append(f"# {s.get('text', '')}")
    lines.append('adata.write_h5ad("preprocessed.h5ad")')
    return "\n".join(lines)


@server.tool(
    "sc_qc_thresholds",
    "Suggest MAD-based QC thresholds from per-cell QC summary stats. Explainable: median ± n_mads*MAD.",
    {
        "type": "object",
        "properties": {
            "stats": {
                "type": "object",
                "description": "Per-metric summary: {'pct_counts_mt': {'median':5,'mad':2}}.",
            },
            "n_mads": {"type": "number", "default": 5},
        },
        "required": ["stats"],
    },
)
def sc_qc_thresholds(stats: Dict[str, Any], n_mads: float = 5):
    out: Dict[str, Any] = {}
    for metric, s in (stats or {}).items():
        med = s.get("median")
        mad = s.get("mad")
        if med is None or mad is None:
            out[metric] = {"error": "需要 median 与 mad"}
            continue
        lower = med - n_mads * mad
        upper = med + n_mads * mad
        out[metric] = {
            "lower": round(lower, 4),
            "upper": round(upper, 4),
            "rule": f"median({med}) ± {n_mads}×MAD({mad})",
            "note": "落在 [lower, upper] 之外视为离群，建议剔除；线粒体比例通常只设上界",
        }
    return {"thresholds": out, "n_mads": n_mads, "method": "MAD-based（对离群稳健，优于固定阈值 / 均值±SD）"}


@server.tool(
    "sc_doublet_recipe",
    "Generate a doublet-detection recipe. Default expected_doublet_rate follows the 10x heuristic (~0.8% per 1000 cells). "
    "Produces Scrublet Python or scDblFinder R code + recipe_hash + provenance skeleton.",
    {
        "type": "object",
        "properties": {
            "n_obs": {"type": "integer", "description": "Number of cells before doublet filtering."},
            "expected_doublet_rate": {"type": "number", "description": "Override expected doublet rate, e.g. 0.04."},
            "method": {"type": "string", "enum": ["scrublet", "scdblfinder"], "default": "scrublet"},
            "seed": {"type": "integer", "default": 0},
        },
        "required": ["n_obs"],
    },
)
def sc_doublet_recipe(n_obs: int, expected_doublet_rate: Optional[float] = None, method: str = "scrublet", seed: int = 0):
    inferred = round(min(0.25, max(0.005, 0.008 * (max(n_obs, 1) / 1000.0))), 4)
    rate = expected_doublet_rate if expected_doublet_rate is not None else inferred
    params = {"n_obs": n_obs, "expected_doublet_rate": rate, "method": method, "seed": seed}
    recipe_hash = _recipe_hash("sc_doublet_recipe", params)
    script = _render_scrublet_script(rate, seed) if method == "scrublet" else _render_scdblfinder_script(rate, seed)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "expected_doublet_rate_formula": "~0.8% per 1000 captured cells; clamped to 0.5%-25% unless user overrides",
        "script_language": "python" if method == "scrublet" else "r",
        "script": script,
        "provenance_skeleton": _provenance_skeleton("sc_doublet_recipe", recipe_hash, {"doublet": params}),
        "notes": [
            "默认 Scrublet：Python/scanpy 生态一致，适合 10x scRNA-seq。",
            "scDblFinder：R/Bioconductor 生态，适合 Seurat/SCE 用户；脚本保留阈值可视化。",
            "doublet 判定是过滤建议，不应在对话里假装已经得出 doublet 数量。",
        ],
    }


def _render_scrublet_script(rate: float, seed: int) -> str:
    return f'''# bio-singlecell.sc_doublet_recipe — Scrublet doublet 检测
import numpy as np
import scanpy as sc
import scrublet as scr
import matplotlib.pyplot as plt

np.random.seed({seed})
adata = sc.read_h5ad("preprocessed_or_raw_counts.h5ad")
counts = adata.layers["counts"] if "counts" in adata.layers else adata.X
scrub = scr.Scrublet(counts, expected_doublet_rate={rate})
doublet_scores, predicted_doublets = scrub.scrub_doublets()
adata.obs["doublet_score"] = doublet_scores
adata.obs["predicted_doublet"] = predicted_doublets

plt.hist(doublet_scores, bins=60)
plt.xlabel("Scrublet doublet score")
plt.ylabel("Cells")
plt.savefig("doublet_score_histogram.png", dpi=180, bbox_inches="tight")
adata.write_h5ad("doublet_annotated.h5ad")
'''


def _render_scdblfinder_script(rate: float, seed: int) -> str:
    return f'''# bio-singlecell.sc_doublet_recipe — scDblFinder doublet 检测
suppressPackageStartupMessages({{
  library(SingleCellExperiment)
  library(scDblFinder)
  library(zellkonverter)
  library(ggplot2)
}})
set.seed({seed})
sce <- readH5AD("preprocessed_or_raw_counts.h5ad")
sce <- scDblFinder(sce, dbr={rate})
png("doublet_score_histogram.png", width=900, height=650)
hist(sce$scDblFinder.score, breaks=60, main="scDblFinder doublet score", xlab="score")
dev.off()
writeH5AD(sce, "doublet_annotated.h5ad")
'''


@server.tool(
    "sc_batch_recipe",
    "Generate a batch-integration recipe and method guidance for Harmony, scVI, BBKNN, or Scanorama. "
    "Includes raw-count warnings for scVI and PCA-space warnings for Harmony.",
    {
        "type": "object",
        "properties": {
            "n_batches": {"type": "integer"},
            "batch_key": {"type": "string", "default": "batch"},
            "n_obs_per_batch": {"type": "array", "items": {"type": "integer"}},
            "method": {"type": "string", "enum": ["auto", "harmony", "scvi", "bbknn", "scanorama"], "default": "auto"},
            "target_model": {"type": "string", "description": "Optional downstream model to keep compatible with scFM."},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_batch_recipe(
    n_batches: Optional[int] = None,
    batch_key: str = "batch",
    n_obs_per_batch: Optional[List[int]] = None,
    method: str = "auto",
    target_model: Optional[str] = None,
    seed: int = 0,
):
    inferred_batches = n_batches or (len(n_obs_per_batch or []) or 1)
    total_cells = sum(n_obs_per_batch or []) if n_obs_per_batch else None
    chosen = method if method != "auto" else _choose_batch_method(inferred_batches, total_cells, target_model)
    params = {
        "n_batches": inferred_batches,
        "batch_key": batch_key,
        "n_obs_per_batch": n_obs_per_batch or [],
        "method": chosen,
        "target_model": target_model,
        "seed": seed,
    }
    recipe_hash = _recipe_hash("sc_batch_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "recommended_method": chosen,
        "method_guidance": {
            "harmony": "≤3 个 batch、同 protocol、想快速在 PCA 空间校正时优先；注意 Harmony 操作的是 PCA embedding。",
            "scvi": ">3 batch、跨 protocol/实验室、需要更稳健整合时优先；必须保留 raw counts 层。",
            "bbknn": ">100k cells 或只想轻量调整邻居图时优先；不是表达矩阵校正。",
            "scanorama": "跨数据集 merge、缺少统一 raw counts 时可作为实用选择。",
        },
        "script": _render_batch_script(chosen, batch_key, seed),
        "provenance_skeleton": _provenance_skeleton("sc_batch_recipe", recipe_hash, {"batch_integration": params}),
        "warnings": _batch_warnings(chosen, target_model),
    }


def _choose_batch_method(n_batches: int, total_cells: Optional[int], target_model: Optional[str]) -> str:
    if total_cells and total_cells > 100_000:
        return "bbknn"
    if target_model in {"scvi", "totalvi", "multivi"}:
        return "scvi"
    if n_batches <= 3:
        return "harmony"
    return "scvi"


def _batch_warnings(method: str, target_model: Optional[str]) -> List[str]:
    out = []
    if method == "scvi":
        out.append("scVI 需要 raw counts 层；不要把 log-normalized 矩阵当 counts。")
    if method == "harmony":
        out.append("Harmony 在 PCA 空间操作，适合校正 embedding/邻居图，不会生成 corrected counts。")
    if target_model:
        out.append(f"若下游要跑 {target_model}，保留未整合的 raw/counts 层与整合后的 embedding，避免覆盖输入。")
    return out


def _render_batch_script(method: str, batch_key: str, seed: int) -> str:
    common = [
        "# bio-singlecell.sc_batch_recipe — batch integration",
        "import numpy as np",
        "import scanpy as sc",
        f"np.random.seed({seed})",
        'adata = sc.read_h5ad("doublet_annotated_or_preprocessed.h5ad")',
        f"batch_key = '{batch_key}'",
        "assert batch_key in adata.obs, f'missing batch_key: {batch_key}'",
    ]
    if method == "harmony":
        body = [
            "import scanpy.external as sce",
            "sc.tl.pca(adata, svd_solver='arpack')",
            "sce.pp.harmony_integrate(adata, key=batch_key)",
            "sc.pp.neighbors(adata, use_rep='X_pca_harmony')",
        ]
    elif method == "scvi":
        body = [
            "import scvi",
            "assert 'counts' in adata.layers, 'scVI integration requires raw counts in adata.layers[\"counts\"]; do not use log-normalized X as counts'",
            "scvi.model.SCVI.setup_anndata(adata, layer='counts', batch_key=batch_key)",
            "model = scvi.model.SCVI(adata, n_latent=30)",
            "model.train(max_epochs=400)",
            "adata.obsm['X_scVI'] = model.get_latent_representation()",
            "sc.pp.neighbors(adata, use_rep='X_scVI')",
        ]
    elif method == "bbknn":
        body = [
            "import bbknn",
            "sc.tl.pca(adata, svd_solver='arpack')",
            "bbknn.bbknn(adata, batch_key=batch_key)",
        ]
    else:
        body = [
            "import scanpy.external as sce",
            "sce.pp.scanorama_integrate(adata, key=batch_key)",
            "sc.pp.neighbors(adata, use_rep='X_scanorama')",
        ]
    tail = ["sc.tl.umap(adata)", f'adata.write_h5ad("batch_integrated_{method}.h5ad")']
    return "\n".join(common + body + tail)


def _workflow_toolchain(batch_method: str, annotation_method: str, include_scfm: bool) -> List[Dict[str, Any]]:
    tools = [
        {"name": "scanpy", "kind": "python", "step": "QC, normalization, HVG, PCA/UMAP/Leiden"},
        {"name": "scrublet", "kind": "python", "step": "doublet detection"},
        {"name": "pandas", "kind": "python", "step": "QC/report/provenance tables"},
    ]
    if batch_method == "scvi":
        tools.append({"name": "scvi-tools", "kind": "python", "step": "SCVI latent integration"})
    else:
        tools.append({"name": "harmonypy", "kind": "python", "step": "Harmony batch integration"})
    if annotation_method == "celltypist":
        tools.append({"name": "celltypist", "kind": "python", "step": "reference cell-type annotation"})
    if annotation_method == "singler":
        tools.extend([
            {"name": "SingleR", "kind": "R/Bioconductor", "step": "R reference annotation"},
            {"name": "celldex", "kind": "R/Bioconductor", "step": "SingleR reference data"},
            {"name": "zellkonverter", "kind": "R/Bioconductor", "step": "H5AD/SCE interchange"},
        ])
    if include_scfm:
        tools.extend([
            {"name": "Geneformer", "kind": "python/model", "step": "foundation-model embedding adapter"},
            {"name": "scGPT", "kind": "python/model", "step": "foundation-model embedding adapter"},
        ])
    return tools


def _validate_input_cli() -> str:
    return '''#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import scanpy as sc

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--batch-key", default="")
ap.add_argument("--organism", default="human")
args = ap.parse_args()

path = Path(args.input)
if not path.is_file():
    raise SystemExit(f"input H5AD not found: {path}")

adata = sc.read_h5ad(path, backed="r")
obs_keys = list(adata.obs_keys())
var_keys = list(adata.var_keys())
layers = list(adata.layers.keys())
warnings = []
if args.batch_key and args.batch_key not in obs_keys:
    warnings.append(f"batch_key '{args.batch_key}' not found; integration will run without batch covariate if supported")
if adata.n_obs == 0 or adata.n_vars == 0:
    raise SystemExit("AnnData has zero cells or zero genes")
if "counts" not in layers:
    warnings.append("counts layer missing; QC step will preserve current X as layers['counts']; verify X is raw counts")

summary = {
    "input_h5ad": str(path),
    "n_obs": int(adata.n_obs),
    "n_vars": int(adata.n_vars),
    "organism": args.organism,
    "batch_key": args.batch_key,
    "obs_keys": obs_keys,
    "var_keys": var_keys,
    "layers": layers,
    "warnings": warnings,
}
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
'''


def _scanpy_qc_cli() -> str:
    return '''#!/usr/bin/env python3
import argparse
import json
import numpy as np
import scanpy as sc

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--metrics", required=True)
ap.add_argument("--min-genes", type=int, default=200)
ap.add_argument("--min-cells", type=int, default=3)
ap.add_argument("--max-pct-mt", type=float, default=None)
ap.add_argument("--seed", type=int, default=0)
args = ap.parse_args()
np.random.seed(args.seed)
adata = sc.read_h5ad(args.input)
if "counts" not in adata.layers:
    adata.layers["counts"] = adata.X.copy()
sc.pp.filter_cells(adata, min_genes=args.min_genes)
sc.pp.filter_genes(adata, min_cells=args.min_cells)
if "mt" not in adata.var:
    adata.var["mt"] = adata.var_names.str.upper().str.startswith(("MT-", "MT."))
sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)
if args.max_pct_mt is not None and "pct_counts_mt" in adata.obs:
    adata = adata[adata.obs["pct_counts_mt"] <= args.max_pct_mt].copy()
adata.obs[["n_genes_by_counts", "total_counts", "pct_counts_mt"]].to_csv(args.metrics, sep="\\t")
adata.uns["biocsswitch_qc"] = {
    "min_genes": args.min_genes,
    "min_cells": args.min_cells,
    "max_pct_mt": args.max_pct_mt,
    "seed": args.seed,
}
adata.write_h5ad(args.output)
'''


def _scrublet_cli() -> str:
    return '''#!/usr/bin/env python3
import argparse
import numpy as np
import scanpy as sc
import scrublet as scr
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--scores", required=True)
ap.add_argument("--histogram", required=True)
ap.add_argument("--expected-doublet-rate", type=float, default=0.06)
ap.add_argument("--seed", type=int, default=0)
args = ap.parse_args()
np.random.seed(args.seed)
adata = sc.read_h5ad(args.input)
counts = adata.layers["counts"] if "counts" in adata.layers else adata.X
scrub = scr.Scrublet(counts, expected_doublet_rate=args.expected_doublet_rate)
doublet_scores, predicted_doublets = scrub.scrub_doublets()
adata.obs["doublet_score"] = doublet_scores
adata.obs["predicted_doublet"] = predicted_doublets
adata.obs[["doublet_score", "predicted_doublet"]].to_csv(args.scores, sep="\\t")
try:
    import matplotlib.pyplot as plt
    plt.hist(doublet_scores, bins=60)
    plt.xlabel("Scrublet doublet score")
    plt.ylabel("Cells")
    plt.savefig(args.histogram, dpi=180, bbox_inches="tight")
except Exception as exc:
    Path(args.histogram).write_text(f"histogram skipped: {exc}\\n", encoding="utf-8")
adata = adata[~adata.obs["predicted_doublet"]].copy()
adata.write_h5ad(args.output)
'''


def _scvi_cli(batch_key: str) -> str:
    return f'''#!/usr/bin/env python3
import argparse
import numpy as np
import scanpy as sc
import scvi

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--latent-key", default="X_scVI")
ap.add_argument("--batch-key", default="{batch_key}")
ap.add_argument("--seed", type=int, default=0)
args = ap.parse_args()
np.random.seed(args.seed)
scvi.settings.seed = args.seed
adata = sc.read_h5ad(args.input)
assert "counts" in adata.layers, "scVI requires raw counts in adata.layers['counts']"
setup_kwargs = {{"layer": "counts"}}
if args.batch_key and args.batch_key in adata.obs:
    setup_kwargs["batch_key"] = args.batch_key
scvi.model.SCVI.setup_anndata(adata, **setup_kwargs)
model = scvi.model.SCVI(adata, n_latent=30)
model.train(max_epochs=400)
adata.obsm[args.latent_key] = model.get_latent_representation()
sc.pp.neighbors(adata, use_rep=args.latent_key)
sc.tl.umap(adata)
sc.tl.leiden(adata, key_added="leiden")
adata.write_h5ad(args.output)
'''


def _harmony_cli(batch_key: str) -> str:
    return f'''#!/usr/bin/env python3
import argparse
import numpy as np
import scanpy as sc
import scanpy.external as sce

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--batch-key", default="{batch_key}")
ap.add_argument("--seed", type=int, default=0)
args = ap.parse_args()
np.random.seed(args.seed)
adata = sc.read_h5ad(args.input)
assert args.batch_key in adata.obs, f"missing batch key: {{args.batch_key}}"
if "counts" not in adata.layers:
    adata.layers["counts"] = adata.X.copy()
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
sc.pp.highly_variable_genes(adata, n_top_genes=3000, flavor="seurat_v3", layer="counts")
adata = adata[:, adata.var["highly_variable"]].copy()
sc.pp.scale(adata, max_value=10)
sc.tl.pca(adata, svd_solver="arpack")
sce.pp.harmony_integrate(adata, key=args.batch_key)
sc.pp.neighbors(adata, use_rep="X_pca_harmony")
sc.tl.umap(adata)
sc.tl.leiden(adata, key_added="leiden")
adata.write_h5ad(args.output)
'''


def _celltypist_cli(organism: str) -> str:
    default_model = "Immune_All_Low.pkl" if organism == "human" else "Mouse_Whole_Brain.pkl"
    return f'''#!/usr/bin/env python3
import argparse
import scanpy as sc
import celltypist
from celltypist import models

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--predictions", required=True)
ap.add_argument("--model", default="{default_model}")
args = ap.parse_args()
adata = sc.read_h5ad(args.input)
models.download_models(force_update=False)
pred = celltypist.annotate(adata, model=args.model, majority_voting=True)
adata = pred.to_adata()
cols = [c for c in ("majority_voting", "predicted_labels", "conf_score") if c in adata.obs]
adata.obs[cols].to_csv(args.predictions, sep="\\t")
adata.write_h5ad(args.output)
'''


def _singler_cli(organism: str) -> str:
    ref = "HumanPrimaryCellAtlasData" if organism == "human" else "MouseRNAseqData"
    return f'''#!/usr/bin/env Rscript
suppressPackageStartupMessages({{
  library(SingleR)
  library(SingleCellExperiment)
  library(zellkonverter)
  library(celldex)
}})
args <- commandArgs(trailingOnly=TRUE)
infile <- args[[1]]
outfile <- args[[2]]
predfile <- args[[3]]
sce <- readH5AD(infile)
ref <- celldex::{ref}()
pred <- SingleR(test=sce, ref=ref, labels=ref$label.main)
sce$SingleR_label <- pred$labels
write.table(as.data.frame(pred), predfile, sep="\\t", quote=FALSE)
writeH5AD(sce, outfile)
'''


def _scfm_adapter_cli() -> str:
    return '''#!/usr/bin/env python3
"""Adapter slot for Geneformer/scGPT embeddings.

This is a real workflow step: it reads a configured model family and writes the
embedding back to AnnData. The exact package APIs move quickly, so the adapter
fails closed until model_dir/checkpoint_dir is pinned in config.yaml.
"""
import argparse
import sys
import scanpy as sc

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--output", required=True)
ap.add_argument("--model", choices=["geneformer", "scgpt"], required=True)
ap.add_argument("--model-dir", required=True)
ap.add_argument("--embedding-key", default=None)
args = ap.parse_args()
if args.model_dir in {"", "<FILL>", "PATH_TO_MODEL"}:
    raise SystemExit("Pin --model-dir/checkpoint first; do not fabricate scFM embeddings.")
adata = sc.read_h5ad(args.input)
key = args.embedding_key or ("X_geneformer" if args.model == "geneformer" else "X_scgpt")
if args.model == "geneformer":
    from geneformer import EmbExtractor  # type: ignore
    # Expected input: Geneformer-tokenized dataset or an adapter that tokenizes
    # this AnnData with Ensembl IDs. Keep raw AnnData hash in provenance.
    extractor = EmbExtractor(model_type="CellClassifier", model_directory=args.model_dir)
    emb = extractor.extract_embs(adata)  # project adapter may override this call
elif args.model == "scgpt":
    import torch  # noqa: F401
    from scgpt.tasks import embed_data  # type: ignore
    emb = embed_data(adata, model_dir=args.model_dir)
adata.obsm[key] = emb
adata.write_h5ad(args.output)
'''


def _qc_report_cli() -> str:
    return '''#!/usr/bin/env python3
import argparse
from pathlib import Path
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--metrics", required=True)
ap.add_argument("--doublets", required=True)
ap.add_argument("--final-h5ad", required=True)
ap.add_argument("--output", required=True)
args = ap.parse_args()

metrics = pd.read_csv(args.metrics, sep="\\t", index_col=0)
doublets = pd.read_csv(args.doublets, sep="\\t", index_col=0)
doublet_rate = float(doublets["predicted_doublet"].mean()) if "predicted_doublet" in doublets else 0.0
lines = [
    "# BioCSSwitch Single-Cell QC Report",
    "",
    f"Final AnnData: `{args.final_h5ad}`",
    f"Cells after QC step: {len(metrics)}",
    f"Median genes/cell: {metrics['n_genes_by_counts'].median():.2f}" if "n_genes_by_counts" in metrics else "Median genes/cell: n/a",
    f"Median total counts/cell: {metrics['total_counts'].median():.2f}" if "total_counts" in metrics else "Median total counts/cell: n/a",
    f"Median mitochondrial percent: {metrics['pct_counts_mt'].median():.2f}" if "pct_counts_mt" in metrics else "Median mitochondrial percent: n/a",
    f"Predicted doublet fraction: {doublet_rate:.4f}",
    "",
    "Review this report before treating clusters, markers, or annotations as biological claims.",
]
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text("\\n".join(lines) + "\\n", encoding="utf-8")
'''


def _provenance_writer_cli() -> str:
    return '''#!/usr/bin/env python3
import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

def sha256_file(path):
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()

ap = argparse.ArgumentParser()
ap.add_argument("--engine", required=True)
ap.add_argument("--recipe-hash", required=True)
ap.add_argument("--input-h5ad", required=True)
ap.add_argument("--final-h5ad", required=True)
ap.add_argument("--config", required=True)
ap.add_argument("--validation-json", required=True)
ap.add_argument("--qc-report", required=True)
ap.add_argument("--output", required=True)
args = ap.parse_args()

record = {
    "schema": "bio-singlecell/workflow-provenance/1",
    "engine": args.engine,
    "recipe_hash": args.recipe_hash,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "python": sys.version,
    "platform": platform.platform(),
    "inputs": {
        "input_h5ad": args.input_h5ad,
        "input_h5ad_sha256": sha256_file(args.input_h5ad),
        "config_sha256": sha256_file(args.config),
        "validation_json_sha256": sha256_file(args.validation_json),
    },
    "outputs": {
        "final_h5ad": args.final_h5ad,
        "final_h5ad_sha256": sha256_file(args.final_h5ad),
        "qc_report": args.qc_report,
        "qc_report_sha256": sha256_file(args.qc_report),
    },
}
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
'''


def _workflow_envs() -> Dict[str, str]:
    return {
        "envs/scanpy.yaml": """channels: [conda-forge, bioconda]
dependencies:
  - python>=3.10
  - scanpy
  - anndata
  - numpy
  - scipy
  - pandas
  - leidenalg
  - igraph
  - pip
""",
        "envs/scrublet.yaml": """channels: [conda-forge, bioconda]
dependencies:
  - python>=3.10
  - scanpy
  - scrublet
  - matplotlib
""",
        "envs/scvi.yaml": """channels: [conda-forge, bioconda, pytorch]
dependencies:
  - python>=3.10
  - scanpy
  - scvi-tools
  - pytorch
  - pip
""",
        "envs/harmony.yaml": """channels: [conda-forge, bioconda]
dependencies:
  - python>=3.10
  - scanpy
  - harmonypy
  - pandas
  - leidenalg
  - igraph
  - pip
""",
        "envs/celltypist.yaml": """channels: [conda-forge, bioconda]
dependencies:
  - python>=3.10
  - scanpy
  - celltypist
""",
        "envs/bioconductor.yaml": """channels: [conda-forge, bioconda]
dependencies:
  - r-base
  - bioconductor-singler
  - bioconductor-singlecellexperiment
  - bioconductor-zellkonverter
  - bioconductor-celldex
  - bioconductor-scdblfinder
""",
        "envs/scfm.yaml": """channels: [conda-forge, bioconda, pytorch]
dependencies:
  - python>=3.10
  - scanpy
  - pytorch
  - pip
  - pip:
      - geneformer
      - scgpt
""",
    }


def _workflow_manifest(engine: str, params: Dict[str, Any], outputs: List[str]) -> str:
    return json.dumps(
        {
            "schema": "bio-singlecell/workflow-package/1",
            "engine": engine,
            "recipe_hash": params.get("recipe_hash"),
            "input_h5ad": params["input_h5ad"],
            "batch_method": params["batch_method"],
            "annotation_method": params["annotation_method"],
            "include_scfm": params["include_scfm"],
            "outputs": outputs,
            "entrypoints": {
                "snakemake": "snakemake --use-conda --cores 8",
                "nextflow": "nextflow run main.nf -profile conda",
            },
        },
        indent=2,
        sort_keys=True,
    )


def _workflow_readme(engine: str, params: Dict[str, Any], command: str, dry_run: str, outputs: List[str]) -> str:
    scfm_note = (
        "scFM embedding is enabled. Set scfm.model_dir/scfm_model_dir to a pinned local checkpoint before running."
        if params["include_scfm"]
        else "scFM embedding is disabled; final output is the annotated H5AD."
    )
    return f"""# BioCSSwitch single-cell workflow package

Engine: {engine}
Recipe hash: {params.get('recipe_hash')}

## Run

1. Put your AnnData file at `{params['input_h5ad']}` or edit the config file.
2. Review `batch_key`, `expected_doublet_rate`, and annotation settings.
3. Dry run:

```bash
{dry_run}
```

4. Execute:

```bash
{command}
```

## Main outputs

{chr(10).join(f"- `{o}`" for o in outputs)}

## Notes

- The workflow starts from an H5AD object. For FASTQ/barcode inputs, run nf-core/scrnaseq first and feed the produced count matrix or H5AD here.
- `counts` is preserved in `adata.layers['counts']`; verify that the input matrix is raw counts before using scVI or Scrublet.
- {scfm_note}
- For PHI or clinical file paths, run the BioCSSwitch privacy pack before sharing configs or logs.
"""


def _render_snakemake_workflow(params: Dict[str, Any]) -> Dict[str, str]:
    batch_script = "scripts/integrate_scvi.py" if params["batch_method"] == "scvi" else "scripts/integrate_harmony.py"
    batch_env = "envs/scvi.yaml" if params["batch_method"] == "scvi" else "envs/harmony.yaml"
    annotation_rule = "annotate_celltypist" if params["annotation_method"] == "celltypist" else "annotate_singler"
    annotation_predictions = "results/celltypist_predictions.tsv" if params["annotation_method"] == "celltypist" else "results/singler_predictions.tsv"
    final_output = "results/scfm_embedded.h5ad" if params["include_scfm"] else "results/celltype_annotated.h5ad"
    outputs = [
        final_output,
        annotation_predictions,
        "results/qc_metrics.tsv",
        "results/doublet_scores.tsv",
        "results/doublet_score_histogram.png",
        "results/qc_report.md",
        "results/provenance.json",
    ]
    command = "snakemake --use-conda --cores 8"
    dry_run = "snakemake --use-conda --cores 1 -n"
    annotation_block = '''rule annotate_celltypist:
    input: OUTDIR + "/batch_integrated.h5ad"
    output:
        h5ad=OUTDIR + "/celltype_annotated.h5ad",
        predictions=OUTDIR + "/celltypist_predictions.tsv"
    params:
        model=config["celltypist_model"]
    conda: "envs/celltypist.yaml"
    shell:
        "python scripts/annotate_celltypist.py --input {input:q} --output {output.h5ad:q} --predictions {output.predictions:q} --model {params.model:q}"
''' if params["annotation_method"] == "celltypist" else '''rule annotate_singler:
    input: OUTDIR + "/batch_integrated.h5ad"
    output:
        h5ad=OUTDIR + "/celltype_annotated.h5ad",
        predictions=OUTDIR + "/singler_predictions.tsv"
    conda: "envs/bioconductor.yaml"
    shell:
        "Rscript scripts/annotate_singler.R {input:q} {output.h5ad:q} {output.predictions:q}"
'''
    scfm_block = '''rule scfm_embedding:
    input: OUTDIR + "/celltype_annotated.h5ad"
    output: OUTDIR + "/scfm_embedded.h5ad"
    params:
        model=config["scfm_model"],
        model_dir=config["scfm_model_dir"]
    conda: "envs/scfm.yaml"
    shell:
        "python scripts/scfm_embed_adapter.py --input {input:q} --output {output:q} --model {params.model:q} --model-dir {params.model_dir:q}"
''' if params["include_scfm"] else ""
    files = {
        "README.md": _workflow_readme("snakemake", params, command, dry_run, outputs),
        "workflow.manifest.json": _workflow_manifest("snakemake", params, outputs),
        "config.yaml": f"""recipe_hash: {params['recipe_hash']}
input_h5ad: {params['input_h5ad']}
outdir: results
seed: {params['seed']}
organism: {params['organism']}
batch_key: {params['batch_key']}
expected_doublet_rate: {params['expected_doublet_rate']}
qc_min_genes: 200
qc_min_cells: 3
qc_max_pct_mt: 20
scvi_max_epochs: 400
scvi_n_latent: 30
celltypist_model: {'Immune_All_Low.pkl' if params['organism'] == 'human' else 'Mouse_Whole_Brain.pkl'}
singler_reference: {'HumanPrimaryCellAtlasData' if params['organism'] == 'human' else 'MouseRNAseqData'}
enable_scfm: {str(params['include_scfm']).lower()}
scfm_model: {params['scfm_model']}
scfm_model_dir: {params['scfm_model_dir']}
""",
        "Snakefile": f'''configfile: "config.yaml"

OUTDIR = config.get("outdir", "results")
FINAL_H5AD = OUTDIR + "/{"scfm_embedded" if params["include_scfm"] else "celltype_annotated"}.h5ad"
ANNOTATION_PREDICTIONS = OUTDIR + "/{"celltypist" if params["annotation_method"] == "celltypist" else "singler"}_predictions.tsv"

rule all:
    input:
        FINAL_H5AD,
        ANNOTATION_PREDICTIONS,
        OUTDIR + "/qc_metrics.tsv",
        OUTDIR + "/doublet_scores.tsv",
        OUTDIR + "/doublet_score_histogram.png",
        OUTDIR + "/qc_report.md",
        OUTDIR + "/provenance.json"

rule validate_input:
    input: config["input_h5ad"]
    output: OUTDIR + "/input_validation.json"
    params:
        batch_key=config["batch_key"],
        organism=config["organism"]
    conda: "envs/scanpy.yaml"
    shell:
        "python scripts/validate_input.py --input {{input:q}} --output {{output:q}} --batch-key {{params.batch_key:q}} --organism {{params.organism:q}}"

rule qc_scanpy:
    input:
        h5ad=config["input_h5ad"],
        validation=OUTDIR + "/input_validation.json"
    output:
        h5ad=OUTDIR + "/qc_filtered.h5ad",
        metrics=OUTDIR + "/qc_metrics.tsv"
    conda: "envs/scanpy.yaml"
    shell:
        "python scripts/qc_scanpy.py --input {{input.h5ad:q}} --output {{output.h5ad:q}} --metrics {{output.metrics:q}} --min-genes {{config[qc_min_genes]}} --min-cells {{config[qc_min_cells]}} --max-pct-mt {{config[qc_max_pct_mt]}} --seed {{config[seed]}}"

rule doublet_scrublet:
    input: OUTDIR + "/qc_filtered.h5ad"
    output:
        h5ad=OUTDIR + "/doublet_filtered.h5ad",
        scores=OUTDIR + "/doublet_scores.tsv",
        histogram=OUTDIR + "/doublet_score_histogram.png"
    conda: "envs/scrublet.yaml"
    shell:
        "python scripts/doublet_scrublet.py --input {{input:q}} --output {{output.h5ad:q}} --scores {{output.scores:q}} --histogram {{output.histogram:q}} --expected-doublet-rate {{config[expected_doublet_rate]}} --seed {{config[seed]}}"

rule batch_integrate:
    input: OUTDIR + "/doublet_filtered.h5ad"
    output: OUTDIR + "/batch_integrated.h5ad"
    params:
        batch_key=config["batch_key"]
    conda: "{batch_env}"
    shell:
        "python {batch_script} --input {{input:q}} --output {{output:q}} --batch-key {{params.batch_key:q}} --seed {{config[seed]}}"

{annotation_block}
{scfm_block}

rule qc_report:
    input:
        metrics=OUTDIR + "/qc_metrics.tsv",
        doublets=OUTDIR + "/doublet_scores.tsv",
        final=FINAL_H5AD
    output: OUTDIR + "/qc_report.md"
    conda: "envs/scanpy.yaml"
    shell:
        "python scripts/qc_report.py --metrics {{input.metrics:q}} --doublets {{input.doublets:q}} --final-h5ad {{input.final:q}} --output {{output:q}}"

rule provenance:
    input:
        final=FINAL_H5AD,
        validation=OUTDIR + "/input_validation.json",
        report=OUTDIR + "/qc_report.md"
    output: OUTDIR + "/provenance.json"
    params:
        recipe_hash=config["recipe_hash"],
        input_h5ad=config["input_h5ad"]
    conda: "envs/scanpy.yaml"
    shell:
        "python scripts/write_provenance.py --engine snakemake --recipe-hash {{params.recipe_hash:q}} --input-h5ad {{params.input_h5ad:q}} --final-h5ad {{input.final:q}} --config config.yaml --validation-json {{input.validation:q}} --qc-report {{input.report:q}} --output {{output:q}}"
''',
        "scripts/validate_input.py": _validate_input_cli(),
        "scripts/qc_scanpy.py": _scanpy_qc_cli(),
        "scripts/doublet_scrublet.py": _scrublet_cli(),
        "scripts/integrate_scvi.py": _scvi_cli(params["batch_key"]),
        "scripts/integrate_harmony.py": _harmony_cli(params["batch_key"]),
        "scripts/annotate_celltypist.py": _celltypist_cli(params["organism"]),
        "scripts/annotate_singler.R": _singler_cli(params["organism"]),
        "scripts/scfm_embed_adapter.py": _scfm_adapter_cli(),
        "scripts/qc_report.py": _qc_report_cli(),
        "scripts/write_provenance.py": _provenance_writer_cli(),
    }
    files.update(_workflow_envs())
    return files


def _render_nextflow_workflow(params: Dict[str, Any]) -> Dict[str, str]:
    integrate_script = "integrate_scvi.py" if params["batch_method"] == "scvi" else "integrate_harmony.py"
    integrate_env = "envs/scvi.yaml" if params["batch_method"] == "scvi" else "envs/harmony.yaml"
    annotation_process = "ANNOTATE_CELLTYPIST" if params["annotation_method"] == "celltypist" else "ANNOTATE_SINGLER"
    annotation_predictions = "celltypist_predictions.tsv" if params["annotation_method"] == "celltypist" else "singler_predictions.tsv"
    final_output = "results/scfm_embedded.h5ad" if params["include_scfm"] else "results/celltype_annotated.h5ad"
    outputs = [
        final_output,
        f"results/{annotation_predictions}",
        "results/qc_metrics.tsv",
        "results/doublet_scores.tsv",
        "results/doublet_score_histogram.png",
        "results/qc_report.md",
        "results/provenance.json",
    ]
    command = "nextflow run main.nf -profile conda --input_h5ad data/input.h5ad"
    dry_run = "nextflow run main.nf -profile conda --input_h5ad data/input.h5ad -preview"
    annotation_block = '''process ANNOTATE_CELLTYPIST {
  conda "envs/celltypist.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "celltype_annotated.h5ad", emit: h5ad
    path "celltypist_predictions.tsv", emit: predictions
  script:
  """
  python scripts/annotate_celltypist.py --input $h5ad --output celltype_annotated.h5ad --predictions celltypist_predictions.tsv --model "${params.celltypist_model}"
  """
}
''' if params["annotation_method"] == "celltypist" else '''process ANNOTATE_SINGLER {
  conda "envs/bioconductor.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "celltype_annotated.h5ad", emit: h5ad
    path "singler_predictions.tsv", emit: predictions
  script:
  """
  Rscript scripts/annotate_singler.R $h5ad celltype_annotated.h5ad singler_predictions.tsv
  """
}
'''
    scfm_process = '''process SCFM_EMBED {
  conda "envs/scfm.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "scfm_embedded.h5ad", emit: h5ad
  script:
  """
  python scripts/scfm_embed_adapter.py --input $h5ad --output scfm_embedded.h5ad --model "${params.scfm_model}" --model-dir "${params.scfm_model_dir}"
  """
}
''' if params["include_scfm"] else ""
    scfm_workflow = """  embedded = SCFM_EMBED(annotated.h5ad)
  final_h5ad = embedded.h5ad
""" if params["include_scfm"] else """  final_h5ad = annotated.h5ad
"""
    files = {
        "README.md": _workflow_readme("nextflow", params, command, dry_run, outputs),
        "workflow.manifest.json": _workflow_manifest("nextflow", params, outputs),
        "nextflow.config": f"""nextflow.enable.dsl = 2
params.input_h5ad = "{params['input_h5ad']}"
params.outdir = "results"
params.seed = {params['seed']}
params.organism = "{params['organism']}"
params.batch_key = "{params['batch_key']}"
params.expected_doublet_rate = {params['expected_doublet_rate']}
params.qc_min_genes = 200
params.qc_min_cells = 3
params.qc_max_pct_mt = 20
params.celltypist_model = "{'Immune_All_Low.pkl' if params['organism'] == 'human' else 'Mouse_Whole_Brain.pkl'}"
params.singler_reference = "{'HumanPrimaryCellAtlasData' if params['organism'] == 'human' else 'MouseRNAseqData'}"
params.enable_scfm = {str(params['include_scfm']).lower()}
params.scfm_model = "{params['scfm_model']}"
params.scfm_model_dir = "{params['scfm_model_dir']}"
conda.enabled = true

profiles {{
  conda {{
    conda.enabled = true
  }}
}}
""",
        "main.nf": f'''process VALIDATE_INPUT {{
  conda "envs/scanpy.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "input_validation.json", emit: json
  script:
  """
  python scripts/validate_input.py --input $h5ad --output input_validation.json --batch-key "${{params.batch_key}}" --organism "${{params.organism}}"
  """
}}

process QC_SCANPY {{
  conda "envs/scanpy.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
    path validation_json
  output:
    path "qc_filtered.h5ad", emit: h5ad
    path "qc_metrics.tsv", emit: metrics
  script:
  """
  python scripts/qc_scanpy.py --input $h5ad --output qc_filtered.h5ad --metrics qc_metrics.tsv --min-genes ${{params.qc_min_genes}} --min-cells ${{params.qc_min_cells}} --max-pct-mt ${{params.qc_max_pct_mt}} --seed ${{params.seed}}
  """
}}

process DOUBLET_SCRUBLET {{
  conda "envs/scrublet.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "doublet_filtered.h5ad", emit: h5ad
    path "doublet_scores.tsv", emit: scores
    path "doublet_score_histogram.png", emit: histogram
  script:
  """
  python scripts/doublet_scrublet.py --input $h5ad --output doublet_filtered.h5ad --scores doublet_scores.tsv --histogram doublet_score_histogram.png --expected-doublet-rate ${{params.expected_doublet_rate}} --seed ${{params.seed}}
  """
}}

process BATCH_INTEGRATE {{
  conda "{integrate_env}"
  publishDir params.outdir, mode: 'copy'
  input:
    path h5ad
  output:
    path "batch_integrated.h5ad", emit: h5ad
  script:
  """
  python scripts/{integrate_script} --input $h5ad --output batch_integrated.h5ad --batch-key "${{params.batch_key}}" --seed ${{params.seed}}
  """
}}

{annotation_block}
{scfm_process}

process QC_REPORT {{
  conda "envs/scanpy.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path metrics
    path doublets
    path final_h5ad
  output:
    path "qc_report.md", emit: report
  script:
  """
  python scripts/qc_report.py --metrics $metrics --doublets $doublets --final-h5ad $final_h5ad --output qc_report.md
  """
}}

process WRITE_PROVENANCE {{
  conda "envs/scanpy.yaml"
  publishDir params.outdir, mode: 'copy'
  input:
    path final_h5ad
    path validation_json
    path qc_report
  output:
    path "provenance.json", emit: provenance
  script:
  """
  python scripts/write_provenance.py --engine nextflow --recipe-hash "{params['recipe_hash']}" --input-h5ad "${{params.input_h5ad}}" --final-h5ad $final_h5ad --config nextflow.config --validation-json $validation_json --qc-report $qc_report --output provenance.json
  """
}}

workflow {{
  input_ch = Channel.fromPath(params.input_h5ad, checkIfExists: true)
  validation = VALIDATE_INPUT(input_ch)
  qc = QC_SCANPY(input_ch, validation.json)
  filtered = DOUBLET_SCRUBLET(qc.h5ad)
  integrated = BATCH_INTEGRATE(filtered.h5ad)
  annotated = {annotation_process}(integrated.h5ad)
{scfm_workflow}  report = QC_REPORT(qc.metrics, filtered.scores, final_h5ad)
  WRITE_PROVENANCE(final_h5ad, validation.json, report.report)
}}
''',
        "scripts/validate_input.py": _validate_input_cli(),
        "scripts/qc_scanpy.py": _scanpy_qc_cli(),
        "scripts/doublet_scrublet.py": _scrublet_cli(),
        "scripts/integrate_scvi.py": _scvi_cli(params["batch_key"]),
        "scripts/integrate_harmony.py": _harmony_cli(params["batch_key"]),
        "scripts/annotate_celltypist.py": _celltypist_cli(params["organism"]),
        "scripts/annotate_singler.R": _singler_cli(params["organism"]),
        "scripts/scfm_embed_adapter.py": _scfm_adapter_cli(),
        "scripts/qc_report.py": _qc_report_cli(),
        "scripts/write_provenance.py": _provenance_writer_cli(),
    }
    files.update(_workflow_envs())
    return files


@server.tool(
    "sc_workflow_recipe",
    "Generate a runnable workflow package instead of a loose Python skeleton. Supports Snakemake or Nextflow "
    "for AnnData/H5AD downstream analysis, with scanpy QC, Scrublet doublet detection, scVI/Harmony batch "
    "integration, CellTypist/SingleR annotation, and an explicit Geneformer/scGPT adapter slot. Also returns "
    "an nf-core/scrnaseq handoff for raw FASTQ inputs.",
    {
        "type": "object",
        "properties": {
            "engine": {"type": "string", "enum": ["snakemake", "nextflow"], "default": "snakemake"},
            "input_h5ad": {"type": "string", "default": "data/input.h5ad"},
            "organism": {"type": "string", "enum": ["human", "mouse"], "default": "human"},
            "batch_key": {"type": "string", "default": "batch"},
            "batch_method": {"type": "string", "enum": ["scvi", "harmony"], "default": "scvi"},
            "annotation_method": {"type": "string", "enum": ["celltypist", "singler"], "default": "celltypist"},
            "include_scfm": {"type": "boolean", "default": True},
            "scfm_model": {"type": "string", "enum": ["geneformer", "scgpt"], "default": "geneformer"},
            "scfm_model_dir": {"type": "string", "default": "PATH_TO_MODEL"},
            "expected_doublet_rate": {"type": "number", "default": 0.06},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_workflow_recipe(
    engine: str = "snakemake",
    input_h5ad: str = "data/input.h5ad",
    organism: str = "human",
    batch_key: str = "batch",
    batch_method: str = "scvi",
    annotation_method: str = "celltypist",
    include_scfm: bool = True,
    scfm_model: str = "geneformer",
    scfm_model_dir: str = "PATH_TO_MODEL",
    expected_doublet_rate: float = 0.06,
    seed: int = 0,
):
    params = {
        "engine": engine,
        "input_h5ad": input_h5ad,
        "organism": organism,
        "batch_key": batch_key,
        "batch_method": batch_method,
        "annotation_method": annotation_method,
        "include_scfm": include_scfm,
        "scfm_model": scfm_model,
        "scfm_model_dir": scfm_model_dir,
        "expected_doublet_rate": expected_doublet_rate,
        "seed": seed,
    }
    recipe_hash = _recipe_hash("sc_workflow_recipe", params)
    params["recipe_hash"] = recipe_hash
    files = _render_nextflow_workflow(params) if engine == "nextflow" else _render_snakemake_workflow(params)
    annotation_predictions = "celltypist_predictions.tsv" if annotation_method == "celltypist" else "singler_predictions.tsv"
    outputs = [
        "results/scfm_embedded.h5ad" if include_scfm else "results/celltype_annotated.h5ad",
        f"results/{annotation_predictions}",
        "results/qc_metrics.tsv",
        "results/doublet_scores.tsv",
        "results/doublet_score_histogram.png",
        "results/qc_report.md",
        "results/provenance.json",
    ]
    command = (
        f"nextflow run main.nf -profile conda --input_h5ad {input_h5ad}"
        if engine == "nextflow"
        else "snakemake --use-conda --cores 8"
    )
    dry_run_command = (
        f"nextflow run main.nf -profile conda --input_h5ad {input_h5ad} -preview"
        if engine == "nextflow"
        else "snakemake --use-conda --cores 1 -n"
    )
    configuration_required = []
    if include_scfm and scfm_model_dir in {"", "<FILL>", "PATH_TO_MODEL"}:
        configuration_required.append("set scfm_model_dir / params.scfm_model_dir to a pinned local Geneformer or scGPT checkpoint")
    runnable = not configuration_required
    return {
        "recipe_hash": recipe_hash,
        "artifact_type": "runnable_workflow" if runnable else "workflow_package",
        "runnable": runnable,
        "engine": engine,
        "params": params,
        "command": command,
        "dry_run_command": dry_run_command,
        "configuration_required": configuration_required,
        "outputs": outputs,
        "files": files,
        "toolchain": _workflow_toolchain(batch_method, annotation_method, include_scfm),
        "nf_core_fastq_handoff": {
            "pipeline": "nf-core/scrnaseq",
            "when": "Use for raw FASTQ/barcode processing before this H5AD downstream workflow.",
            "command": "nextflow run nf-core/scrnaseq -profile docker --input samplesheet.csv --fasta genome.fa --gtf genes.gtf --protocol 10XV3 --aligner star --outdir nfcore_out",
            "samplesheet_columns": ["sample", "fastq_1", "fastq_2", "expected_cells"],
        },
        "provenance_skeleton": _provenance_skeleton("sc_workflow_recipe", recipe_hash, {"workflow": params}),
        "warnings": (
            ["scFM adapter fails closed until model_dir/checkpoint is pinned; do not fabricate embeddings."]
            if include_scfm else []
        ) + [
            "For clinical/PHI data, run bio-privacy before writing file paths into shared workflow configs.",
        ],
    }


@server.tool(
    "sc_geneid_convert",
    "Generate a gene ID conversion guide/script for symbol, Ensembl, and Entrez IDs. Includes multi-mapping warnings and bio-gene handoff.",
    {
        "type": "object",
        "properties": {
            "source_id_type": {"type": "string", "enum": ["symbol", "ensembl", "entrez"]},
            "target_id_type": {"type": "string", "enum": ["symbol", "ensembl", "entrez"]},
            "organism": {"type": "string", "enum": ["human", "mouse"], "default": "human"},
            "strategy": {"type": "string", "enum": ["mygene", "biomart", "hgnc_file"], "default": "mygene"},
        },
        "required": ["source_id_type", "target_id_type"],
    },
)
def sc_geneid_convert(source_id_type: str, target_id_type: str, organism: str = "human", strategy: str = "mygene"):
    params = {"source_id_type": source_id_type, "target_id_type": target_id_type, "organism": organism, "strategy": strategy}
    recipe_hash = _recipe_hash("sc_geneid_convert", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": _render_geneid_script(source_id_type, target_id_type, organism, strategy),
        "pitfalls": [
            "一对多 / 多对一映射必须保留 audit 表，不要静默丢弃。",
            "Ensembl 版本后缀（如 ENSG... .12）应先剥离再映射，同时记录原始 ID。",
            "deprecated symbols / alias 可能导致错误合并；人类基因可用 HGNC current symbol 表核对。",
        ],
        "bio_gene_handoff": "个别关键基因可调用 bio-gene 的 ensembl_lookup_by_symbol / ensembl_lookup_by_id 核实。",
        "provenance_skeleton": _provenance_skeleton("sc_geneid_convert", recipe_hash, {"gene_id_mapping": params}),
    }


def _render_geneid_script(source: str, target: str, organism: str, strategy: str) -> str:
    species = "human" if organism == "human" else "mouse"
    if strategy == "hgnc_file":
        return f'''# HGNC 文件映射方案（无 API，适合可复现归档）
import pandas as pd
ids = pd.read_csv("gene_ids.tsv", sep="\\t")  # column: gene_id
hgnc = pd.read_csv("hgnc_complete_set.txt", sep="\\t", dtype=str)
hgnc["ensembl_gene_id"] = hgnc["ensembl_gene_id"].str.replace(r"\\.\\d+$", "", regex=True)
mapping = hgnc[["symbol", "ensembl_gene_id", "entrez_id", "prev_symbol", "alias_symbol"]].copy()
# TODO: map {source} -> {target}; 保留 unmatched 与 multi-mapping 审计表
'''
    if strategy == "biomart":
        return f'''# pybiomart 映射方案：{source} -> {target} ({species})
import pandas as pd
from pybiomart import Server
ids = pd.read_csv("gene_ids.tsv", sep="\\t")["gene_id"].astype(str)
server = Server(host="http://www.ensembl.org")
dataset = server.marts["ENSEMBL_MART_ENSEMBL"].datasets["{'hsapiens_gene_ensembl' if species == 'human' else 'mmusculus_gene_ensembl'}"]
attrs = ["ensembl_gene_id", "external_gene_name", "entrezgene_id"]
bm = dataset.query(attributes=attrs)
# TODO: normalize columns and merge; 输出 mapped.tsv / unmapped.tsv / multimapped.tsv
'''
    return f'''# mygene 映射方案：{source} -> {target} ({species})
import pandas as pd
import mygene
mg = mygene.MyGeneInfo()
ids = pd.read_csv("gene_ids.tsv", sep="\\t")["gene_id"].astype(str).str.replace(r"\\.\\d+$", "", regex=True)
scope = {{"symbol": "symbol", "ensembl": "ensembl.gene", "entrez": "entrezgene"}}["{source}"]
field = {{"symbol": "symbol", "ensembl": "ensembl.gene", "entrez": "entrezgene"}}["{target}"]
res = mg.querymany(ids.tolist(), scopes=scope, fields=field + ",symbol,entrezgene,ensembl.gene", species="{species}", as_dataframe=True)
res.to_csv("gene_id_mapping_audit.tsv", sep="\\t")
# 检查 duplicated query / notfound / 多对多映射后，再写回 adata.var
'''


@server.tool(
    "sc_celltype_recipe",
    "Generate a cell-type annotation recipe for CellTypist, SingleR, or marker-based scoring. Includes QC visualizations.",
    {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["celltypist", "singler", "marker_based"], "default": "celltypist"},
            "organism": {"type": "string", "enum": ["human", "mouse"], "default": "human"},
            "tissue": {"type": "string"},
            "reference_dataset": {"type": "string"},
            "embedding_key": {"type": "string", "default": "X_umap"},
        },
    },
)
def sc_celltype_recipe(
    method: str = "celltypist",
    organism: str = "human",
    tissue: Optional[str] = None,
    reference_dataset: Optional[str] = None,
    embedding_key: str = "X_umap",
):
    ref = reference_dataset or _default_celltype_reference(method, organism, tissue)
    params = {"method": method, "organism": organism, "tissue": tissue, "reference_dataset": ref, "embedding_key": embedding_key}
    recipe_hash = _recipe_hash("sc_celltype_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "recommended_reference": ref,
        "script": _render_celltype_script(method, organism, ref, embedding_key),
        "qc_outputs": ["confidence score distribution", "UMAP colored by predicted cell type", "confusion/cross-tab by cluster"],
        "cellxgene_hint": "可用 bio-sc-atlas 的 cellxgene_search 按 tissue/organism/disease 找参考数据集。",
        "provenance_skeleton": _provenance_skeleton("sc_celltype_recipe", recipe_hash, {"celltype_annotation": params}),
    }


def _default_celltype_reference(method: str, organism: str, tissue: Optional[str]) -> str:
    t = (tissue or "").lower()
    if method == "celltypist":
        if "blood" in t or "pbmc" in t or "immune" in t:
            return "Immune_All_Low.pkl"
        if "lung" in t:
            return "Healthy_Lung.pkl"
        return "Human_AdultAged_Hematopoietic.pkl" if organism == "human" else "Mouse_Whole_Brain.pkl"
    if method == "singler":
        return "HumanPrimaryCellAtlasData" if organism == "human" else "MouseRNAseqData"
    return "user_supplied_marker_gene_dict"


def _render_celltype_script(method: str, organism: str, ref: str, embedding_key: str) -> str:
    if method == "celltypist":
        return f'''# CellTypist annotation
import scanpy as sc
import celltypist
from celltypist import models
adata = sc.read_h5ad("batch_integrated_or_preprocessed.h5ad")
models.download_models(force_update=False)
pred = celltypist.annotate(adata, model="{ref}", majority_voting=True)
adata = pred.to_adata()
sc.pl.umap(adata, color=["majority_voting", "conf_score"], save="_celltypist.png")
adata.obs[["majority_voting", "conf_score"]].to_csv("celltypist_predictions.tsv", sep="\\t")
adata.write_h5ad("celltype_annotated.h5ad")
'''
    if method == "singler":
        return f'''# SingleR annotation ({organism})
suppressPackageStartupMessages({{
  library(SingleR)
  library(zellkonverter)
  library(SingleCellExperiment)
  library(celldex)
}})
sce <- readH5AD("batch_integrated_or_preprocessed.h5ad")
ref <- celldex::{ref}()
pred <- SingleR(test=sce, ref=ref, labels=ref$label.main)
sce$SingleR_label <- pred$labels
write.table(as.data.frame(pred), "singler_predictions.tsv", sep="\\t", quote=FALSE)
writeH5AD(sce, "celltype_annotated.h5ad")
'''
    return f'''# Marker-based annotation
import json
import scanpy as sc
adata = sc.read_h5ad("batch_integrated_or_preprocessed.h5ad")
markers = json.load(open("marker_genes.json", encoding="utf-8"))
for cell_type, genes in markers.items():
    genes = [g for g in genes if g in adata.var_names]
    if genes:
        sc.tl.score_genes(adata, genes, score_name=f"score_{{cell_type}}")
sc.pl.umap(adata, color=[c for c in adata.obs.columns if c.startswith("score_")], save="_marker_scores.png")
adata.write_h5ad("marker_scored.h5ad")
# embedding key expected: {embedding_key}
'''


@server.tool(
    "sc_multimodal_recipe",
    "Generate CITE-seq or multiome preprocessing recipes: CLR/DSB for ADT, WNN/totalVI, TF-IDF/LSI/MultiVI for ATAC.",
    {
        "type": "object",
        "properties": {
            "modality": {"type": "string", "enum": ["cite_seq", "multiome"], "default": "cite_seq"},
            "method": {"type": "string", "enum": ["auto", "wnn", "totalvi", "multivi", "signac"], "default": "auto"},
            "rna_layer": {"type": "string", "default": "counts"},
            "protein_obsm": {"type": "string", "default": "protein_expression"},
            "batch_key": {"type": "string"},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_multimodal_recipe(
    modality: str = "cite_seq",
    method: str = "auto",
    rna_layer: str = "counts",
    protein_obsm: str = "protein_expression",
    batch_key: Optional[str] = None,
    seed: int = 0,
):
    chosen = "totalvi" if modality == "cite_seq" and method == "auto" else "multivi" if method == "auto" else method
    params = {"modality": modality, "method": chosen, "rna_layer": rna_layer, "protein_obsm": protein_obsm, "batch_key": batch_key, "seed": seed}
    recipe_hash = _recipe_hash("sc_multimodal_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": _render_multimodal_script(modality, chosen, rna_layer, protein_obsm, batch_key, seed),
        "notes": [
            "CITE-seq ADT 推荐 CLR 或 DSB normalization，并保留 RNA raw counts。",
            "Multiome ATAC 侧需 TF-IDF + LSI / peak calling / gene activity；MultiVI 需要 modality 标注。",
            "totalVI/MultiVI 是每份数据自训 baseline，不是预训练 foundation model。",
        ],
        "provenance_skeleton": _provenance_skeleton("sc_multimodal_recipe", recipe_hash, {"multimodal_preprocessing": params}),
    }


def _render_multimodal_script(modality: str, method: str, rna_layer: str, protein_obsm: str, batch_key: Optional[str], seed: int) -> str:
    if modality == "cite_seq":
        return f'''# CITE-seq preprocessing: RNA + ADT
import numpy as np
import scanpy as sc
np.random.seed({seed})
adata = sc.read_h5ad("cite_seq.h5ad")
if "{rna_layer}" not in adata.layers:
    # Assumes X is raw RNA counts. If X is already normalized/log-transformed, create {rna_layer} from raw counts first.
    adata.layers["{rna_layer}"] = adata.X.copy()
# RNA: standard scanpy
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
sc.pp.highly_variable_genes(adata, n_top_genes=3000, flavor="seurat_v3", layer="{rna_layer}")
# ADT: CLR normalization / optional DSB normalization
protein = adata.obsm["{protein_obsm}"]
# TODO: apply CLR normalization to ADT matrix; DSB requires empty-droplet/background controls
# totalVI joint model
import scvi
scvi.model.TOTALVI.setup_anndata(adata, layer="{rna_layer}", protein_expression_obsm_key="{protein_obsm}"{", batch_key='" + batch_key + "'" if batch_key else ""})
model = scvi.model.TOTALVI(adata)
model.train()
adata.obsm["X_totalVI"] = model.get_latent_representation()
adata.write_h5ad("cite_seq_totalvi.h5ad")
'''
    return f'''# Multiome preprocessing: RNA + ATAC
import numpy as np
import scanpy as sc
np.random.seed({seed})
adata = sc.read_h5ad("multiome_rna_atac.h5ad")
if "counts" not in adata.layers:
    # Assumes X is raw RNA counts. If X is already normalized/log-transformed, create counts from raw RNA first.
    adata.layers["counts"] = adata.X.copy()
# RNA: standard scanpy preprocessing
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
sc.pp.highly_variable_genes(adata, n_top_genes=3000, flavor="seurat_v3", layer="counts")
# ATAC: TF-IDF + LSI / peak calling / gene activity should be run with muon, episcanpy, ArchR or Signac.
# MultiVI joint model skeleton
import scvi
scvi.model.MULTIVI.setup_anndata(adata, batch_key={repr(batch_key)} if {repr(batch_key)} else None)
model = scvi.model.MULTIVI(adata)
model.train()
adata.obsm["X_multiVI"] = model.get_latent_representation()
adata.write_h5ad("multiome_multivi.h5ad")
'''


@server.tool(
    "sc_spatial_recipe",
    "Generate spatial transcriptomics preprocessing recipes for Visium, MERFISH/seqFISH, or Slide-seq using squidpy-style workflows.",
    {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": ["visium", "merfish", "seqfish", "slideseq"], "default": "visium"},
            "organism": {"type": "string", "enum": ["human", "mouse"], "default": "human"},
            "has_image": {"type": "boolean", "default": True},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_spatial_recipe(platform: str = "visium", organism: str = "human", has_image: bool = True, seed: int = 0):
    params = {"platform": platform, "organism": organism, "has_image": has_image, "seed": seed}
    recipe_hash = _recipe_hash("sc_spatial_recipe", params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": _render_spatial_script(platform, has_image, seed),
        "notes": [
            "Visium：squidpy spatial neighbors、Moran's I、neighborhood enrichment 是轻量起点。",
            "MERFISH/seqFISH：先确认 cell segmentation 与 molecule assignment，不能只按 count matrix 下结论。",
            "Slide-seq：需要 bead/cell 坐标；空间邻域半径应按平台分辨率设置。",
        ],
        "provenance_skeleton": _provenance_skeleton("sc_spatial_recipe", recipe_hash, {"spatial_preprocessing": params}),
    }


def _render_spatial_script(platform: str, has_image: bool, seed: int) -> str:
    if platform == "visium":
        loader = 'adata = sq.read.visium("SPACERANGER_OUT_DIR")' if has_image else 'adata = sc.read_h5ad("visium_counts_with_spatial.h5ad")'
    else:
        loader = 'adata = sc.read_h5ad("spatial_counts_with_xy.h5ad")  # must include adata.obsm["spatial"]'
    return f'''# Spatial transcriptomics preprocessing ({platform})
import numpy as np
import scanpy as sc
import squidpy as sq
np.random.seed({seed})
{loader}
sc.pp.filter_genes(adata, min_cells=3)
if "counts" not in adata.layers:
    adata.layers["counts"] = adata.X.copy()
sc.pp.normalize_total(adata, target_sum=1e4)
sc.pp.log1p(adata)
sc.pp.highly_variable_genes(adata, n_top_genes=3000, flavor="seurat_v3", layer="counts")
sc.tl.pca(adata)
sc.pp.neighbors(adata)
sc.tl.leiden(adata, key_added="leiden")
sc.tl.umap(adata)
sq.gr.spatial_neighbors(adata)
sq.gr.spatial_autocorr(adata, mode="moran")
sq.gr.nhood_enrichment(adata, cluster_key="leiden")
sq.pl.spatial_scatter(adata, color=["leiden"], save="_spatial_clusters.png")
adata.write_h5ad("spatial_preprocessed.h5ad")
'''


if __name__ == "__main__":
    server.run()
