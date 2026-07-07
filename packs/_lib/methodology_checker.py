"""Methodology checklist engine for scientific critique.

The checker does not read papers. A model or caller supplies per-check
judgments after reading the study; this module validates those judgments,
adds metadata-derived flags, and computes deterministic quality arithmetic.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


CHECKLIST: List[Dict[str, Any]] = [
    {
        "id": "METH-01",
        "name": "missing control group",
        "name_zh": "对照组缺失",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-02",
        "name": "randomization concerns",
        "name_zh": "随机化存疑",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-03",
        "name": "underpowered sample size",
        "name_zh": "样本量不足",
        "grade_domain": "imprecision",
        "auto_detectable": True,
    },
    {
        "id": "METH-04",
        "name": "selective reporting",
        "name_zh": "选择性报告",
        "grade_domain": "risk_of_bias",
        "auto_detectable": False,
    },
    {
        "id": "METH-05",
        "name": "multiple testing without correction",
        "name_zh": "多重检验未校正",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-06",
        "name": "confounding risk",
        "name_zh": "混杂因素",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-07",
        "name": "high attrition",
        "name_zh": "失访率过高",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-08",
        "name": "measurement bias",
        "name_zh": "测量偏倚",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-09",
        "name": "excessive subgroup analysis",
        "name_zh": "亚组分析过多",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
    {
        "id": "METH-10",
        "name": "inappropriate statistical method",
        "name_zh": "统计方法不当",
        "grade_domain": "risk_of_bias",
        "auto_detectable": True,
    },
]

CHECK_BY_ID = {c["id"]: c for c in CHECKLIST}
_FINDING_RANK = {"none": 0, "minor": 1, "major": 2, "critical": 3}
_PENALTY = {"none": 0, "minor": 6, "major": 16, "critical": 28}


def _lower_blob(*values: Any) -> str:
    parts: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            parts.append(_lower_blob(*value.values()))
        elif isinstance(value, (list, tuple, set)):
            parts.append(_lower_blob(*value))
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _auto(check_id: str, finding: str, reason: str, signals: List[str]) -> Dict[str, Any]:
    return {
        "check_id": check_id,
        "check_name": CHECK_BY_ID[check_id]["name"],
        "check_name_zh": CHECK_BY_ID[check_id]["name_zh"],
        "finding": finding,
        "reason": reason,
        "signals": signals,
        "grade_domain": CHECK_BY_ID[check_id]["grade_domain"],
    }


def auto_detect_methodology(metadata: Optional[Dict[str, Any]] = None, intensity: str = "standard") -> List[Dict[str, Any]]:
    """Return checklist flags inferred from structured metadata."""
    metadata = metadata or {}
    text = _lower_blob(metadata.get("method_text"), metadata.get("abstract"), metadata.get("notes"))
    design = str(metadata.get("design") or metadata.get("study_design") or "").lower()
    endpoint = str(metadata.get("endpoint_type") or metadata.get("endpoint") or "").lower()
    auto: List[Dict[str, Any]] = []

    has_comparator = metadata.get("has_control_group")
    if has_comparator is None:
        has_comparator = any(w in text for w in ("control", "placebo", "comparator", "standard of care", "对照", "安慰剂"))
    comparative_claim = bool(metadata.get("comparative_claim")) or any(
        w in text for w in ("compared with", "versus", "vs", "superior", "noninferior", "优于", "相比")
    )
    if ("single-arm" in design or "uncontrolled" in design or metadata.get("single_arm")) and comparative_claim and not has_comparator:
        auto.append(_auto(
            "METH-01",
            "major",
            "Single-arm or uncontrolled evidence is being used for a comparative efficacy claim.",
            [f"design={design or 'single_arm'}", "has_control_group=false"],
        ))

    if ("rct" in design or "randomized" in design or "randomised" in design) and not any(
        w in text for w in ("blind", "masked", "allocation conceal", "stratified random", "盲法", "分层随机", "分配隐藏")
    ):
        auto.append(_auto(
            "METH-02",
            "minor",
            "The study is labeled randomized/RCT, but blinding or allocation-concealment details are not visible in metadata.",
            [f"design={design}", "blind_or_allocation_signal=missing"],
        ))

    n = _as_int(metadata.get("sample_size") or metadata.get("n") or metadata.get("n_participants"))
    if n is not None and n < 30:
        auto.append(_auto(
            "METH-03",
            "major",
            "Sample size is below 30, making precision and power fragile.",
            [f"n={n}"],
        ))
    elif intensity == "aggressive" and n is not None and n < 100:
        auto.append(_auto(
            "METH-03",
            "minor",
            "Sample size is below 100 and should not support categorical claims without power justification.",
            [f"n={n}"],
        ))

    endpoints = _as_int(metadata.get("endpoint_count") or metadata.get("n_endpoints"))
    correction_text = text + " " + str(metadata.get("multiple_testing_correction") or "").lower()
    if endpoints is not None and endpoints >= 5 and not any(w in correction_text for w in ("fdr", "bonferroni", "holm", "q-value", "multiplicity", "校正")):
        auto.append(_auto(
            "METH-05",
            "major",
            "At least five endpoints were assessed without a visible multiplicity correction.",
            [f"endpoint_count={endpoints}", "multiplicity_correction=missing"],
        ))

    observational = any(w in design for w in ("observational", "cohort", "case-control", "retrospective", "cross-sectional", "观察"))
    adjustment_text = text + " " + str(metadata.get("adjustment") or "").lower()
    if observational and not any(w in adjustment_text for w in ("propensity", "multivariable", "multivariate", "instrumental variable", "matching", "ipw", "倾向", "多变量", "匹配")):
        auto.append(_auto(
            "METH-06",
            "major",
            "Observational evidence lacks a visible confounding adjustment strategy.",
            [f"design={design}", "adjustment_signal=missing"],
        ))

    attrition = metadata.get("loss_to_followup_pct")
    try:
        attrition_f = float(attrition) if attrition is not None else None
    except (TypeError, ValueError):
        attrition_f = None
    if attrition_f is not None and attrition_f > 20 and "sensitivity" not in text and "敏感性" not in text:
        auto.append(_auto(
            "METH-07",
            "major",
            "Loss to follow-up exceeds 20% without a visible sensitivity analysis.",
            [f"loss_to_followup_pct={attrition_f}"],
        ))

    blinded = metadata.get("blinded")
    subjective = endpoint in {"subjective", "patient-reported", "symptom", "quality-of-life"} or any(
        w in endpoint for w in ("subjective", "symptom", "quality", "主观", "症状", "生活质量")
    )
    if blinded is False and subjective:
        auto.append(_auto(
            "METH-08",
            "major",
            "A subjective endpoint was assessed without blinding.",
            ["blinded=false", f"endpoint_type={endpoint}"],
        ))

    subgroups = _as_int(metadata.get("subgroup_count") or metadata.get("n_subgroups"))
    prereg = bool(metadata.get("preregistered") or metadata.get("pre_registered"))
    if subgroups is not None and subgroups >= 5 and not prereg:
        auto.append(_auto(
            "METH-09",
            "major",
            "At least five subgroup analyses are reported without a preregistration signal.",
            [f"subgroup_count={subgroups}", "preregistered=false"],
        ))

    stat_text = str(metadata.get("statistical_methods") or metadata.get("statistics") or "").lower()
    if "chi" in stat_text and any(w in stat_text for w in ("continuous", "mean", "sd", "连续")):
        auto.append(_auto(
            "METH-10",
            "critical",
            "Chi-square testing is described for continuous variables.",
            ["statistical_methods=chi-square+continuous"],
        ))
    if "t-test" in stat_text and any(w in stat_text for w in ("non-normal", "skewed", "mann-whitney", "非正态", "偏态")):
        auto.append(_auto(
            "METH-10",
            "major",
            "A t-test appears to be used despite non-normal or skewed distribution signals.",
            ["statistical_methods=t-test+non-normal"],
        ))

    return auto


def _normalize_judgment(raw: Dict[str, Any]) -> Dict[str, Any]:
    check_id = str(raw.get("check_id") or raw.get("id") or "").upper()
    finding = str(raw.get("finding") or "none").lower().replace("_", "-")
    if finding == "not_applicable":
        finding = "none"
    if finding not in _FINDING_RANK:
        finding = "none"
    return {
        "check_id": check_id,
        "finding": finding,
        "reason": str(raw.get("reason") or ""),
        "evidence_snippet": str(raw.get("evidence_snippet") or raw.get("snippet") or ""),
        "source": raw.get("source") or "model",
    }


def evaluate_methodology(
    judgments: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    intensity: str = "standard",
) -> Dict[str, Any]:
    """Validate checklist judgments and compute a 0-100 methodology score."""
    judgments = judgments or []
    by_id: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    unknown: List[str] = []

    for raw in judgments:
        item = _normalize_judgment(raw)
        cid = item["check_id"]
        if cid not in CHECK_BY_ID:
            if cid:
                unknown.append(cid)
                warnings.append(f"Unknown methodology check_id: {cid}")
            continue
        prev = by_id.get(cid)
        if prev is None or _FINDING_RANK[item["finding"]] >= _FINDING_RANK[prev["finding"]]:
            by_id[cid] = item

    for check in CHECKLIST:
        by_id.setdefault(check["id"], {
            "check_id": check["id"],
            "finding": "none",
            "reason": "",
            "evidence_snippet": "",
            "source": "default",
        })

    for item in by_id.values():
        if item["finding"] in {"major", "critical"} and not item["reason"]:
            warnings.append(f"{item['check_id']} finding={item['finding']} requires a reason.")
        if item["finding"] == "critical" and not item["evidence_snippet"]:
            warnings.append(f"{item['check_id']} finding=critical should include an evidence_snippet.")

    auto = auto_detect_methodology(metadata, intensity=intensity)
    contradictions: List[Dict[str, Any]] = []
    for flag in auto:
        cid = flag["check_id"]
        model_item = by_id.get(cid)
        if model_item and _FINDING_RANK[model_item["finding"]] < _FINDING_RANK[flag["finding"]]:
            contradictions.append({
                "check_id": cid,
                "model_finding": model_item["finding"],
                "auto_finding": flag["finding"],
                "reason": flag["reason"],
                "signals": flag["signals"],
            })
            warnings.append(f"{cid} auto-detected {flag['finding']} but model marked {model_item['finding']}.")

    report: List[Dict[str, Any]] = []
    penalty = 0
    for check in CHECKLIST:
        item = by_id[check["id"]]
        auto_same = [a for a in auto if a["check_id"] == check["id"]]
        effective_finding = item["finding"]
        effective_reason = item["reason"]
        if auto_same:
            strongest = sorted(auto_same, key=lambda x: _FINDING_RANK[x["finding"]], reverse=True)[0]
            if _FINDING_RANK[strongest["finding"]] > _FINDING_RANK[effective_finding]:
                effective_finding = strongest["finding"]
                effective_reason = strongest["reason"]
        penalty += _PENALTY[effective_finding]
        report.append({
            "check_id": check["id"],
            "name": check["name"],
            "name_zh": check["name_zh"],
            "grade_domain": check["grade_domain"],
            "finding": item["finding"],
            "effective_finding": effective_finding,
            "reason": item["reason"],
            "evidence_snippet": item["evidence_snippet"],
            "auto_detected": auto_same,
            "effective_reason": effective_reason,
        })

    quality_score = max(0, 100 - penalty)
    by_grade: Dict[str, List[str]] = {}
    for row in report:
        if row["effective_finding"] != "none":
            by_grade.setdefault(row["grade_domain"], []).append(row["check_id"])

    return {
        "checklist": CHECKLIST,
        "methodology_report": report,
        "auto_detected": auto,
        "contradictions": contradictions,
        "quality_score": quality_score,
        "comparison_to_grade": by_grade,
        "warnings": warnings,
        "unknown_check_ids": unknown,
    }

