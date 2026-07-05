#!/usr/bin/env python3
"""GRADE / SoF 引擎 MCP —— 给每个 outcome 评「证据确定性有多高、为什么」。

GRADE（Grading of Recommendations Assessment, Development and Evaluation）是顶级
医学（Cochrane / WHO / UpToDate / 各国指南）评证据确定性的事实标准。核心：**不是给
一篇文献评级，而是给一个 outcome 的整体证据体（body of evidence）评级**，产出四档
确定性 High / Moderate / Low / Very Low，并逐条说明升/降级理由。

本引擎的分工（与 bio-audit 一贯的「工具定死算术、模型给判断」原则一致）：
  - **模型**：读文献后对 5 个降级域 + 3 个升级域给出判断（serious / very serious + 理由）。
    这些需要读研究才能判断（如"结果不一致"），工具替代不了。
  - **工具**：把设计→起始档、升降级→算术**确定性地**算完，产出确定性符号（⊕⊕⊕⊝）+
    结构化理由 + GRADE 规则守卫（RCT 不能升级、无理由的降级要警告……），并渲染 SoF 表。

这样模型无法含糊地说"中等确定性"——它必须逐域声明"为什么"，工具把算术锁死、把规则违背暴露。

工具：
  grade_outcome      — 单个 outcome 的确定性评级（起始档 + 降级 + 升级 → 四档 + 逐域理由）
  grade_sof_table    — 跨 outcome 汇总成 Summary of Findings 表（Markdown）
  grade_explain      — 返回 GRADE 域定义速查（透明度，便于模型/用户对齐判断标准）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-audit-grade", "0.2.0")


# ---------------------------------------------------------------------------
# 起始确定性：由研究设计决定
# ---------------------------------------------------------------------------
# GRADE：RCT 体系起于 High(4)；非随机/观察性起于 Low(2)；病例系列/机制起于 Very Low(1)。
# 关键：**meta-analysis / systematic-review 本身不决定起始档**——取决于纳入研究设计；
# **"clinical-trial" 是模糊词**，必须拆成随机对照(High) vs 单臂/非随机(Low)。二者都不许默认 High。
_DESIGN_START = {
    # —— RCT 体系 → High(4) ——
    "rct": 4, "randomized": 4, "randomised": 4,
    "randomized-controlled-trial": 4, "randomised-controlled-trial": 4,
    "systematic-review-of-rcts": 4, "meta-analysis-of-rcts": 4,
    # —— 非随机干预 / 观察性 → Low(2) ——
    "non-randomized-trial": 2, "nonrandomized": 2, "non-randomised-trial": 2,
    "single-arm-trial": 2, "single-arm": 2, "uncontrolled-trial": 2,
    "quasi-experimental": 2, "phase-1": 2, "phase-2-single-arm": 2, "nrsi": 2,
    "observational": 2, "cohort": 2, "prospective-cohort": 2, "retrospective-cohort": 2,
    "case-control": 2, "cross-sectional": 2, "nested-case-control": 2,
    # —— 更弱 → Very Low(1) ——
    "case-series": 1, "case-report": 1, "mechanistic": 1, "in-vitro": 1,
    "animal": 1, "preclinical": 1,
}
# 只有"真·RCT 体系"才禁止升级；单臂/非随机试验按观察性，可升级。
_DESIGN_IS_RCT = {"rct", "randomized", "randomised", "randomized-controlled-trial",
                  "randomised-controlled-trial", "systematic-review-of-rcts",
                  "meta-analysis-of-rcts"}
# 需要拆类型 / 需要 underlying_design 才能定档的模糊设计词。
_AMBIGUOUS_TRIAL = {"clinical-trial", "clinical trial", "trial", "interventional"}
_META_LIKE = {"meta-analysis", "systematic-review", "meta analysis", "systematic review",
              "sr", "ma"}

_LEVEL_NAME = {4: "High", 3: "Moderate", 2: "Low", 1: "Very Low"}
_LEVEL_SYMBOL = {4: "⊕⊕⊕⊕", 3: "⊕⊕⊕⊝", 2: "⊕⊕⊝⊝", 1: "⊕⊝⊝⊝"}
_LEVEL_ZH = {4: "高", 3: "中", 2: "低", 1: "极低"}

_DOWNGRADE_DOMAINS = ["risk_of_bias", "inconsistency", "indirectness",
                      "imprecision", "publication_bias"]
_DOWNGRADE_ZH = {
    "risk_of_bias": "偏倚风险", "inconsistency": "不一致性", "indirectness": "间接性",
    "imprecision": "不精确性", "publication_bias": "发表偏倚",
}
_UPGRADE_DOMAINS = ["large_effect", "dose_response", "plausible_confounding"]
_UPGRADE_ZH = {
    "large_effect": "大效应量", "dose_response": "剂量-反应梯度",
    "plausible_confounding": "残余混杂会削弱效应",
}

_SERIOUS_POINTS = {"not_serious": 0, "none": 0, "serious": -1, "very_serious": -2}
_LARGE_POINTS = {"none": 0, "large": 1, "very_large": 2}
_PRESENT_POINTS = {"none": 0, "present": 1, "would_reduce": 1}


def _start_certainty(design: str, underlying_design: Optional[str]):
    """返回 (起始档 int, warnings list, is_rct_body bool)。
    刻意不给 meta/SR、模糊 clinical-trial 默认 High——那是原来的 bug。"""
    d = (design or "").strip().lower()
    u = (underlying_design or "").strip().lower()
    warns: List[str] = []

    # meta-analysis / systematic-review：起始档取决于纳入研究设计
    if d in _META_LIKE:
        if not u:
            warns.append("meta-analysis / systematic-review 的起始确定性取决于纳入研究设计，"
                         "未声明 underlying_design → 保守按 observational(Low) 处理；"
                         "请补 underlying_design=rct 或 observational")
            return 2, warns, False
        if u in _DESIGN_START:
            is_rct = u in _DESIGN_IS_RCT or u in ("rct", "randomized")
            return _DESIGN_START[u], warns, is_rct
        warns.append(f"underlying_design='{underlying_design}' 无法识别 → 保守按 observational(Low)")
        return 2, warns, False

    # 模糊的 "clinical-trial"：必须拆随机与否
    if d in _AMBIGUOUS_TRIAL:
        warns.append("'clinical-trial' 未指明是否随机对照——GRADE 需拆类型："
                     "随机对照(RCT)→High，单臂/非随机→observational(Low)。"
                     "已保守按非随机(Low) 处理；请把 design 改成 rct / single-arm-trial / "
                     "non-randomized-trial 等明确类型")
        return 2, warns, False

    if d in _DESIGN_START:
        return _DESIGN_START[d], warns, (d in _DESIGN_IS_RCT)

    warns.append(f"design='{design}' 无法识别 → 保守按 observational(Low)；"
                 "请用明确设计词（rct / cohort / case-control / single-arm-trial …）")
    return 2, warns, False


@server.tool(
    "grade_outcome",
    "Rate the CERTAINTY OF EVIDENCE for ONE outcome using GRADE. Starting certainty is set by study "
    "design (RCT→High, non-randomized/single-arm/observational→Low, case-series→Very Low). NOTE: bare "
    "'clinical-trial' is ambiguous — pass rct / single-arm-trial / non-randomized-trial. meta-analysis / "
    "systematic-review do NOT default to High — pass underlying_design (rct|observational) or they are "
    "treated conservatively as Low with a warning. The MODEL supplies per-domain judgments (with reasons) for "
    "5 downgrade domains (risk_of_bias, inconsistency, indirectness, imprecision, publication_bias) and, "
    "for observational bodies only, 3 upgrade domains (large_effect, dose_response, plausible_confounding). "
    "The TOOL does the GRADE arithmetic deterministically, returns the four-level certainty "
    "(High/Moderate/Low/Very Low with ⊕ symbols), a per-domain justification of WHY, and GRADE rule "
    "warnings (e.g. RCTs can't be upgraded; serious downgrades need a reason).",
    {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "description": "The specific outcome, e.g. 'all-cause mortality at 12 months'"},
            "design": {"type": "string",
                       "description": "rct / observational / cohort / case-control / case-series / meta-analysis / systematic-review"},
            "underlying_design": {"type": "string",
                                  "description": "For meta-analysis/SR: the design of included studies (rct/observational)"},
            "n_studies": {"type": "integer"},
            "n_participants": {"type": "integer"},
            "domains": {
                "type": "object",
                "description": "Downgrade judgments. Each: {rating: not_serious|serious|very_serious, reason: str}.",
                "properties": {
                    "risk_of_bias": {"type": "object"},
                    "inconsistency": {"type": "object"},
                    "indirectness": {"type": "object"},
                    "imprecision": {"type": "object"},
                    "publication_bias": {"type": "object"},
                },
            },
            "upgrades": {
                "type": "object",
                "description": "Observational only. large_effect:{rating:none|large|very_large}, "
                               "dose_response:{rating:none|present}, plausible_confounding:{rating:none|would_reduce}.",
            },
            "effect": {
                "type": "object",
                "description": "Optional effect for SoF: {measure: 'RR'|'HR'|'OR'|'MD', value, ci_low, ci_high}.",
            },
        },
        "required": ["outcome", "design", "domains"],
    },
)
def grade_outcome(outcome: str, design: str, domains: Dict[str, Any],
                  underlying_design: Optional[str] = None,
                  n_studies: Optional[int] = None, n_participants: Optional[int] = None,
                  upgrades: Optional[Dict[str, Any]] = None,
                  effect: Optional[Dict[str, Any]] = None):
    start, start_warns, is_rct = _start_certainty(design, underlying_design)
    warnings: List[str] = list(start_warns)

    # ---- 降级 ----
    downgrade_total = 0
    downgrade_detail: List[Dict[str, Any]] = []
    for dom in _DOWNGRADE_DOMAINS:
        d = (domains or {}).get(dom) or {}
        rating = (d.get("rating") or "not_serious").lower()
        pts = _SERIOUS_POINTS.get(rating, 0)
        reason = d.get("reason") or ""
        if pts < 0 and not reason:
            warnings.append(f"{_DOWNGRADE_ZH[dom]}判为 {rating} 却未给理由——GRADE 要求每次降级都说明「为什么」")
        downgrade_total += pts
        downgrade_detail.append({"domain": dom, "domain_zh": _DOWNGRADE_ZH[dom],
                                 "rating": rating, "points": pts, "reason": reason})

    # ---- 升级（仅观察性、且无降级时才考虑）----
    upgrade_total = 0
    upgrade_detail: List[Dict[str, Any]] = []
    ups = upgrades or {}
    if ups:
        if is_rct:
            warnings.append("研究设计属 RCT 体系，GRADE 规定不可升级——已忽略 upgrades")
        else:
            if downgrade_total < 0:
                warnings.append("存在降级项时通常不再升级（GRADE：升级仅用于无严重局限的观察性证据）——请复核")
            le = (ups.get("large_effect") or {})
            le_pts = _LARGE_POINTS.get((le.get("rating") or "none").lower(), 0)
            dr = (ups.get("dose_response") or {})
            dr_pts = _PRESENT_POINTS.get((dr.get("rating") or "none").lower(), 0)
            pc = (ups.get("plausible_confounding") or {})
            pc_pts = _PRESENT_POINTS.get((pc.get("rating") or "none").lower(), 0)
            for name, pts, obj in [("large_effect", le_pts, le), ("dose_response", dr_pts, dr),
                                   ("plausible_confounding", pc_pts, pc)]:
                if pts > 0:
                    upgrade_detail.append({"domain": name, "domain_zh": _UPGRADE_ZH[name],
                                           "points": pts, "reason": obj.get("reason") or ""})
            upgrade_total = le_pts + dr_pts + pc_pts

    final = start + downgrade_total + upgrade_total
    final = max(1, min(4, final))

    # 不精确性常与样本量相关：给个软提示
    if n_participants is not None and n_participants < 300 and \
            (domains.get("imprecision", {}) or {}).get("rating", "not_serious") == "not_serious":
        warnings.append(f"总样本 {n_participants} < 300，通常需考虑不精确性（imprecision）是否 serious")

    certainty_reasons = [
        f"起始档：{design} → {_LEVEL_NAME[start]}（{_LEVEL_ZH[start]}）"]
    for dd in downgrade_detail:
        if dd["points"] < 0:
            certainty_reasons.append(
                f"↓ {dd['domain_zh']} {dd['rating']}（{dd['points']}）：{dd['reason'] or '未说明'}")
    for ud in upgrade_detail:
        certainty_reasons.append(f"↑ {ud['domain_zh']}（+{ud['points']}）：{ud['reason'] or '未说明'}")

    return {
        "outcome": outcome,
        "certainty": _LEVEL_NAME[final],
        "certainty_zh": _LEVEL_ZH[final],
        "symbol": _LEVEL_SYMBOL[final],
        "score": final,
        "starting_certainty": _LEVEL_NAME[start],
        "downgrade_total": downgrade_total,
        "upgrade_total": upgrade_total,
        "downgrade_detail": downgrade_detail,
        "upgrade_detail": upgrade_detail,
        "why": certainty_reasons,
        "warnings": warnings,
        "n_studies": n_studies,
        "n_participants": n_participants,
        "effect": effect,
    }


@server.tool(
    "grade_sof_table",
    "Build a GRADE Summary of Findings (SoF) table (Markdown) from a list of graded outcomes "
    "(each = the output of grade_outcome). Columns: Outcome · № participants (studies) · Effect · "
    "Certainty (⊕) · Why (key downgrades). This is the artifact top-tier medical reviews put in front "
    "of clinicians — certainty per outcome, with the reason visible.",
    {
        "type": "object",
        "properties": {
            "graded_outcomes": {
                "type": "array",
                "description": "List of grade_outcome results.",
                "items": {"type": "object"},
            },
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
        },
        "required": ["graded_outcomes"],
    },
)
def grade_sof_table(graded_outcomes: List[Dict[str, Any]], language: str = "zh"):
    if language == "zh":
        headers = ["Outcome（结局）", "参与者 (研究数)", "效应量", "证据确定性", "关键降级理由"]
    else:
        headers = ["Outcome", "№ participants (studies)", "Effect", "Certainty", "Key reasons"]
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for g in graded_outcomes:
        outcome = (g.get("outcome") or "").replace("|", "\\|")
        n_p = g.get("n_participants")
        n_s = g.get("n_studies")
        part = f"{n_p if n_p is not None else '—'} ({n_s if n_s is not None else '?'})"
        eff = g.get("effect") or {}
        if eff.get("measure"):
            ci = ""
            if eff.get("ci_low") is not None and eff.get("ci_high") is not None:
                ci = f" [{eff['ci_low']}, {eff['ci_high']}]"
            eff_s = f"{eff.get('measure')} {eff.get('value', '?')}{ci}"
        else:
            eff_s = "—"
        cert = f"{g.get('symbol', '')} {g.get('certainty', '?')}"
        key = "; ".join(d["domain_zh"] for d in (g.get("downgrade_detail") or [])
                        if d.get("points", 0) < 0) or "无降级"
        lines.append("| " + " | ".join([outcome, part, eff_s, cert, key]) + " |")
    # 脚注：确定性含义
    lines.append("")
    lines.append("> 证据确定性（GRADE）：⊕⊕⊕⊕ 高 · ⊕⊕⊕⊝ 中 · ⊕⊕⊝⊝ 低 · ⊕⊝⊝⊝ 极低。"
                 "确定性反映「我们对效应估计的把握」，不是效应大小。")
    return "\n".join(lines)


@server.tool(
    "grade_explain",
    "Return GRADE domain definitions (the 5 downgrade + 3 upgrade domains) and rating criteria — "
    "a transparency reference so the model and user align on what 'serious inconsistency' etc. mean.",
    {"type": "object", "properties": {}},
)
def grade_explain():
    return {
        "starting_certainty": {
            "RCT / systematic-review-of-rcts": "High (⊕⊕⊕⊕)",
            "非随机试验 / 单臂试验 / 观察性（cohort/case-control）": "Low (⊕⊕⊝⊝)",
            "病例系列 / 机制 / 动物 / 体外": "Very Low (⊕⊝⊝⊝)",
            "⚠ 'clinical-trial'（模糊）": "必须拆成 rct（High）或 single-arm/non-randomized（Low），不默认 High",
            "⚠ meta-analysis / systematic-review": "起始档取决于 underlying_design；未声明则保守按 Low，不默认 High",
        },
        "downgrade_domains": {
            "risk_of_bias（偏倚风险）": "研究层面的方法学缺陷：随机化/盲法/失访/选择性报告。serious=-1, very serious=-2",
            "inconsistency（不一致性）": "各研究结果方向/大小不一致，异质性大（I² 高、置信区间少重叠）",
            "indirectness（间接性）": "人群/干预/对照/结局与临床问题不完全吻合（含跨物种、替代终点）",
            "imprecision（不精确性）": "置信区间宽，或样本量/事件数不足，跨越临床决策阈值",
            "publication_bias（发表偏倚）": "小样本阳性结果被优先发表（漏斗图不对称、行业赞助）",
        },
        "upgrade_domains": {
            "large_effect（大效应量）": "RR<0.5 或 >2（+1）；RR<0.2 或 >5（+2）。仅观察性、无降级时用",
            "dose_response（剂量-反应）": "存在剂量-反应梯度（+1）",
            "plausible_confounding（残余混杂会削弱）": "所有可能的混杂只会削弱观察到的效应（+1）",
        },
        "rules": [
            "RCT 体系不可升级——升级只用于无严重局限的观察性证据。",
            "每一次降级/升级都必须给出「为什么」的具体理由，否则不透明。",
            "确定性是对'效应估计的把握'的评级，与效应大小、统计显著性是两回事。",
            "GRADE 评的是一个 outcome 的整体证据体，不是单篇文献。",
        ],
    }


# =====================================================================
# EtD：从证据确定性到推荐强度（Evidence to Decision）
# =====================================================================
# certainty 只回答"证据有多确定"。要不要**推荐**、推荐得**多强**（strong / conditional），
# 还得看 GRADE EtD 框架的其它维度：获益/危害平衡、价值观与偏好、资源/成本、公平性、
# 可接受性、可行性。工具把"从各维度判断到推荐方向+强度"的规则做成确定性映射，模型给判断。

_CERTAINTY_SCORE = {"high": 4, "moderate": 3, "low": 2, "very low": 1, "very_low": 1}
_BALANCE = {"favors_intervention", "favors_comparison", "balanced", "uncertain"}


@server.tool(
    "etd_recommendation",
    "GRADE Evidence-to-Decision layer: turn certainty of evidence + judgments on benefit/harm balance, "
    "values & preferences, resources/cost (and optionally equity/acceptability/feasibility) into a "
    "recommendation DIRECTION (for/against) and STRENGTH (strong/conditional). Deterministic mapping + "
    "GRADE guards: a STRONG recommendation on LOW/VERY-LOW certainty is flagged as a 'discordant' "
    "recommendation that must fit one of GRADE's paradigmatic exceptions or be downgraded to conditional. "
    "Certainty alone never implies a recommendation — this is the required next step after grade_outcome.",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "PICO question the recommendation answers"},
            "certainty": {"type": "string", "enum": ["High", "Moderate", "Low", "Very Low"],
                          "description": "Overall certainty across critical outcomes (from grade_outcome)"},
            "benefit_harm_balance": {
                "type": "object",
                "description": "{rating: favors_intervention|favors_comparison|balanced|uncertain, reason}",
            },
            "values_preferences": {
                "type": "object",
                "description": "{rating: no_important_variability|important_variability|uncertain, reason}",
            },
            "resources": {
                "type": "object",
                "description": "{rating: favors_intervention|favors_comparison|negligible|uncertain, reason}",
            },
            "equity": {"type": "object", "description": "optional {rating, reason}"},
            "acceptability": {"type": "object", "description": "optional {rating, reason}"},
            "feasibility": {"type": "object", "description": "optional {rating, reason}"},
        },
        "required": ["certainty", "benefit_harm_balance"],
    },
)
def etd_recommendation(certainty: str, benefit_harm_balance: Dict[str, Any],
                       question: str = "",
                       values_preferences: Optional[Dict[str, Any]] = None,
                       resources: Optional[Dict[str, Any]] = None,
                       equity: Optional[Dict[str, Any]] = None,
                       acceptability: Optional[Dict[str, Any]] = None,
                       feasibility: Optional[Dict[str, Any]] = None):
    warnings: List[str] = []
    c_score = _CERTAINTY_SCORE.get((certainty or "").strip().lower(), 1)
    balance = (benefit_harm_balance or {}).get("rating", "uncertain")
    values = (values_preferences or {}).get("rating", "uncertain")
    res = (resources or {}).get("rating", "uncertain")

    # 方向
    if balance == "favors_intervention":
        direction = "for"
    elif balance == "favors_comparison":
        direction = "against"
    else:
        direction = "either / no clear direction"

    # 强度：strong 需 ≥Moderate 确定性 + 明确净获益 + 价值观无重要变异 + 资源不反对
    strong_ok = (
        c_score >= 3
        and balance in ("favors_intervention", "favors_comparison")
        and values == "no_important_variability"
        and res != "favors_comparison"
    )
    strength = "strong" if strong_ok else "conditional"

    # GRADE 守卫：低确定性上的强推荐属"不一致推荐"
    if strength == "strong" and c_score <= 2:
        warnings.append("低/极低确定性证据上给强推荐属 GRADE『不一致(discordant)推荐』，"
                        "仅在 5 类特殊情形成立（如低确定性但获益巨大/危害极小、生命威胁、"
                        "等价选项中成本悬殊等）——必须显式说明属哪一类，否则应降为 conditional")
    if direction.startswith("either") and strength == "strong":
        strength = "conditional"
        warnings.append("获益/危害平衡不明确 → 无法给强推荐，已降为 conditional")
    if values == "important_variability" and strength == "strong":
        strength = "conditional"
        warnings.append("患者价值观/偏好存在重要变异 → GRADE 倾向 conditional（应支持共同决策）")
    for name, obj in [("resources", resources), ("values_preferences", values_preferences)]:
        if obj and obj.get("rating") and not obj.get("reason"):
            warnings.append(f"{name} 给了判断但没写理由——EtD 每个维度都要可核对")

    # 措辞（GRADE 惯例：strong='recommend'，conditional='suggest'）
    verb = "recommend" if strength == "strong" else "suggest"
    zh_strength = "强推荐" if strength == "strong" else "有条件推荐（弱推荐）"
    if direction == "for":
        statement = f"We {verb} using the intervention.（{zh_strength}使用）"
    elif direction == "against":
        statement = f"We {verb} against the intervention.（{zh_strength}不使用）"
    else:
        statement = f"证据不足以给出明确方向；建议个体化 / 共同决策（{zh_strength}框架下）。"

    rationale = [
        f"证据确定性：{certainty}",
        f"获益/危害平衡：{balance}（{(benefit_harm_balance or {}).get('reason') or '未说明'}）",
        f"价值观与偏好：{values}（{(values_preferences or {}).get('reason') or '未说明'}）",
        f"资源/成本：{res}（{(resources or {}).get('reason') or '未说明'}）",
    ]
    for label, obj in [("公平性", equity), ("可接受性", acceptability), ("可行性", feasibility)]:
        if obj and obj.get("rating"):
            rationale.append(f"{label}：{obj.get('rating')}（{obj.get('reason') or '未说明'}）")

    return {
        "question": question,
        "direction": direction,
        "strength": strength,
        "strength_zh": zh_strength,
        "statement": statement,
        "rationale": rationale,
        "warnings": warnings,
        "note": "certainty 只是 EtD 的一个维度；推荐强度由全部维度共同决定。"
                "strong→'recommend'，conditional→'suggest'（GRADE 措辞惯例）。",
    }


if __name__ == "__main__":
    server.run()
