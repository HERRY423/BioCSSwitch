"""Structured multi-agent scientific debate for BioCSSwitch Ultra.

The arena reuses Ultra route contexts but changes the topology from
single-answer fallback to parallel role pressure.  Each role receives the same
question under a different scientific obligation, then a deterministic local
synthesizer produces a debate record, uncertainty estimate, evidence grade, and
experimental roadmap.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import fallback_policy
import task_router


ROLE_SPECS = [
    {
        "id": "proponent",
        "name": "Mechanistic Proponent",
        "stance": "argue the strongest biologically plausible case for the hypothesis",
        "focus": ["mechanism", "supporting evidence", "testable predictions"],
    },
    {
        "id": "skeptic",
        "name": "Adversarial Skeptic",
        "stance": "attack weak inference, confounding, extrapolation, and missing controls",
        "focus": ["bias", "alternative explanations", "boundary conditions"],
    },
    {
        "id": "methodologist",
        "name": "Methods and Statistics Auditor",
        "stance": "evaluate design, power, endpoints, controls, and reproducibility",
        "focus": ["study design", "statistics", "GRADE downgrades"],
    },
    {
        "id": "experimentalist",
        "name": "Translational Experimentalist",
        "stance": "convert the debate into executable next experiments",
        "focus": ["assays", "model systems", "failure modes"],
    },
]


@dataclass
class DebateTurn:
    role_id: str
    role_name: str
    provider: str
    model: str
    profile_id: str
    round_index: int
    status: int
    outcome: str
    text: str
    reason: str = ""


def should_run_debate(req: Dict[str, Any], task_id: str, mode: str, config: Dict[str, Any]) -> bool:
    meta = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
    ultra = config.get("ultra") if isinstance(config.get("ultra"), dict) else {}
    if meta.get("csswitch_debate") is True or meta.get("bio_debate") is True:
        return True
    if task_id == "scientific-debate":
        return True
    mode_l = str(mode or "").lower()
    if "debate" in mode_l or "arena" in mode_l:
        return True
    return bool(ultra.get("force_scientific_debate"))


def debate_body(req_model: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "msg_biocsswitch_debate",
        "type": "message",
        "role": "assistant",
        "model": req_model or "bio-debate-arena",
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2, default=str)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def run_debate(
    req: Dict[str, Any],
    contexts: List[Dict[str, Any]],
    call_model: Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any], int], Tuple[int, Dict[str, Any], fallback_policy.Failure, str]],
    task_id: str = "scientific-debate",
    max_agents: int = 4,
    rounds: int = 2,
) -> Dict[str, Any]:
    max_agents = max(2, min(int(max_agents or 4), len(ROLE_SPECS)))
    rounds = max(1, min(int(rounds or 2), 3))
    roles = ROLE_SPECS[:max_agents]
    if not contexts:
        raise ValueError("debate requires at least one route context")

    turns: List[DebateTurn] = []
    transcript = ""
    for round_index in range(1, rounds + 1):
        for idx, role in enumerate(roles):
            ctx = contexts[idx % len(contexts)]
            role_req = _role_request(req, role, round_index, transcript)
            status, body, failure, model = call_model(role_req, ctx, role, round_index)
            text = fallback_policy.response_text(body) if isinstance(body, dict) else ""
            turn = DebateTurn(
                role_id=role["id"],
                role_name=role["name"],
                provider=str(ctx.get("provider", "")),
                model=model,
                profile_id=str(ctx.get("profile_id", "")),
                round_index=round_index,
                status=status,
                outcome=failure.kind,
                text=text.strip(),
                reason=failure.reason,
            )
            turns.append(turn)
            transcript += f"\n[{round_index}:{role['id']}]\n{text.strip()[:4000]}\n"

    synthesis = synthesize_debate(req, turns, task_id)
    return {
        "schema": "bio-debate/scientific-debate/1",
        "task_id": task_id,
        "roles": roles,
        "route_contexts": [
            {
                "profile_id": c.get("profile_id", ""),
                "profile_name": c.get("profile_name", ""),
                "provider": c.get("provider", ""),
                "model": c.get("model", ""),
                "route_source": c.get("_route_source", ""),
            }
            for c in contexts[:max_agents]
        ],
        "turns": [t.__dict__ for t in turns],
        **synthesis,
    }


def _role_request(req: Dict[str, Any], role: Dict[str, Any], round_index: int, transcript: str) -> Dict[str, Any]:
    out = copy.deepcopy(req)
    out["stream"] = False
    base_system = _system_text(out.get("system"))
    instruction = f"""
You are the {role['name']} in BioCSSwitch's Multi-Agent Scientific Debate Arena.
Your stance: {role['stance']}.
Focus areas: {', '.join(role['focus'])}.

Return a compact structured argument with:
- claim_summary
- strongest_evidence
- weakest_link
- uncertainty_0_to_1
- evidence_grade: High|Moderate|Low|Very Low
- decisive_next_experiment
- citations_or_evidence_ids_only_if_grounded

Round {round_index}: {'initial argument' if round_index == 1 else 'respond to prior rounds and update your position'}.
Do not fabricate PMID/DOI/NCT identifiers.
"""
    if transcript and round_index > 1:
        instruction += "\nPrior debate transcript:\n" + transcript[-6000:]
    out["system"] = (base_system + "\n\n" + instruction).strip()
    return out


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
    return ""


def synthesize_debate(req: Dict[str, Any], turns: List[DebateTurn], task_id: str) -> Dict[str, Any]:
    successful = [t for t in turns if t.outcome == fallback_policy.OK and t.text]
    all_text = "\n".join(t.text for t in successful)
    grades = _grade_votes(all_text)
    uncertainty_values = _uncertainty_values(all_text)
    citations = _citation_ids(all_text)
    disagreements = _disagreement_map(successful)
    uncertainty = _aggregate_uncertainty(successful, uncertainty_values, disagreements)
    evidence_grade = _aggregate_grade(grades, citations, disagreements)
    roadmap = _roadmap(successful, all_text)
    final = _final_judgment(evidence_grade, uncertainty, disagreements, successful)
    return {
        "structured_debate_record": {
            "question": task_router.request_text(req)[:2000],
            "rounds": max([t.round_index for t in turns], default=0),
            "successful_turns": len(successful),
            "failed_turns": len(turns) - len(successful),
            "debate_axes": disagreements,
        },
        "evidence_grade": evidence_grade,
        "uncertainty": uncertainty,
        "citations_or_evidence_ids_detected": citations,
        "integrated_judgment": final,
        "experimental_roadmap": roadmap,
        "quality_gate": {
            "verdict": "warn" if len(successful) < max(2, len({t.role_id for t in turns}) // 2) else "pass",
            "notes": _quality_notes(turns, citations, disagreements),
        },
    }


def _grade_votes(text: str) -> Dict[str, int]:
    votes = {"High": 0, "Moderate": 0, "Low": 0, "Very Low": 0}
    for grade in votes:
        votes[grade] += len(re.findall(rf"\b{re.escape(grade)}\b", text or "", re.I))
    return votes


def _uncertainty_values(text: str) -> List[float]:
    values = []
    for m in re.finditer(r"uncertainty(?:_0_to_1)?\s*[:=]\s*([01](?:\.\d+)?)", text or "", re.I):
        try:
            values.append(max(0.0, min(float(m.group(1)), 1.0)))
        except ValueError:
            pass
    return values


def _citation_ids(text: str) -> List[str]:
    ids = []
    ids.extend(f"PMID:{m.group(1)}" for m in re.finditer(r"\bPMID\s*[:#]?\s*(\d{4,9})\b", text or "", re.I))
    ids.extend(m.group(0).upper() for m in re.finditer(r"\bNCT\d{8}\b", text or "", re.I))
    ids.extend(m.group(0).lower() for m in re.finditer(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text or "", re.I))
    return sorted(dict.fromkeys(ids))


def _disagreement_map(turns: List[DebateTurn]) -> List[Dict[str, Any]]:
    axes = [
        ("mechanism", ("mechanism", "pathway", "causal", "direct")),
        ("clinical_translation", ("patient", "clinical", "human", "trial")),
        ("model_boundary", ("mouse", "cell line", "organoid", "in vitro", "xenograft")),
        ("statistics", ("power", "sample size", "endpoint", "multiple testing")),
        ("reproducibility", ("control", "batch", "blinding", "randomization", "replicate")),
    ]
    out = []
    role_text = {t.role_id: t.text.lower() for t in turns}
    for axis, needles in axes:
        supporting_roles = [rid for rid, txt in role_text.items() if any(n in txt for n in needles)]
        skeptical_roles = [
            rid
            for rid, txt in role_text.items()
            if any(n in txt for n in needles) and any(w in txt for w in ("weak", "missing", "uncertain", "confound", "bias", "risk"))
        ]
        if supporting_roles:
            out.append({
                "axis": axis,
                "roles_engaged": sorted(set(supporting_roles)),
                "skeptical_roles": sorted(set(skeptical_roles)),
                "contested": bool(skeptical_roles),
            })
    return out


def _aggregate_uncertainty(turns: List[DebateTurn], values: List[float], disagreements: List[Dict[str, Any]]) -> Dict[str, Any]:
    if values:
        base = sum(values) / len(values)
    else:
        low = "\n".join(t.text.lower() for t in turns)
        hedge = sum(low.count(w) for w in ("uncertain", "may", "might", "suggest", "limited", "missing", "confound"))
        base = min(0.85, 0.25 + hedge * 0.03)
    contested = sum(1 for d in disagreements if d.get("contested"))
    score = max(0.0, min(1.0, base + contested * 0.04))
    return {
        "probability_claim_wrong_or_overstated": round(score, 2),
        "confidence_in_synthesis": round(1 - score, 2),
        "drivers": [d["axis"] for d in disagreements if d.get("contested")],
    }


def _aggregate_grade(grades: Dict[str, int], citations: List[str], disagreements: List[Dict[str, Any]]) -> Dict[str, Any]:
    if any(grades.values()):
        ordered = ["Very Low", "Low", "Moderate", "High"]
        # Conservative tie-break: if votes are tied, keep the lower evidence grade.
        winner = min(ordered, key=lambda g: (-grades[g], ordered.index(g)))
    else:
        winner = "Moderate" if citations else "Low"
    penalty = sum(1 for d in disagreements if d.get("contested"))
    ladder = ["Very Low", "Low", "Moderate", "High"]
    idx = ladder.index(winner)
    if not citations:
        idx = min(idx, 1)
    idx = max(0, idx - (1 if penalty >= 2 else 0))
    final = ladder[idx]
    return {
        "grade": final,
        "vote_counts": grades,
        "downgrade_reasons": (
            ["no grounded citation/evidence IDs detected"] if not citations else []
        ) + ([f"{penalty} contested debate axes"] if penalty else []),
    }


def _roadmap(turns: List[DebateTurn], text: str) -> List[Dict[str, Any]]:
    candidates = []
    for line in re.split(r"[\n.;]+", text or ""):
        low = line.lower()
        if any(k in low for k in ("experiment", "assay", "validate", "test", "power", "chip", "crispr", "rnai", "organoid")):
            clean = line.strip(" -\t")
            if 12 <= len(clean) <= 260:
                candidates.append(clean)
    if not candidates:
        candidates = [
            "Run a powered perturbation experiment with orthogonal controls.",
            "Add direct mechanism validation and a prespecified statistical analysis plan.",
            "Update the local KG with supported and refuted causal edges after results are audited.",
        ]
    out = []
    for idx, item in enumerate(dict.fromkeys(candidates[:6]), 1):
        out.append({
            "priority": idx,
            "experiment": item,
            "purpose": "resolve contested inference" if idx <= 2 else "increase evidence boundary clarity",
            "handoff": "bio-experiment.agentic_experiment_plan",
        })
    return out


def _final_judgment(
    evidence_grade: Dict[str, Any],
    uncertainty: Dict[str, Any],
    disagreements: List[Dict[str, Any]],
    turns: List[DebateTurn],
) -> Dict[str, Any]:
    grade = evidence_grade["grade"]
    u = uncertainty["probability_claim_wrong_or_overstated"]
    if grade in {"High", "Moderate"} and u < 0.35:
        verdict = "provisionally_supported"
    elif grade == "Very Low" or u >= 0.65:
        verdict = "not_ready_for_strong_claim"
    else:
        verdict = "plausible_but_contested"
    return {
        "verdict": verdict,
        "one_sentence": f"{grade} evidence with uncertainty {u:.2f}; {len([d for d in disagreements if d.get('contested')])} contested axes remain.",
        "minority_reports": [
            {"role": t.role_id, "excerpt": t.text[:360]}
            for t in turns
            if any(w in t.text.lower() for w in ("not ready", "weak", "missing", "confound", "bias"))
        ][:3],
    }


def _quality_notes(turns: List[DebateTurn], citations: List[str], disagreements: List[Dict[str, Any]]) -> List[str]:
    notes = []
    failed = [t for t in turns if t.outcome != fallback_policy.OK]
    if failed:
        notes.append(f"{len(failed)} debate turns failed or degraded")
    if not citations:
        notes.append("no grounded citation/evidence IDs were detected; run bio-audit before final claims")
    if not disagreements:
        notes.append("low explicit disagreement; consider adding an adversarial profile or deeper round")
    return notes
