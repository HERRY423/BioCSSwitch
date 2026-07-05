"""bio_eval 分类 gold cases 汇总。

9 大类（对应需求 4）：文献综述 / 临床试验 / 靶点发现 / 药物再利用 / 组学分析 /
证据审计 / PHI 处理 / JSON 稳定性 / 多轮工具调用。

每个 <category>.py 暴露 `CASES` 与 `CATEGORY`。这里汇总成一个总表 + 分类索引。

**关于 20–50 个/类的目标**：这里落地的是**可运行的 seed 集**（每类 5–6 个高质量、
可核对的 case）+ 一套多维 rubric 框架（rubric.py）。要扩到每类 20–50，见
README.md 的「扩充清单」——机械型 case（单点检索 / JSON shape）可批量派生，
需要 gold 判定的（综述冲突 / 证据边界）逐个人工策展，避免为凑数写不可核对的 case。
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import (
    clinical_trials,
    drug_repurposing,
    evidence_audit,
    json_stability,
    lit_review,
    multi_turn,
    omics,
    phi,
    privacy_redteam,
    safety_redteam,
    target_discovery,
)

_MODULES = [
    lit_review, clinical_trials, target_discovery, drug_repurposing,
    omics, evidence_audit, phi, json_stability, multi_turn,
    safety_redteam, privacy_redteam,
]

CATEGORIES: List[str] = [m.CATEGORY for m in _MODULES]

ALL_CASES: List[Dict[str, Any]] = []
for _m in _MODULES:
    ALL_CASES.extend(_m.CASES)


def counts_by_category() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in ALL_CASES:
        out[c["category"]] = out.get(c["category"], 0) + 1
    return out
