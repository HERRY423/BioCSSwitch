"""Believability arithmetic for claim-level critique."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_SEVERITY_PENALTY = {"low": 3, "minor": 3, "moderate": 8, "major": 12, "high": 15, "critical": 22}


def _blob(*values: Any) -> str:
    parts: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            parts.append(_blob(*value.values()))
        elif isinstance(value, (list, tuple, set)):
            parts.append(_blob(*value))
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _evidence_strength(evidence_level: str, verdict: str = "") -> int:
    blob = _blob(evidence_level, verdict)
    if "unsupported" in blob or "无有效" in blob or "no valid" in blob:
        return 0
    if "meta-analysis" in blob or "systematic" in blob or "荟萃" in blob or "系统综述" in blob:
        base = 25
    elif "rct" in blob or "randomized" in blob or "随机" in blob:
        base = 23
    elif "clinical" in blob or "trial" in blob or "临床" in blob:
        base = 19
    elif "cohort" in blob or "case-control" in blob or "observational" in blob or "队列" in blob or "观察" in blob:
        base = 15
    elif "case" in blob or "series" in blob or "病例" in blob:
        base = 9
    elif "animal" in blob or "in-vitro" in blob or "preclinical" in blob or "动物" in blob or "体外" in blob:
        base = 6
    elif not blob.strip():
        base = 10
    else:
        base = 11
    if "contested" in blob:
        base = min(base, 14)
    return max(0, min(25, base))


def _methodology_score(methodology: Optional[Dict[str, Any]]) -> int:
    if not methodology:
        return 18
    if "quality_score" in methodology:
        try:
            return max(0, min(25, round(float(methodology["quality_score"]) * 0.25)))
        except (TypeError, ValueError):
            return 18
    report = methodology.get("methodology_report") or []
    score = 25
    for item in report:
        finding = item.get("effective_finding") or item.get("finding") or "none"
        score -= _SEVERITY_PENALTY.get(str(finding).lower(), 0)
    return max(0, min(25, score))


def _deduct_from_items(items: List[Dict[str, Any]], start: int = 25) -> int:
    score = start
    for item in items or []:
        sev = str(item.get("severity") or item.get("finding") or item.get("effective_finding") or "low").lower()
        score -= _SEVERITY_PENALTY.get(sev, 4)
    return max(0, min(start, score))


def _consistency_score(
    verdict: str = "",
    conflicts: Optional[List[Any]] = None,
    counter_evidence: Optional[List[Any]] = None,
    retraction_flags: Optional[List[Dict[str, Any]]] = None,
) -> int:
    score = 25
    verdict_l = (verdict or "").lower()
    if verdict_l == "unsupported":
        return 0
    if verdict_l == "contested":
        score -= 8
    score -= min(12, 4 * len(conflicts or []))
    score -= min(16, 8 * len(counter_evidence or []))
    for flag in retraction_flags or []:
        status = str(flag.get("status") or "").lower()
        if "retracted" in status:
            score -= 18
        elif "erratum" in status or "concern" in status:
            score -= 8
    return max(0, min(25, score))


def _star(total: int) -> int:
    if total >= 85:
        return 5
    if total >= 70:
        return 4
    if total >= 50:
        return 3
    if total >= 30:
        return 2
    return 1


def _label(stars: int, language: str) -> str:
    labels_zh = {
        5: "高度可信",
        4: "较为可信",
        3: "需要谨慎",
        2: "显著不确定",
        1: "不可信/缺据",
    }
    labels_en = {
        5: "highly believable",
        4: "reasonably believable",
        3: "use caution",
        2: "substantially uncertain",
        1: "not believable / insufficient evidence",
    }
    return (labels_zh if language == "zh" else labels_en)[stars]


def _key_concern(parts: Dict[str, int], issues: List[str], language: str) -> str:
    if issues:
        return issues[0]
    worst = min(parts, key=lambda k: parts[k])
    if language == "zh":
        names = {
            "evidence_strength": "证据强度不足",
            "methodology": "方法学质量不足或未知",
            "extrapolation": "外推风险",
            "consistency": "证据一致性不足",
        }
        return names[worst]
    return worst.replace("_", " ")


def _upgrade_path(parts: Dict[str, int], issues: List[str], language: str) -> str:
    worst = min(parts, key=lambda k: parts[k])
    if language == "zh":
        paths = {
            "evidence_strength": "补充更高等级、可核对的一手人体证据或系统综述。",
            "methodology": "补齐对照、随机化/盲法、样本量效力和预注册等方法学证据。",
            "extrapolation": "先把结论降到当前证据边界内，或做桥接实验验证外推边界。",
            "consistency": "核查冲突文献、撤回状态和反证，解释异质性后再升级结论。",
        }
    else:
        paths = {
            "evidence_strength": "Add higher-level, verifiable human evidence or a systematic review.",
            "methodology": "Resolve design, control, randomization, power, and preregistration gaps.",
            "extrapolation": "Narrow the conclusion to the tested boundary or run bridging validation.",
            "consistency": "Resolve conflicting evidence, retractions, and heterogeneity before upgrading.",
        }
    if issues:
        return paths[worst] + (" 关键问题：" + issues[0] if language == "zh" else f" Key issue: {issues[0]}")
    return paths[worst]


def score_believability(
    evidence_level: str = "",
    verdict: str = "",
    extrapolations: Optional[List[Dict[str, Any]]] = None,
    methodology: Optional[Dict[str, Any]] = None,
    methodology_flags: Optional[List[Dict[str, Any]]] = None,
    conflicts: Optional[List[Any]] = None,
    counter_evidence: Optional[List[Any]] = None,
    retraction_flags: Optional[List[Dict[str, Any]]] = None,
    language: str = "zh",
) -> Dict[str, Any]:
    """Return a 1-5 star believability score with a four-part breakdown."""
    extrapolations = extrapolations or []
    methodology_flags = methodology_flags or []
    issues: List[str] = []

    evidence = _evidence_strength(evidence_level, verdict)
    methodology_part = _methodology_score(methodology)
    if methodology_flags:
        methodology_part = min(methodology_part, _deduct_from_items(methodology_flags, 25))
    extrapolation = _deduct_from_items(extrapolations, 25)
    consistency = _consistency_score(verdict, conflicts, counter_evidence, retraction_flags)

    for item in sorted(extrapolations, key=lambda x: _SEVERITY_PENALTY.get(str(x.get("severity")).lower(), 0), reverse=True):
        issues.append(f"{item.get('rule_id')}: {item.get('description')}")
    for item in sorted(methodology_flags, key=lambda x: _SEVERITY_PENALTY.get(str(x.get("severity")).lower(), 0), reverse=True):
        issues.append(f"{item.get('check_id')}: {item.get('description')}")
    if conflicts:
        issues.append(str(conflicts[0]))
    if counter_evidence:
        issues.append("Counter-evidence exists" if language != "zh" else "存在反证")

    breakdown = {
        "evidence_strength": evidence,
        "methodology": methodology_part,
        "extrapolation": extrapolation,
        "consistency": consistency,
    }
    total = sum(breakdown.values())
    stars = _star(total)
    return {
        "score": stars,
        "score_100": total,
        "stars": "★" * stars + "☆" * (5 - stars),
        "label": _label(stars, language),
        "breakdown": breakdown,
        "key_concern": _key_concern(breakdown, issues, language),
        "upgrade_path": _upgrade_path(breakdown, issues, language),
        "notes": issues[:5],
    }

