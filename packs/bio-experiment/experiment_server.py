#!/usr/bin/env python3
"""Agentic experimental design engine for BioCSSwitch."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib.server import MCPServer  # noqa: E402


server = MCPServer("bio-experiment", "0.1.0")


def _hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm_ppf(p: float) -> float:
    """Acklam normal quantile approximation."""
    if not 0 < p < 1:
        raise ValueError("p must be between 0 and 1")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def _z_alpha(alpha: float) -> float:
    return abs(_norm_ppf(alpha / 2.0))


def _z_power(power: float) -> float:
    return _norm_ppf(power)


def _infer_assay_family(text: str, assay_family: str = "") -> str:
    if assay_family:
        return assay_family.lower().replace(" ", "_")
    low = (text or "").lower()
    if any(x in low for x in ("chip", "cut&run", "cut&tag", "binding")):
        return "chromatin_binding"
    if any(x in low for x in ("crispr", "knockout", "knockdown", "rnai", "sirna", "shrna")):
        return "perturbation"
    if any(x in low for x in ("single-cell", "scrna", "spatial", "xenium", "visium")):
        return "single_cell_or_spatial"
    if any(x in low for x in ("drug", "compound", "dose", "ic50")):
        return "compound_response"
    if any(x in low for x in ("western", "qpcr", "rna-seq", "expression")):
        return "expression_validation"
    return "mechanism_validation"


def _dimension(name: str, score: int, reason: str) -> Dict[str, Any]:
    return {"dimension": name, "score": max(0, min(int(score), 10)), "reason": reason}


def _grade(score: int) -> str:
    if score >= 80:
        return "ready_for_prespecified_experiment"
    if score >= 60:
        return "testable_after_pilot_or_refinement"
    if score >= 40:
        return "underspecified_high_risk"
    return "not_currently_testable"


def _testability_core(
    hypothesis: str,
    disease_context: str = "",
    model_system: str = "",
    endpoint: str = "",
    perturbation: str = "",
    assay_family: str = "",
    prior_evidence: str = "",
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    constraints = constraints or {}
    text = " ".join(str(x or "") for x in (hypothesis, disease_context, model_system, endpoint, perturbation, assay_family, prior_evidence))
    assay = _infer_assay_family(text, assay_family)
    dims = [
        _dimension("hypothesis_specificity", 9 if hypothesis and any(v in hypothesis.lower() for v in ("increase", "decrease", "activate", "inhibit", "upregulate", "downregulate", "causes", "drives")) else 5,
                   "Directional causal language is present." if hypothesis else "No explicit hypothesis supplied."),
        _dimension("measurable_endpoint", 9 if endpoint else 4, "Primary endpoint is prespecified." if endpoint else "Endpoint is missing or vague."),
        _dimension("model_system_fit", 8 if model_system else 4, "Model system is named." if model_system else "Model system must be chosen before execution."),
        _dimension("perturbability", 8 if perturbation else (7 if assay in {"perturbation", "compound_response"} else 5),
                   "Intervention/perturbation is named." if perturbation else "Perturbation strategy needs specification."),
        _dimension("assay_specificity", 8 if assay != "mechanism_validation" else 6, f"Assay family inferred as {assay}."),
        _dimension("statistical_power_readiness", 8 if endpoint and constraints.get("effect_size") else 5,
                   "Effect size is available for power analysis." if constraints.get("effect_size") else "Needs pilot estimate or minimally important effect."),
        _dimension("evidence_boundary", 8 if prior_evidence else 5, "Prior evidence/gap summary supplied." if prior_evidence else "Evidence gap not yet grounded in references."),
        _dimension("reagent_availability", 7 if model_system or perturbation else 5, "Named system/intervention can be mapped to reagent checks." if (model_system or perturbation) else "Reagent feasibility unknown."),
        _dimension("ethics_and_safety", 8 if not constraints.get("human_subjects") else 6,
                   "No human-subject signal supplied." if not constraints.get("human_subjects") else "Human-subject governance is required before execution."),
        _dimension("eln_readiness", 8 if endpoint and model_system and perturbation else 5, "Protocol can be serialized for ELN." if endpoint and model_system and perturbation else "Protocol lacks required ELN fields."),
    ]
    score = round(sum(d["score"] for d in dims) * 10 / len(dims))
    blockers = [d for d in dims if d["score"] <= 5]
    return {
        "schema": "bio-experiment/testability-score/1",
        "score_100": score,
        "grade": _grade(score),
        "assay_family": assay,
        "dimensions": dims,
        "blockers": blockers,
        "grade_inversion": "GRADE is used in reverse here: certainty is not the claim strength, but whether current methods can answer the hypothesis.",
    }


def _power_core(
    outcome_type: str = "continuous",
    alpha: float = 0.05,
    power: float = 0.8,
    effect_size: Optional[float] = None,
    sd: Optional[float] = None,
    baseline_rate: Optional[float] = None,
    treatment_rate: Optional[float] = None,
    hazard_ratio: Optional[float] = None,
    allocation_ratio: float = 1.0,
) -> Dict[str, Any]:
    outcome_type = (outcome_type or "continuous").lower()
    alpha = max(1e-6, min(float(alpha), 0.5))
    power = max(0.5, min(float(power), 0.999))
    ratio = max(0.1, min(float(allocation_ratio or 1.0), 10.0))
    za = _z_alpha(alpha)
    zb = _z_power(power)
    notes: List[str] = []
    n_per_arm = None
    total = None
    events = None

    if outcome_type == "continuous":
        if effect_size is None and sd and treatment_rate is not None and baseline_rate is not None:
            effect_size = abs(float(treatment_rate) - float(baseline_rate)) / float(sd)
        d = abs(float(effect_size if effect_size is not None else 0.5))
        if d <= 0:
            d = 0.5
            notes.append("Effect size was missing or zero; used Cohen d=0.5 placeholder.")
        n_equal = 2 * ((za + zb) ** 2) / (d ** 2)
        n_per_arm = math.ceil(n_equal * (1 + ratio) / (2 * math.sqrt(ratio)))
        total = math.ceil(n_per_arm * (1 + ratio))
    elif outcome_type == "binary":
        p1 = float(baseline_rate if baseline_rate is not None else 0.2)
        p2 = float(treatment_rate if treatment_rate is not None else 0.4)
        p1 = max(1e-4, min(p1, 0.9999))
        p2 = max(1e-4, min(p2, 0.9999))
        pbar = (p1 + p2) / 2
        delta = abs(p2 - p1)
        if delta <= 0:
            delta = 0.2
            notes.append("Binary rates were equal or missing; used absolute difference 0.20 placeholder.")
        n_equal = ((za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (delta ** 2)
        n_per_arm = math.ceil(n_equal * (1 + ratio) / (2 * math.sqrt(ratio)))
        total = math.ceil(n_per_arm * (1 + ratio))
    elif outcome_type == "survival":
        hr = abs(float(hazard_ratio if hazard_ratio is not None else 0.67))
        if hr <= 0 or math.isclose(hr, 1.0):
            hr = 0.67
            notes.append("Hazard ratio was missing, invalid, or 1.0; used HR=0.67 placeholder.")
        events = math.ceil(((za + zb) ** 2) / (math.log(hr) ** 2))
        total = events
        notes.append("Survival output is required events, not recruited participants; inflate for accrual and censoring.")
    elif outcome_type == "correlation":
        r = abs(float(effect_size if effect_size is not None else 0.3))
        r = max(0.05, min(r, 0.95))
        z = 0.5 * math.log((1 + r) / (1 - r))
        total = math.ceil(((za + zb) / z) ** 2 + 3)
    else:
        notes.append("Unsupported outcome_type; returned planning scaffold only.")

    return {
        "schema": "bio-experiment/power-analysis/1",
        "outcome_type": outcome_type,
        "alpha": alpha,
        "power": power,
        "allocation_ratio": ratio,
        "n_per_arm": n_per_arm,
        "total_n_or_events": total,
        "required_events": events,
        "assumptions": {
            "effect_size": effect_size,
            "sd": sd,
            "baseline_rate": baseline_rate,
            "treatment_rate": treatment_rate,
            "hazard_ratio": hazard_ratio,
        },
        "notes": notes + ["Treat this as a priori planning; replace placeholders with pilot or literature-derived estimates."],
    }


def _reagents(assay_family: str, model_system: str = "", perturbation: str = "", species: str = "") -> List[Dict[str, Any]]:
    assay_family = _infer_assay_family(" ".join([assay_family, perturbation]))
    items = [
        {"item": "authenticated model system", "purpose": "biological test bed", "lookup": "ATCC/DSMZ/Coriell search; record lot and STR or genotype QC"},
        {"item": "mycoplasma test kit", "purpose": "cell culture QC", "lookup": "vendor catalog"},
        {"item": "validated positive and negative controls", "purpose": "assay calibration", "lookup": "literature plus local inventory"},
    ]
    if assay_family in {"perturbation", "compound_response", "mechanism_validation"}:
        items.extend([
            {"item": "perturbation reagent", "purpose": perturbation or "loss/gain-of-function or compound arm", "lookup": "Addgene/ChEMBL/vendor; capture sequence, clone, lot, and concentration"},
            {"item": "rescue or orthogonal perturbation reagent", "purpose": "rule out off-target effects", "lookup": "Addgene/vendor"},
        ])
    if assay_family == "chromatin_binding":
        items.extend([
            {"item": "ChIP/CUT&RUN-grade antibody", "purpose": "direct binding assay", "lookup": "Antibodypedia/CiteAb/vendor validation in target species"},
            {"item": "input and IgG controls", "purpose": "background control", "lookup": "vendor catalog"},
            {"item": "library prep kit", "purpose": "sequencing-ready DNA", "lookup": "sequencing core catalog"},
        ])
    if assay_family in {"expression_validation", "single_cell_or_spatial", "mechanism_validation"}:
        items.extend([
            {"item": "qPCR primers or RNA-seq library kit", "purpose": "transcript endpoint", "lookup": "Primer-BLAST/vendor/core facility"},
            {"item": "protein validation antibody", "purpose": "orthogonal protein endpoint", "lookup": "Antibodypedia with knockout validation preferred"},
        ])
    if "mouse" in (model_system + species).lower() or "xenograft" in assay_family:
        items.append({"item": "animal protocol materials", "purpose": "in vivo validation", "lookup": "IACUC-approved vendor and humane endpoint SOP"})
    return items


def _equipment(assay_family: str) -> List[str]:
    assay_family = _infer_assay_family(assay_family)
    base = ["calibrated pipettes", "biosafety cabinet", "incubator or validated sample handling chain", "ELN with protocol versioning"]
    if assay_family == "chromatin_binding":
        base.extend(["sonicator or MNase/CUT&RUN workflow", "qPCR instrument", "sequencer or sequencing core"])
    elif assay_family == "single_cell_or_spatial":
        base.extend(["single-cell controller or spatial platform", "sequencing core", "GPU/CPU analysis workstation"])
    elif assay_family == "compound_response":
        base.extend(["plate reader or high-content imager", "automated dispenser if dose-response is dense"])
    else:
        base.extend(["qPCR or western blot system", "flow cytometer or imaging system if endpoint requires it"])
    return base


def _timeline(assay_family: str) -> List[Dict[str, Any]]:
    assay_family = _infer_assay_family(assay_family)
    weeks = [
        ("literature gap lock and preregistration", 1),
        ("reagent ordering and QC", 2),
        ("pilot assay and effect-size calibration", 2),
        ("powered main experiment", 3),
        ("analysis, robustness checks, and KG update", 2),
    ]
    if assay_family in {"single_cell_or_spatial", "chromatin_binding"}:
        weeks[3] = ("powered main experiment and sequencing/core queue", 5)
    cursor = 0
    out = []
    for name, duration in weeks:
        out.append({"phase": name, "start_week": cursor + 1, "duration_weeks": duration})
        cursor += duration
    return out


def _cost(assay_family: str, n_total: Optional[int], budget_usd: Optional[float] = None) -> Dict[str, Any]:
    assay_family = _infer_assay_family(assay_family)
    n = max(1, int(n_total or 12))
    per_sample = {
        "chromatin_binding": 450,
        "single_cell_or_spatial": 1800,
        "compound_response": 80,
        "expression_validation": 120,
        "perturbation": 160,
        "mechanism_validation": 140,
    }.get(assay_family, 140)
    fixed = 2500 if assay_family in {"single_cell_or_spatial", "chromatin_binding"} else 900
    estimate = fixed + per_sample * n
    return {
        "currency": "USD",
        "rough_estimate": int(estimate),
        "budget_usd": budget_usd,
        "budget_fit": None if budget_usd is None else ("within_budget" if estimate <= float(budget_usd) else "over_budget"),
        "cost_model": {"fixed": fixed, "per_sample": per_sample, "n": n},
        "warning": "Rough planning only; replace with institutional core quotes and local reagent pricing.",
    }


def _failure_modes(assay_family: str, model_system: str = "") -> List[Dict[str, Any]]:
    assay_family = _infer_assay_family(assay_family)
    modes = [
        {"failure_mode": "effect size smaller than assumed", "early_signal": "pilot CI overlaps null or primary endpoint variance is high", "mitigation": "revise power assumptions before main run"},
        {"failure_mode": "model system does not recapitulate disease state", "early_signal": "marker panel or baseline phenotype fails QC", "mitigation": "add second model, primary cells, organoid, or spatial validation"},
        {"failure_mode": "batch or operator effect dominates", "early_signal": "PCA/plate/date explains more variance than treatment", "mitigation": "block randomization, blinded processing, batch covariate in model"},
        {"failure_mode": "off-target or nonspecific perturbation", "early_signal": "single reagent effect not reproduced by orthogonal reagent", "mitigation": "use multiple guides/siRNAs, rescue, and dose titration"},
    ]
    if assay_family == "chromatin_binding":
        modes.append({"failure_mode": "antibody lacks target specificity", "early_signal": "IgG/input controls or knockout control fail", "mitigation": "switch antibody, use CUT&RUN/CUT&Tag, or orthogonal reporter assay"})
    if assay_family == "single_cell_or_spatial":
        modes.append({"failure_mode": "cell-state or spatial niche is underpowered", "early_signal": "rare state count below prespecified minimum", "mitigation": "enrich target population or redesign sampling strata"})
    if "mouse" in model_system.lower():
        modes.append({"failure_mode": "preclinical-to-human extrapolation", "early_signal": "human dataset does not show matching direction", "mitigation": "add human tissue or public cohort validation before translational claims"})
    return modes


def _stats_plan(outcome_type: str, endpoint: str, assay_family: str) -> Dict[str, Any]:
    outcome_type = outcome_type or "continuous"
    if outcome_type == "binary":
        model = "logistic regression or Fisher exact test with prespecified covariates"
    elif outcome_type == "survival":
        model = "Cox model plus Kaplan-Meier visualization; report required events"
    elif outcome_type == "correlation":
        model = "Pearson/Spearman with prespecified direction and multiplicity control"
    else:
        model = "linear model or two-sample test; include batch/block as covariate where applicable"
    multiple = "Benjamini-Hochberg FDR" if assay_family in {"single_cell_or_spatial", "chromatin_binding"} else "Holm or prespecified single-primary endpoint"
    return {
        "primary_endpoint": endpoint or "<FILL primary endpoint>",
        "primary_model": model,
        "randomization": "block by batch/date/model passage where possible",
        "blinding": "blind endpoint quantification and analysis labels until QC lock",
        "multiplicity": multiple,
        "exclusions": "predefine technical QC failures before seeing treatment labels",
        "reporting": "effect size, CI, exact n, missing data, and all prespecified controls",
    }


def _protocol(
    research_question: str,
    hypothesis: str,
    model_system: str,
    intervention: str,
    endpoint: str,
    assay_family: str,
) -> List[Dict[str, Any]]:
    return [
        {"section": "objective", "text": research_question or hypothesis},
        {"section": "hypothesis", "text": hypothesis},
        {"section": "model_system", "text": model_system or "<FILL model system and authentication/QC>"},
        {"section": "intervention", "text": intervention or "<FILL perturbation/compound/exposure>"},
        {"section": "controls", "text": "vehicle/mock/non-targeting control, positive control, and orthogonal validation arm"},
        {"section": "assay", "text": assay_family},
        {"section": "primary_endpoint", "text": endpoint or "<FILL endpoint before execution>"},
        {"section": "qc_lock", "text": "lock sample inclusion, batch QC, and analysis plan before unblinding"},
        {"section": "data_capture", "text": "store raw data paths, reagent lot IDs, script hashes, and KG edge IDs in ELN"},
    ]


@server.tool(
    "hypothesis_testability_score",
    "Score whether a biomedical hypothesis can be answered with current experimental methods. "
    "This reverses the GRADE idea: instead of rating how certain a conclusion is, it rates how testable the hypothesis is.",
    {
        "type": "object",
        "properties": {
            "hypothesis": {"type": "string"},
            "disease_context": {"type": "string"},
            "model_system": {"type": "string"},
            "endpoint": {"type": "string"},
            "perturbation": {"type": "string"},
            "assay_family": {"type": "string"},
            "prior_evidence": {"type": "string"},
            "constraints": {"type": "object"},
        },
        "required": ["hypothesis"],
    },
)
def hypothesis_testability_score(
    hypothesis: str,
    disease_context: str = "",
    model_system: str = "",
    endpoint: str = "",
    perturbation: str = "",
    assay_family: str = "",
    prior_evidence: str = "",
    constraints: Optional[Dict[str, Any]] = None,
):
    return _testability_core(hypothesis, disease_context, model_system, endpoint, perturbation, assay_family, prior_evidence, constraints)


@server.tool(
    "power_analysis_plan",
    "A priori power/sample-size scaffold for common biomedical designs using stdlib-only normal approximations.",
    {
        "type": "object",
        "properties": {
            "outcome_type": {"type": "string", "enum": ["continuous", "binary", "survival", "correlation"]},
            "alpha": {"type": "number", "default": 0.05},
            "power": {"type": "number", "default": 0.8},
            "effect_size": {"type": "number"},
            "sd": {"type": "number"},
            "baseline_rate": {"type": "number"},
            "treatment_rate": {"type": "number"},
            "hazard_ratio": {"type": "number"},
            "allocation_ratio": {"type": "number", "default": 1.0},
        },
    },
)
def power_analysis_plan(
    outcome_type: str = "continuous",
    alpha: float = 0.05,
    power: float = 0.8,
    effect_size: Optional[float] = None,
    sd: Optional[float] = None,
    baseline_rate: Optional[float] = None,
    treatment_rate: Optional[float] = None,
    hazard_ratio: Optional[float] = None,
    allocation_ratio: float = 1.0,
):
    return _power_core(outcome_type, alpha, power, effect_size, sd, baseline_rate, treatment_rate, hazard_ratio, allocation_ratio)


@server.tool(
    "reagent_equipment_checklist",
    "Generate a structured reagent/equipment checklist with lookup hints for Addgene, ATCC, Antibodypedia, ChEMBL, and local inventory.",
    {
        "type": "object",
        "properties": {
            "assay_family": {"type": "string"},
            "model_system": {"type": "string"},
            "perturbation": {"type": "string"},
            "species": {"type": "string"},
        },
    },
)
def reagent_equipment_checklist(
    assay_family: str = "",
    model_system: str = "",
    perturbation: str = "",
    species: str = "",
):
    assay = _infer_assay_family(" ".join([assay_family, perturbation]), assay_family)
    return {
        "schema": "bio-experiment/reagent-equipment-checklist/1",
        "assay_family": assay,
        "reagents": _reagents(assay, model_system, perturbation, species),
        "equipment": _equipment(assay),
        "external_lookup_contract": {
            "Addgene": "search plasmids/guides; record plasmid ID, depositor, sequence, antibiotic, and MTA status",
            "ATCC": "search cell line; record catalog number, authentication method, passage, and mycoplasma status",
            "Antibodypedia": "prefer knockout-validated antibodies in the target species and assay type",
            "ChEMBL": "for compounds, record ChEMBL ID, activity endpoint, units, and assay organism",
        },
    }


@server.tool(
    "preregistration_template",
    "Generate OSF-style and ClinicalTrials.gov PRS-style preregistration fields for a biomedical experiment.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "hypothesis": {"type": "string"},
            "primary_endpoint": {"type": "string"},
            "model_system": {"type": "string"},
            "intervention": {"type": "string"},
            "outcome_type": {"type": "string"},
            "sample_size_plan": {"type": "object"},
        },
    },
)
def preregistration_template(
    title: str = "",
    hypothesis: str = "",
    primary_endpoint: str = "",
    model_system: str = "",
    intervention: str = "",
    outcome_type: str = "continuous",
    sample_size_plan: Optional[Dict[str, Any]] = None,
):
    sample_size_plan = sample_size_plan or {}
    return {
        "schema": "bio-experiment/preregistration/1",
        "osf": {
            "title": title or hypothesis[:100],
            "research_question": "<FILL>",
            "hypothesis": hypothesis,
            "design_plan": {
                "study_type": "preclinical/mechanistic experiment",
                "model_system": model_system,
                "intervention": intervention,
                "allocation": "randomized where feasible",
                "blinding": "analysis labels blinded until QC lock",
            },
            "sampling_plan": sample_size_plan,
            "variables": {"primary_endpoint": primary_endpoint, "outcome_type": outcome_type},
            "analysis_plan": _stats_plan(outcome_type, primary_endpoint, ""),
            "exclusion_criteria": "technical QC failures defined before unblinding",
        },
        "ctgov_prs_like": {
            "brief_title": title or hypothesis[:100],
            "study_type": "Interventional" if "patient" in model_system.lower() else "Basic science / not a clinical trial",
            "primary_outcome_measures": [primary_endpoint or "<FILL primary outcome>"],
            "arms_and_interventions": [intervention or "<FILL intervention/control arms>"],
            "eligibility_or_model_criteria": model_system or "<FILL model criteria>",
            "responsible_party": "<FILL>",
        },
    }


@server.tool(
    "failure_mode_analysis",
    "Pre-mortem failure mode analysis for a proposed biomedical experiment.",
    {
        "type": "object",
        "properties": {
            "hypothesis": {"type": "string"},
            "assay_family": {"type": "string"},
            "model_system": {"type": "string"},
        },
    },
)
def failure_mode_analysis(
    hypothesis: str = "",
    assay_family: str = "",
    model_system: str = "",
):
    assay = _infer_assay_family(" ".join([hypothesis, assay_family]), assay_family)
    return {
        "schema": "bio-experiment/failure-mode-analysis/1",
        "assay_family": assay,
        "failure_modes": _failure_modes(assay, model_system),
        "stopping_rules": [
            "Stop before main experiment if positive/negative controls fail.",
            "Stop or redesign if pilot variance makes the powered design infeasible.",
            "Do not upgrade conclusion scope if orthogonal validation fails.",
        ],
    }


@server.tool(
    "agentic_experiment_plan",
    "Closed-loop experimental design engine: literature gap -> hypothesis -> testability -> power -> protocol -> reagents/equipment -> cost/timeline -> preregistration -> failure modes.",
    {
        "type": "object",
        "properties": {
            "research_question": {"type": "string"},
            "hypothesis": {"type": "string"},
            "disease_context": {"type": "string"},
            "model_system": {"type": "string"},
            "intervention": {"type": "string"},
            "primary_endpoint": {"type": "string"},
            "assay_family": {"type": "string"},
            "outcome_type": {"type": "string", "enum": ["continuous", "binary", "survival", "correlation"]},
            "effect_size": {"type": "number"},
            "baseline_rate": {"type": "number"},
            "treatment_rate": {"type": "number"},
            "hazard_ratio": {"type": "number"},
            "power": {"type": "number", "default": 0.8},
            "alpha": {"type": "number", "default": 0.05},
            "budget_usd": {"type": "number"},
            "timeline_weeks": {"type": "integer"},
            "prior_evidence": {"type": "string"},
            "species": {"type": "string"},
            "constraints": {"type": "object"},
        },
        "required": ["hypothesis"],
    },
)
def agentic_experiment_plan(
    hypothesis: str,
    research_question: str = "",
    disease_context: str = "",
    model_system: str = "",
    intervention: str = "",
    primary_endpoint: str = "",
    assay_family: str = "",
    outcome_type: str = "continuous",
    effect_size: Optional[float] = None,
    baseline_rate: Optional[float] = None,
    treatment_rate: Optional[float] = None,
    hazard_ratio: Optional[float] = None,
    power: float = 0.8,
    alpha: float = 0.05,
    budget_usd: Optional[float] = None,
    timeline_weeks: Optional[int] = None,
    prior_evidence: str = "",
    species: str = "",
    constraints: Optional[Dict[str, Any]] = None,
):
    constraints = constraints or {}
    assay = _infer_assay_family(" ".join([hypothesis, assay_family, intervention]), assay_family)
    testability = _testability_core(
        hypothesis=hypothesis,
        disease_context=disease_context,
        model_system=model_system,
        endpoint=primary_endpoint,
        perturbation=intervention,
        assay_family=assay,
        prior_evidence=prior_evidence,
        constraints={**constraints, "effect_size": effect_size},
    )
    power_result = _power_core(
        outcome_type=outcome_type,
        alpha=alpha,
        power=power,
        effect_size=effect_size,
        baseline_rate=baseline_rate,
        treatment_rate=treatment_rate,
        hazard_ratio=hazard_ratio,
    )
    n_total = power_result.get("total_n_or_events")
    prereg = preregistration_template(
        title=research_question or hypothesis[:100],
        hypothesis=hypothesis,
        primary_endpoint=primary_endpoint,
        model_system=model_system,
        intervention=intervention,
        outcome_type=outcome_type,
        sample_size_plan=power_result,
    )
    plan_core = {
        "research_question": research_question,
        "hypothesis": hypothesis,
        "disease_context": disease_context,
        "model_system": model_system,
        "intervention": intervention,
        "primary_endpoint": primary_endpoint,
        "assay_family": assay,
    }
    return {
        "schema": "bio-experiment/agentic-plan/1",
        "plan_id": _hash(plan_core)[:24],
        "workflow": [
            {"step": 1, "name": "literature_gap_lock", "handoff": "bio-lit + bio-audit evidence_graph; record unresolved gaps"},
            {"step": 2, "name": "hypothesis_generation", "handoff": "multi-agent debate can propose rival hypotheses; choose testable one"},
            {"step": 3, "name": "testability_gate", "result": testability["grade"]},
            {"step": 4, "name": "a_priori_power", "result": power_result},
            {"step": 5, "name": "eln_protocol", "result": "protocol_sections"},
            {"step": 6, "name": "reagent_equipment_lock", "result": "reagents_equipment"},
            {"step": 7, "name": "preregistration", "result": "osf_and_prs_like_templates"},
            {"step": 8, "name": "premortem_failure_modes", "result": "failure_modes"},
            {"step": 9, "name": "kg_update", "handoff": "bio-kg kg_add_triples after results are curated"},
        ],
        "testability": testability,
        "sample_size": {"analysis": power_result},
        "statistics": _stats_plan(outcome_type, primary_endpoint, assay),
        "protocol_sections": _protocol(research_question, hypothesis, model_system, intervention, primary_endpoint, assay),
        "reagents_equipment": reagent_equipment_checklist(assay, model_system, intervention, species),
        "cost_time": {
            "timeline": _timeline(assay),
            "target_timeline_weeks": timeline_weeks,
            "cost": _cost(assay, n_total, budget_usd),
        },
        "preregistration": prereg,
        "failure_modes": _failure_modes(assay, model_system),
        "execution_contract": {
            "eln_importable": True,
            "must_fill_before_execution": [
                "exact reagent IDs and lot numbers",
                "pilot-derived effect size if placeholder was used",
                "randomization/blinding implementation",
                "raw data storage path and script hash",
            ],
            "claim_scope_rule": "Do not state mechanistic or translational certainty beyond the model system and validation arms actually completed.",
        },
    }


if __name__ == "__main__":
    server.run()
