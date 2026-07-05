"""证据审计类 gold cases。考核：调 evidence_verify/graph/profile，且把审计结论正确用进答复。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schemas  # noqa: E402

CATEGORY = "evidence_audit"


def _score_verdict_used(ctx):
    """审计后答复里应体现 verdict（支持/不支持/存在/不存在）。工具结果里有 verdict，答复应引用。"""
    text = (ctx["final_text"] or "").lower()
    kws = ["verdict", "支持", "不存在", "unsupported", "supported", "存在", "未核实", "撤回"]
    return 1.0 if any(k in text for k in kws) else 0.4


CASES = [
    {
        "id": "ea_batch_verify",
        "prompt": "对以下 3 条声明做引用校验（用 evidence_verify）：\n"
                  "1. 二甲双胍能降低 2 型糖尿病心血管事件 [PMID:9742976]\n"
                  "2. Sotorasib 是 KRAS G12C 抑制剂 [PMID:32955186]\n"
                  "3. 阿司匹林一级预防在高危患者中的作用 [PMID:30146931]\n"
                  "报告每条的 verdict。",
        "tools": schemas.resolve(["evidence_verify"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["evidence_verify"],
            "require_grounding": True,
            "gate": ["tool_invoked"],
            "custom": _score_verdict_used,
            # 三个 PMID 都是真实存在的专家金标准引用
            "gold": {"ids": ["9742976", "32955186", "30146931"], "mode": "all"},
        },
    },
    {
        "id": "ea_catch_fake",
        "prompt": "校验这条声明的引用：「二甲双胍可降低乳腺癌复发率约 40% [PMID:99999999]」。"
                  "用 evidence_verify。若引用不存在，明确说不存在并撤回结论，不要换一个相似 PMID。",
        "tools": schemas.resolve(["evidence_verify"]),
        "max_tokens": 600,
        "rubric": {
            "expect_tools": ["evidence_verify"],
            "gate": ["tool_invoked"],
            "custom": _score_verdict_used,
            # 这个 PMID 是故意埋的假 ID，模型必须点名它「不存在」——提及不算幻觉
            "expected_fake_ids": ["99999999"],
        },
    },
    {
        "id": "ea_profile_species",
        "prompt": "有人用 PMID:32955186 支持「sotorasib 在患者中的疗效」这条人类结论。"
                  "用 evidence_profile 深挖这篇的物种/实验类型/样本量，判断它能不能支撑一条人类临床 claim。",
        "tools": schemas.resolve(["evidence_profile"]),
        "max_tokens": 700,
        "rubric": {
            "expect_tools": ["evidence_profile"],
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ea_graph_boundary",
        "prompt": "把「二甲双胍降低 2 型糖尿病患者心血管事件」拆成 claim，用 evidence_graph 绑证据"
                  "（asserted.species=human，refs 用 PMID:9742976 stance=supports），"
                  "报告证据等级、适用边界、有无反证。",
        "tools": schemas.resolve(["evidence_graph"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["evidence_graph"],
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ea_graph_to_ledger",
        "prompt": "对「PARP 抑制剂改善 BRCA 突变卵巢癌 PFS」做完整审计：evidence_graph 绑证据，"
                  "再把结果喂 uncertainty_ledger 产出五段面板。答复末尾附五段。",
        "tools": schemas.resolve(["evidence_graph", "uncertainty_ledger"]),
        "max_tokens": 1200,
        "rubric": {
            "expect_tools": ["evidence_graph", "uncertainty_ledger"],
            "require_uncertainty": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ea_mismatch_detect",
        "prompt": "有人用一篇小鼠研究 PMID 去支持「该通路在人类患者中可作为治疗靶点」。"
                  "用 evidence_graph（asserted.species=human）检出物种错配，并说明该结论应如何改写（临床前证据显示…）。",
        "tools": schemas.resolve(["evidence_graph", "evidence_profile"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["evidence_graph"],
            "gate": ["tool_invoked"],
        },
    },
]

for _c in CASES:
    _c["category"] = CATEGORY
    _c.setdefault("tool_choice", "auto")
