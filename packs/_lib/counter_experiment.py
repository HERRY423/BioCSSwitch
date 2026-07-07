"""Counter-experiment design templates for critique findings."""

from __future__ import annotations

import re
from urllib.parse import quote_plus
from typing import Any, Dict, List, Optional


_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "EX-01": {
        "design_type": "human PK/PD bridging study or Phase I dose-escalation",
        "design_type_zh": "人体 PK/PD 桥接研究或 I 期剂量递增试验",
        "primary_endpoint": "human exposure, target engagement, safety, and pharmacodynamic biomarker",
        "primary_endpoint_zh": "人体暴露量、靶点占用/药效标志物与安全性",
        "key_controls": ["predefined stopping rules", "dose/exposure monitoring", "human biomarker assay"],
        "timeline": "6-18 months",
        "cost_tier": "high",
        "sample_size": "Typical Phase I/PK-PD bridging: 20-60 participants; final n requires protocol-specific power/safety rules.",
    },
    "EX-02": {
        "design_type": "target-population subgroup validation",
        "design_type_zh": "目标人群亚组验证",
        "primary_endpoint": "same outcome measured in the underrepresented subgroup",
        "primary_endpoint_zh": "在缺失/目标亚组中重复测量同一主要终点",
        "key_controls": ["pre-specified subgroup", "interaction test", "balanced baseline covariates"],
        "timeline": "3-12 months if retrospective; 12-36 months if prospective",
        "cost_tier": "medium",
        "sample_size": "Start from subgroup interaction power; often at least 100-200 per major subgroup for clinical outcomes.",
    },
    "EX-03": {
        "design_type": "stage-specific prospective cohort or stratified trial",
        "design_type_zh": "目标分期前瞻性队列或分层试验",
        "primary_endpoint": "outcome in the target stage/treatment line",
        "primary_endpoint_zh": "目标分期/治疗线内的主要结局",
        "key_controls": ["stage-stratified enrollment", "standardized staging criteria", "pre-specified analysis"],
        "timeline": "12-36 months",
        "cost_tier": "medium-high",
        "sample_size": "Power the target stage separately; do not borrow precision from other stages without a justified hierarchical model.",
    },
    "EX-04": {
        "design_type": "dose-ranging or exposure-response study",
        "design_type_zh": "剂量探索或暴露-反应研究",
        "primary_endpoint": "dose-response gradient, safety margin, and target engagement",
        "primary_endpoint_zh": "剂量-反应梯度、安全窗与靶点占用",
        "key_controls": ["multiple dose arms", "predefined exposure bins", "toxicity monitoring"],
        "timeline": "6-18 months",
        "cost_tier": "medium-high",
        "sample_size": "Use trend-test or model-based dose-response power; include at least 3 exposure levels when feasible.",
    },
    "EX-05": {
        "design_type": "hard-endpoint randomized controlled trial",
        "design_type_zh": "硬终点随机对照试验",
        "primary_endpoint": "overall survival, mortality, hospitalization, or another patient-important endpoint",
        "primary_endpoint_zh": "总生存、死亡率、住院率或其他患者重要硬终点",
        "key_controls": ["randomized comparator", "event-driven design", "blinded endpoint adjudication"],
        "timeline": "24-60 months",
        "cost_tier": "very high",
        "sample_size": "Event-driven log-rank/Cox design. Skeleton: events = 4*(z_alpha/2+z_beta)^2/(ln(HR))^2.",
    },
    "EX-06": {
        "design_type": "extended follow-up or registry-linked outcome study",
        "design_type_zh": "延长随访或登记系统链接研究",
        "primary_endpoint": "durability of effect at the clinically relevant time horizon",
        "primary_endpoint_zh": "目标时间窗内疗效持久性/长期安全性",
        "key_controls": ["predefined follow-up window", "attrition sensitivity analysis", "registry linkage if possible"],
        "timeline": "12-60 months",
        "cost_tier": "medium",
        "sample_size": "Power by expected event rate at the long-term horizon; plan attrition inflation.",
    },
    "EX-07": {
        "design_type": "human tissue biomarker validation or early translational study",
        "design_type_zh": "人体组织生物标志物验证或早期转化研究",
        "primary_endpoint": "mechanism observed in target human tissue plus clinical correlation",
        "primary_endpoint_zh": "目标人体组织中的机制复现及其临床相关性",
        "key_controls": ["orthogonal biomarker assay", "negative/positive controls", "matched clinical metadata"],
        "timeline": "6-24 months",
        "cost_tier": "medium",
        "sample_size": "Pilot translational validation often starts at 20-50 human samples; power depends on biomarker variance.",
    },
}


def _claim_terms(claim_text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", claim_text or "")
    if not tokens:
        return (claim_text or "clinical trial").strip()[:80]
    stop = {"the", "and", "for", "with", "that", "this", "patients", "clinical"}
    keep = [t for t in tokens if t.lower() not in stop][:8]
    return " ".join(keep) or "clinical trial"


def design_counter_experiment(
    claim_text: str,
    extrapolations: Optional[List[Dict[str, Any]]] = None,
    boundary: Optional[Dict[str, Any]] = None,
    language: str = "zh",
) -> Dict[str, Any]:
    """Map critique rules to a minimal falsification or validation design."""
    extrapolations = extrapolations or []
    boundary = boundary or {}
    rule_ids = [e.get("rule_id") for e in extrapolations if e.get("rule_id")]
    if not rule_ids:
        rule_ids = ["EX-03" if boundary.get("disease_stage") else "EX-07"]
    primary_rule = sorted(rule_ids, key=lambda r: {"EX-05": 0, "EX-01": 1, "EX-07": 2}.get(str(r), 5))[0]
    tmpl = _TEMPLATES.get(primary_rule, _TEMPLATES["EX-07"])
    terms = _claim_terms(claim_text)
    ctgov_query = quote_plus(terms)
    purpose = (
        f"验证或推翻该结论是否能跨越 {primary_rule} 所标记的证据边界。"
        if language == "zh"
        else f"Test whether the claim survives the evidence boundary flagged by {primary_rule}."
    )
    return {
        "primary_rule": primary_rule,
        "purpose": purpose,
        "design_type": tmpl["design_type_zh"] if language == "zh" else tmpl["design_type"],
        "minimum_sample_size": tmpl["sample_size"],
        "primary_endpoint": tmpl["primary_endpoint_zh"] if language == "zh" else tmpl["primary_endpoint"],
        "key_controls": tmpl["key_controls"],
        "estimated_timeline": tmpl["timeline"],
        "cost_tier": tmpl["cost_tier"],
        "existing_trials": {
            "note": "需要用户或联网工具进一步核查 ClinicalTrials.gov；此处只生成可复核检索入口。"
            if language == "zh" else "Check ClinicalTrials.gov; this tool only creates a verifiable search entry.",
            "clinicaltrials_gov_search": f"https://clinicaltrials.gov/search?term={ctgov_query}",
        },
        "power_snippet": (
            "For binary outcomes: n_per_arm ~= 2*(z_alpha/2+z_beta)^2*pbar*(1-pbar)/(p1-p2)^2. "
            "For time-to-event: events ~= 4*(z_alpha/2+z_beta)^2/(ln(HR))^2."
        ),
        "boundary_used": boundary,
    }

