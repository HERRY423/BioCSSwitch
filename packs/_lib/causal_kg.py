"""Local causal knowledge-graph primitives for BioCSSwitch.

The module is intentionally stdlib-only.  It gives MCP servers and the proxy a
shared representation for biomedical causal triples without requiring a NER
model or a graph database.  NetworkX can sit above this later, but the durable
format stays plain JSONL so the graph is easy to inspect and repair.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


POSITIVE_RELATIONS = {
    "activates": "causally_upregulates",
    "activate": "causally_upregulates",
    "upregulates": "causally_upregulates",
    "upregulate": "causally_upregulates",
    "induces": "causally_upregulates",
    "induce": "causally_upregulates",
    "promotes": "causally_upregulates",
    "promote": "causally_upregulates",
    "drives": "causally_upregulates",
    "drive": "causally_upregulates",
    "increases": "causally_upregulates",
    "increase": "causally_upregulates",
    "causes": "causally_upregulates",
    "cause": "causally_upregulates",
}

NEGATIVE_RELATIONS = {
    "inhibits": "causally_downregulates",
    "inhibit": "causally_downregulates",
    "suppresses": "causally_downregulates",
    "suppress": "causally_downregulates",
    "downregulates": "causally_downregulates",
    "downregulate": "causally_downregulates",
    "represses": "causally_downregulates",
    "repress": "causally_downregulates",
    "reduces": "causally_downregulates",
    "reduce": "causally_downregulates",
    "decreases": "causally_downregulates",
    "decrease": "causally_downregulates",
}

RELATION_DIRECTION = {
    "causally_upregulates": "positive",
    "causally_downregulates": "negative",
    "associated_with": "neutral",
}

_GENE = r"[A-Z][A-Z0-9-]{1,15}"
_ARROW = re.compile(rf"\b({_GENE})\s*(?:->|=>|-->|→)\s*({_GENE})\b")
_VERB = re.compile(
    rf"\b({_GENE})\s+"
    rf"({'|'.join(sorted(set(POSITIVE_RELATIONS) | set(NEGATIVE_RELATIONS), key=len, reverse=True))})"
    rf"\s+({_GENE})\b",
    re.I,
)
_ASSOC = re.compile(rf"\b({_GENE})\s+(?:is\s+)?(?:associated|correlates)\s+with\s+({_GENE})\b", re.I)
_PMID = re.compile(r"\bPMID\s*[:#]?\s*(\d{4,9})\b", re.I)
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)

_EXPERIMENT_TERMS = [
    "ChIP-seq",
    "CUT&RUN",
    "CUT&Tag",
    "RNAi",
    "siRNA",
    "shRNA",
    "CRISPR",
    "CRISPRi",
    "CRISPRa",
    "qPCR",
    "RNA-seq",
    "ATAC-seq",
    "western blot",
    "flow cytometry",
    "luciferase",
    "ELISA",
    "xenograft",
    "organoid",
]

_CELL_LINE = re.compile(r"\b([A-Z][A-Za-z0-9-]{1,12}(?:-[A-Za-z0-9]+)?(?:\s+cell line)?)\b")
_STOP_ENTITIES = {
    "PMID",
    "DOI",
    "RNA",
    "DNA",
    "GRADE",
    "ELISA",
    "CRISPR",
    "RNAI",
    "PCR",
    "JSON",
    "MCP",
}


def default_graph_path(env: Optional[Dict[str, str]] = None) -> str:
    env = env or os.environ
    return env.get("BIOCSSWITCH_KG_PATH") or env.get("CSSWITCH_KG_PATH") or str(
        Path.home() / ".csswitch" / "bio_knowledge_graph.jsonl"
    )


def relation_direction(relation: str) -> str:
    return RELATION_DIRECTION.get(str(relation or ""), "neutral")


def opposite_direction(a: str, b: str) -> bool:
    return {relation_direction(a), relation_direction(b)} == {"positive", "negative"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean_entity(name: str) -> Optional[str]:
    n = re.sub(r"[^A-Za-z0-9-]", "", str(name or "").strip()).upper()
    if len(n) < 2 or n in _STOP_ENTITIES:
        return None
    if n.isdigit():
        return None
    return n


def _evidence_ids(text: str) -> List[str]:
    ids = [f"PMID:{m.group(1)}" for m in _PMID.finditer(text or "")]
    ids.extend(f"DOI:{m.group(0)}" for m in _DOI.finditer(text or ""))
    return sorted(dict.fromkeys(ids))


def _experiment_type(text: str) -> List[str]:
    low = (text or "").lower()
    return [term for term in _EXPERIMENT_TERMS if term.lower() in low]


def _model_system(text: str) -> Optional[str]:
    low = (text or "").lower()
    for marker in ("hepg2", "hek293", "hela", "mcf7", "a549", "u87", "k562", "mouse", "mice", "organoid"):
        if marker in low:
            return marker.upper() if marker not in {"mouse", "mice", "organoid"} else marker
    if "cell line" in low:
        m = _CELL_LINE.search(text or "")
        if m:
            return m.group(1)
        return "cell line"
    return None


def _confidence(text: str, relation: str) -> float:
    score = 0.45
    if _evidence_ids(text):
        score += 0.15
    experiments = _experiment_type(text)
    if experiments:
        score += min(0.2, 0.08 * len(experiments))
    if relation != "associated_with":
        score += 0.1
    low = (text or "").lower()
    if any(x in low for x in ("may", "might", "suggest", "hypothesis", "possible")):
        score -= 0.12
    return round(max(0.05, min(score, 0.95)), 2)


def _edge_id(subject: str, relation: str, obj: str, context: str, evidence: Iterable[str]) -> str:
    raw = json.dumps(
        {
            "s": subject,
            "r": relation,
            "o": obj,
            "c": context or "",
            "e": sorted(evidence or []),
        },
        sort_keys=True,
    )
    return "kg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_triple(triple: Dict[str, Any], source: str = "manual") -> Optional[Dict[str, Any]]:
    s_raw = triple.get("subject")
    o_raw = triple.get("object")
    s_name = s_raw.get("name") if isinstance(s_raw, dict) else s_raw
    o_name = o_raw.get("name") if isinstance(o_raw, dict) else o_raw
    subject = _clean_entity(str(s_name or ""))
    obj = _clean_entity(str(o_name or ""))
    if not subject or not obj or subject == obj:
        return None

    relation = str(triple.get("relation") or "associated_with").strip()
    if relation in POSITIVE_RELATIONS:
        relation = POSITIVE_RELATIONS[relation]
    elif relation in NEGATIVE_RELATIONS:
        relation = NEGATIVE_RELATIONS[relation]
    elif relation not in RELATION_DIRECTION:
        relation = "associated_with"

    evidence = triple.get("evidence") or triple.get("evidence_ids") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    context = str(triple.get("context") or "").strip()
    experiments = triple.get("experiment_type") or []
    if isinstance(experiments, str):
        experiments = [experiments]
    out = {
        "id": str(triple.get("id") or _edge_id(subject, relation, obj, context, evidence)),
        "subject": {
            "name": subject,
            "type": (s_raw.get("type") if isinstance(s_raw, dict) else None) or "gene_or_biomolecule",
            **({"id": s_raw.get("id")} if isinstance(s_raw, dict) and s_raw.get("id") else {}),
        },
        "relation": relation,
        "object": {
            "name": obj,
            "type": (o_raw.get("type") if isinstance(o_raw, dict) else None) or "gene_or_biomolecule",
            **({"id": o_raw.get("id")} if isinstance(o_raw, dict) and o_raw.get("id") else {}),
        },
        "direction": triple.get("direction") or relation_direction(relation),
        "context": context,
        "experiment_type": list(dict.fromkeys(str(x) for x in experiments if x)),
        "model_system": triple.get("model_system"),
        "evidence": list(dict.fromkeys(str(x) for x in evidence if x)),
        "confidence": round(float(triple.get("confidence", 0.5)), 2),
        "timestamp": triple.get("timestamp") or _now_iso(),
        "source": triple.get("source") or source,
    }
    if triple.get("claim_text"):
        out["claim_text"] = str(triple["claim_text"])[:600]
    return out


def extract_triples(
    text: str,
    context: str = "",
    source: str = "conversation",
    timestamp: Optional[str] = None,
) -> List[Dict[str, Any]]:
    text = text or ""
    evidence = _evidence_ids(text)
    experiments = _experiment_type(text)
    model = _model_system(text)
    triples: List[Dict[str, Any]] = []

    def add(s: str, relation: str, o: str, claim: str) -> None:
        ns, no = _clean_entity(s), _clean_entity(o)
        if not ns or not no or ns == no:
            return
        t = normalize_triple(
            {
                "subject": {"name": ns, "type": "gene_or_biomolecule"},
                "relation": relation,
                "object": {"name": no, "type": "gene_or_biomolecule"},
                "evidence": evidence,
                "context": context,
                "experiment_type": experiments,
                "model_system": model,
                "confidence": _confidence(text, relation),
                "timestamp": timestamp or _now_iso(),
                "source": source,
                "claim_text": claim.strip(),
            },
            source=source,
        )
        if t:
            triples.append(t)

    for m in _ARROW.finditer(text):
        add(m.group(1), "causally_upregulates", m.group(2), m.group(0))
    for m in _VERB.finditer(text):
        verb = m.group(2).lower()
        rel = POSITIVE_RELATIONS.get(verb) or NEGATIVE_RELATIONS.get(verb) or "associated_with"
        add(m.group(1), rel, m.group(3), m.group(0))
    for m in _ASSOC.finditer(text):
        add(m.group(1), "associated_with", m.group(2), m.group(0))

    dedup: Dict[str, Dict[str, Any]] = {}
    for t in triples:
        dedup.setdefault(t["id"], t)
    return list(dedup.values())


def append_triples(path: str, triples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with p.open("a", encoding="utf-8") as f:
        for triple in triples:
            norm = normalize_triple(triple, source=triple.get("source") or "manual")
            if not norm:
                continue
            f.write(json.dumps(norm, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    return {"path": str(p), "written": written}


def load_triples(path: Optional[str] = None, triples: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in triples or []:
        norm = normalize_triple(item, source=item.get("source") if isinstance(item, dict) else "manual")
        if norm:
            out.append(norm)
    if path:
        p = Path(path)
        if p.is_file():
            raw = p.read_text("utf-8").strip()
            if raw:
                if raw.startswith("{"):
                    data = json.loads(raw)
                    rows = data.get("triples") if isinstance(data, dict) else []
                    for row in rows or []:
                        norm = normalize_triple(row, source=row.get("source", "file"))
                        if norm:
                            out.append(norm)
                else:
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        norm = normalize_triple(row, source=row.get("source", "file"))
                        if norm:
                            out.append(norm)
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in out:
        dedup[item["id"]] = item
    return list(dedup.values())


def query_triples(
    triples: List[Dict[str, Any]],
    subject: str = "",
    obj: str = "",
    relation: str = "",
    context: str = "",
) -> List[Dict[str, Any]]:
    s = _clean_entity(subject) if subject else None
    o = _clean_entity(obj) if obj else None
    rel = relation.strip() if relation else ""
    ctx = context.lower().strip()
    rows = []
    for t in triples:
        if s and t["subject"]["name"] != s:
            continue
        if o and t["object"]["name"] != o:
            continue
        if rel and t.get("relation") != rel:
            continue
        if ctx and ctx not in str(t.get("context", "")).lower():
            continue
        rows.append(t)
    return rows


def _token_set(value: str) -> Set[str]:
    return {x for x in re.split(r"[^a-z0-9]+", (value or "").lower()) if len(x) >= 3}


def context_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.5
    aa, bb = _token_set(a), _token_set(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def find_conflicts(
    triples: List[Dict[str, Any]],
    candidate: Optional[Dict[str, Any]] = None,
    min_context_similarity: float = 0.2,
) -> List[Dict[str, Any]]:
    rows = triples
    candidates = [normalize_triple(candidate or {}, source="candidate")] if candidate else rows
    candidates = [c for c in candidates if c]
    conflicts: List[Dict[str, Any]] = []
    for cand in candidates:
        for existing in rows:
            if cand["id"] == existing["id"]:
                continue
            same_pair = (
                cand["subject"]["name"] == existing["subject"]["name"]
                and cand["object"]["name"] == existing["object"]["name"]
            )
            if not same_pair:
                continue
            if not opposite_direction(cand["relation"], existing["relation"]):
                continue
            sim = context_similarity(cand.get("context", ""), existing.get("context", ""))
            if sim < min_context_similarity:
                continue
            conflicts.append({
                "candidate": cand,
                "conflicting_edge": existing,
                "context_similarity": round(sim, 3),
                "reason": "same directed entity pair has opposite causal direction in overlapping context",
            })
    return conflicts


def causal_paths(
    triples: List[Dict[str, Any]],
    source: str,
    target: str,
    max_depth: int = 4,
) -> List[List[Dict[str, Any]]]:
    src = _clean_entity(source)
    dst = _clean_entity(target)
    if not src or not dst:
        return []
    max_depth = max(1, min(int(max_depth), 6))
    adjacency: Dict[str, List[Dict[str, Any]]] = {}
    for t in triples:
        adjacency.setdefault(t["subject"]["name"], []).append(t)

    paths: List[List[Dict[str, Any]]] = []

    def dfs(node: str, seen: Set[str], path: List[Dict[str, Any]]) -> None:
        if len(path) >= max_depth:
            return
        for edge in adjacency.get(node, []):
            nxt = edge["object"]["name"]
            if nxt in seen:
                continue
            new_path = path + [edge]
            if nxt == dst:
                paths.append(new_path)
                continue
            dfs(nxt, seen | {nxt}, new_path)

    dfs(src, {src}, [])
    paths.sort(key=lambda p: (-min(e.get("confidence", 0.0) for e in p), len(p)))
    return paths[:20]


def gap_analysis(triples: List[Dict[str, Any]], focus_entity: str = "", context: str = "") -> List[Dict[str, Any]]:
    focus = _clean_entity(focus_entity) if focus_entity else None
    ctx = context.lower().strip()
    gaps: List[Dict[str, Any]] = []
    for t in triples:
        if focus and focus not in {t["subject"]["name"], t["object"]["name"]}:
            continue
        if ctx and ctx not in str(t.get("context", "")).lower():
            continue
        experiments = {str(x).lower() for x in t.get("experiment_type") or []}
        relation = t.get("relation")
        if relation in {"causally_upregulates", "causally_downregulates"}:
            if not ({"chip-seq", "cut&run", "cut&tag"} & experiments):
                gaps.append({
                    "edge_id": t["id"],
                    "gap": "direct_binding_not_validated",
                    "recommendation": "Add ChIP-seq, CUT&RUN, CUT&Tag, or reporter evidence before calling this a direct transcriptional edge.",
                    "priority": "high" if t.get("confidence", 0) >= 0.7 else "medium",
                })
            if not ({"rnai", "sirna", "shrna", "crispr", "crispri", "crispra"} & experiments):
                gaps.append({
                    "edge_id": t["id"],
                    "gap": "perturbation_causality_missing",
                    "recommendation": "Add loss/gain-of-function perturbation with a prespecified endpoint and rescue control.",
                    "priority": "high",
                })
        if not t.get("evidence"):
            gaps.append({
                "edge_id": t["id"],
                "gap": "citation_missing",
                "recommendation": "Attach PMID, DOI, NCT, or local evidence record before using this edge as established knowledge.",
                "priority": "high",
            })
        if t.get("confidence", 0) < 0.5:
            gaps.append({
                "edge_id": t["id"],
                "gap": "low_confidence_extraction",
                "recommendation": "Have a model or curator re-extract this relation with JSON Schema and evidence snippets.",
                "priority": "medium",
            })
    return gaps


def summarize_graph(triples: List[Dict[str, Any]]) -> Dict[str, Any]:
    nodes = sorted({t["subject"]["name"] for t in triples} | {t["object"]["name"] for t in triples})
    relations: Dict[str, int] = {}
    contexts: Dict[str, int] = {}
    for t in triples:
        relations[t["relation"]] = relations.get(t["relation"], 0) + 1
        if t.get("context"):
            contexts[t["context"]] = contexts.get(t["context"], 0) + 1
    return {
        "nodes": len(nodes),
        "edges": len(triples),
        "relation_counts": relations,
        "top_contexts": sorted(contexts.items(), key=lambda kv: kv[1], reverse=True)[:10],
        "engine": "stdlib_adjacency",
    }
