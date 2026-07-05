#!/usr/bin/env python3
"""单细胞基础模型（scFM）计算工具适配层 MCP（bio-scfm）。

**把 Geneformer / scGPT 当"计算工具"用，不是当聊天模型。** 这些是把单细胞表达谱映射成
embedding 的编码器；它们没有"意见"，只有确定性的数值输出。所以本适配层的铁律是：

  **任何 embedding 都必须附一份 provenance 记录**，记全：
    - 输入 AnnData 内容哈希（来自 bio-singlecell.anndata_fingerprint）
    - 预处理参数哈希（来自 bio-singlecell.sc_preprocess_recipe）
    - 模型名 + 版本 + checkpoint
    - embedding 维度 + 输出层名 + 输出内容哈希 + pooling
    - 运行环境（python / 包版本）+ seed + device
  少任一项 = 不可复现 = 这份 embedding 不可信，不许进下游分析。

和整个项目一致：适配层**不在 MCP 子进程里跑模型**（GPU + 重依赖，且会占资源/不可控），
而是产出「可复现的运行脚本 + provenance 骨架」，用户在自己机器上跑，embedding 与 provenance
落用户磁盘。工具替你把版本钉死、把 provenance 结构锁死、把哈希算对。

工具：
  scfm_registry           — 已钉版本的 Geneformer / scGPT checkpoint + 输入要求
  scfm_embed_plan         — 产出 embedding 运行脚本 + provenance 骨架
  scfm_provenance_record  — 构造带 content hash 的规范 provenance 记录
  scfm_provenance_verify  — 重算哈希 + 校验必填字段完整性（把"没记全"暴露出来）
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import provenance as prov  # noqa: E402
from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-scfm", "0.1.0")

PROVENANCE_SCHEMA = "bio-scfm/embedding-provenance/1"

# 已钉版本的模型登记表。版本/‑checkpoint 取自各自官方发布；用户应在 registry 里核对
# 自己实际装的版本（工具会把它记进 provenance，不是替用户断言）。
_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ===== 预训练基础模型（foundation models）=====
    "geneformer": {
        "category": "foundation-model",
        "source": "ctheodoris/Geneformer (Hugging Face)",
        "input_id_type": "ensembl",
        "tokenization": "rank-value（按每细胞非零基因表达排序，无需 log/HVG/scale）",
        "requires": ["Ensembl 基因 ID", "每细胞 total counts（用于 median 归一）"],
        "checkpoints": [
            {"name": "gf-6L-30M-i2048", "layers": 6, "input_size": 2048, "pretrain": "Genecorpus-30M"},
            {"name": "gf-12L-30M-i2048", "layers": 12, "input_size": 2048, "pretrain": "Genecorpus-30M"},
            {"name": "gf-12L-95M-i4096", "layers": 12, "input_size": 4096, "pretrain": "Genecorpus-95M"},
        ],
        "default_checkpoint": "gf-12L-30M-i2048",
        "embedding": {"cell_emb_layer": -2, "pooling": "mean over gene tokens"},
        "note": "版本以你本地安装的 Geneformer 包/commit 为准；把确切 commit 记进 provenance.model.commit。",
    },
    "scgpt": {
        "category": "foundation-model",
        "source": "bowang-lab/scGPT",
        "input_id_type": "symbol",
        "tokenization": "gene symbol + value binning",
        "requires": ["基因 symbol", "HVG 子集", "value binning（默认 51 bins）"],
        "checkpoints": [
            {"name": "scGPT_human", "note": "whole-human 预训练（~33M 细胞）"},
            {"name": "scGPT_CP", "note": "continual pretrained"},
        ],
        "default_checkpoint": "scGPT_human",
        "embedding": {"cell_emb": "<cls> token", "dim": 512},
        "note": "版本以你本地 scGPT 权重目录里的 args.json / vocab 为准；记进 provenance.model.version。",
    },
    "cellfm": {
        "category": "foundation-model",
        "source": "biomap-research/CellFM",
        "input_id_type": "symbol",
        "tokenization": "gene expression tokens（~800M 参数，单人细胞图谱预训练）",
        "requires": ["基因 symbol", "对齐到模型基因词表", "raw / 归一化按官方 preprocessing"],
        "checkpoints": [{"name": "CellFM-800M", "note": "以官方发布 checkpoint 为准"}],
        "default_checkpoint": "CellFM-800M",
        "embedding": {"cell_emb": "cell token", "dim": "以 checkpoint 为准"},
        "note": "API/输入要求以官方仓库为准，工具只钉版本与 provenance，不替你断言接口。",
    },
    "uce": {
        "category": "foundation-model",
        "source": "snap-stanford/UCE (Universal Cell Embeddings)",
        "input_id_type": "ensembl-protein",
        "tokenization": "跨物种 protein-embedding 化基因表示（zero-shot，无需微调）",
        "requires": ["基因映射到 ESM2 蛋白 embedding", "物种信息", "官方 eval 脚本预处理"],
        "checkpoints": [{"name": "UCE-33L", "note": "33 层，4-layer 版亦有"},
                        {"name": "UCE-4L", "note": "轻量版"}],
        "default_checkpoint": "UCE-33L",
        "embedding": {"cell_emb": "cell token", "dim": 1280},
        "note": "UCE 卖点是 zero-shot 跨物种；输入是蛋白 embedding 化的基因，别喂原始 counts。",
    },
    # ===== domain-specific baseline（每份数据自训 VAE，非预训练；用于对照 scFM）=====
    "scvi": {
        "category": "domain-baseline",
        "source": "scverse/scvi-tools",
        "input_id_type": "symbol/ensembl",
        "tokenization": "raw counts → VAE 潜变量（在你自己的数据上训练）",
        "requires": ["raw counts 层", "HVG 选择", "batch_key（如需批次校正）"],
        "checkpoints": [{"name": "SCVI", "note": "非预训练——每份数据现训"}],
        "default_checkpoint": "SCVI",
        "embedding": {"cell_emb": "latent z", "dim": "n_latent（默认 10-30）"},
        "note": "baseline，不是 foundation model：结果依赖你这份数据的训练，provenance 需额外记训练超参与 epoch。",
    },
    "totalvi": {
        "category": "domain-baseline",
        "source": "scverse/scvi-tools",
        "input_id_type": "symbol",
        "tokenization": "CITE-seq（RNA + 蛋白）联合 VAE",
        "requires": ["RNA raw counts", "蛋白（ADT）counts", "batch_key"],
        "checkpoints": [{"name": "TOTALVI", "note": "非预训练——CITE-seq 多模态 baseline"}],
        "default_checkpoint": "TOTALVI",
        "embedding": {"cell_emb": "latent z", "dim": "n_latent"},
        "note": "多模态 CITE-seq baseline；与 scFM 对照时注意模态不同，不能直接同表比较。",
    },
    "multivi": {
        "category": "domain-baseline",
        "source": "scverse/scvi-tools",
        "input_id_type": "symbol",
        "tokenization": "multiome（RNA + ATAC）联合 VAE",
        "requires": ["RNA counts", "ATAC peak counts", "modality 标注"],
        "checkpoints": [{"name": "MULTIVI", "note": "非预训练——multiome 多模态 baseline"}],
        "default_checkpoint": "MULTIVI",
        "embedding": {"cell_emb": "latent z", "dim": "n_latent"},
        "note": "multiome baseline；用于给 scFM 的 RNA-only embedding 提供多模态参照。",
    },
}

_FOUNDATION = {k for k, v in _REGISTRY.items() if v.get("category") == "foundation-model"}
_BASELINE = {k for k, v in _REGISTRY.items() if v.get("category") == "domain-baseline"}


@server.tool(
    "scfm_registry",
    "List pinned single-cell model checkpoints and input requirements. Covers FOUNDATION models "
    "(Geneformer, scGPT, CellFM, UCE — pretrained encoders) AND domain-specific BASELINES (scVI, totalVI, "
    "MultiVI — per-dataset VAEs, kept for honest comparison). Transparency + the exact version strings to "
    "embed in provenance. Pass `model` for one, omit for all.",
    {
        "type": "object",
        "properties": {"model": {"type": "string",
                                 "enum": ["geneformer", "scgpt", "cellfm", "uce",
                                          "scvi", "totalvi", "multivi"]}},
    },
)
def scfm_registry(model: Optional[str] = None):
    if model:
        m = _REGISTRY.get(model.lower())
        return m or {"error": f"unknown model: {model}", "available": list(_REGISTRY)}
    return {"models": _REGISTRY,
            "foundation_models": sorted(_FOUNDATION),
            "domain_baselines": sorted(_BASELINE),
            "note": "foundation model = 预训练编码器；domain baseline = 在你自己数据上现训的 VAE，"
                    "保留作诚实对照——别把 baseline 当成'预训练 foundation model'。这些都是计算工具，输出 embedding。"}


@server.tool(
    "scfm_model_matrix",
    "Return the single-cell model matrix as a comparison table: which are pretrained foundation models vs "
    "per-dataset domain baselines, their input requirements (gene ID type, modality), and what to compare. "
    "Use to pick a model AND its honest baseline before embedding.",
    {"type": "object", "properties": {}},
)
def scfm_model_matrix():
    rows = []
    for name, m in _REGISTRY.items():
        rows.append({
            "model": name,
            "category": m.get("category"),
            "input_id_type": m.get("input_id_type"),
            "modality": ("RNA+ADT" if name == "totalvi" else
                         "RNA+ATAC" if name == "multivi" else "RNA"),
            "pretrained": m.get("category") == "foundation-model",
            "source": m.get("source"),
        })
    return {
        "matrix": rows,
        "foundation_models": sorted(_FOUNDATION),
        "domain_baselines": sorted(_BASELINE),
        "guidance": [
            "跑 foundation model（Geneformer/scGPT/CellFM/UCE）时，**至少配一个 domain baseline**"
            "（scVI 系）对照——否则无法判断 foundation 的 embedding 是不是真比自训 VAE 强。",
            "foundation model 是预训练、zero-shot/少样本；baseline 是你这份数据现训，provenance 要额外记训练超参。",
            "多模态数据（CITE-seq/multiome）用 totalVI/MultiVI 作 baseline；单模态用 scVI。",
        ],
    }


@server.tool(
    "scfm_embed_plan",
    "Produce a reproducibility SKELETON for an embedding run (artifact_type=skeleton, runnable=false): "
    "(1) a TEMPLATE Python script — NOT runnable as-is; it starts with a NOT-RUNNABLE banner + a "
    "SystemExit guard and contains TODO/pseudo-code for the model API — the user must pin versions, fill "
    "TODOs, and verify the current official API before running; (2) the provenance SKELETON to fill after "
    "the run (input fingerprint + preprocessing hash already wired in). Does NOT run the model and does NOT "
    "produce a ready-to-run script. Requires the anndata fingerprint + preprocessing recipe_hash from "
    "bio-singlecell.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string",
                      "enum": ["geneformer", "scgpt", "cellfm", "uce",
                               "scvi", "totalvi", "multivi"]},
            "checkpoint": {"type": "string", "description": "One of registry checkpoints; default used if omitted."},
            "anndata_fingerprint": {"type": "string", "description": "From anndata_fingerprint (metadata proxy)."},
            "anndata_sha256": {"type": "string", "description": "TRUE content hash from the fingerprint snippet."},
            "preprocessing_hash": {"type": "string", "description": "recipe_hash from sc_preprocess_recipe."},
            "seed": {"type": "integer", "default": 0},
            "output_layer": {"type": "string", "description": "obsm key for the embedding, e.g. X_geneformer."},
        },
        "required": ["model"],
    },
)
def scfm_embed_plan(model: str, checkpoint: Optional[str] = None,
                    anndata_fingerprint: Optional[str] = None,
                    anndata_sha256: Optional[str] = None,
                    preprocessing_hash: Optional[str] = None,
                    seed: int = 0, output_layer: Optional[str] = None):
    m = _REGISTRY.get(model.lower())
    if not m:
        return {"error": f"unknown model: {model}", "available": list(_REGISTRY)}
    ckpt = checkpoint or m["default_checkpoint"]
    out_layer = output_layer or f"X_{model.lower()}"
    gaps: List[str] = []
    if not anndata_sha256:
        gaps.append("缺 anndata_sha256（真·内容哈希）—— 先跑 anndata_fingerprint 的 snippet 算出来")
    if not preprocessing_hash:
        gaps.append("缺 preprocessing_hash —— 先调 sc_preprocess_recipe 拿到 recipe_hash")

    script = _render_embed_script(model.lower(), ckpt, out_layer, seed, m)
    provenance_skeleton = {
        "schema": PROVENANCE_SCHEMA,
        "model": {"name": model.lower(), "checkpoint": ckpt, "version": "<FILL: 本地安装版本>",
                  "commit": "<FILL: git commit / HF revision>", "source": m["source"]},
        "input": {"anndata_fingerprint": anndata_fingerprint or "<FILL>",
                  "anndata_sha256": anndata_sha256 or "<FILL: 跑 snippet 得到>",
                  "var_id_type": m["input_id_type"]},
        "preprocessing": {"recipe_hash": preprocessing_hash or "<FILL>"},
        "embedding": {"output_layer": out_layer, "n_dims": "<FILL: 运行后填>",
                      "output_sha256": "<FILL: 运行后对 obsm 算哈希>",
                      "pooling": m["embedding"]},
        "run": {"seed": seed, "device": "<FILL: cuda/cpu>", "created_at": "<FILL: ISO8601>"},
        "environment": {"python": "<FILL>", "packages": {model.lower(): "<FILL>", "scanpy": "<FILL>",
                                                          "torch": "<FILL>"}},
    }
    return {
        "model": model.lower(),
        "checkpoint": ckpt,
        "artifact_type": "skeleton",
        "runnable": False,
        "input_requirements": m["requires"],
        "script": script,
        "script_todos": _script_todos(model.lower()),
        "provenance_skeleton": provenance_skeleton,
        "gaps": gaps,
        "note": "⚠️ 这是 SKELETON（模板），不可直接运行。补全 TODO、钉版本、核对官方 API 后才能跑；"
                "跑完把 <FILL> 补齐，调 scfm_provenance_record 生成正式记录。",
    }


def _script_todos(model: str) -> List[str]:
    common = ["装好并 pin 住模型与依赖版本", "核对该模型当前官方 API（下方为示意伪代码，非稳定接口）",
              "补全数据加载 / tokenization / 权重路径", "运行后把 embedding_dim 与 output_sha256 填回 provenance"]
    if model == "geneformer":
        return ["先用 TranscriptomeTokenizer 把 .h5ad 转成 .dataset"] + common
    if model in ("scvi", "totalvi", "multivi"):
        return ["这是 domain-specific baseline：在你自己的数据上训练 VAE（非预训练 foundation model）"] + common
    return common


def _render_embed_script(model: str, ckpt: str, out_layer: str, seed: int,
                         m: Dict[str, Any]) -> str:
    banner = [
        "# " + "=" * 68,
        "# ⚠️  SKELETON —— 不可直接运行（NOT RUNNABLE AS-IS）",
        "# 这是模板，不是可执行脚本。你必须：",
        "#   (1) 装好模型与依赖并钉版本  (2) 补全所有 TODO",
        "#   (3) 核对官方 API（下方为示意伪代码，接口会变）后才能运行。",
        "# 直接执行会在下一行 SystemExit 处停止——补全后删掉那一行。",
        "# " + "=" * 68,
        f"# 由 bio-scfm.scfm_embed_plan 生成 —— {model} embedding 骨架",
        f"# checkpoint: {ckpt}  | output obsm: {out_layer}",
        'raise SystemExit("SKELETON：补全 TODO、核对 API、钉版本后删除本行再运行")',
        "",
        "import hashlib, json, numpy as np, scanpy as sc",
        f"np.random.seed({seed})",
        'adata = sc.read_h5ad("preprocessed.h5ad")  # bio-singlecell 预处理产物',
    ]
    if model == "geneformer":
        body = [
            "# TODO Geneformer：rank-value tokenization → 取倒数第二层 cell embedding（伪代码）",
            "# from geneformer import EmbExtractor, TranscriptomeTokenizer  # 版本以本地安装为准",
            "# TODO: TranscriptomeTokenizer 把 .h5ad → .dataset",
            "# embex = EmbExtractor(model_type='CellClassifier', emb_layer=-2, ...)",
            f"# adata.obsm['{out_layer}'] = embex.extract_embs(model_dir, tokenized_dir, ...)",
        ]
    elif model == "scgpt":
        body = [
            "# TODO scGPT：<cls> token 作为 cell embedding（伪代码）",
            "# import scgpt as scg  # 版本以本地权重目录 args.json 为准",
            f"# adata.obsm['{out_layer}'] = scg.tasks.embed_data(adata, model_dir='scGPT_human', ...)",
        ]
    elif model in ("scvi", "totalvi", "multivi"):
        body = [
            f"# TODO {model}：domain-specific baseline —— 在你自己的数据上训练 VAE（伪代码）",
            "# import scvi",
            f"# scvi.model.{'SCVI' if model=='scvi' else ('TOTALVI' if model=='totalvi' else 'MULTIVI')}.setup_anndata(adata, ...)",
            "# vae = ...; vae.train()",
            f"# adata.obsm['{out_layer}'] = vae.get_latent_representation()",
        ]
    else:  # cellfm / uce / 其它 foundation model
        body = [
            f"# TODO {model}：foundation model cell embedding（伪代码；核对官方仓库 API）",
            f"# adata.obsm['{out_layer}'] = <call {model} encoder on adata>",
        ]
    tail = [
        "# —— 输出内容哈希（填进 provenance.embedding.output_sha256）——",
        f"E = np.ascontiguousarray(adata.obsm['{out_layer}'])",
        "print('embedding_dim:', E.shape[1])",
        "print('output_sha256:sha256:' + hashlib.sha256(E.tobytes()).hexdigest())",
        f'adata.write_h5ad("embedded_{model}.h5ad")',
    ]
    return "\n".join(banner + body + tail)


_REQUIRED_PROVENANCE = [
    "schema", "model.name", "model.checkpoint", "model.version",
    "input.anndata_sha256", "input.var_id_type",
    "preprocessing.recipe_hash",
    "embedding.output_layer", "embedding.output_sha256", "embedding.n_dims",
    "run.seed", "run.created_at",
]


@server.tool(
    "scfm_provenance_record",
    "Build a canonical embedding-provenance record with a content hash. Pass the filled-in fields (model "
    "version, anndata_sha256, preprocessing recipe_hash, embedding output_sha256 + dims, env, seed). "
    "Returns the record + provenance_hash (sha256 over the canonical record). Attach this to every "
    "embedding; without it the embedding is not reproducible.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "object", "description": "{name, checkpoint, version, commit, source}"},
            "input": {"type": "object", "description": "{anndata_fingerprint, anndata_sha256, var_id_type}"},
            "preprocessing": {"type": "object", "description": "{recipe_hash, ...}"},
            "embedding": {"type": "object", "description": "{output_layer, n_dims, output_sha256, pooling}"},
            "run": {"type": "object", "description": "{seed, device, created_at, batch_size}"},
            "environment": {"type": "object", "description": "{python, packages:{...}}"},
        },
        "required": ["model", "input", "preprocessing", "embedding"],
    },
)
def scfm_provenance_record(model: Dict[str, Any], input: Dict[str, Any],  # noqa: A002
                           preprocessing: Dict[str, Any], embedding: Dict[str, Any],
                           run: Optional[Dict[str, Any]] = None,
                           environment: Optional[Dict[str, Any]] = None):
    run = dict(run or {})
    run.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    record = {
        "schema": PROVENANCE_SCHEMA,
        "model": model, "input": input, "preprocessing": preprocessing,
        "embedding": embedding, "run": run, "environment": environment or {},
    }
    missing = prov.required_fields_missing(record, _REQUIRED_PROVENANCE)
    record["provenance_hash"] = prov.content_hash(record)
    return {
        "record": record,
        "provenance_hash": record["provenance_hash"],
        "complete": not missing,
        "missing_fields": missing,
        "note": "provenance_hash 是对整条记录（除自身）的规范 sha256，第三方可重算验真。"
                + ("" if not missing else " ⚠️ 有必填缺失，补齐后再重算。"),
    }


@server.tool(
    "scfm_provenance_verify",
    "Verify an embedding-provenance record: recompute its provenance_hash and check all required fields "
    "are present. Returns hash_match + missing_fields. Use to gate whether an embedding is trustworthy "
    "enough to enter downstream analysis.",
    {
        "type": "object",
        "properties": {"record": {"type": "object"}},
        "required": ["record"],
    },
)
def scfm_provenance_verify(record: Dict[str, Any]):
    record = dict(record or {})
    claimed = record.pop("provenance_hash", None)
    recomputed = prov.content_hash(record)
    missing = prov.required_fields_missing(record, _REQUIRED_PROVENANCE)
    return {
        "hash_match": claimed == recomputed,
        "claimed_hash": claimed,
        "recomputed_hash": recomputed,
        "complete": not missing,
        "missing_fields": missing,
        "verdict": "trustworthy" if (claimed == recomputed and not missing) else "not_reproducible",
    }


if __name__ == "__main__":
    server.run()
