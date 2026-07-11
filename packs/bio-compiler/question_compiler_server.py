#!/usr/bin/env python3
"""生物医学研究问题编译器 MCP。

把一个模糊的自然语言问题（"EGFR 在 GBM 里还有没有新靶点价值"）**编译**成一份
结构化研究任务书：研究对象 / 疾病 / 分子 / 干预 / 终点 / 数据库 / 排除标准 /
证据等级门槛 / 推荐工具链 / 该进哪个 workflow skill。

设计原则（和整个项目一致）：
  1. **确定性、可核对**。每个字段都能指到"凭哪条规则得到"（via 字段）。识别不到就标
     unknown / needs_user_input，绝不编。
  2. **不代替用户拍板**。编译结果是"给用户确认的任务书草案"——skill 会把它读给用户，
     缺口（排除标准、干预、终点细化）由用户补齐后才往下走。
  3. **直接接到既有工具链**。toolchain 里的工具名都是本项目真实存在的 MCP / 生成器，
     不虚构工具。

对外工具：
  compile_research_question — 主编译器
  finalize_research_brief    — 校验草案哈希并用用户答案完成任务书
  compiler_capabilities     — 自述能识别哪些实体 / 原型（透明度，便于用户判断覆盖范围）
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # packs/ → 找得到 _lib
sys.path.insert(0, str(Path(__file__).resolve().parent))      # 本目录 → 找得到 compiler_lexicon

from _lib.server import MCPServer  # noqa: E402

import compiler_lexicon as lex  # noqa: E402


server = MCPServer("bio-compiler", "0.1.0")


BRIEF_SCHEMA = "biocsswitch/research-brief/1"
BRIEF_SCHEMA_VERSION = 1
WORKFLOW_HINTS = (
    "lit-review",
    "omics-code",
    "experimental-design",
    "crossmodal-discovery",
)


# The compiler owns task semantics, not desktop pack installation.  These
# field contracts are deliberately pack-free: the host maps a validated
# workflow hint to its own allow-listed packs.
_WORKFLOW_FIELDS: Dict[str, List[Dict[str, Any]]] = {
    "lit-review": [
        {
            "field": "research_question",
            "label": "研究问题",
            "prompt": "请确认要由综述回答的研究问题。",
            "required": True,
        },
        {
            "field": "population_or_condition",
            "label": "人群或疾病",
            "prompt": "请明确目标人群、疾病及关键亚型。",
            "required": True,
        },
        {
            "field": "intervention_or_exposure",
            "label": "干预或暴露",
            "prompt": "请明确干预、对照暴露、标志物或待评估分子。",
            "required": True,
        },
        {
            "field": "primary_outcome",
            "label": "主要结局",
            "prompt": "请指定一个主要结局；其余结局可列为次要结局。",
            "required": True,
        },
        {
            "field": "review_scope",
            "label": "综述深度",
            "prompt": "请选择快速范围综述（scoping）或系统评估（systematic）。",
            "required": True,
            "options": ["scoping", "systematic"],
        },
    ],
    "omics-code": [
        {
            "field": "analysis_goal",
            "label": "分析目标",
            "prompt": "请明确希望回答的数据分析问题或交付物。",
            "required": True,
        },
        {
            "field": "input_data",
            "label": "输入数据",
            "prompt": "请说明输入数据格式或数据对象（如 h5ad、10x、FASTQ、count matrix）。",
            "required": True,
        },
        {
            "field": "organism",
            "label": "物种",
            "prompt": "请明确数据物种；不得从组织或数据集名称猜测。",
            "required": True,
        },
        {
            "field": "assay",
            "label": "测序或检测类型",
            "prompt": "请明确 assay（如 scRNA-seq、scATAC-seq、spatial 或 bulk RNA-seq）。",
            "required": True,
        },
    ],
    "experimental-design": [
        {
            "field": "hypothesis",
            "label": "可证伪假设",
            "prompt": "请把研究问题写成带方向、可被结果推翻的假设。",
            "required": True,
        },
        {
            "field": "model_system",
            "label": "模型系统",
            "prompt": "请明确细胞、类器官、动物或受试者模型及关键背景。",
            "required": True,
        },
        {
            "field": "perturbation",
            "label": "扰动或干预",
            "prompt": "请明确实验扰动、剂量/强度与方向。",
            "required": True,
        },
        {
            "field": "primary_endpoint",
            "label": "主要终点",
            "prompt": "请指定用于证伪假设的主要终点。",
            "required": True,
        },
    ],
    "crossmodal-discovery": [
        {
            "field": "disease",
            "label": "疾病",
            "prompt": "请提供规范疾病名；必要时后续用 disambiguate 落实本体 ID。",
            "required": True,
        },
        {
            "field": "unmet_need",
            "label": "未满足需求",
            "prompt": "请明确要解决的未满足临床或生物学需求，而不只是泛泛地寻找靶点。",
            "required": True,
        },
    ],
}


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_value(value: Any) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _content_material(brief: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in brief.items() if key != "content_hash"}


def _seal_new_brief(brief: Dict[str, Any]) -> Dict[str, Any]:
    identity_material = {
        key: value
        for key, value in brief.items()
        if key not in {"brief_id", "content_hash"}
    }
    identity_digest = _hash_value(identity_material).split(":", 1)[1]
    brief["brief_id"] = f"brief_{identity_digest[:24]}"
    brief["content_hash"] = _hash_value(_content_material(brief))
    return brief


def _reseal_brief(brief: Dict[str, Any]) -> Dict[str, Any]:
    brief.pop("content_hash", None)
    brief["content_hash"] = _hash_value(_content_material(brief))
    return brief


def _verify_brief(brief: Mapping[str, Any]) -> None:
    if brief.get("schema") != BRIEF_SCHEMA or brief.get("schema_version") != BRIEF_SCHEMA_VERSION:
        raise ValueError("unsupported research brief schema")
    claimed = str(brief.get("content_hash") or "")
    actual = _hash_value(_content_material(brief))
    if not claimed or claimed != actual:
        raise ValueError("draft content_hash mismatch")
    workflow = str(brief.get("workflow_hint") or "")
    if workflow not in WORKFLOW_HINTS:
        raise ValueError(f"unsupported workflow_hint: {workflow!r}")
    if any(key in brief for key in ("packs", "pack_ids", "required_packs")):
        raise ValueError("research briefs must not carry host pack lists")


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _field_value(fields: Mapping[str, Any], field: str) -> Any:
    entry = fields.get(field)
    if isinstance(entry, Mapping) and "value" in entry:
        return entry.get("value")
    return entry


def _required_fields(workflow_hint: str) -> List[str]:
    return [
        spec["field"]
        for spec in _WORKFLOW_FIELDS[workflow_hint]
        if spec.get("required")
    ]


def _clarifications(workflow_hint: str, fields: Mapping[str, Any]) -> List[Dict[str, Any]]:
    pending: List[Dict[str, Any]] = []
    for spec in _WORKFLOW_FIELDS[workflow_hint]:
        if not spec.get("required") or _value_present(_field_value(fields, spec["field"])):
            continue
        item = {
            "id": f"{workflow_hint}.{spec['field']}",
            "field": spec["field"],
            "label": spec["label"],
            "prompt": spec["prompt"],
            "required": True,
            "answer_type": "string",
        }
        if spec.get("options"):
            item["options"] = list(spec["options"])
        pending.append(item)
    return pending


def _explicit_outcome(question: str) -> Optional[str]:
    patterns = (
        (r"\bOS\b|overall survival|总生存", "OS"),
        (r"\bPFS\b|progression[- ]free survival|无进展生存", "PFS"),
        (r"\bORR\b|objective response rate|客观缓解率", "ORR"),
        (r"\bDFS\b|disease[- ]free survival|无病生存", "DFS"),
        (r"\bAUC\b|area under the curve|曲线下面积", "AUC"),
        (r"toxicity|adverse event|安全性|毒性|不良反应", "safety/toxicity"),
    )
    for pattern, label in patterns:
        if re.search(pattern, question, re.I):
            return label
    return None


def _explicit_review_scope(question: str) -> Optional[str]:
    if re.search(r"systematic review|系统综述|系统评价", question, re.I):
        return "systematic"
    if re.search(r"scoping review|范围综述|快速综述|快速概览", question, re.I):
        return "scoping"
    return None


def _explicit_organism(question: str) -> Optional[str]:
    human = bool(re.search(r"\bhuman\b|\bpatients?\b|人类|患者|受试者", question, re.I))
    mouse = bool(re.search(r"\bmouse\b|\bmice\b|murine|小鼠", question, re.I))
    if human == mouse:
        return None
    return "human" if human else "mouse"


def _explicit_assay(question: str) -> Optional[str]:
    patterns = (
        (r"scRNA[- ]?seq|single[- ]cell RNA|单细胞转录组", "scRNA-seq"),
        (r"scATAC[- ]?seq|single[- ]cell ATAC|单细胞染色质", "scATAC-seq"),
        (r"spatial transcript|空间转录组|\bVisium\b|\bXenium\b|\bCosMx\b", "spatial transcriptomics"),
        (r"bulk RNA[- ]?seq|bulk 转录组", "bulk RNA-seq"),
    )
    for pattern, label in patterns:
        if re.search(pattern, question, re.I):
            return label
    return None


def _explicit_input_data(question: str) -> Optional[str]:
    patterns = (
        (r"\b[A-Za-z0-9_.-]+\.h5ad\b", None),
        (r"\bh5ad\b|\bAnnData\b", "h5ad/AnnData"),
        (r"\b10x\b", "10x"),
        (r"\bFASTQ\b", "FASTQ"),
        (r"count matrix|counts matrix|计数矩阵", "count matrix"),
        (r"Seurat object|Seurat 对象", "Seurat object"),
    )
    for pattern, label in patterns:
        match = re.search(pattern, question, re.I)
        if match:
            return label or match.group(0)
    return None


def _explicit_model_system(question: str) -> Optional[str]:
    patterns = (
        (r"patient[- ]derived organoid|患者来源类器官", "patient-derived organoid"),
        (r"\borganoid\b|类器官", "organoid"),
        (r"\bcell line\b|细胞系", "cell line"),
        (r"\bmouse\b|\bmice\b|murine|小鼠", "mouse"),
        (r"\bpatients?\b|受试者|患者队列", "human participants"),
    )
    for pattern, label in patterns:
        if re.search(pattern, question, re.I):
            return label
    return None


def _resolve_workflow_hint(explicit: Optional[str], archetype: str, recommended_skill: str) -> str:
    if explicit:
        if explicit not in WORKFLOW_HINTS:
            raise ValueError(f"unsupported workflow_hint: {explicit!r}")
        return explicit
    if archetype in {"target-validation", "drug-repurposing"} or recommended_skill == "target-discovery":
        return "crossmodal-discovery"
    return "lit-review"


def _prefill_workflow_fields(
    workflow_hint: str,
    question: str,
    diseases: List[Dict[str, Any]],
    genes: List[Dict[str, Any]],
    drugs: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    fields: Dict[str, Dict[str, Any]] = {}

    def put(field: str, value: Any, via: str) -> None:
        if _value_present(value):
            fields[field] = {"value": value, "via": via}

    disease = diseases[0]["name"] if diseases else None
    molecule = genes[0]["symbol"] if genes else None
    drug = drugs[0]["name"] if drugs else None

    if workflow_hint == "lit-review":
        put("research_question", question, "raw-question")
        put("population_or_condition", disease, "detected-disease")
        put("intervention_or_exposure", drug or molecule, "detected-drug" if drug else "detected-gene")
        put("primary_outcome", _explicit_outcome(question), "explicit-outcome")
        put("review_scope", _explicit_review_scope(question), "explicit-review-scope")
    elif workflow_hint == "omics-code":
        put("analysis_goal", question, "raw-question")
        put("input_data", _explicit_input_data(question), "explicit-input-format")
        put("organism", _explicit_organism(question), "explicit-organism")
        put("assay", _explicit_assay(question), "explicit-assay")
    elif workflow_hint == "experimental-design":
        # A research question is not silently promoted to a directional,
        # falsifiable hypothesis.  Keep it as context and ask for the actual
        # hypothesis unless the user supplies it through finalization.
        put("research_question", question, "raw-question")
        put("model_system", _explicit_model_system(question), "explicit-model-system")
        put("perturbation", drug, "detected-drug")
        put("primary_endpoint", _explicit_outcome(question), "explicit-outcome")
    elif workflow_hint == "crossmodal-discovery":
        put("disease", disease, "detected-disease")
        if re.search(r"unmet (?:clinical )?need|未满足(?:临床|生物学)?需求", question, re.I):
            put("unmet_need", question, "explicit-unmet-need")
        if genes:
            put("seed_targets", [row["symbol"] for row in genes], "detected-genes")
    return fields


def _normalize_answer(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return [" ".join(item.split()) if isinstance(item, str) else item for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("answers must be strings, arrays, numbers, booleans, or null")


def _mention_order(question: str, row: Mapping[str, Any], value_keys: tuple[str, ...]) -> tuple[int, str]:
    """Keep multi-entity output deterministic while preferring user mention order.

    Some legacy lexicons are sets, so their iteration order varies with
    PYTHONHASHSEED.  A content-addressed brief must not inherit that variance.
    """

    q = question.casefold()
    candidates = [str(row.get(key) or "") for key in value_keys]
    via = str(row.get("via") or "")
    if via.startswith("zh:"):
        candidates.append(via[3:])
    positions = [q.find(value.casefold()) for value in candidates if value]
    positions = [position for position in positions if position >= 0]
    canonical = next((str(row.get(key) or "") for key in value_keys if row.get(key)), "")
    return (min(positions) if positions else len(q) + 1, canonical.casefold())


def _pick_research_object(genes, drugs, diseases) -> Dict[str, Any]:
    """研究对象 = 最主要的分子/药物 + 疾病组合。"""
    obj: Dict[str, Any] = {}
    if genes:
        obj["molecule"] = genes[0]["symbol"]
        obj["molecule_type"] = "gene/protein"
        obj["molecule_confidence"] = genes[0]["confidence"]
    elif drugs:
        obj["molecule"] = drugs[0]["name"]
        obj["molecule_type"] = "drug/compound"
        obj["molecule_confidence"] = drugs[0]["confidence"]
    if diseases:
        obj["disease"] = diseases[0]["name"]
    return obj


def _suggest_exclusions(diseases, archetype) -> List[str]:
    out = ["物种：默认仅人类证据下结论；临床前（动物/体外）证据须显式标注、不外推",
           "语言：若只纳英文文献，中文/日文文献可能漏检——需显式声明"]
    areas = {d.get("area") for d in diseases if d.get("area")}
    if "oncology" in areas:
        out.append("肿瘤类型：区分组织学亚型/分子分型，避免把泛癌结论套到单一癌种")
        out.append("线数/分期：区分一线 vs 后线、早期 vs 转移，疗效不可跨线数外推")
    if archetype in ("efficacy-comparison", "safety"):
        out.append("研究设计：优先 RCT / 前瞻队列；回顾性/单臂研究降级或单列")
    if archetype == "target-validation":
        out.append("证据来源：把 text-mining co-mention 与功能实验/临床关联分开计权")
    return out


@server.tool(
    "compile_research_question",
    "Compile a vague biomedical question (e.g. 'does EGFR still have new target value in GBM?') "
    "into a STRUCTURED research task: research object, disease, molecule, intervention, endpoints, "
    "databases, exclusion criteria, evidence bar, recommended toolchain, and which workflow skill "
    "to enter. Deterministic & auditable — every field records how it was derived; unresolved "
    "fields are flagged needs_user_input rather than guessed. Run this FIRST on any open-ended "
    "research question before searching.",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The raw, possibly vague research question"},
            "language": {"type": "string", "enum": ["zh", "en"], "default": "zh"},
            "workflow_hint": {
                "type": "string",
                "enum": list(WORKFLOW_HINTS),
                "description": "Optional workbench trajectory. It selects a field contract, never a host pack list.",
            },
        },
        "required": ["question"],
    },
)
def compile_research_question(
    question: str,
    language: str = "zh",
    workflow_hint: Optional[str] = None,
):
    q = " ".join(str(question or "").split())
    if not q:
        raise ValueError("question is required")
    if len(q) > 5000:
        raise ValueError("question must be <= 5000 characters")
    genes = lex.detect_genes(q)
    drugs = lex.detect_drugs(q)
    diseases = lex.detect_diseases(q)
    genes.sort(key=lambda row: _mention_order(q, row, ("symbol",)))
    drugs.sort(key=lambda row: _mention_order(q, row, ("name",)))
    diseases.sort(key=lambda row: _mention_order(q, row, ("raw", "name")))
    archetype, arch_hits = lex.detect_archetype(q)
    route = lex.ROUTES.get(archetype, lex.ROUTES["unknown"])
    resolved_workflow = _resolve_workflow_hint(workflow_hint, archetype, route["skill"])

    research_object = _pick_research_object(genes, drugs, diseases)

    # 干预：优先用户提到的药物；否则按原型给"待定"提示
    if drugs:
        intervention = {"value": drugs[0]["name"], "via": "detected-drug"}
    elif archetype in ("target-validation", "drug-repurposing"):
        intervention = {"value": None,
                        "needs_user_input": "未指定干预方式（抑制剂 / 抗体 / ADC / 降解剂 / 细胞疗法？）——"
                                            "靶点价值高度依赖成药方式，需先明确"}
    else:
        intervention = {"value": None, "needs_user_input": "问题未含明确干预，请补充"}

    # 缺口检查
    gaps: List[str] = []
    if not diseases:
        gaps.append("未识别到疾病 —— 请提供规范疾病名（或让 disambiguate 归一）")
    if not genes and not drugs:
        gaps.append("未识别到分子/药物 —— 请确认研究对象")
    if archetype == "unknown":
        gaps.append("问题原型未识别（靶点验证/老药新用/标志物/机制/疗效/流行病学/安全性）—— 请澄清目标")
    if any(g.get("confidence") == "candidate" for g in genes):
        gaps.append("部分基因符号仅按形状猜测，建议用 disambiguate 确认")

    compiled = {
        "schema": BRIEF_SCHEMA,
        "schema_version": BRIEF_SCHEMA_VERSION,
        "revision": 1,
        "raw_question": q,
        "language": language,
        "archetype": archetype,
        "archetype_signals": arch_hits,
        "research_object": research_object or {"needs_user_input": "研究对象不明"},
        "disease": (diseases[0] if diseases else {"needs_user_input": "疾病不明"}),
        "all_diseases": diseases,
        "molecules": genes,
        "drugs": drugs,
        "intervention": intervention,
        "endpoints": route["endpoints"],
        "databases": route["databases"],
        "exclusion_criteria": _suggest_exclusions(diseases, archetype),
        "evidence_bar": route["evidence_bar"],
        "recommended_toolchain": [{"tool": t, "purpose": p} for t, p in route["toolchain"]],
        "recommended_skill": route["skill"],
        "gaps": gaps,
        "note": "这是任务书草案：请先与用户确认 gaps 与 intervention，再按 recommended_toolchain 执行；"
                "结论阶段必须走 evidence_graph + uncertainty_ledger。",
    }
    workflow_fields = _prefill_workflow_fields(
        resolved_workflow,
        q,
        diseases,
        genes,
        drugs,
    )
    clarifications = _clarifications(resolved_workflow, workflow_fields)
    compiled.update({
        "workflow_hint": resolved_workflow,
        "required_fields": _required_fields(resolved_workflow),
        "workflow_fields": workflow_fields,
        "clarifications": clarifications,
        "status": "needs_clarification" if clarifications else "ready",
        "answer_audit": [],
    })
    return _seal_new_brief(compiled)


@server.tool(
    "finalize_research_brief",
    "Apply explicit user answers to a versioned research-brief draft. The draft hash is verified, "
    "only fields declared by the selected workflow contract are accepted, every applied answer is "
    "hashed for audit, and status remains needs_clarification until all required fields are present. "
    "This tool never returns or accepts host pack lists.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "draft": {"type": "object", "description": "A brief returned by compile_research_question or this tool."},
            "answers": {
                "type": "object",
                "description": "Mapping of clarification field (or workflow.field id) to the user's explicit answer.",
            },
        },
        "required": ["draft", "answers"],
    },
)
def finalize_research_brief(draft: Dict[str, Any], answers: Dict[str, Any]):
    if not isinstance(draft, Mapping):
        raise TypeError("draft must be an object")
    if not isinstance(answers, Mapping):
        raise TypeError("answers must be an object")
    _verify_brief(draft)

    # JSON round-trip gives us a detached, JSON-only copy and fails closed for
    # values that could not have crossed MCP faithfully.
    updated: Dict[str, Any] = json.loads(_stable_json(draft))
    workflow = str(updated["workflow_hint"])
    field_specs = {spec["field"]: spec for spec in _WORKFLOW_FIELDS[workflow]}
    allowed_fields = set(field_specs)
    fields = updated.get("workflow_fields")
    if not isinstance(fields, dict):
        fields = {}
        updated["workflow_fields"] = fields

    next_revision = int(updated.get("revision") or 1) + 1
    applied: List[Dict[str, Any]] = []
    for raw_key, raw_value in answers.items():
        key = str(raw_key)
        prefix = f"{workflow}."
        field = key[len(prefix):] if key.startswith(prefix) else key
        if field not in allowed_fields:
            raise ValueError(f"answer field is not declared by {workflow}: {field}")
        value = _normalize_answer(raw_value)
        options = field_specs[field].get("options") or []
        if _value_present(value) and options and value not in options:
            raise ValueError(f"answer for {field} must be one of {options}")
        answer_hash = _hash_value({"field": field, "value": value})
        if _value_present(value):
            fields[field] = {
                "value": value,
                "via": "user-answer",
                "answer_hash": answer_hash,
            }
        else:
            fields.pop(field, None)
        applied.append({
            "field": field,
            "answer_hash": answer_hash,
            "revision": next_revision,
            "source": "explicit-user-answer",
        })

    prior_audit = updated.get("answer_audit")
    if not isinstance(prior_audit, list):
        prior_audit = []
    updated["answer_audit"] = prior_audit + sorted(applied, key=lambda row: row["field"])
    updated["parent_content_hash"] = draft["content_hash"]
    updated["revision"] = next_revision
    updated["required_fields"] = _required_fields(workflow)
    updated["clarifications"] = _clarifications(workflow, fields)
    updated["status"] = "needs_clarification" if updated["clarifications"] else "ready"
    return _reseal_brief(updated)


@server.tool(
    "compiler_capabilities",
    "List what the question compiler can currently recognize (disease abbreviations, known targets, "
    "drug patterns, question archetypes). Transparency tool so the user knows coverage limits.",
    {"type": "object", "properties": {}},
)
def compiler_capabilities():
    return {
        "research_brief_schema": BRIEF_SCHEMA,
        "research_brief_schema_version": BRIEF_SCHEMA_VERSION,
        "workflow_hints": list(WORKFLOW_HINTS),
        "disease_abbreviations": sorted(lex.DISEASE_ABBR.keys()),
        "disease_zh_aliases": sorted(lex.DISEASE_ZH.keys()),
        "known_targets_count": len(lex.KNOWN_TARGETS),
        "drug_suffixes": list(lex.DRUG_SUFFIX),
        "archetypes": [a for a, _ in lex.ARCHETYPES] + ["unknown"],
        "note": "识别是启发式：疾病缩写/中文别名靠词表，基因靠形状+已知靶点集，药物靠后缀+已知药名。"
                "未覆盖的实体会标 candidate / needs_user_input，交给 disambiguate 或用户确认。",
    }


if __name__ == "__main__":
    server.run()
