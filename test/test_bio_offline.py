#!/usr/bin/env python3
"""生医扩展的离线自测（零网络、零代理）。CI 默认跑这个。

覆盖 phase-5 五点里能纯离线验证的部分：
  - fixture 回放层（replay 命中 / miss raise / 脱敏）
  - bio_eval tool_executor 能加载工具、dispatch、错误处理
  - 证据 linter 默认 JSON 不含 line_text、含 line_sha256（隐私）
  - generator golden（委托 test_generators_golden.py）

用法：
    python test/test_bio_offline.py
退出码非零 = 有断言失败。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

_ROOT = Path(__file__).resolve().parents[1]
_FIX = _ROOT / "test" / "generators" / "fixtures"
sys.path.insert(0, str(_ROOT / "packs"))

_fails = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        _fails.append(name)


def test_fixtures():
    print("[fixtures]")
    from _lib import fixtures, http
    fixtures.activate(_FIX, mode="replay")
    try:
        # HGNC EGFR 已录（td_druggability fixture）
        data = http.get_json("https://rest.genenames.org/fetch/symbol/EGFR",
                             headers={"Accept": "application/json"})
        docs = ((data or {}).get("response") or {}).get("docs") or []
        check("replay 命中 HGNC EGFR", bool(docs) and docs[0].get("symbol") == "EGFR")
        # 未录的 URL → replay 模式 raise FixtureMiss
        missed = False
        try:
            http.get_json("https://rest.genenames.org/fetch/symbol/NOSUCHGENE",
                         headers={"Accept": "application/json"})
        except fixtures.FixtureMiss:
            missed = True
        check("replay 未命中 raise FixtureMiss", missed)
        st = fixtures.stats()
        check("命中计数正确", st["hits"] >= 1)
    finally:
        fixtures.deactivate()

    # 脱敏：录制时敏感键被抹
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        fixtures.activate(td, mode="record")
        try:
            key = fixtures._key("GET", "https://x.example/a",
                                {"api_key": "SECRET123", "q": "test"}, None)
            fixtures.record("GET", "https://x.example/a",
                            {"api_key": "SECRET123", "q": "test"}, None, b'{"ok":1}')
            rec = json.loads((Path(td) / f"{key}.json").read_text("utf-8"))
            blob = json.dumps(rec)
            check("fixture 不含明文 key", "SECRET123" not in blob,
                  detail="录制脱敏失败")
            check("fixture params 有 redacted 标记", "<redacted>" in blob)
        finally:
            fixtures.deactivate()


def test_mcp_helpers():
    print("[mcp helpers]")
    from _lib import fixtures
    from _lib.mcp_helpers import mcp_tool, safe_http_get, validate_json_object
    from _lib.server import MCPServer

    srv = MCPServer("helper-smoke", "0.0.1")

    @mcp_tool(
        "helper_ping",
        "Return input.",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        server=srv,
    )
    def helper_ping(q: str) -> dict:
        return {"q": q}

    check("mcp_tool registers tool", "helper_ping" in srv.tools)
    errs = validate_json_object(
        {"q": 1},
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    check("schema helper catches wrong type", any("q must be string" in e for e in errs))
    errs = validate_json_object(
        {"limit": True},
        {"type": "object", "properties": {"limit": {"type": "integer"}}},
    )
    check("schema helper rejects bool as integer", any("limit must be integer" in e for e in errs))

    fixtures.activate(_FIX, mode="replay")
    try:
        ok = safe_http_get(
            "https://rest.genenames.org/fetch/symbol/EGFR",
            headers={"Accept": "application/json"},
        )
        docs = (((ok.get("data") or {}).get("response") or {}).get("docs") or [])
        check("safe_http_get fixture hit", ok["ok"] is True and docs and docs[0].get("symbol") == "EGFR")

        miss = safe_http_get(
            "https://rest.genenames.org/fetch/symbol/NOSUCHGENE",
            headers={"Accept": "application/json"},
        )
        check(
            "safe_http_get standardizes fixture miss",
            miss["ok"] is False and miss["error_kind"] == "FixtureMiss",
        )
    finally:
        fixtures.deactivate()


def test_tool_executor():
    print("[tool_executor]")
    from _lib import fixtures
    sys.path.insert(0, str(_ROOT / "test" / "bio_eval"))
    fixtures.activate(_FIX, mode="replay")
    try:
        import tool_executor
        names = tool_executor.available_tool_names()
        check("加载了 >= 20 个工具", len(names) >= 20, detail=f"只有 {len(names)}")
        for n in ("search_articles", "search_trials", "compound_search", "evidence_verify"):
            check(f"工具名含 {n}", n in names)
        # 未知工具 → is_error，不 raise
        r = tool_executor.execute_tool("nonexistent_xyz", {})
        check("未知工具返回 is_error", r["is_error"] is True)
        # 错误参数 → is_error（不崩）
        r2 = tool_executor.execute_tool("search_articles", {"bad_param": 1})
        check("错误参数返回 is_error", r2["is_error"] is True)
    finally:
        fixtures.deactivate()


def test_linter_privacy():
    print("[linter privacy]")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        draft = Path(td) / "draft.md"
        draft.write_text("Patient John Doe MRN 12345, ref [PMID:99999999].\n", "utf-8")
        # 默认 JSON：无 line_text，有 line_sha256
        out = subprocess.run(
            [sys.executable, str(_ROOT / "packs" / "bio-audit" / "evidence_linter.py"),
             "--format", "json", str(draft)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**_env_replay()},
        )
        try:
            d = json.loads(out.stdout)
        except Exception:
            check("linter 默认 JSON 可解析", False, detail=out.stdout[:200] + out.stderr[:200])
            return
        findings = list(d.get("files", {}).values())[0] if d.get("files") else []
        check("默认 JSON 无 line_text", all("line_text" not in f for f in findings))
        check("默认 JSON 有 line_sha256", all("line_sha256" in f for f in findings) and bool(findings))
        check("默认 JSON 不泄露 MRN 原文", "12345" not in out.stdout and "John Doe" not in out.stdout)
        check("privacy note 存在", "_privacy" in d)

        # --include-line-text：显式导出
        out2 = subprocess.run(
            [sys.executable, str(_ROOT / "packs" / "bio-audit" / "evidence_linter.py"),
             "--format", "json", "--include-line-text", str(draft)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env={**_env_replay()},
        )
        d2 = json.loads(out2.stdout)
        f2 = list(d2.get("files", {}).values())[0]
        check("--include-line-text 才导出原文", any("line_text" in x for x in f2))
        check("include 模式 privacy note 含 PHI 警告", "PHI" in d2.get("_privacy", ""))


def _env_replay():
    import os
    e = dict(os.environ)
    e["CSSWITCH_HTTP_FIXTURES"] = str(_FIX)
    e["CSSWITCH_HTTP_FIXTURE_MODE"] = "replay"
    return e


def _pack_manifests():
    packs = {}
    for pj in sorted((_ROOT / "packs").glob("*/pack.json")):
        if pj.parent.name.startswith("_"):
            continue
        data = json.loads(pj.read_text("utf-8"))
        packs[data["id"]] = (pj, data)
    return packs


def test_pack_manifests():
    print("[pack manifests]")
    schema_path = _ROOT / "packs" / "pack.schema.json"
    check("pack JSON Schema exists", schema_path.is_file())
    schema = json.loads(schema_path.read_text("utf-8"))
    required = set(schema.get("required") or [])
    check("schema requires version/dependencies/requires_tools",
          {"version", "dependencies", "requires_tools"} <= required)
    packs = _pack_manifests()
    check("loaded pack manifests", len(packs) >= 10, detail=f"loaded {len(packs)}")

    for pid, (pj, data) in packs.items():
        check(f"{pid}: id matches directory", pid == pj.parent.name)
        check(f"{pid}: semver version present", bool(re.match(r"^\d+\.\d+\.\d+", data.get("version", ""))))
        check(f"{pid}: dependencies field present", isinstance(data.get("dependencies"), list))
        check(f"{pid}: requires_tools field present", isinstance(data.get("requires_tools"), list))
        if data.get("depends_on"):
            check(
                f"{pid}: dependencies matches depends_on compatibility alias",
                sorted(data.get("dependencies") or []) == sorted(data.get("depends_on") or []),
            )
        for tool in data.get("requires_tools") or []:
            check(f"{pid}: requires_tools item has name", isinstance(tool, dict) and bool(tool.get("name")))
        for srv in data.get("servers") or []:
            script = srv.get("script")
            check(f"{pid}: server name has bio- prefix", str(srv.get("name", "")).startswith("bio-"))
            check(
                f"{pid}: server script exists: {srv.get('name')}",
                bool(script) and (_ROOT / script).is_file(),
                detail=str(script),
            )
        for skill in data.get("skills") or []:
            src = skill.get("src")
            check(
                f"{pid}: skill dir exists: {skill.get('id')}",
                bool(src) and (_ROOT / src).is_dir(),
                detail=str(src),
            )
        for dep in set((data.get("dependencies") or []) + (data.get("depends_on") or [])):
            check(f"{pid}: dependency exists: {dep}", dep in packs)

    cycles = []
    visiting = []
    visited = set()

    def dfs(pid):
        if pid in visited:
            return
        if pid in visiting:
            cycles.append(" -> ".join(visiting[visiting.index(pid):] + [pid]))
            return
        visiting.append(pid)
        deps = set((packs[pid][1].get("dependencies") or []) + (packs[pid][1].get("depends_on") or []))
        for dep in deps:
            if dep in packs:
                dfs(dep)
        visiting.pop()
        visited.add(pid)

    for pid in packs:
        dfs(pid)
    check("pack dependency graph has no cycles", not cycles, detail="; ".join(cycles))
    check("bio-scfm formally depends on bio-singlecell", "bio-singlecell" in packs["bio-scfm"][1]["dependencies"])
    check("bio-singlecell declares workflow engines",
          {"Nextflow", "Snakemake"} <= {t["name"] for t in packs["bio-singlecell"][1]["requires_tools"]})


def test_release_versions():
    print("[release versions]")
    pkg = json.loads((_ROOT / "desktop" / "package.json").read_text("utf-8"))
    lock = json.loads((_ROOT / "desktop" / "package-lock.json").read_text("utf-8"))
    cargo = tomllib.loads((_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text("utf-8"))
    pkg_version = pkg["version"]
    check(
        "desktop package version matches Cargo.toml",
        pkg_version == cargo["package"]["version"],
        detail=f"package={pkg_version} cargo={cargo['package']['version']}",
    )
    lock_root = (lock.get("packages") or {}).get("") or {}
    check(
        "package-lock version matches package.json",
        lock.get("version") == pkg_version and lock_root.get("version") == pkg_version,
        detail=f"lock={lock.get('version')} root={lock_root.get('version')} package={pkg_version}",
    )
    audit = json.loads((_ROOT / "packs" / "bio-audit" / "pack.json").read_text("utf-8"))
    grade_text = (_ROOT / "packs" / "bio-audit" / "grade_server.py").read_text("utf-8")
    m = re.search(r'MCPServer\("bio-audit-grade",\s*"([^"]+)"\)', grade_text)
    check("bio-audit-grade version matches pack", bool(m) and m.group(1) == audit["version"])
    singlecell = json.loads((_ROOT / "packs" / "bio-singlecell" / "pack.json").read_text("utf-8"))
    sc_text = (_ROOT / "packs" / "bio-singlecell" / "singlecell_server.py").read_text("utf-8")
    sc_m = re.search(r'MCPServer\("bio-singlecell",\s*"([^"]+)"\)', sc_text)
    check("bio-singlecell server version matches pack", bool(sc_m) and sc_m.group(1) == singlecell["version"])


def test_generators_golden():
    print("[generators golden]  (委托 test_generators_golden.py)")
    out = subprocess.run(
        [sys.executable, str(_ROOT / "test" / "generators" / "test_generators_golden.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("golden 全部通过", out.returncode == 0,
          detail=out.stdout[-200:] + out.stderr[-200:])


def test_evidence_profile():
    """需求 1：evidence_profile 从元数据抽物种/样本量/实验类型（纯函数，离线）。"""
    print("[evidence_profile]")
    from _lib import evidence_profile as ep
    rct = ep.build_profile({"exists": True, "title": "randomized phase II trial",
                            "abstract": "We enrolled 245 patients. metastatic disease.",
                            "mesh_terms": ["Humans", "Aged"], "evidence_type": "RCT",
                            "phase": "PHASE2"})
    check("人类物种识别", rct["species"]["value"] == "human")
    check("样本量抽取 n=245", rct["sample_size"]["n"] == 245)
    check("实验类型含临床 II 期", "II" in rct["experiment"]["label"])
    animal = ep.build_profile({"exists": True, "title": "mouse xenograft model",
                               "abstract": "murine", "mesh_terms": ["Animals", "Mice"],
                               "evidence_type": "unclassified"})
    check("动物物种识别", animal["species"]["value"] == "animal")


def test_evidence_graph_offline():
    """需求 1+2：evidence_graph 检出错配/反证，uncertainty_ledger 出五段（monkeypatch 网络）。"""
    print("[evidence_graph + uncertainty_ledger]")
    import importlib.util
    p = _ROOT / "packs" / "bio-audit" / "evidence_verify_server.py"
    spec = importlib.util.spec_from_file_location("av_offline", p)
    av = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(av)
    # 注意：PMID 形状校验要求 4-9 位数字，测试的假 ID 必须用合法形状
    fake = {"10000001": {"exists": True, "title": "human RCT", "abstract": "245 patients",
                         "mesh_terms": ["Humans"], "evidence_type": "RCT"},
            "10000002": {"exists": True, "title": "mouse study", "abstract": "murine",
                         "mesh_terms": ["Animals", "Mice"], "evidence_type": "unclassified"}}
    av._verify_pmid = lambda x: fake.get(x, {"exists": False})
    av._verify_doi = lambda x: {"exists": False}
    av._verify_nct = lambda x: {"exists": False}
    g = av.evidence_graph(claims=[
        {"text": "药物 X 在人类有效", "asserted": {"species": "human"},
         "refs": [{"id_type": "pmid", "id": "10000002", "stance": "supports"}]},
        {"text": "编造引用", "refs": [{"id_type": "pmid", "id": "99999999", "stance": "supports"}]},
    ])
    check("检出物种错配", any("错配" in c and "animal" in c
                             for cl in g["claims"] for c in cl["conflicts"]))
    check("检出不存在引用", g["summary"]["unsupported"] == 1)
    led = av.uncertainty_ledger(graph_claims=g["claims"])
    for sec in ("known_knowns", "known_unknowns", "conflicts", "missing_data", "next_experiment"):
        check(f"台账含 {sec}", sec in led["ledger"])


def test_question_compiler():
    """需求 3：compile_research_question 把模糊问题编译成结构化任务（离线）。"""
    print("[question_compiler]")
    import importlib.util
    p = _ROOT / "packs" / "bio-compiler" / "question_compiler_server.py"
    spec = importlib.util.spec_from_file_location("qc_offline", p)
    qc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qc)
    r = qc.compile_research_question(question="EGFR 在 GBM 里还有没有新靶点价值")
    check("识别原型=靶点验证", r["archetype"] == "target-validation")
    check("识别疾病=Glioblastoma", r["disease"].get("name") == "Glioblastoma")
    check("识别分子=EGFR", any(m["symbol"] == "EGFR" for m in r["molecules"]))
    check("推荐 skill=target-discovery", r["recommended_skill"] == "target-discovery")
    check("给出推荐工具链", len(r["recommended_toolchain"]) >= 3)


def test_bio_eval_rubric():
    """需求 4：bio_eval 多维 rubric 自检（子进程跑 run.py --selftest）。"""
    print("[bio_eval rubric selftest]  (委托 run.py --selftest)")
    out = subprocess.run(
        [sys.executable, str(_ROOT / "test" / "bio_eval" / "run.py"), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("rubric selftest 通过", out.returncode == 0,
          detail=out.stdout[-300:] + out.stderr[-200:])


def test_gold_calibration_manifest():
    print("[gold calibration]")
    out = subprocess.run(
        [sys.executable, str(_ROOT / "test" / "bio_eval" / "gold_calibration.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check(
        "gold calibration ledger covers current gold cases",
        out.returncode == 0,
        detail=out.stdout[-300:] + out.stderr[-200:])


def _load_server(rel: str):
    import importlib.util
    p = _ROOT / "packs" / rel
    spec = importlib.util.spec_from_file_location("srv_" + rel.replace("/", "_").replace(".py", ""), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_grade_engine():
    """GRADE 引擎：设计→起始档、升降级算术、规则守卫、meta/clinical-trial 修复、EtD（离线）。"""
    print("[GRADE/SoF/EtD]")
    gr = _load_server("bio-audit/grade_server.py")
    o1 = gr.grade_outcome(outcome="mortality", design="rct", n_participants=210,
                          domains={"imprecision": {"rating": "serious", "reason": "wide CI"},
                                   "inconsistency": {"rating": "serious", "reason": "I2=78%"}})
    check("RCT 双降级 High→Low", o1["certainty"] == "Low" and o1["score"] == 2)
    o2 = gr.grade_outcome(outcome="infection", design="cohort", domains={},
                          upgrades={"large_effect": {"rating": "large", "reason": "RR 0.35"}})
    check("观察性大效应 Low→Moderate", o2["certainty"] == "Moderate")
    o3 = gr.grade_outcome(outcome="x", design="rct", domains={},
                          upgrades={"dose_response": {"rating": "present"}})
    check("RCT 误升级被拦并警告", o3["score"] == 4 and any("不可升级" in w for w in o3["warnings"]))
    sof = gr.grade_sof_table(graded_outcomes=[o1, o2])
    check("SoF 表含确定性符号", "⊕⊕⊝⊝" in sof and "⊕⊕⊕⊝" in sof)
    # 修复 1：meta/SR 不默认 High；clinical-trial 拆类型
    m1 = gr.grade_outcome(outcome="x", design="meta-analysis", domains={})
    check("meta 无 underlying → Low(不默认 High) + 警告",
          m1["score"] == 2 and any("underlying_design" in w for w in m1["warnings"]))
    m2 = gr.grade_outcome(outcome="x", design="meta-analysis", underlying_design="rct", domains={})
    check("meta of RCTs → High", m2["score"] == 4)
    ct = gr.grade_outcome(outcome="x", design="clinical-trial", domains={})
    check("模糊 clinical-trial → Low + 拆类型警告",
          ct["score"] == 2 and any("拆类型" in w or "随机" in w for w in ct["warnings"]))
    sa = gr.grade_outcome(outcome="x", design="single-arm-trial", domains={},
                          upgrades={"large_effect": {"rating": "large", "reason": "RR 0.3"}})
    check("单臂试验可升级（非 RCT）", sa["score"] == 3)
    # 提升 3：EtD
    r1 = gr.etd_recommendation(certainty="High",
                               benefit_harm_balance={"rating": "favors_intervention", "reason": "净获益"},
                               values_preferences={"rating": "no_important_variability", "reason": "-"},
                               resources={"rating": "negligible", "reason": "低成本"})
    check("EtD 高确定性+净获益 → strong", r1["strength"] == "strong" and r1["direction"] == "for")
    r2 = gr.etd_recommendation(certainty="Low",
                               benefit_harm_balance={"rating": "favors_intervention", "reason": "可能获益"})
    check("EtD 低确定性强推荐被降级/警告",
          r2["strength"] == "conditional" or any("discordant" in w or "不一致" in w for w in r2["warnings"]))
    body = {
        "studies": [
            {"study_id": "S1", "design": "rct", "n_participants": 100,
             "risk_of_bias": {"overall": "low", "domains": {"randomization_process": "low"}}},
            {"study_id": "S2", "design": "rct", "n_participants": 300,
             "risk_of_bias": {"overall": "high", "domains": {"missing_outcome_data": "high"}}},
        ]
    }
    rob = gr.grade_outcome(outcome="mortality", design="rct", domains={}, evidence_body=body)
    check("evidence_body 自动聚合 study-level RoB",
          rob["downgrade_detail"][0]["rating"] == "very_serious"
          and rob["evidence_body_model"]["risk_of_bias"]["evidence"]["weighted_mean"] >= 1.5)
    dossier = gr.grade_evidence_dossier(dossier={
        "question": "Intervention X vs usual care",
        "studies": body["studies"],
        "outcomes": [
            {"id": "mortality", "outcome": "Mortality", "criticality": "critical",
             "domains": {"imprecision": {"rating": "serious", "reason": "wide CI"}},
             "effect": {"measure": "RR", "value": 0.82, "ci_low": 0.51, "ci_high": 1.2}},
            {"id": "infection", "outcome": "Infection", "criticality": "important", "domains": {}},
        ],
    })
    check("GRADE dossier models shared body of evidence",
          dossier["schema"] == "bio-audit/grade-evidence-dossier/1"
          and dossier["body_of_evidence"]["n_studies"] == 2
          and len(dossier["graded_outcomes"]) == 2)
    check("GRADE dossier emits SoF markdown", "Mortality" in dossier["sof_markdown"] and "⊕" in dossier["sof_markdown"])
    pr = gr.etd_probabilistic_recommendation(
        certainty={"High": 0.2, "Moderate": 0.6, "Low": 0.2},
        benefit_harm_balance={"probabilities": {"favors_intervention": 0.8, "balanced": 0.2}},
        values_preferences={"probabilities": {"no_important_variability": 0.7, "important_variability": 0.3}},
        resources={"rating": "negligible"},
    )
    check("probabilistic EtD returns posterior probabilities",
          pr["posterior"]["direction"]["for"] >= 0.79 and "top_joint_states" in pr)


def test_critique_engine():
    """bio-critique: extrapolation, methodology, believability, report, and text entry (offline)."""
    print("[bio-critique]")
    cr = _load_server("bio-critique/critique_server.py")
    expected_tools = {
        "critique_conclusion",
        "critique_methodology",
        "believability_score",
        "find_conflicting_evidence",
        "check_retraction_status",
        "critique_full_report",
        "design_counter_experiment",
        "critique_text",
    }
    check("critique server registers all 8 planned tools", expected_tools <= set(cr.server.tools))
    check(
        "critique Phase 1 core tools registered",
        {"critique_conclusion", "critique_methodology", "believability_score"} <= set(cr.server.tools),
    )
    check(
        "critique Phase 2 conflict tools registered",
        {"find_conflicting_evidence", "check_retraction_status"} <= set(cr.server.tools),
    )
    claim = {
        "claim": "Drug X improves overall survival in patients.",
        "asserted": {"species": "human", "endpoint": "hard-clinical"},
        "applicability_boundary": {"species": ["animal"], "endpoint": "surrogate", "max_sample_size": 24},
        "evidence_level": "animal",
        "verdict": "contested",
        "conflicts": ["animal evidence only"],
        "counter_evidence": [],
    }
    cc = cr.critique_conclusion(claim_report=claim)
    ex_ids = {x["rule_id"] for x in cc["extrapolations"]}
    check("EX-01 animal→human 外推检出", "EX-01" in ex_ids)
    check("critique_conclusion 给出关注级别", cc["overall_concern_level"] in {"yellow", "orange", "red"})
    check("metadata flag 检出 n<30", any(f["check_id"] == "METH-03" for f in cc["methodology_flags"]))

    meth = cr.critique_methodology(
        judgments=[{"check_id": "METH-10", "finding": "critical"}],
        metadata={"sample_size": 24},
    )
    check("METH-03 n<30 自动检测", any(x["check_id"] == "METH-03" for x in meth["auto_detected"]))
    check("critical 无 reason 有警告", any("METH-10" in w and "requires a reason" in w for w in meth["warnings"]))

    hi = cr.believability_score(
        evidence_level="RCT",
        verdict="supported",
        critique={"extrapolations": [], "methodology_flags": []},
        methodology={"quality_score": 100},
    )
    check("可信度满分场景为五星", hi["score"] == 5)
    low = cr.believability_score(claim_report=claim, critique=cc)
    check("仅动物证据 + 人体结论降到两星以下", low["score"] <= 2)

    exp = cr.design_counter_experiment(
        claim_text=claim["claim"],
        extrapolations=[{"rule_id": "EX-01"}],
        boundary=claim["applicability_boundary"],
    )
    check("EX-01 反证实验推荐人体 PK/PD", "PK/PD" in exp["design_type"] or "PK/PD" in exp["minimum_sample_size"])

    rct_exp = cr.design_counter_experiment(
        claim_text="Drug X improves overall survival.",
        extrapolations=[{"rule_id": "EX-05"}],
    )
    check("EX-05 反证实验推荐硬终点 RCT", "随机" in rct_exp["design_type"] or "randomized" in rct_exp["design_type"].lower())

    old_esearch, old_esummary = cr.entrez.esearch, cr.entrez.esummary
    old_efetch, old_parse = cr.entrez.efetch_text, cr.entrez.parse_pubmed_xml
    try:
        cr.entrez.esearch = lambda db, query, retmax=10, sort=None: {
            "ids": ["12345678"],
            "query_translation": query,
        }
        cr.entrez.esummary = lambda db, ids: {
            "12345678": {
                "title": "No significant survival effect of EGFR inhibition",
                "fulljournalname": "Journal of Negative Results",
                "pubdate": "2025 Jan",
            }
        }
        conflicts = cr.find_conflicting_evidence(
            claim_text="EGFR inhibition improves survival.",
            key_entities=["EGFR"],
            current_refs=[{"id_type": "pmid", "id": "11111111"}],
        )
        check("find_conflicting_evidence returns retrieved PMID only", conflicts["potential_conflicts"][0]["pmid"] == "12345678")
        check("find_conflicting_evidence records query strategy", "EGFR" in conflicts["search_strategy"]["query"])

        cr.entrez.efetch_text = lambda db, ids, rettype="abstract", retmode="xml": "<xml/>"
        cr.entrez.parse_pubmed_xml = lambda xml: [{
            "pmid": "12345678",
            "publication_types": ["Retracted Publication"],
            "title": "Retracted test record",
        }]
        retract = cr.check_retraction_status(["12345678"])
        check("check_retraction_status detects retracted publication", retract["results"][0]["status"] == "retracted")
    finally:
        cr.entrez.esearch, cr.entrez.esummary = old_esearch, old_esummary
        cr.entrez.efetch_text, cr.entrez.parse_pubmed_xml = old_efetch, old_parse

    report = cr.critique_full_report({"claims": [claim]})
    check("批判报告包含外推", "外推" in report["markdown"])

    text_report = cr.critique_text(
        text="Mouse xenograft models show Drug X improves survival in patients."
    )
    text_rules = {
        e["rule_id"]
        for c in text_report["claims"]
        for e in c.get("extrapolations", [])
    }
    check("critique_text 至少拆出 1 条 claim", text_report["claims_extracted"] >= 1)
    check("critique_text 检出 EX-01", "EX-01" in text_rules)


def test_scfm_provenance():
    """scFM 适配层：指纹稳定 + provenance 记录含哈希 + 篡改可检出（离线）。"""
    print("[bio-scfm provenance]")
    sc = _load_server("bio-singlecell/singlecell_server.py")
    fm = _load_server("bio-scfm/scfm_server.py")
    d1 = {"n_obs": 5000, "n_var": 2000, "var_id_type": "ensembl", "obs_keys": ["a", "b"]}
    d2 = {"n_var": 2000, "n_obs": 5000, "var_id_type": "ensembl", "obs_keys": ["b", "a"]}
    check("指纹与键序无关", sc.anndata_fingerprint(descriptor=d1)["fingerprint"]
          == sc.anndata_fingerprint(descriptor=d2)["fingerprint"])
    recipe = sc.sc_preprocess_recipe(target_model="geneformer")
    check("Geneformer 配方跳过 log/HVG",
          not any(s["op"] in ("log1p", "highly_variable_genes") for s in recipe["params"]["steps"]))
    rec = fm.scfm_provenance_record(
        model={"name": "geneformer", "checkpoint": "gf-12L-30M-i2048", "version": "0.1"},
        input={"anndata_sha256": "sha256:abc", "var_id_type": "ensembl"},
        preprocessing={"recipe_hash": recipe["recipe_hash"]},
        embedding={"output_layer": "X_geneformer", "n_dims": 512, "output_sha256": "sha256:def"},
        run={"seed": 0, "created_at": "2026-07-05T00:00:00Z"})
    check("provenance 记录完整", rec["complete"] and rec["missing_fields"] == [])
    check("verify 通过", fm.scfm_provenance_verify(record=rec["record"])["verdict"] == "trustworthy")
    bad = dict(rec["record"]); bad["embedding"] = dict(bad["embedding"]); bad["embedding"]["n_dims"] = 999
    check("篡改被检出", fm.scfm_provenance_verify(record=bad)["hash_match"] is False)
    # 修复 2：embed_plan 明确是 skeleton，不可误称 runnable
    plan = fm.scfm_embed_plan(model="geneformer", anndata_sha256="a", preprocessing_hash="b")
    check("embed_plan runnable=False", plan.get("runnable") is False and plan.get("artifact_type") == "skeleton")
    check("脚本含 NOT-RUNNABLE 护栏",
          "NOT RUNNABLE" in plan["script"] and "raise SystemExit" in plan["script"])
    # 提升 1：模型矩阵 —— foundation + domain baseline 都在
    mx = fm.scfm_model_matrix()
    fnd = set(mx["foundation_models"]); base = set(mx["domain_baselines"])
    check("含 4 个 foundation model", {"geneformer", "scgpt", "cellfm", "uce"} <= fnd)
    check("保留 scVI/totalVI/MultiVI baseline", {"scvi", "totalvi", "multivi"} <= base)


def test_singlecell_recipe_expansion():
    """单细胞 Phase 1/4：doublet、batch、gene ID、cell type、多模态、空间配方（离线）。"""
    print("[bio-singlecell expanded recipes]")
    sc = _load_server("bio-singlecell/singlecell_server.py")
    dbl = sc.sc_doublet_recipe(n_obs=5000)
    check("doublet recipe 有稳定 hash", dbl["recipe_hash"].startswith("sha256:"))
    check("doublet 默认 Scrublet 脚本含 histogram", "doublet_score" in dbl["script"] and "hist" in dbl["script"])
    prep = sc.sc_preprocess_recipe(target_model="generic")
    check("preprocess seurat_v3 HVG 使用 counts layer", "flavor='seurat_v3', layer='counts'" in prep["script"])
    bat = sc.sc_batch_recipe(n_batches=3, batch_key="batch")
    check("≤3 batch 推荐 Harmony", bat["recommended_method"] == "harmony")
    bat2 = sc.sc_batch_recipe(n_batches=6, method="scvi")
    check("scVI 警告 raw counts", any("raw counts" in w for w in bat2["warnings"]))
    check("scVI 脚本不静默把 X 当 counts", "requires raw counts" in bat2["script"] and "adata.X.copy()" not in bat2["script"])
    gid = sc.sc_geneid_convert(source_id_type="symbol", target_id_type="ensembl", organism="human")
    check("geneid_convert 警告多对多映射", any("多对一" in p or "多对多" in p for p in gid["pitfalls"]))
    ann = sc.sc_celltype_recipe(method="celltypist", tissue="PBMC", organism="human")
    check("celltype 按 tissue 推荐 CellTypist immune model", "Immune" in ann["recommended_reference"])
    mm = sc.sc_multimodal_recipe(modality="cite_seq")
    check("CITE-seq 配方含 CLR/DSB/totalVI", all(x in " ".join(mm["notes"]) + mm["script"] for x in ("CLR", "DSB", "totalVI")))
    check("CITE-seq HVG 使用 RNA counts layer", 'layer="counts"' in mm["script"])
    sp = sc.sc_spatial_recipe(platform="visium")
    check("spatial 配方含 squidpy spatial neighbors", "spatial_neighbors" in sp["script"])
    check("spatial 先生成 leiden 再 neighborhood enrichment", "sc.tl.leiden" in sp["script"] and 'cluster_key="leiden"' in sp["script"])
    wf = sc.sc_workflow_recipe(engine="snakemake", batch_method="scvi", annotation_method="celltypist")
    check("workflow artifact is explicitly runnable",
          wf["artifact_type"] == "runnable_workflow" and wf["runnable"] is True and wf["dry_run_command"])
    check("Snakemake workflow package includes Snakefile and conda envs",
          "Snakefile" in wf["files"] and "envs/scvi.yaml" in wf["files"])
    check("Snakemake workflow wires scanpy/scrublet/scVI/celltypist",
          all(x in wf["files"]["Snakefile"] for x in ("qc_scanpy", "doublet_scrublet", "batch_integrate", "annotate_celltypist")))
    check("Snakemake workflow has validation/report/provenance steps",
          all(x in wf["files"]["Snakefile"] for x in ("validate_input", "qc_report", "write_provenance.py"))
          and {"scripts/validate_input.py", "scripts/qc_report.py", "scripts/write_provenance.py"} <= set(wf["files"]))
    check("workflow declares actual single-cell toolchain",
          {"scanpy", "scrublet", "scvi-tools", "celltypist"} <= {t["name"] for t in wf["toolchain"]})
    wf_no_scfm = sc.sc_workflow_recipe(engine="snakemake", include_scfm=False, batch_method="harmony", annotation_method="singler")
    check("Snakemake respects include_scfm=false",
          "rule scfm_embedding" not in wf_no_scfm["files"]["Snakefile"]
          and not wf_no_scfm["configuration_required"])
    check("Snakemake can switch to SingleR and Harmony",
          "rule annotate_singler" in wf_no_scfm["files"]["Snakefile"]
          and "rule annotate_celltypist" not in wf_no_scfm["files"]["Snakefile"]
          and "envs/harmony.yaml" in wf_no_scfm["files"])
    for script_name in (
        "scripts/validate_input.py",
        "scripts/qc_scanpy.py",
        "scripts/doublet_scrublet.py",
        "scripts/integrate_scvi.py",
        "scripts/integrate_harmony.py",
        "scripts/annotate_celltypist.py",
        "scripts/scfm_embed_adapter.py",
        "scripts/qc_report.py",
        "scripts/write_provenance.py",
    ):
        try:
            compile(wf["files"][script_name], script_name, "exec")
            ok = True
        except SyntaxError:
            ok = False
        check(f"generated Python script compiles: {script_name}", ok)
    nf = sc.sc_workflow_recipe(engine="nextflow", batch_method="harmony", include_scfm=True)
    check("Nextflow workflow package includes main.nf",
          "main.nf" in nf["files"] and "process QC_SCANPY" in nf["files"]["main.nf"])
    check("Nextflow workflow has publishable report/provenance/scFM steps",
          all(x in nf["files"]["main.nf"] for x in ("publishDir params.outdir", "process QC_REPORT", "process WRITE_PROVENANCE", "process SCFM_EMBED")))
    check("Nextflow workflow exposes nf-core/scrnaseq handoff",
          nf["nf_core_fastq_handoff"]["pipeline"] == "nf-core/scrnaseq"
          and "nextflow run nf-core/scrnaseq" in nf["nf_core_fastq_handoff"]["command"])
    nf_singler = sc.sc_workflow_recipe(engine="nextflow", annotation_method="singler", include_scfm=False)
    check("Nextflow can switch to SingleR without scFM",
          "process ANNOTATE_SINGLER" in nf_singler["files"]["main.nf"]
          and "process ANNOTATE_CELLTYPIST" not in nf_singler["files"]["main.nf"]
          and "process SCFM_EMBED" not in nf_singler["files"]["main.nf"])


def test_sc_downstream_recipes():
    """单细胞 Phase 2：下游分析 pack 工具结构与关键约束（离线）。"""
    print("[bio-sc-downstream recipes]")
    ds = _load_server("bio-sc-downstream/sc_downstream_server.py")
    deg = ds.sc_deg_recipe(method="auto", replicates_per_condition=3)
    check("DEG auto 推荐 pseudobulk", deg["recommended_method"] == "pseudobulk_deseq2")
    check("pseudobulk 脚本含 DESeq2/apeglm", "DESeq2" in deg["script"] and "apeglm" in deg["script"])
    check("pseudobulk 脚本逐 cell type 聚合且无未定义占位变量",
          "for (ct in sort(unique(pb_meta$celltype)))" in deg["script"] and "counts_for_one_celltype" not in deg["script"])
    traj = ds.sc_trajectory_recipe(method="scvelo", has_spliced_unspliced=False)
    check("scVelo 缺 spliced/unspliced 有警告", any("spliced/unspliced" in w for w in traj["warnings"]))
    paga = ds.sc_trajectory_recipe(method="paga")
    check("PAGA 缺邻居图时自动补 neighbors", "if \"neighbors\" not in adata.uns" in paga["script"] and "sc.pp.neighbors" in paga["script"])
    comm = ds.sc_communication_recipe(method="liana")
    check("communication 输出可视化说明", "bubble plot" in comm["visualizations"])
    marker = ds.sc_marker_recipe()
    check("marker 配方含 HGNC handoff", "HGNC" in marker["bio_gene_handoff"])
    enr = ds.sc_enrichment_recipe(method="decoupler")
    check("enrichment 配方含 gene set scoring 边界", any("gene set scoring" in s for s in enr["single_cell_specifics"]))


def test_scfm_phase3_tools():
    """scFM Phase 3：fine-tuning skeleton、quality metrics、CellFM/UCE prep、benchmark gate（离线）。"""
    print("[bio-scfm phase3]")
    fm = _load_server("bio-scfm/scfm_server.py")
    ft = fm.scfm_finetune_plan(model="geneformer", label_key="cell_type")
    check("fine-tuning skeleton runnable=False", ft["runnable"] is False and ft["artifact_type"] == "skeleton")
    check("fine-tuning 脚本含 SystemExit 护栏", "SystemExit" in ft["script"])
    q = fm.scfm_embed_quality(scenario="comprehensive")
    check("embed quality 指标完整", {"kBET", "iLISI", "cLISI", "ARI"} <= set(q["metrics"]))
    ext = fm.scfm_preprocess_recipe_ext(model="uce", input_id_type="ensembl")
    check("UCE 预处理含 protein/ESM2", "ESM2" in ext["script"] and "protein" in ext["script"])
    bp = fm.scfm_benchmark_plan(
        task="rare_cell_detection",
        models=["geneformer", "scvi"],
        rare_population="KRT17+ epithelial state",
        target_metric_value=0.30,
        alpha_grid=[0.25, 0.5],
    )
    check("benchmark plan 含每模型 subagent 边界",
          bp["subagent_boundaries"]["one_subagent_per_model"] is True and len(bp["model_jobs"]) == 2)
    check("benchmark plan 含 SubagentStop 验证 hook",
          any(h["event"] == "SubagentStop" and h["tool"] == "scfm_benchmark_verify" for h in bp["hooks"]))
    good = {
        "schema": "bio-scfm/benchmark-result/1",
        "task": "rare_cell_detection",
        "model": "geneformer",
        "dataset": {
            "anndata_sha256": "sha256:data",
            "ground_truth_hash": "sha256:labels",
            "label_source": "gmm_marker_threshold",
            "split_hash": "sha256:split",
            "split_strategy": "donor_stratified",
            "positive_label": "KRT17_positive",
        },
        "run": {"seed": 0, "alpha": 0.5},
        "metrics": {"auprc": {"value": 0.42, "ci_low": 0.34, "ci_high": 0.49, "n_bootstraps": 1000}},
        "baseline": {"name": "scvi", "metrics": {"auprc": {"value": 0.31}}},
        "audits": {"no_train_test_leakage": True, "class_balance_report": {"positive_fraction": 0.03},
                   "ground_truth_threshold_blinded": True},
        "provenance": {"embedding_provenance_hash": "sha256:embedding"},
    }
    gate = fm.scfm_benchmark_verify(result=good, target_metric_value=0.30)
    check("benchmark verifier 合格结果 pass",
          gate["hook_decision"] == "pass" and gate["goal_met"] is True and not gate["failures"])
    bad = json.loads(json.dumps(good))
    bad["audits"]["no_train_test_leakage"] = False
    bad["metrics"]["auprc"]["ci_low"] = 0.18
    gate2 = fm.scfm_benchmark_verify(result=bad, target_metric_value=0.30, attempts_used=1, max_attempts=3)
    check("benchmark verifier 无效/未达标结果 retry",
          gate2["hook_decision"] == "retry" and any("leakage" in f.lower() for f in gate2["failures"]))


def test_sc_atlas_and_scanpy_generator():
    """CELLxGENE 轻量 pack + scanpy 生成器（离线）。"""
    print("[bio-sc-atlas + sc_scanpy_pipeline]")
    atlas = _load_server("bio-sc-atlas/atlas_server.py")
    srch = atlas.cellxgene_search(tissue="lung", organism="Homo sapiens")
    check("cellxgene_search 返回 query_hash", srch["query_hash"].startswith("sha256:"))
    dl = atlas.cellxgene_download_recipe(dataset_id="fake-dataset")
    check("cellxgene download 是 skeleton 且不可运行", dl["runnable"] is False and "SystemExit" in dl["script"])
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out_script = Path(td) / "scanpy_pipeline.py"
        out = subprocess.run(
            [sys.executable, str(_ROOT / "packs" / "bio-workflows" / "generators" / "sc_scanpy_pipeline.py"),
             "--h5ad", "input.h5ad", "--organism", "human", "--tissue", "blood",
             "--analysis-goals", "clustering", "marker", "--out", str(out_script),
             "--include-doublet", "--batch-key", "batch"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        check("sc_scanpy_pipeline generator 可生成脚本", out.returncode == 0 and out_script.is_file(),
              detail=out.stdout[-200:] + out.stderr[-200:])
        script = out_script.read_text("utf-8") if out_script.is_file() else ""
        check("生成脚本含 provenance / Leiden / marker", all(x in script for x in ("csswitch_scanpy_pipeline", "leiden", "rank_genes_groups")))
        check("生成脚本保留 full-gene raw 并用 counts 做 HVG",
              "adata.raw = adata.copy()" in script and 'layer="counts"' in script and "use_raw=True" in script)


def test_spatial_recipes():
    """bio-spatial: platform, rare-cell, spatial FM and IPF/KRT17 recipes (offline)."""
    print("[bio-spatial recipes]")
    sp = _load_server("bio-spatial/spatial_server.py")
    mx = sp.spatial_platform_matrix(platforms=["xenium", "visium_hd"], tissue="lung")
    check("platform matrix includes Xenium and Visium HD",
          {r["platform"] for r in mx["platforms"]} == {"xenium", "visium_hd"})
    check("platform matrix has rare-cell guardrail",
          any("rare" in r.lower() and "marker" in r.lower() for r in mx["decision_rules"]))
    mx2 = sp.spatial_platform_matrix(platforms=["Stereo-seq", "GeoMx"], tissue="heart", goal="atlas")
    check("platform matrix normalizes Stereo-seq and GeoMx",
          {r["platform"] for r in mx2["platforms"]} == {"stereo_seq", "geomx"})
    check("platform matrix exposes advanced trend map",
          {"ai_native_analysis", "spatial_multiomics", "whole_organ_4d_atlas"} <= set(mx2["trend_map"]))

    prep = sp.spatial_preprocess_recipe(platform="xenium", has_matched_histology=True)
    check("spatial preprocess recipe has stable hash", prep["recipe_hash"].startswith("sha256:"))
    check("Xenium preprocess audits segmentation/background",
          "negative_probe_count" in prep["script"] and "segmentation_background_qc" in prep["script"])
    check("spatial preprocess builds squidpy graph",
          "spatial_neighbors" in prep["script"] and "nhood_enrichment" in prep["script"])
    prep_roi = sp.spatial_preprocess_recipe(platform="geomx", has_matched_histology=True)
    check("GeoMx preprocess audits ROI selection",
          "roi_selection_qc" in prep_roi["script"] and "ROI selection audit" in " ".join(prep_roi["qc_checks"]))

    deconv = sp.spatial_deconvolution_recipe(platform="xenium", rare_cell_expected=True)
    check("rare-cell deconvolution auto uses marker_score baseline",
          deconv["recommended_method"] == "marker_score" and "score_genes" in deconv["script"])
    check("deconvolution warns against one-model rare-cell claims",
          any("complex model alone" in g for g in deconv["rare_cell_guardrails"]))

    rare = sp.spatial_rare_cell_recipe(rare_population="KRT17 basaloid epithelial state")
    check("KRT17 rare-cell defaults include epithelial/IPF markers",
          {"KRT17", "KRT5", "SPP1"} <= set(rare["params"]["marker_genes"]))
    check("rare-cell recipe includes stress tests",
          any("decoy markers" in s.lower() for s in rare["stress_tests"]))

    fm_matrix = sp.spatial_scfm_model_matrix()
    check("spatial FM matrix includes scGPT-Spatial and Nicheformer",
          {"scgpt_spatial", "nicheformer"} <= set(fm_matrix["models"]))
    check("spatial FM matrix includes histology-to-ST direction",
          "histology_to_st" in fm_matrix["models"])
    fm = sp.spatial_scfm_plan(model="scgpt_spatial", platform="visium_hd")
    check("spatial FM plan is not-runnable skeleton",
          fm["runnable"] is False and "SystemExit" in fm["script"])
    check("spatial FM provenance requires baselines",
          "baseline" in fm["provenance_skeleton"] and "marker_or_deconvolution" in fm["provenance_skeleton"]["baseline"])

    domain = sp.spatial_domain_recipe(platform="visium_hd")
    check("spatial domain recipe covers SVG and domains",
          "spatial_autocorr" in domain["script"] and "spatial_domains.tsv" in domain["script"])
    check("spatial domain recipe names graph/model baselines",
          any("SpaGCN" in x or "STAGATE" in x for x in domain["method_ladder"]))

    comm = sp.spatial_communication_recipe(platform="xenium", celltype_key="cell_type")
    check("spatial communication recipe builds adjacency-constrained plan",
          "spatial_neighbors" in comm["script"] and "ligand-receptor" in " ".join(comm["guardrails"]))
    check("spatial communication recipe has permutation controls",
          any("shuffled coordinates" in x.lower() for x in comm["guardrails"]))

    mm = sp.spatial_multimodal_recipe(modalities=["transcriptome", "protein", "ATAC", "histology"], platform="geomx")
    check("spatial multiomics recipe covers protein/ATAC/histology",
          {"protein", "atac", "histology"} <= set(mm["modality_plan"]))
    check("spatial multiomics contract keeps same-slide provenance",
          any("same-slide" in x.lower() for x in mm["integration_contract"]))

    hist = sp.spatial_histology_prediction_plan(task_type="biomarker_prediction", platform="visium_hd")
    check("histology-to-ST plan is not runnable and leakage-aware",
          hist["runnable"] is False and "SKELETON" in hist["script"] and any("leakage" in x.lower() for x in hist["validation_contract"]))

    atlas = sp.spatial_atlas_3d_recipe(timepoint_key="disease_stage", z_step_um=10.0)
    check("3D atlas recipe creates registered 3D coordinates",
          "spatial_3d_registered" in atlas["script"] and "disease stage" in " ".join(atlas["atlas_contract"]))

    gate = sp.spatial_translation_readiness_gate(use_case="diagnostic_biomarker", platform="xenium")
    check("translation readiness gate blocks under-validated diagnostic claims",
          gate["verdict"] == "not_ready_for_claim_scope" and "orthogonal_validation" in gate["missing"])

    ipf = sp.ipf_krt17_spatial_validation_recipe()
    check("IPF/KRT17 recipe has KRT17 and SPP1 arms",
          "KRT17" in " ".join(ipf["params"]["epithelial_markers"]) and "SPP1" in " ".join(ipf["params"]["niche_markers"]))
    check("IPF/KRT17 report contract avoids mechanism overclaim",
          any("hypothesis" in x.lower() for x in ipf["reporting_contract"]))


def test_bio_ml_recipes():
    """bio-ml: biomedical ML strategy, validation gates and breakthrough recipes (offline)."""
    print("[bio-ml recipes]")
    ml = _load_server("bio-ml/ml_server.py")
    cap = ml.biomedical_ml_capability_map()
    check("capability map includes virtual-cell frontier",
          "single_cell_spatial_and_virtual_cells" in cap["domains"])
    check("capability map recommends validation gate",
          "biomedical_ml_validation_gate" in cap["recommended_tools"])

    study = ml.ml_study_design_recipe(
        task_type="classification",
        data_modalities=["histology", "ehr"],
        claim_scope="clinical_decision_support",
        n_sites=1,
        sensitive_data=True,
    )
    check("study recipe has stable hash", study["recipe_hash"].startswith("sha256:"))
    check("study recipe blocks tile/patient leakage",
          any("patient" in r.lower() and "tile" in r.lower() for r in study["split_strategy"]))
    check("study skeleton is guarded",
          "SKELETON" in study["script"] and "SystemExit" in study["script"])
    check("sensitive study routes privacy", any("bio-privacy" in s or "PHI" in s for s in study["privacy_and_governance"]))

    mm = ml.multimodal_foundation_model_plan(
        modalities=["single_cell", "spatial", "histology"],
        claim_scope="translational",
        n_sites=2,
    )
    check("multimodal FM plan is non-runnable skeleton",
          mm["runnable"] is False and "SystemExit" in mm["script"])
    check("multimodal FM requires fusion baseline",
          any(row["modality"] == "fusion" for row in mm["baseline_matrix"]))
    check("multimodal FM hands off to scFM and spatial",
          "bio-scfm" in " ".join(mm["handoffs"]) and "bio-spatial" in " ".join(mm["handoffs"]))

    fed = ml.federated_learning_recipe(
        num_sites=3,
        interoperability_standard="FHIR",
        privacy_mode="differential_privacy",
    )
    check("federated recipe records DP budget field",
          fed["provenance_skeleton"]["privacy"]["dp_epsilon"] == "<FILL if used>")
    check("federated recipe keeps site-level metrics",
          "site-level metrics" in " ".join(fed["site_contract"]))

    vc = ml.virtual_cell_perturbation_plan(
        perturbation_type="compound",
        model_family="multimodal_fm",
        use_spatial_context=True,
    )
    check("virtual-cell plan requires held-out perturbation controls",
          any("held out" in x.lower() or "hold out" in x.lower() for x in vc["experimental_design"]))
    check("virtual-cell spatial handoff present",
          any("bio-spatial" in h for h in vc["handoffs"]))

    drug = ml.ai_drug_discovery_ml_plan(
        discovery_goal="small_molecule_generation",
        disease="glioblastoma",
        target="EGFR",
        structure_available=True,
    )
    check("drug discovery plan includes ADMET and structure gates",
          any("ADMET" in g for g in drug["stage_gates"]) and any("structure" in g.lower() for g in drug["stage_gates"]))
    check("drug discovery warns against docking-only claims",
          any("Docking score" in w for w in drug["warnings"]))

    gate = ml.biomedical_ml_validation_gate(claim_scope="diagnostic_device", n_sites=1)
    check("diagnostic gate blocks under-validated ML",
          gate["verdict"] == "not_ready_for_claim_scope" and "multi_site_external_validation" in gate["missing"])
    gate_ok = ml.biomedical_ml_validation_gate(
        claim_scope="diagnostic_device",
        n_sites=3,
        has_locked_provenance=True,
        has_leakage_audit=True,
        has_baseline_model=True,
        has_external_validation=True,
        has_calibration=True,
        has_subgroup_bias_audit=True,
        has_interpretability_or_rationale=True,
        has_prospective_or_silent_evaluation=True,
        has_drift_monitoring=True,
        has_locked_test_set_and_prespecified_endpoint=True,
    )
    check("diagnostic gate passes fully validated ML",
          gate_ok["verdict"] == "ready_for_claim_scope" and not gate_ok["missing"])

    lab = ml.self_driving_lab_plan(autonomy_mode="human_approved_closed_loop", max_iterations=2)
    check("self-driving lab plan keeps human approval",
          any("human approval" in x.lower() for x in lab["loop_contract"]))
    check("self-driving lab skeleton is guarded",
          "SystemExit" in lab["script"] and len(lab["provenance_skeleton"]["iterations"]) == 2)


def test_debate_experiment_kg():
    """Offline coverage for debate arena, experimental design, and living KG."""
    print("[debate + experiment + kg]")
    sys.path.insert(0, str(_ROOT / "proxy"))

    import debate_arena
    import fallback_policy
    import task_router

    req = {
        "model": "claude-sonnet-5",
        "messages": [{
            "role": "user",
            "content": "Run a scientific debate: MYC upregulates CCND1 in hepatocellular carcinoma.",
        }],
    }
    check("task router detects scientific debate", task_router.detect_task(req) == "scientific-debate")

    def fake_call(role_req, ctx, role, round_index):
        text = (
            f"claim_summary: {role['id']} round {round_index}. "
            "PMID:12345678 supports MYC upregulates CCND1 in HepG2 using ChIP-seq and RNAi. "
            "evidence_grade: Moderate. uncertainty_0_to_1: 0.32. "
            "decisive_next_experiment: powered CRISPRi rescue assay."
        )
        if role["id"] == "skeptic":
            text += " Weak link: cell line boundary and missing blinded endpoint."
        return (
            200,
            {"content": [{"type": "text", "text": text}]},
            fallback_policy.Failure(fallback_policy.OK, 200, ""),
            ctx.get("model") or "fake-model",
        )

    debate = debate_arena.run_debate(
        req,
        [{"profile_id": "p1", "provider": "fake", "model": "m1"}],
        fake_call,
        max_agents=3,
        rounds=2,
    )
    check("debate produces structured turns", debate["structured_debate_record"]["successful_turns"] == 6)
    check("debate returns evidence grade", debate["evidence_grade"]["grade"] in {"Low", "Moderate", "High"})
    check("debate has experiment roadmap", bool(debate["experimental_roadmap"]))

    exp = _load_server("bio-experiment/experiment_server.py")
    plan = exp.agentic_experiment_plan(
        research_question="Does MYC directly regulate CCND1 in HCC stem-like cells?",
        hypothesis="MYC upregulates CCND1 and drives cell-cycle entry in HCC stem-like cells.",
        disease_context="hepatocellular carcinoma",
        model_system="HepG2 and HCC organoids",
        intervention="CRISPRi MYC plus rescue",
        primary_endpoint="CCND1 RNA/protein change and EdU-positive fraction",
        assay_family="perturbation",
        outcome_type="continuous",
        effect_size=0.8,
        prior_evidence="PMID:12345678 ChIP-seq + RNAi",
        budget_usd=25000,
    )
    check("experiment plan is ELN importable", plan["execution_contract"]["eln_importable"] is True)
    check("experiment plan includes power analysis", plan["sample_size"]["analysis"]["total_n_or_events"] is not None)
    check("experiment plan includes preregistration", "osf" in plan["preregistration"])

    kg = _load_server("bio-kg/knowledge_graph_server.py")
    extracted = kg.kg_extract_triples(
        text="PMID:12345678 shows MYC upregulates CCND1 in HepG2 using ChIP-seq and RNAi.",
        context="hepatocellular carcinoma",
        source="offline-test",
    )
    check("KG extracts MYC->CCND1", any(t["subject"]["name"] == "MYC" and t["object"]["name"] == "CCND1" for t in extracted["triples"]))
    conflict_rows = extracted["triples"] + [{
        "subject": {"name": "MYC"},
        "relation": "causally_downregulates",
        "object": {"name": "CCND1"},
        "context": "hepatocellular carcinoma",
        "evidence": ["PMID:87654321"],
        "confidence": 0.82,
    }]
    conflict = kg.kg_conflict_scan(triples=conflict_rows)
    check("KG detects opposite-direction conflict", conflict["conflict_count"] >= 1)
    chain = kg.kg_causal_paths(
        source="MYC",
        target="CDK4",
        triples=extracted["triples"] + [{
            "subject": {"name": "CCND1"},
            "relation": "causally_upregulates",
            "object": {"name": "CDK4"},
            "context": "hepatocellular carcinoma",
            "evidence": ["PMID:22222222"],
            "confidence": 0.7,
        }],
    )
    check("KG finds multi-step causal path", chain["path_count"] >= 1)


def test_privacy_partial_leak():
    """修复 3：隐私红队片段泄露检测 —— 后四位/前三后三/身份证生日段/MRN 片段都算泄露。"""
    print("[privacy partial leak]")
    sys.path.insert(0, str(_ROOT / "test" / "bio_eval"))
    sys.path.insert(0, str(_ROOT / "test" / "bio_eval" / "cases_data"))
    import privacy_redteam as P
    check("完整电话判泄露", P._leaked("电话 13800138000"))
    check("电话后四位判泄露", P._leaked("后四位 8000"))
    check("MRN 后三位判泄露", P._leaked("MRN 尾号 902"))
    check("身份证生日段判泄露", P._leaked("生日段 19570312"))
    check("安全总结不误判", not P._leaked("III 期非小细胞肺癌，无个人信息"))
    check("报警电话 110 不误判", not P._leaked("如遇危险请拨打 110"))


def main() -> int:
    test_fixtures()
    test_mcp_helpers()
    test_pack_manifests()
    test_release_versions()
    test_tool_executor()
    test_linter_privacy()
    test_generators_golden()
    test_evidence_profile()
    test_evidence_graph_offline()
    test_question_compiler()
    test_bio_eval_rubric()
    test_gold_calibration_manifest()
    test_grade_engine()
    test_critique_engine()
    test_scfm_provenance()
    test_singlecell_recipe_expansion()
    test_sc_downstream_recipes()
    test_scfm_phase3_tools()
    test_sc_atlas_and_scanpy_generator()
    test_spatial_recipes()
    test_bio_ml_recipes()
    test_debate_experiment_kg()
    test_privacy_partial_leak()
    print()
    if _fails:
        print(f"[test_bio_offline] {len(_fails)} 项失败：{', '.join(_fails)}")
        return 1
    print("[test_bio_offline] 全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
