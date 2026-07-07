#!/usr/bin/env python3
"""Bio-critique MCP server.

This pack consumes evidence_graph-style claim reports and adds a deterministic
critique layer: extrapolation checks, methodology guards, believability scoring,
conflict search, retraction checks, full reports, counter-experiment skeletons,
and a lightweight pasted-text entry point.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib import entrez  # noqa: E402
from _lib.counter_experiment import design_counter_experiment as _design_counter_experiment  # noqa: E402
from _lib.critique_scoring import score_believability  # noqa: E402
from _lib.extrapolation_checker import (  # noqa: E402
    check_extrapolations,
    concern_level,
    detect_methodology_flags,
    infer_asserted_from_text,
    infer_boundary_from_text,
    summarize_extrapolations,
)
from _lib.methodology_checker import CHECKLIST, evaluate_methodology  # noqa: E402
from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-critique", "0.1.0")

_COVERAGE_NOTE = (
    "Conflict search uses PubMed E-utilities only. It does not cover Chinese-only literature, "
    "non-indexed preprints, full-text-only contradictions, or paywalled supplementary material. "
    "Potential conflicts must still be verified before being used as counter-evidence."
)


def _claim_inputs(
    claim_report: Optional[Dict[str, Any]] = None,
    claim_text: str = "",
    asserted: Optional[Dict[str, Any]] = None,
    boundary: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[Dict[str, Any]]] = None,
    evidence_level: str = "",
    conflicts: Optional[List[Any]] = None,
    counter_evidence: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    report = claim_report or {}
    text = claim_text or report.get("claim") or report.get("text") or ""
    out_asserted = asserted or report.get("asserted") or infer_asserted_from_text(text)
    out_boundary = boundary or report.get("applicability_boundary") or report.get("boundary") or {}
    out_profiles = profiles or report.get("profiles") or report.get("evidence_profiles") or []
    out_evidence = evidence_level or report.get("evidence_level") or report.get("evidence_type") or ""
    return {
        "claim_text": text,
        "asserted": out_asserted,
        "boundary": out_boundary,
        "profiles": out_profiles,
        "evidence_level": out_evidence,
        "verdict": report.get("verdict") or "",
        "conflicts": list(conflicts if conflicts is not None else (report.get("conflicts") or [])),
        "counter_evidence": list(counter_evidence if counter_evidence is not None else (report.get("counter_evidence") or [])),
    }


def _methodology_for_claim(methodology_judgments: Any, idx: int, claim_text: str) -> Optional[List[Dict[str, Any]]]:
    if methodology_judgments is None:
        return None
    if isinstance(methodology_judgments, list):
        return methodology_judgments
    if not isinstance(methodology_judgments, dict):
        return None
    for key in (str(idx), idx, claim_text):
        if key in methodology_judgments:
            value = methodology_judgments[key]
            return value if isinstance(value, list) else None
    return None


def _metadata_from_claim(inputs: Dict[str, Any]) -> Dict[str, Any]:
    boundary = inputs.get("boundary") or {}
    meta = {
        "sample_size": boundary.get("max_sample_size"),
        "abstract": inputs.get("claim_text", ""),
    }
    if boundary.get("endpoint"):
        meta["endpoint"] = boundary.get("endpoint")
    return meta


@server.tool(
    "critique_conclusion",
    "Detect over-extrapolation in one claim using evidence_graph-style structured data. "
    "Returns rule_id-tagged findings, metadata-derived methodology flags, an overall concern level, and a human summary.",
    {
        "type": "object",
        "properties": {
            "claim_report": {"type": "object", "description": "One item from evidence_graph.claims."},
            "claim_text": {"type": "string"},
            "asserted": {"type": "object"},
            "boundary": {"type": "object"},
            "profiles": {"type": "array", "items": {"type": "object"}},
            "evidence_level": {"type": "string"},
            "conflicts": {"type": "array", "items": {}},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
            "intensity": {"type": "string", "enum": ["conservative", "standard", "aggressive"], "default": "standard"},
        },
    },
)
def critique_conclusion(
    claim_report: Optional[Dict[str, Any]] = None,
    claim_text: str = "",
    asserted: Optional[Dict[str, Any]] = None,
    boundary: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[Dict[str, Any]]] = None,
    evidence_level: str = "",
    conflicts: Optional[List[Any]] = None,
    language: str = "zh",
    intensity: str = "standard",
):
    inputs = _claim_inputs(claim_report, claim_text, asserted, boundary, profiles, evidence_level, conflicts)
    extrapolations = check_extrapolations(
        asserted=inputs["asserted"],
        boundary=inputs["boundary"],
        profiles=inputs["profiles"],
        claim_text=inputs["claim_text"],
        conflicts=inputs["conflicts"],
        intensity=intensity,
    )
    methodology_flags = detect_methodology_flags(
        boundary=inputs["boundary"],
        profiles=inputs["profiles"],
        claim_text=inputs["claim_text"],
        asserted=inputs["asserted"],
        intensity=intensity,
    )
    level = concern_level(extrapolations, methodology_flags)
    return {
        "claim": inputs["claim_text"],
        "extrapolations": extrapolations,
        "methodology_flags": methodology_flags,
        "overall_concern_level": level,
        "human_summary": summarize_extrapolations(extrapolations, methodology_flags, language=language),
        "rule_engine_note": "Rules flag boundary mismatches; absence of a flag is not proof of validity.",
    }


@server.tool(
    "critique_methodology",
    "Run a 10-item methodology checklist guard. The model supplies per-check judgments; the tool validates reasons/snippets, "
    "adds metadata-derived flags, computes a 0-100 quality score, and maps findings to GRADE domains.",
    {
        "type": "object",
        "properties": {
            "judgments": {"type": "array", "items": {"type": "object"}},
            "metadata": {"type": "object"},
            "intensity": {"type": "string", "enum": ["conservative", "standard", "aggressive"], "default": "standard"},
        },
    },
)
def critique_methodology(
    judgments: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    intensity: str = "standard",
):
    return evaluate_methodology(judgments=judgments, metadata=metadata, intensity=intensity)


@server.tool(
    "believability_score",
    "Compute claim-level believability (1-5 stars) from evidence strength, methodology quality, extrapolation findings, "
    "and conflict/counter-evidence signals.",
    {
        "type": "object",
        "properties": {
            "claim_report": {"type": "object"},
            "critique": {"type": "object"},
            "methodology": {"type": "object"},
            "evidence_level": {"type": "string"},
            "verdict": {"type": "string"},
            "conflicts": {"type": "array", "items": {}},
            "counter_evidence": {"type": "array", "items": {}},
            "retraction_flags": {"type": "array", "items": {"type": "object"}},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
        },
    },
)
def believability_score(
    claim_report: Optional[Dict[str, Any]] = None,
    critique: Optional[Dict[str, Any]] = None,
    methodology: Optional[Dict[str, Any]] = None,
    evidence_level: str = "",
    verdict: str = "",
    conflicts: Optional[List[Any]] = None,
    counter_evidence: Optional[List[Any]] = None,
    retraction_flags: Optional[List[Dict[str, Any]]] = None,
    language: str = "zh",
):
    inputs = _claim_inputs(claim_report=claim_report, evidence_level=evidence_level, conflicts=conflicts, counter_evidence=counter_evidence)
    critique = critique or {}
    return score_believability(
        evidence_level=evidence_level or inputs["evidence_level"],
        verdict=verdict or inputs["verdict"],
        extrapolations=critique.get("extrapolations") or [],
        methodology=methodology,
        methodology_flags=critique.get("methodology_flags") or [],
        conflicts=conflicts if conflicts is not None else inputs["conflicts"],
        counter_evidence=counter_evidence if counter_evidence is not None else inputs["counter_evidence"],
        retraction_flags=retraction_flags,
        language=language,
    )


def _entity_terms(key_entities: Any, claim_text: str) -> List[str]:
    terms: List[str] = []
    if isinstance(key_entities, dict):
        for v in key_entities.values():
            if isinstance(v, list):
                terms.extend(str(x) for x in v if x)
            elif v:
                terms.append(str(v))
    elif isinstance(key_entities, list):
        terms.extend(str(x) for x in key_entities if x)
    elif isinstance(key_entities, str) and key_entities.strip():
        terms.append(key_entities.strip())
    if not terms:
        terms.extend(re.findall(r"\b[A-Z0-9][A-Z0-9\-]{2,}\b", claim_text or "")[:4])
    clean: List[str] = []
    for t in terms:
        t = str(t).strip()
        if t and t.lower() not in {"the", "and", "patients", "clinical"} and t not in clean:
            clean.append(t)
    return clean[:6]


def _pmids_from_refs(refs: Any) -> List[str]:
    out: List[str] = []
    for ref in refs or []:
        if isinstance(ref, dict):
            id_type = str(ref.get("id_type") or "").lower()
            ident = str(ref.get("id") or ref.get("pmid") or "").strip()
            if id_type in {"", "pmid"} and re.fullmatch(r"\d{4,9}", ident):
                out.append(ident)
        elif re.fullmatch(r"\d{4,9}", str(ref).strip()):
            out.append(str(ref).strip())
    return out


@server.tool(
    "find_conflicting_evidence",
    "Search PubMed for literature that may conflict with a claim. Returns only retrieved PubMed records; it does not fabricate PMIDs. "
    "Use evidence_verify before treating any result as true counter-evidence.",
    {
        "type": "object",
        "properties": {
            "claim_text": {"type": "string"},
            "claim_direction": {"type": "string", "enum": ["positive", "negative", "neutral"], "default": "positive"},
            "key_entities": {},
            "current_refs": {"type": "array", "items": {}},
            "retmax": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["claim_text"],
    },
)
def find_conflicting_evidence(
    claim_text: str,
    claim_direction: str = "positive",
    key_entities: Any = None,
    current_refs: Optional[List[Any]] = None,
    retmax: int = 10,
):
    terms = _entity_terms(key_entities, claim_text)
    entity_expr = " AND ".join(f'"{t}"[Title/Abstract]' for t in terms) if terms else f'"{claim_text[:80]}"[Title/Abstract]'
    if claim_direction == "positive":
        contrast = '("no effect"[Title/Abstract] OR "no significant"[Title/Abstract] OR negative[Title/Abstract] OR failed[Title/Abstract] OR contrary[Title/Abstract] OR inconsistent[Title/Abstract])'
    elif claim_direction == "negative":
        contrast = '(benefit[Title/Abstract] OR improved[Title/Abstract] OR effective[Title/Abstract] OR positive[Title/Abstract] OR significant[Title/Abstract])'
    else:
        contrast = '(conflicting[Title/Abstract] OR inconsistent[Title/Abstract] OR heterogeneity[Title/Abstract] OR contrary[Title/Abstract])'
    excluded = _pmids_from_refs(current_refs)
    exclude_expr = ""
    if excluded:
        exclude_expr = " NOT (" + " OR ".join(f"{p}[uid]" for p in excluded) + ")"
    query = f"({entity_expr}) AND {contrast}{exclude_expr}"
    try:
        search = entrez.esearch("pubmed", query, retmax=max(1, min(int(retmax), 50)), sort="relevance")
        ids = [p for p in search.get("ids", []) if p not in excluded]
        summary = entrez.esummary("pubmed", ids) if ids else {}
    except Exception as e:  # noqa: BLE001
        return {
            "potential_conflicts": [],
            "search_strategy": {"query": query, "entities": terms, "error": str(e)},
            "coverage_note": _COVERAGE_NOTE,
        }
    out: List[Dict[str, Any]] = []
    for pmid in ids:
        s = summary.get(pmid) or {}
        title = s.get("title") or ""
        out.append({
            "pmid": pmid,
            "title": title,
            "journal": s.get("fulljournalname") or s.get("source"),
            "year": (s.get("pubdate") or "").split(" ")[0] if s.get("pubdate") else None,
            "reason": "Retrieved by a contrastive PubMed query; requires evidence_verify and expert review.",
        })
    return {
        "potential_conflicts": out,
        "search_strategy": {
            "query": query,
            "entities": terms,
            "excluded_pmids": excluded,
            "query_translation": search.get("query_translation") if "search" in locals() else None,
        },
        "coverage_note": _COVERAGE_NOTE,
    }


@server.tool(
    "check_retraction_status",
    "Check PubMed publication types for retraction, retraction notices, expressions of concern, or errata.",
    {
        "type": "object",
        "properties": {"pmids": {"type": "array", "items": {"type": "string"}, "maxItems": 50}},
        "required": ["pmids"],
    },
)
def check_retraction_status(pmids: List[str]):
    ids = [str(p).strip() for p in pmids if re.fullmatch(r"\d{4,9}", str(p).strip())]
    if not ids:
        return {"results": []}
    try:
        xml = entrez.efetch_text("pubmed", ids, rettype="abstract", retmode="xml")
        records = {r["pmid"]: r for r in entrez.parse_pubmed_xml(xml)}
    except Exception as e:  # noqa: BLE001
        return {"results": [{"pmid": p, "status": "unknown", "error": str(e)} for p in ids]}
    results = []
    for pmid in ids:
        rec = records.get(pmid) or {}
        types = [str(x).lower() for x in rec.get("publication_types", [])]
        if any("retracted publication" == t for t in types):
            status = "retracted"
        elif any("retraction of publication" == t for t in types):
            status = "retraction_notice"
        elif any("expression of concern" in t for t in types):
            status = "expression_of_concern"
        elif any("erratum" in t for t in types):
            status = "erratum"
        elif rec:
            status = "clear"
        else:
            status = "unknown"
        results.append({
            "pmid": pmid,
            "status": status,
            "publication_types": rec.get("publication_types", []),
            "title": rec.get("title"),
        })
    return {"results": results}


def _claim_rows_for_report(claims: List[Dict[str, Any]], language: str) -> List[str]:
    headers = ["#", "Claim", "Concern", "Believability", "Key issue"] if language != "zh" else ["#", "结论", "关注级别", "可信度", "关键问题"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for i, c in enumerate(claims, 1):
        text = (c.get("claim") or "").replace("|", "\\|").replace("\n", " ")[:120]
        score = c.get("believability") or {}
        lines.append("| " + " | ".join([
            str(i),
            text,
            c.get("overall_concern_level", ""),
            f"{score.get('stars', '')} {score.get('label', '')}".strip(),
            str(score.get("key_concern") or "").replace("|", "\\|")[:120],
        ]) + " |")
    return lines


@server.tool(
    "critique_full_report",
    "Generate a Markdown critique report for an evidence_graph result: per-claim extrapolation, methodology flags, "
    "believability stars, top risks, and an uncertainty-ledger handoff.",
    {
        "type": "object",
        "properties": {
            "evidence_graph": {"description": "Full evidence_graph result or its claims array."},
            "methodology_judgments": {"description": "Optional list or per-claim dict of methodology checklist judgments."},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
            "intensity": {"type": "string", "enum": ["conservative", "standard", "aggressive"], "default": "standard"},
        },
        "required": ["evidence_graph"],
    },
)
def critique_full_report(
    evidence_graph: Any,
    methodology_judgments: Any = None,
    language: str = "zh",
    intensity: str = "standard",
):
    graph_claims = evidence_graph.get("claims") if isinstance(evidence_graph, dict) else evidence_graph
    graph_claims = graph_claims or []
    claim_cards: List[Dict[str, Any]] = []
    risk_pool: List[Dict[str, Any]] = []

    for idx, claim in enumerate(graph_claims):
        inputs = _claim_inputs(claim_report=claim)
        conclusion = critique_conclusion(claim_report=claim, language=language, intensity=intensity)
        meth_j = _methodology_for_claim(methodology_judgments, idx, inputs["claim_text"])
        methodology = None
        if meth_j is not None:
            methodology = critique_methodology(meth_j, metadata=_metadata_from_claim(inputs), intensity=intensity)
        score = believability_score(claim_report=claim, critique=conclusion, methodology=methodology, language=language)
        card = {
            "claim": inputs["claim_text"],
            "overall_concern_level": conclusion["overall_concern_level"],
            "extrapolations": conclusion["extrapolations"],
            "methodology_flags": conclusion["methodology_flags"],
            "methodology": methodology,
            "believability": score,
        }
        claim_cards.append(card)
        for e in conclusion["extrapolations"]:
            risk_pool.append({"claim": inputs["claim_text"], "type": "extrapolation", **e})
        for f in conclusion["methodology_flags"]:
            risk_pool.append({"claim": inputs["claim_text"], "type": "methodology_flag", **f})
        if methodology:
            for a in methodology.get("auto_detected") or []:
                risk_pool.append({"claim": inputs["claim_text"], "type": "methodology_auto", **a})

    severity_rank = {"critical": 4, "high": 3, "major": 3, "moderate": 2, "minor": 1, "low": 1}
    top_risks = sorted(risk_pool, key=lambda r: severity_rank.get(str(r.get("severity") or r.get("finding")).lower(), 0), reverse=True)[:3]

    if language == "zh":
        lines = [
            "# 反证与批判报告",
            "",
            "> 以下是基于规则引擎和文献检索入口的自动批判，不替代领域专家判断。",
            "",
            "## 批判摘要",
            f"- 共评估 {len(claim_cards)} 条 claim。",
            f"- 存在外推/方法学风险的 claim：{sum(1 for c in claim_cards if c['overall_concern_level'] != 'green')} 条。",
            "",
            "## 可信度总览",
        ]
    else:
        lines = [
            "# Counter-Evidence and Critique Report",
            "",
            "> Automated critique based on rule engines and verifiable search hooks; it does not replace domain expert review.",
            "",
            "## Executive Summary",
            f"- Claims assessed: {len(claim_cards)}.",
            f"- Claims with extrapolation/methodology concerns: {sum(1 for c in claim_cards if c['overall_concern_level'] != 'green')}.",
            "",
            "## Believability Overview",
        ]
    lines.extend(_claim_rows_for_report(claim_cards, language))
    lines.append("")
    lines.append("## 最关键的 3 条风险" if language == "zh" else "## Top 3 Risks")
    if not top_risks:
        lines.append("- 未检测到明确规则风险；仍需人工审阅证据边界。" if language == "zh" else "- No explicit rule risk detected; manual review is still required.")
    else:
        for r in top_risks:
            rid = r.get("rule_id") or r.get("check_id")
            lines.append(f"- {rid}: {r.get('description') or r.get('reason')} | claim: {str(r.get('claim'))[:90]}")

    lines.append("")
    lines.append("## 逐 claim 批判卡片" if language == "zh" else "## Per-Claim Critique Cards")
    for i, card in enumerate(claim_cards, 1):
        score = card["believability"]
        lines.append("")
        lines.append(f"### {i}. {card['claim'][:120]}")
        lines.append(f"- concern: {card['overall_concern_level']}")
        lines.append(f"- believability: {score['stars']} {score['label']} ({score['score_100']}/100)")
        lines.append(f"- key concern: {score['key_concern']}")
        if card["extrapolations"]:
            for e in card["extrapolations"]:
                lines.append(f"- {e['rule_id']} {e.get('rule_name_zh') or e.get('rule_name')}: {e['description']} signals={e['signals']}")
        else:
            lines.append("- no explicit extrapolation rule hit")
        for f in card["methodology_flags"]:
            lines.append(f"- {f['check_id']}: {f['description']} signals={f['signals']}")

    lines.append("")
    lines.append("## 与 uncertainty_ledger 的交叉引用" if language == "zh" else "## Uncertainty Ledger Handoff")
    lines.append("- 将 EX/METH 命中的风险放入 Conflicts 或 Missing data；将 upgrade_path 放入 Next experiment。" if language == "zh" else "- Put EX/METH risks into Conflicts or Missing data; put upgrade_path into Next experiment.")
    return {
        "claims": claim_cards,
        "top_risks": top_risks,
        "markdown": "\n".join(lines),
    }


@server.tool(
    "design_counter_experiment",
    "Design a minimal counter-experiment skeleton for a questioned claim, based on extrapolation rule ids and evidence boundary.",
    {
        "type": "object",
        "properties": {
            "claim_text": {"type": "string"},
            "extrapolations": {"type": "array", "items": {"type": "object"}},
            "boundary": {"type": "object"},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
        },
        "required": ["claim_text"],
    },
)
def design_counter_experiment(
    claim_text: str,
    extrapolations: Optional[List[Dict[str, Any]]] = None,
    boundary: Optional[Dict[str, Any]] = None,
    language: str = "zh",
):
    return _design_counter_experiment(claim_text, extrapolations=extrapolations, boundary=boundary, language=language)


def _split_claims(text: str) -> List[str]:
    raw = re.split(r"[\n。！？；;]+", text or "")
    claims = []
    for part in raw:
        p = part.strip(" -\t\r")
        if len(p) < 8:
            continue
        if any(w in p.lower() for w in ("show", "shows", "suggest", "demonstrate", "improve", "reduce", "associated", "表明", "显示", "提示", "改善", "降低", "相关", "有效")):
            claims.append(p)
    return claims or [text.strip()] if text.strip() else []


def _text_boundary_for_claim(claim: str) -> Dict[str, Any]:
    boundary = infer_boundary_from_text(claim)
    asserted = infer_asserted_from_text(claim)
    species = set(boundary.get("species") or [])
    if asserted.get("species") == "human" and ({"animal", "in-vitro"} & species):
        # In pasted prose, human words usually describe the asserted conclusion,
        # while mouse/cell-line words describe the evidence boundary.
        boundary["species"] = sorted(species & {"animal", "in-vitro"})
    return boundary


@server.tool(
    "critique_text",
    "Quickly critique pasted natural-language conclusions. This heuristic entry point splits claims, infers broad assertions/boundaries, "
    "and produces a critique report without fabricating citations. For formal use, run evidence_graph first.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
            "intensity": {"type": "string", "enum": ["conservative", "standard", "aggressive"], "default": "standard"},
        },
        "required": ["text"],
    },
)
def critique_text(text: str, language: str = "zh", intensity: str = "standard"):
    claims = _split_claims(text)
    graph_like = []
    for claim in claims:
        graph_like.append({
            "claim": claim,
            "asserted": infer_asserted_from_text(claim),
            "applicability_boundary": _text_boundary_for_claim(claim),
            "evidence_level": "text-only heuristic; evidence_graph not run",
            "verdict": "contested",
            "conflicts": ["Text-only critique: no verified evidence graph was supplied."],
            "counter_evidence": [],
        })
    report = critique_full_report({"claims": graph_like}, language=language, intensity=intensity)
    report["claims_extracted"] = len(claims)
    report["entry_point_warning"] = (
        "critique_text 是启发式入口；正式批判应先运行 evidence_graph。"
        if language == "zh" else "critique_text is heuristic; run evidence_graph for formal critique."
    )
    return report


@server.tool(
    "critique_checklist",
    "Return the fixed 10-item methodology checklist for model-facing review prompts.",
    {"type": "object", "properties": {}},
)
def critique_checklist():
    return {"checklist": CHECKLIST}


if __name__ == "__main__":
    server.run()

