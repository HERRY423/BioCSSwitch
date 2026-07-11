from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs" / "bio-compiler" / "question_compiler_server.py"


def _load_compiler():
    name = "question_compiler_brief_tests"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


qc = _load_compiler()


def _missing_fields(brief):
    return {item["field"] for item in brief["clarifications"]}


def test_egfr_gbm_keeps_legacy_fields_and_builds_auditable_crossmodal_draft():
    brief = qc.compile_research_question(
        question="EGFR 在 GBM 里还有没有新靶点价值",
        workflow_hint="crossmodal-discovery",
    )

    # Existing consumers keep their original contract.
    assert brief["archetype"] == "target-validation"
    assert brief["disease"]["name"] == "Glioblastoma"
    assert any(row["symbol"] == "EGFR" for row in brief["molecules"])
    assert brief["recommended_skill"] == "target-discovery"
    assert len(brief["recommended_toolchain"]) >= 3

    # Workbench v1 adds a deterministic, pack-free envelope.
    assert brief["schema"] == "biocsswitch/research-brief/1"
    assert brief["schema_version"] == 1
    assert brief["revision"] == 1
    assert brief["brief_id"].startswith("brief_")
    assert brief["content_hash"].startswith("sha256:")
    assert brief["workflow_hint"] == "crossmodal-discovery"
    assert brief["workflow_fields"]["disease"] == {
        "value": "Glioblastoma",
        "via": "detected-disease",
    }
    assert brief["workflow_fields"]["seed_targets"]["value"] == ["EGFR"]
    assert _missing_fields(brief) == {"unmet_need"}
    assert brief["status"] == "needs_clarification"
    assert "required_packs" not in brief
    assert "packs" not in brief


@pytest.mark.parametrize(
    ("workflow_hint", "question", "expected_required", "expected_missing"),
    [
        (
            "lit-review",
            "阿司匹林能否预防结直肠癌",
            {
                "research_question",
                "population_or_condition",
                "intervention_or_exposure",
                "primary_outcome",
                "review_scope",
            },
            {"primary_outcome", "review_scope"},
        ),
        (
            "omics-code",
            "分析我的数据",
            {"analysis_goal", "input_data", "organism", "assay"},
            {"input_data", "organism", "assay"},
        ),
        (
            "experimental-design",
            "为 STAT3 在 GBM 中的作用设计实验",
            {"hypothesis", "model_system", "perturbation", "primary_endpoint"},
            {"hypothesis", "model_system", "perturbation", "primary_endpoint"},
        ),
        (
            "crossmodal-discovery",
            "帮我找新的治疗方向",
            {"disease", "unmet_need"},
            {"disease", "unmet_need"},
        ),
    ],
)
def test_each_workflow_exposes_only_missing_required_fields(
    workflow_hint,
    question,
    expected_required,
    expected_missing,
):
    brief = qc.compile_research_question(question=question, workflow_hint=workflow_hint)

    assert set(brief["required_fields"]) == expected_required
    assert _missing_fields(brief) == expected_missing
    assert all(item["required"] is True for item in brief["clarifications"])
    assert brief["status"] == "needs_clarification"


def test_omics_prefills_only_explicit_input_organism_and_assay():
    brief = qc.compile_research_question(
        question="用 human scRNA-seq sample.h5ad 做 QC 和细胞注释",
        workflow_hint="omics-code",
    )

    fields = brief["workflow_fields"]
    assert fields["input_data"]["value"] == "sample.h5ad"
    assert fields["organism"]["value"] == "human"
    assert fields["assay"]["value"] == "scRNA-seq"
    assert fields["analysis_goal"]["via"] == "raw-question"
    assert brief["clarifications"] == []
    assert brief["status"] == "ready"


def test_finalize_blocks_until_required_answers_exist_then_returns_ready_revision():
    draft = qc.compile_research_question(
        question="EGFR 在 GBM 里还有没有新靶点价值",
        workflow_hint="crossmodal-discovery",
    )

    blocked = qc.finalize_research_brief(draft=draft, answers={})
    assert blocked["status"] == "needs_clarification"
    assert _missing_fields(blocked) == {"unmet_need"}
    assert blocked["brief_id"] == draft["brief_id"]
    assert blocked["revision"] == 2
    assert blocked["parent_content_hash"] == draft["content_hash"]
    assert blocked["content_hash"] != draft["content_hash"]

    ready = qc.finalize_research_brief(
        draft=draft,
        answers={
            "crossmodal-discovery.unmet_need": (
                "寻找能跨越血脑屏障、并改善成人 IDH-wildtype GBM 生存的干预靶点"
            )
        },
    )
    assert ready["status"] == "ready"
    assert ready["clarifications"] == []
    assert ready["brief_id"] == draft["brief_id"]
    assert ready["revision"] == 2
    field = ready["workflow_fields"]["unmet_need"]
    assert field["via"] == "user-answer"
    assert field["answer_hash"].startswith("sha256:")
    assert ready["answer_audit"] == [
        {
            "field": "unmet_need",
            "answer_hash": field["answer_hash"],
            "revision": 2,
            "source": "explicit-user-answer",
        }
    ]


def test_hashes_are_stable_and_answer_order_does_not_change_final_record():
    first = qc.compile_research_question(
        question="比较 pembrolizumab 和 nivolumab 在 NSCLC 的 OS",
        workflow_hint="lit-review",
    )
    second = qc.compile_research_question(
        question="比较 pembrolizumab 和 nivolumab 在 NSCLC 的 OS",
        workflow_hint="lit-review",
    )
    assert first["brief_id"] == second["brief_id"]
    assert first["content_hash"] == second["content_hash"]

    answers_a = {
        "review_scope": "systematic",
        "intervention_or_exposure": "pembrolizumab versus nivolumab",
    }
    answers_b = {
        "intervention_or_exposure": "pembrolizumab versus nivolumab",
        "review_scope": "systematic",
    }
    final_a = qc.finalize_research_brief(first, answers_a)
    final_b = qc.finalize_research_brief(second, answers_b)
    assert final_a["content_hash"] == final_b["content_hash"]
    assert final_a["answer_audit"] == final_b["answer_audit"]


def test_compile_hash_is_stable_across_python_hash_seeds():
    code = f"""
import importlib.util
p = {str(MODULE_PATH)!r}
s = importlib.util.spec_from_file_location('seeded_question_compiler', p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
brief = m.compile_research_question(
    'Compare pembrolizumab and nivolumab efficacy in NSCLC',
    workflow_hint='lit-review',
)
print(brief['content_hash'])
"""
    hashes = []
    for seed in ("1", "98765"):
        env = dict(os.environ)
        env.update({"PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        hashes.append(result.stdout.strip())
    assert hashes[0] == hashes[1]


def test_finalize_rejects_tampering_unknown_fields_and_pack_injection():
    draft = qc.compile_research_question(
        question="EGFR 在 GBM 里还有没有新靶点价值",
        workflow_hint="crossmodal-discovery",
    )

    tampered = copy.deepcopy(draft)
    tampered["raw_question"] = "changed after compilation"
    with pytest.raises(ValueError, match="content_hash mismatch"):
        qc.finalize_research_brief(tampered, {})

    with pytest.raises(ValueError, match="not declared"):
        qc.finalize_research_brief(draft, {"required_packs": ["anything"]})

    injected = copy.deepcopy(draft)
    injected["required_packs"] = ["anything"]
    injected["content_hash"] = qc._hash_value(qc._content_material(injected))
    with pytest.raises(ValueError, match="must not carry host pack lists"):
        qc.finalize_research_brief(injected, {})


def test_mcp_lists_finalize_tool_and_workflow_hint_enum():
    tools = qc.server._handle({"method": "tools/list"})["tools"]
    by_name = {tool["name"]: tool for tool in tools}

    assert "finalize_research_brief" in by_name
    hint = by_name["compile_research_question"]["inputSchema"]["properties"]["workflow_hint"]
    assert hint["enum"] == list(qc.WORKFLOW_HINTS)
