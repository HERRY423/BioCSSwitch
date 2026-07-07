"""Rule engine for claim extrapolation critique.

The functions in this module are deliberately offline and deterministic. They
consume structured evidence_graph-style data and return traceable rule hits:
every finding carries a rule id, severity, signals, and a recommendation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


RULES: List[Dict[str, str]] = [
    {
        "id": "EX-01",
        "name": "species extrapolation",
        "name_zh": "物种外推",
        "description": "Animal or in-vitro evidence is used for a human or clinical conclusion.",
    },
    {
        "id": "EX-02",
        "name": "population extrapolation",
        "name_zh": "人群外推",
        "description": "Evidence from a narrow age, sex, or subgroup is generalized to a broader population.",
    },
    {
        "id": "EX-03",
        "name": "stage extrapolation",
        "name_zh": "分期外推",
        "description": "Evidence in a specific disease stage or treatment line is generalized across stages.",
    },
    {
        "id": "EX-04",
        "name": "dose extrapolation",
        "name_zh": "剂量外推",
        "description": "A specific dose, exposure, or regimen is generalized beyond the tested regimen.",
    },
    {
        "id": "EX-05",
        "name": "endpoint extrapolation",
        "name_zh": "终点外推",
        "description": "A surrogate endpoint is treated as proof of a hard clinical endpoint.",
    },
    {
        "id": "EX-06",
        "name": "time extrapolation",
        "name_zh": "时间外推",
        "description": "Short follow-up is used to claim long-term benefit or durability.",
    },
    {
        "id": "EX-07",
        "name": "mechanism extrapolation",
        "name_zh": "机制外推",
        "description": "Mechanistic or in-vitro evidence is used as if it established clinical efficacy.",
    },
]

RULE_BY_ID = {r["id"]: r for r in RULES}
_SEVERITY_RANK = {"low": 1, "moderate": 2, "high": 3, "critical": 4}

_HUMAN_HINTS = (
    "human", "patient", "patients", "clinical", "clinic", "participants",
    "people", "population", "survival", "mortality", "efficacy", "therapy",
    "treatment", "benefit", "患者", "病人", "人类", "人体", "临床", "人群",
    "疗效", "治疗", "生存", "获益",
)
_GENERAL_POP_HINTS = (
    "all patients", "patients with", "population", "general", "broadly",
    "所有", "全部", "广泛", "普遍", "患者中", "人群中",
)
_SPECIFIC_STAGE_HINTS = (
    "stage i", "stage ii", "stage iii", "stage iv", "metastatic", "advanced",
    "early-stage", "first-line", "second-line", "refractory", "relapsed",
    "晚期", "早期", "转移", "复发", "一线", "二线", "难治", "分期",
)
_GENERIC_STAGE_HINTS = ("cancer", "disease", "patients", "肿瘤", "疾病", "患者")
_HARD_ENDPOINT_HINTS = (
    "overall survival", " os", "mortality", "death", "cure", "long-term survival",
    "survival benefit", "总生存", "死亡", "治愈", "长期生存", "生存获益",
)
_SURROGATE_HINTS = (
    "orr", "response rate", "objective response", "pfs", "progression-free",
    "biomarker", "tumor shrinkage", "surrogate", "pathologic response",
    "缓解率", "客观缓解", "无进展", "生物标志物", "肿瘤缩小", "替代终点",
)
_LONG_TERM_HINTS = ("long-term", "durable", "sustained", "years", "长期", "持久", "多年")
_SHORT_TERM_HINTS = ("short-term", "weeks", "28 days", "3 months", "短期", "数周", "3个月")
_MECHANISM_HINTS = (
    "mechanism", "pathway", "signaling", "expression", "activation", "inhibition",
    "体外", "细胞", "机制", "通路", "表达", "激活", "抑制",
)
_CLINICAL_EFFICACY_HINTS = (
    "effective", "efficacy", "improves", "treats", "therapy", "benefit",
    "有效", "疗效", "改善", "治疗", "获益",
)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


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


def _contains(text: str, hints: Iterable[str]) -> bool:
    tl = text.lower()
    return any(h.lower() in tl for h in hints)


def _profile_species(profiles: Optional[List[Dict[str, Any]]]) -> List[str]:
    out: List[str] = []
    for p in profiles or []:
        sp = ((p or {}).get("species") or {}).get("value")
        if sp and sp not in out:
            out.append(str(sp))
    return out


def _species(boundary: Dict[str, Any], profiles: Optional[List[Dict[str, Any]]]) -> List[str]:
    values = [str(x) for x in _as_list((boundary or {}).get("species")) if x]
    for sp in _profile_species(profiles):
        if sp not in values:
            values.append(sp)
    return values


def _sample_n(boundary: Dict[str, Any], profiles: Optional[List[Dict[str, Any]]]) -> Optional[int]:
    candidates: List[int] = []
    val = (boundary or {}).get("max_sample_size")
    if val is not None:
        try:
            candidates.append(int(val))
        except (TypeError, ValueError):
            pass
    for p in profiles or []:
        n = (((p or {}).get("sample_size") or {}).get("n"))
        if n is not None:
            try:
                candidates.append(int(n))
            except (TypeError, ValueError):
                pass
    return max(candidates) if candidates else None


def infer_asserted_from_text(claim_text: str) -> Dict[str, Any]:
    """Best-effort assertion hints from text.

    This is intentionally conservative: it only fills broad hints that help
    flag obvious overreach when no evidence_graph asserted object is available.
    """
    text = claim_text or ""
    low = text.lower()
    asserted: Dict[str, Any] = {}
    if _contains(low, _HUMAN_HINTS):
        asserted["species"] = "human"
    if _contains(low, _HARD_ENDPOINT_HINTS):
        asserted["endpoint"] = "hard-clinical"
    elif _contains(low, _SURROGATE_HINTS):
        asserted["endpoint"] = "surrogate"
    if _contains(low, _LONG_TERM_HINTS):
        asserted["timeframe"] = "long-term"
    if re.search(r"\b\d+(\.\d+)?\s*(mg|ug|mcg|g|ml|iu)\b", low):
        asserted["dose"] = "specific"
    if _contains(low, _SPECIFIC_STAGE_HINTS):
        asserted["disease_stage"] = "specific"
    return asserted


def infer_boundary_from_text(text: str) -> Dict[str, Any]:
    """Heuristic boundary for pasted text when evidence_graph is unavailable."""
    low = (text or "").lower()
    species: List[str] = []
    if any(w in low for w in ("mouse", "mice", "murine", "rat", "xenograft", "动物", "小鼠")):
        species.append("animal")
    if any(w in low for w in ("in vitro", "cell line", "organoid", "细胞", "体外")):
        species.append("in-vitro")
    if any(w in low for w in ("patient", "patients", "clinical", "cohort", "human", "患者", "临床")):
        species.append("human")
    boundary: Dict[str, Any] = {}
    if species:
        boundary["species"] = sorted(set(species))
    if _contains(low, _SURROGATE_HINTS):
        boundary["endpoint"] = "surrogate"
    if _contains(low, _SHORT_TERM_HINTS):
        boundary["follow_up"] = "short-term"
    if _contains(low, _SPECIFIC_STAGE_HINTS):
        boundary["disease_stage"] = ["specific"]
    return boundary


def _finding(
    rule_id: str,
    severity: str,
    description: str,
    signals: List[str],
    recommendation: str,
    confidence: float = 0.8,
) -> Dict[str, Any]:
    rule = RULE_BY_ID[rule_id]
    return {
        "rule_id": rule_id,
        "rule_name": rule["name"],
        "rule_name_zh": rule["name_zh"],
        "severity": severity,
        "description": description,
        "signals": signals,
        "recommendation": recommendation,
        "confidence": round(confidence, 2),
    }


def _filter_intensity(findings: List[Dict[str, Any]], intensity: str) -> List[Dict[str, Any]]:
    level = (intensity or "standard").lower()
    if level == "conservative":
        minimum = 3
    elif level == "aggressive":
        minimum = 1
    else:
        minimum = 2
    return [f for f in findings if _SEVERITY_RANK.get(f.get("severity"), 0) >= minimum]


def check_extrapolations(
    asserted: Optional[Dict[str, Any]] = None,
    boundary: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[Dict[str, Any]]] = None,
    claim_text: str = "",
    conflicts: Optional[List[str]] = None,
    intensity: str = "standard",
) -> List[Dict[str, Any]]:
    """Compare asserted scope with evidence boundary and return EX rule hits."""
    asserted = dict(asserted or {})
    if claim_text and not asserted:
        asserted.update(infer_asserted_from_text(claim_text))
    boundary = dict(boundary or {})
    conflicts = conflicts or []

    text_blob = _blob(claim_text, asserted)
    boundary_blob = _blob(boundary, profiles, conflicts)
    species = _species(boundary, profiles)
    findings: List[Dict[str, Any]] = []

    claim_says_human = asserted.get("species") == "human" or _contains(text_blob, _HUMAN_HINTS)
    preclinical_only = bool(species) and set(species) <= {"animal", "in-vitro"}
    if claim_says_human and preclinical_only:
        sev = "critical" if "in-vitro" in species and "animal" not in species else "high"
        findings.append(_finding(
            "EX-01",
            sev,
            "The claim reaches a human or clinical conclusion while supporting evidence is only preclinical.",
            [f"asserted_species={asserted.get('species') or 'human-like text'}",
             f"boundary_species={','.join(species)}"],
            "Downgrade the conclusion to preclinical evidence, or add human PK/PD, Phase I, or clinical evidence.",
            0.9,
        ))

    age_groups = [str(x) for x in _as_list(boundary.get("age_groups")) if x]
    sex = [str(x) for x in _as_list(boundary.get("sex")) if x]
    pop = str(asserted.get("population") or "")
    broad_population_claim = (
        _contains(text_blob, _GENERAL_POP_HINTS)
        or pop.lower() in {"", "all", "general", "patients", "population", "broad"}
    )
    if broad_population_claim and ((sex and len(set(sex)) == 1) or (age_groups and len(set(age_groups)) <= 2)):
        findings.append(_finding(
            "EX-02",
            "moderate",
            "The evidence population appears narrower than the population implied by the claim.",
            [f"boundary_age={','.join(age_groups) or 'not reported'}",
             f"boundary_sex={','.join(sex) or 'not reported'}"],
            "State the tested subgroup explicitly and avoid generalizing until the missing subgroup is validated.",
            0.65,
        ))

    stages = [str(x) for x in _as_list(boundary.get("disease_stage")) if x]
    asserted_stage = str(asserted.get("disease_stage") or "")
    if stages and (asserted_stage.lower() in {"", "all", "general", "any"} or (
        _contains(text_blob, _GENERIC_STAGE_HINTS) and not _contains(text_blob, _SPECIFIC_STAGE_HINTS)
    )):
        findings.append(_finding(
            "EX-03",
            "moderate",
            "Evidence is tied to a specific disease stage or treatment line, but the claim does not preserve that boundary.",
            [f"boundary_stage={','.join(stages)}", f"asserted_stage={asserted_stage or 'not specified'}"],
            "Attach the stage/treatment-line boundary to the conclusion, or seek evidence in the target stage.",
            0.7,
        ))

    asserted_dose = str(asserted.get("dose") or "").lower()
    boundary_dose = str(boundary.get("dose") or boundary.get("regimen") or "").lower()
    if boundary_dose and asserted_dose in {"", "any", "all", "general", "dose-independent"}:
        findings.append(_finding(
            "EX-04",
            "moderate",
            "A specific dose or regimen is being generalized beyond the tested exposure.",
            [f"boundary_dose={boundary_dose}", f"asserted_dose={asserted_dose or 'not specified'}"],
            "Keep the dose/regimen in the conclusion or require dose-ranging evidence.",
            0.7,
        ))

    asserted_endpoint = str(asserted.get("endpoint") or "").lower()
    boundary_endpoint = str(boundary.get("endpoint") or boundary.get("outcome_type") or "").lower()
    if (
        asserted_endpoint in {"hard-clinical", "survival", "mortality", "os"}
        or _contains(text_blob, _HARD_ENDPOINT_HINTS)
    ) and (
        "surrogate" in boundary_endpoint
        or _contains(boundary_blob, _SURROGATE_HINTS)
    ):
        findings.append(_finding(
            "EX-05",
            "high",
            "A surrogate endpoint is being used to imply a hard clinical endpoint.",
            [f"asserted_endpoint={asserted_endpoint or 'hard endpoint text'}",
             f"boundary_endpoint={boundary_endpoint or 'surrogate signal'}"],
            "Separate surrogate activity from survival/mortality claims until hard-endpoint data exist.",
            0.8,
        ))

    asserted_time = str(asserted.get("timeframe") or asserted.get("duration") or "").lower()
    boundary_time = str(boundary.get("follow_up") or boundary.get("follow_up_months") or "").lower()
    short_follow = False
    if boundary_time:
        short_follow = "short" in boundary_time or _contains(boundary_time, _SHORT_TERM_HINTS)
        nums = re.findall(r"\d+(?:\.\d+)?", boundary_time)
        if nums:
            try:
                short_follow = short_follow or float(nums[0]) < 12
            except ValueError:
                pass
    if (
        asserted_time in {"long-term", "durable", "sustained"}
        or _contains(text_blob, _LONG_TERM_HINTS)
    ) and short_follow:
        findings.append(_finding(
            "EX-06",
            "moderate",
            "Short follow-up is being used to imply long-term or durable benefit.",
            [f"asserted_time={asserted_time or 'long-term text'}", f"boundary_follow_up={boundary_time}"],
            "Report follow-up duration and frame long-term benefit as unproven until extended follow-up is available.",
            0.75,
        ))

    mechanistic_boundary = (
        "in-vitro" in species
        or _contains(boundary_blob, _MECHANISM_HINTS)
        or "mechanistic" in boundary_blob
    )
    clinical_claim = claim_says_human and _contains(text_blob, _CLINICAL_EFFICACY_HINTS)
    if clinical_claim and mechanistic_boundary:
        findings.append(_finding(
            "EX-07",
            "high",
            "Mechanistic or in-vitro evidence is being treated as clinical efficacy evidence.",
            ["clinical_efficacy_claim=true", "mechanistic_or_invitro_boundary=true"],
            "Phrase the result as a mechanism hypothesis and add human tissue, biomarker, or clinical validation.",
            0.85,
        ))

    # Deduplicate if multiple rules are triggered by the same preclinical signals.
    dedup: Dict[str, Dict[str, Any]] = {}
    for f in findings:
        existing = dedup.get(f["rule_id"])
        if not existing or _SEVERITY_RANK[f["severity"]] > _SEVERITY_RANK[existing["severity"]]:
            dedup[f["rule_id"]] = f
    return _filter_intensity(list(dedup.values()), intensity)


def detect_methodology_flags(
    boundary: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[Dict[str, Any]]] = None,
    claim_text: str = "",
    asserted: Optional[Dict[str, Any]] = None,
    intensity: str = "standard",
) -> List[Dict[str, Any]]:
    """Lightweight auto flags derived from metadata, not a full methodology review."""
    boundary = dict(boundary or {})
    asserted = dict(asserted or {})
    if claim_text and not asserted:
        asserted.update(infer_asserted_from_text(claim_text))
    n = _sample_n(boundary, profiles)
    flags: List[Dict[str, Any]] = []
    if n is not None and n < 30:
        flags.append({
            "check_id": "METH-03",
            "severity": "major",
            "description": "Sample size is below 30; statistical precision is likely fragile.",
            "signals": [f"max_sample_size={n}"],
            "recommendation": "Require power calculation or independent validation before strong claims.",
        })
    elif intensity == "aggressive" and n is not None and n < 100:
        flags.append({
            "check_id": "METH-03",
            "severity": "minor",
            "description": "Sample size is below 100; precision should be discussed explicitly.",
            "signals": [f"max_sample_size={n}"],
            "recommendation": "Report effect uncertainty and avoid categorical claims.",
        })
    elif n is None and intensity == "aggressive":
        flags.append({
            "check_id": "METH-03",
            "severity": "minor",
            "description": "Supporting evidence did not expose sample size.",
            "signals": ["max_sample_size=unknown"],
            "recommendation": "Inspect the source and add sample size before grading precision.",
        })

    species = _species(boundary, profiles)
    if (asserted.get("species") == "human" or _contains(claim_text, _HUMAN_HINTS)) and species and set(species) <= {"animal", "in-vitro"}:
        flags.append({
            "check_id": "GRADE-indirectness",
            "severity": "major",
            "description": "Evidence is indirect for the human/clinical target population.",
            "signals": [f"boundary_species={','.join(species)}"],
            "recommendation": "Downgrade for indirectness unless bridging human evidence is added.",
        })
    return flags


def concern_level(extrapolations: List[Dict[str, Any]], methodology_flags: Optional[List[Dict[str, Any]]] = None) -> str:
    all_items = list(extrapolations or []) + list(methodology_flags or [])
    max_rank = 0
    for item in all_items:
        sev = item.get("severity", "low")
        max_rank = max(max_rank, _SEVERITY_RANK.get(sev, 1))
    if max_rank >= 4:
        return "red"
    if max_rank == 3:
        return "orange"
    if max_rank == 2:
        return "yellow"
    return "green"


def summarize_extrapolations(
    extrapolations: List[Dict[str, Any]],
    methodology_flags: Optional[List[Dict[str, Any]]] = None,
    language: str = "zh",
) -> str:
    level = concern_level(extrapolations, methodology_flags)
    if not extrapolations and not methodology_flags:
        return "未检测到明确过度外推；这不等同于结论已被充分证明。" if language == "zh" else (
            "No explicit extrapolation was detected; this does not mean the claim is proven."
        )
    top = sorted(
        list(extrapolations or []) + list(methodology_flags or []),
        key=lambda x: _SEVERITY_RANK.get(x.get("severity", "low"), 1),
        reverse=True,
    )[0]
    label = top.get("rule_name_zh") or top.get("check_id") if language == "zh" else top.get("rule_name") or top.get("check_id")
    if language == "zh":
        return f"总体关注级别为 {level}；最主要问题是 {label}：{top.get('description')}"
    return f"Overall concern level is {level}; the main issue is {label}: {top.get('description')}"

