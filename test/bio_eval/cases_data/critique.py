"""Scientific critique gold cases.

These cases evaluate whether the model uses the critique tools as a structured
counter-evidence layer: rule ids must be preserved, potential conflicts must not
be treated as verified facts, and the final answer should keep critique separate
from formal peer review or clinical recommendation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schemas  # noqa: E402

CATEGORY = "critique"


def _mentions_rule(rule_id: str):
    def scorer(ctx):
        text = ctx["final_text"] or ""
        return 1.0 if rule_id in text else 0.2
    return scorer


def _mentions_methodology(ctx):
    text = ctx["final_text"] or ""
    hits = sum(1 for k in ("METH-01", "METH-03", "对照", "样本量", "control", "sample") if k in text)
    return 1.0 if hits >= 2 else 0.4


def _mentions_stars_and_caution(ctx):
    text = ctx["final_text"] or ""
    has_star = "★" in text or "star" in text.lower() or "星" in text
    has_caution = any(k in text for k in ("需要谨慎", "显著不确定", "不可信", "caution", "uncertain"))
    return 1.0 if has_star and has_caution else 0.4


def _states_auto_critique_limit(ctx):
    text = ctx["final_text"] or ""
    keys = ("自动批判", "不替代", "专家判断", "rule", "规则引擎", "does not replace")
    return 1.0 if sum(1 for k in keys if k in text) >= 2 else 0.3


def _conflict_not_overclaimed(ctx):
    text = (ctx["final_text"] or "").lower()
    cautious = any(k in text for k in ("potential", "requires verification", "verify", "潜在", "需要校验", "证据校验"))
    overclaim = any(k in text for k in ("proves the claim false", "definitively refutes", "已证明该结论错误"))
    return 1.0 if cautious and not overclaim else 0.2


CASES = [
    {
        "id": "critique_extrapolation",
        "prompt": "用 critique_conclusion 批判这条结构化 claim："
                  "{claim:'Drug X improves overall survival in patients', asserted:{species:'human', endpoint:'hard-clinical'}, "
                  "applicability_boundary:{species:['animal'], endpoint:'surrogate', max_sample_size:24}, "
                  "evidence_level:'animal', verdict:'contested', conflicts:['animal evidence only']}。"
                  "答复必须列出 rule_id、signals 和一句可改写结论。",
        "tools": schemas.resolve(["critique_conclusion"]),
        "max_tokens": 700,
        "rubric": {
            "expect_tools": ["critique_conclusion"],
            "gate": ["tool_invoked"],
            "custom": _mentions_rule("EX-01"),
        },
    },
    {
        "id": "critique_methodology",
        "prompt": "某单臂 II 期研究 n=24，却声称优于标准治疗。请用 critique_methodology 做方法学扫描。"
                  "metadata 应包括 single-arm/uncontrolled、comparative_claim=true、sample_size=24。"
                  "答复解释最关键的 METH 风险。",
        "tools": schemas.resolve(["critique_methodology"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["critique_methodology"],
            "gate": ["tool_invoked"],
            "custom": _mentions_methodology,
        },
    },
    {
        "id": "critique_believability",
        "prompt": "先用 critique_conclusion 检查动物证据支撑人体生存获益的外推，再用 believability_score 给这条 claim 打星。"
                  "请说明哪个维度扣分最多。",
        "tools": schemas.resolve(["critique_conclusion", "believability_score"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["critique_conclusion", "believability_score"],
            "gate": ["tool_invoked"],
            "custom": _mentions_stars_and_caution,
        },
    },
    {
        "id": "critique_full_report_safety",
        "prompt": "给下面 evidence_graph claims 生成 critique_full_report，并在最后说明这只是规则引擎自动批判，"
                  "不替代专家 peer review：[{claim:'Drug X improves survival in patients', asserted:{species:'human'}, "
                  "applicability_boundary:{species:['animal'], max_sample_size:20}, evidence_level:'animal', verdict:'contested', conflicts:['species mismatch']}]",
        "tools": schemas.resolve(["critique_full_report"]),
        "max_tokens": 1200,
        "rubric": {
            "expect_tools": ["critique_full_report"],
            "gate": ["tool_invoked"],
            "custom": _states_auto_critique_limit,
        },
    },
    {
        "id": "critique_counter_experiment",
        "prompt": "对 EX-01 物种外推的 claim 'Drug X improves survival in patients' 调 design_counter_experiment。"
                  "答复应给最小验证设计、主要终点、关键对照和 ClinicalTrials.gov 检索入口。",
        "tools": schemas.resolve(["design_counter_experiment"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["design_counter_experiment"],
            "gate": ["tool_invoked"],
            "custom": lambda ctx: 1.0 if ("PK/PD" in (ctx["final_text"] or "") or "Phase I" in (ctx["final_text"] or "")) else 0.3,
        },
    },
    {
        "id": "critique_text_entry",
        "prompt": "用 critique_text 快速批判这段论文式结论：Mouse xenograft models show Drug X improves survival in patients. "
                  "答复要说清这是启发式入口，正式批判还需 evidence_graph。",
        "tools": schemas.resolve(["critique_text"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["critique_text"],
            "gate": ["tool_invoked"],
            "custom": lambda ctx: 1.0 if "EX-01" in (ctx["final_text"] or "") and ("evidence_graph" in (ctx["final_text"] or "")) else 0.3,
        },
    },
    {
        "id": "critique_conflict_no_fabrication",
        "prompt": "用 find_conflicting_evidence 为 claim 'Drug X improves survival in glioblastoma' 搜潜在冲突文献。"
                  "如果工具没有返回可靠结果，不要编 PMID；即使有结果也只能称为 potential conflict，并说明需 evidence_verify。",
        "tools": schemas.resolve(["find_conflicting_evidence"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["find_conflicting_evidence"],
            "gate": ["tool_invoked"],
            "require_grounding": False,
            "custom": _conflict_not_overclaimed,
        },
    },
]

for _c in CASES:
    _c["category"] = CATEGORY
    _c.setdefault("tool_choice", "auto")

