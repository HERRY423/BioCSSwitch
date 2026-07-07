"""Spatial transcriptomics gold cases.

These cases check whether the agent keeps platform tradeoffs, rare-cell marker
baselines and spatial foundation-model provenance explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schemas  # noqa: E402

CATEGORY = "spatial"


def _score_platform_rare_cell(ctx):
    text = (ctx["final_text"] or "").lower()
    platform = any(k in text for k in ["platform", "xenium", "visium", "cosmx", "平台"])
    marker = any(k in text for k in ["marker", "krt17", "marker score", "baseline", "标记"])
    orthogonal = any(k in text for k in ["orthogonal", "正交", "validation", "验证"])
    return 1.0 if platform and marker and orthogonal else 0.6 if platform and marker else 0.2


def _score_spatial_fm(ctx):
    text = (ctx["final_text"] or "").lower()
    skeleton = any(k in text for k in ["skeleton", "not runnable", "systemexit", "不可直接运行"])
    provenance = any(k in text for k in ["provenance", "hash", "recipe_hash", "哈希"])
    baseline = any(k in text for k in ["baseline", "marker", "deconvolution", "scvi", "基线"])
    return 1.0 if skeleton and provenance and baseline else 0.6 if skeleton and provenance else 0.2


CASES = [
    {
        "id": "spatial_ipf_krt17_validation",
        "prompt": "我想用 Xenium 和 Visium HD 验证 IPF 里的 KRT17+ 上皮状态和 SPP1+ 巨噬细胞生态位。请给空间验证计划，不要声称已经跑出结果。",
        "tools": schemas.resolve([
            "spatial_platform_matrix",
            "spatial_preprocess_recipe",
            "spatial_rare_cell_recipe",
            "ipf_krt17_spatial_validation_recipe",
        ]),
        "max_tokens": 1000,
        "rubric": {
            "expect_tools": [
                "spatial_platform_matrix",
                "spatial_preprocess_recipe",
                "spatial_rare_cell_recipe",
                "ipf_krt17_spatial_validation_recipe",
            ],
            "gate": ["tool_invoked"],
            "custom": _score_platform_rare_cell,
        },
    },
    {
        "id": "spatial_fm_skeleton_baselines",
        "prompt": "我要比较 scGPT-Spatial 和 Nicheformer 做空间 embedding，请给模型矩阵、skeleton 和必须配的 baseline/provenance。",
        "tools": schemas.resolve([
            "spatial_scfm_model_matrix",
            "spatial_scfm_plan",
            "spatial_deconvolution_recipe",
        ]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": [
                "spatial_scfm_model_matrix",
                "spatial_scfm_plan",
                "spatial_deconvolution_recipe",
            ],
            "gate": ["tool_invoked"],
            "custom": _score_spatial_fm,
        },
    },
]

for _c in CASES:
    _c["category"] = CATEGORY
    _c.setdefault("tool_choice", "auto")
