"""药物再利用（老药新用）类 gold cases。考核：机制→相邻疾病→临床先例检查，安全性阻断意识。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schemas  # noqa: E402

CATEGORY = "drug_repurposing"

CASES = [
    {
        "id": "dr_metformin_mech",
        "prompt": "二甲双胍除了降糖还能重定位到哪些方向？先 compound_search 拿 ChEMBL id，"
                  "再 get_mechanism 看主要靶点，据此推候选方向。",
        "tools": schemas.resolve(["compound_search", "get_mechanism", "ot_target_associated_diseases"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["compound_search", "get_mechanism"],
            "primary_tool": "compound_search",
            "query_keywords": ["metformin"],
            "gate": ["tool_invoked"],
            # 专家金标准：二甲双胍的经典分子机制线索（AMPK / 复合物 I），不在 prompt 里
            "gold": {"must_mention": ["AMPK", "complex I", "呼吸链复合物", "线粒体"], "mode": "any"},
        },
    },
    {
        "id": "dr_compile",
        "prompt": "用户问「阿司匹林还能不能用在结直肠癌预防」。用 compile_research_question 编译问题"
                  "（应识别为 drug-repurposing 原型），再按 toolchain 起手。",
        "tools": schemas.resolve(["compile_research_question", "search_trials", "search_articles"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["compile_research_question"],
            "primary_tool": "compile_research_question",
            "query_keywords": ["阿司匹林", "结直肠癌"],
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "dr_clinical_precedent",
        "prompt": "有人提议把 thalidomide 重定位到某炎症病。先查现有临床证据：search_trials "
                  "intervention=thalidomide 看有没有相关试验（失败/在跑/没做过），别只凭机制下结论。",
        "tools": schemas.resolve(["search_trials", "search_articles"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["search_trials"],
            "primary_tool": "search_trials",
            "query_keywords": ["thalidomide"],
            "require_grounding": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "dr_repurpose_uncertainty",
        "prompt": "评估「西地那非（sildenafil）重定位到肺纤维化」是否值得做。检索机制与临床证据后，"
                  "结尾**必须**给不确定性五段面板，尤其把已有的阴性试验列进 Conflicts。",
        "tools": schemas.resolve(["compound_search", "get_mechanism", "search_trials", "evidence_graph"]),
        "max_tokens": 1300,
        "rubric": {
            "expect_tools": ["search_trials"],
            "require_uncertainty": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "dr_safety_block",
        "prompt": "某老药机制上适合某新适应症，但它有已知的严重安全性问题。评估重定位时不能只看疗效，"
                  "检索时要显式检查安全性/禁忌是否卡住该适应症。给出你的检索计划并至少调一个检索工具。",
        "tools": schemas.resolve(["search_articles", "search_trials"]),
        "max_tokens": 800,
        "rubric": {
            "expect_tools": ["search_articles"],
            "force_tool_dim": True,
            "gate": ["tool_invoked"],
        },
    },
]

for _c in CASES:
    _c["category"] = CATEGORY
    _c.setdefault("tool_choice", "auto")
