"""临床试验类 gold cases。考核：能否用 search_trials/get_trial_details 拿到真实 NCT + 正确使用其结果。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schemas  # noqa: E402

CATEGORY = "clinical_trials"

CASES = [
    {
        "id": "ct_pembro_nsclc",
        "prompt": "找一个 pembrolizumab 在 NSCLC 一线的 Phase 3 recruiting 试验的 NCT 号。"
                  "用 search_trials，condition=non-small cell lung cancer，intervention=pembrolizumab。",
        "tools": schemas.resolve(["search_trials", "get_trial_details"]),
        "max_tokens": 400,
        "rubric": {
            "expect_tools": ["search_trials"],
            "primary_tool": "search_trials",
            "query_keywords": ["non-small cell lung cancer", "pembrolizumab"],
            "require_grounding": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ct_kras_landscape",
        "prompt": "分析 KRAS G12C 抑制剂的临床管线：search_trials 用 intervention=sotorasib 拉一批，"
                  "再 intervention=adagrasib 拉一批，然后按 phase 分层报告 NCT 清单。",
        "tools": schemas.resolve(["search_trials"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["search_trials"],
            "require_grounding": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ct_detail_endpoints",
        "prompt": "试验 NCT02576574 的主要终点、入组人数、期别是什么？用 get_trial_details 查 nct_id=NCT02576574，"
                  "只报告工具返回的字段，别自己补。",
        "tools": schemas.resolve(["get_trial_details"]),
        "max_tokens": 500,
        "rubric": {
            "expect_tools": ["get_trial_details"],
            "require_grounding": True,
            "require_semantic": True,  # 报告的入组数/期别等数字必须来自工具返回，不能编
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ct_endpoint_compare",
        "prompt": "比较 CDK4/6 抑制剂在 HR+ 乳腺癌里几项 III 期的主要终点定义（PFS vs OS）。"
                  "用 search_trials 找试验，用 analyze_endpoints 做终点比较。",
        "tools": schemas.resolve(["search_trials", "analyze_endpoints"]),
        "max_tokens": 900,
        "rubric": {
            "expect_tools": ["search_trials", "analyze_endpoints"],
            "require_grounding": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ct_landscape_uncertainty",
        "prompt": "给「Lecanemab 类抗淀粉样蛋白抗体在早期 AD」的临床试验 landscape，用 search_trials 检索。"
                  "报告后**必须**给不确定性五段面板：已确证什么、还不知道什么、有哪些相反结果、缺什么数据、下一步该看什么试验。",
        "tools": schemas.resolve(["search_trials", "get_trial_details"]),
        "max_tokens": 1200,
        "rubric": {
            "expect_tools": ["search_trials"],
            "require_grounding": True,
            "require_uncertainty": True,
            "gate": ["tool_invoked"],
        },
    },
    {
        "id": "ct_no_fabricate",
        "prompt": "有没有「口服胰岛素治愈 1 型糖尿病」的已完成 III 期试验？用 search_trials 查 "
                  "condition=type 1 diabetes，intervention=oral insulin。如果没有符合的，直说没有，不要编 NCT。",
        "tools": schemas.resolve(["search_trials"]),
        "max_tokens": 500,
        "rubric": {
            "expect_tools": ["search_trials"],
            "primary_tool": "search_trials",
            "query_keywords": ["type 1 diabetes", "oral insulin"],
            # 反幻觉 case：不强制 grounding（正确答案可能是"没有"），但 linter 会抓编造的 NCT
            "gate": ["tool_invoked"],
        },
    },
]

for _c in CASES:
    _c["category"] = CATEGORY
    _c.setdefault("tool_choice", "auto")
