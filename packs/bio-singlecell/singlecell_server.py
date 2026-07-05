#!/usr/bin/env python3
"""单细胞数据处理与指纹 MCP（bio-singlecell）。

定位：**给 scFM（Geneformer / scGPT）喂数据前的标准化 + 可追溯层**，不是替用户跑分析。
和 bio-workflows/generators 一个哲学——工具产出「可复现的配方 + provenance 记录」，实际
重活（读 .h5ad、跑 scanpy）在用户机器上跑，中间对象落用户磁盘、可复现。

工具：
  anndata_fingerprint   — 对 AnnData 描述符取可核对指纹 + 生成算"真·内容哈希"的代码片段
  sc_preprocess_recipe  — 产出确定性的 scanpy 预处理配方（参数 + 生成脚本 + provenance）
  sc_qc_thresholds      — 按 scverse 最佳实践给 MAD-based QC 阈值建议（可解释，不是黑盒）

为什么必须记指纹/参数：换了预处理（normalize / HVG / n_top_genes），下游 embedding 就变；
不记录 = 不可复现 = 结果不可信。scFM 适配层会**要求**引用这里产出的 fingerprint 与 recipe。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import provenance as prov  # noqa: E402
from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-singlecell", "0.1.0")


@server.tool(
    "anndata_fingerprint",
    "Fingerprint an AnnData dataset for reproducibility. Takes a DESCRIPTOR (metadata only: n_obs, "
    "n_var, var_id_type, obs/var keys, layers, optional X_checksum) and returns a stable, auditable "
    "fingerprint hash — plus a Python snippet the user runs on their machine to compute the TRUE "
    "content hash of the .h5ad (X + var_names + obs_names). Record this fingerprint before any scFM "
    "embedding so the exact input is provenance-tracked.",
    {
        "type": "object",
        "properties": {
            "descriptor": {
                "type": "object",
                "description": "AnnData metadata: n_obs, n_var, var_id_type (ensembl|symbol|entrez), "
                               "obs_keys, var_keys, layers, X_dtype, X_checksum, assay, organism, raw_present.",
            },
            "hash_layer": {"type": "string",
                           "description": "Which layer the true-hash snippet should hash (default: X)."},
        },
        "required": ["descriptor"],
    },
)
def anndata_fingerprint(descriptor: Dict[str, Any], hash_layer: Optional[str] = None):
    fp = prov.hash_descriptor(descriptor)
    warns: List[str] = []
    if not descriptor.get("var_id_type"):
        warns.append("未声明 var_id_type（ensembl / symbol / entrez）——scFM 对基因 ID 类型敏感，必须明确")
    if not descriptor.get("X_checksum"):
        warns.append("descriptor 未含 X_checksum：当前指纹只覆盖元数据。跑 true_content_hash_snippet "
                     "算出真·内容哈希后，填回 descriptor.X_checksum 再指纹一次，才能保证同数据同指纹")
    return {
        "fingerprint": fp,
        "descriptor_used": {k: descriptor.get(k) for k in
                            ("n_obs", "n_var", "var_id_type", "layers", "assay", "organism")},
        "true_content_hash_snippet": prov.anndata_hash_snippet(hash_layer),
        "warnings": warns,
        "note": "fingerprint 是元数据级可核对代理；真·内容哈希请用 snippet 在本地算。两者都记进 provenance。",
    }


# scanpy 预处理步骤的默认参数（scverse 常规流程）
_DEFAULT_STEPS = [
    {"op": "filter_cells", "min_genes": 200},
    {"op": "filter_genes", "min_cells": 3},
    {"op": "normalize_total", "target_sum": 1e4},
    {"op": "log1p"},
    {"op": "highly_variable_genes", "n_top_genes": 2000, "flavor": "seurat_v3"},
]


@server.tool(
    "sc_preprocess_recipe",
    "Produce a DETERMINISTIC scanpy preprocessing recipe (params + generated Python script + provenance "
    "hash of the params). Override any step's params via `overrides`. The recipe's provenance hash must be "
    "carried into the scFM embedding provenance so the exact preprocessing is reproducible. Different "
    "models want different prep (Geneformer: rank-value, no HVG scaling; scGPT: HVG + binning) — pass "
    "`target_model` to get a model-appropriate default.",
    {
        "type": "object",
        "properties": {
            "target_model": {"type": "string", "enum": ["geneformer", "scgpt", "generic"],
                             "default": "generic"},
            "overrides": {"type": "object",
                          "description": "Per-op param overrides, e.g. {'highly_variable_genes': {'n_top_genes': 3000}}."},
            "seed": {"type": "integer", "default": 0},
        },
    },
)
def sc_preprocess_recipe(target_model: str = "generic",
                         overrides: Optional[Dict[str, Any]] = None, seed: int = 0):
    steps = [dict(s) for s in _DEFAULT_STEPS]
    # 模型特异性调整（可解释）
    notes: List[str] = []
    if target_model == "geneformer":
        # Geneformer 用 rank-value encoding，不做 log / HVG / scale；只需非零基因排序
        steps = [{"op": "filter_cells", "min_genes": 200},
                 {"op": "filter_genes", "min_cells": 3},
                 {"op": "require_ensembl_ids"},
                 {"op": "note", "text": "Geneformer 走 rank-value tokenization，不做 normalize/log/HVG；"
                                        "需 Ensembl ID + 每细胞 total counts 用于 median 归一"}]
        notes.append("Geneformer：跳过 log1p / HVG；基因必须是 Ensembl ID。")
    elif target_model == "scgpt":
        steps = [{"op": "filter_cells", "min_genes": 200},
                 {"op": "filter_genes", "min_cells": 3},
                 {"op": "normalize_total", "target_sum": 1e4},
                 {"op": "log1p"},
                 {"op": "highly_variable_genes", "n_top_genes": 1200, "flavor": "seurat_v3"},
                 {"op": "value_binning", "n_bins": 51}]
        notes.append("scGPT：HVG(~1200) + value binning(51)；基因用 symbol。")
    if overrides:
        for s in steps:
            if s.get("op") in overrides:
                s.update(overrides[s["op"]])
    params = {"target_model": target_model, "seed": seed, "steps": steps}
    recipe_hash = prov.content_hash(params)
    script = _render_scanpy_script(params)
    return {
        "recipe_hash": recipe_hash,
        "params": params,
        "script": script,
        "notes": notes,
        "note": "把 recipe_hash 记进 scFM provenance.preprocessing_hash；脚本在用户机器上跑，产物落本地。",
    }


def _render_scanpy_script(params: Dict[str, Any]) -> str:
    lines = ["# 由 bio-singlecell.sc_preprocess_recipe 生成 —— 确定性预处理，请勿手改参数",
             f"# recipe params hash: {prov.content_hash(params)}",
             "import scanpy as sc, numpy as np",
             f"np.random.seed({params.get('seed', 0)})",
             'adata = sc.read_h5ad("YOUR_FILE.h5ad")']
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
            lines.append(f"sc.pp.highly_variable_genes(adata, n_top_genes={s.get('n_top_genes', 2000)}, "
                         f"flavor='{s.get('flavor', 'seurat_v3')}')")
        elif op == "value_binning":
            lines.append(f"# scGPT value binning into {s.get('n_bins', 51)} bins（见 scGPT 预处理）")
        elif op == "require_ensembl_ids":
            lines.append("assert adata.var_names.str.startswith('ENSG').mean() > 0.9, "
                         "'Geneformer 需要 Ensembl 基因 ID'")
        elif op == "note":
            lines.append(f"# {s.get('text', '')}")
    lines.append('adata.write_h5ad("preprocessed.h5ad")')
    return "\n".join(lines)


@server.tool(
    "sc_qc_thresholds",
    "Suggest MAD-based QC thresholds (scverse best practice) from per-cell QC summary stats. "
    "Explainable: returns the threshold = median ± n_mads*MAD and which cells it would drop and why — "
    "not a black box. Pass summary stats (median/MAD of n_genes, total_counts, pct_mito).",
    {
        "type": "object",
        "properties": {
            "stats": {
                "type": "object",
                "description": "Per-metric summary: e.g. {'pct_counts_mt': {'median':5,'mad':2}, "
                               "'log1p_total_counts': {'median':8.1,'mad':0.6}}.",
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
            "lower": round(lower, 4), "upper": round(upper, 4),
            "rule": f"median({med}) ± {n_mads}×MAD({mad})",
            "note": "落在 [lower, upper] 之外视为离群，建议剔除；线粒体比例通常只设上界",
        }
    return {"thresholds": out, "n_mads": n_mads,
            "method": "MAD-based（对离群稳健，优于固定阈值 / 均值±SD）"}


if __name__ == "__main__":
    server.run()
