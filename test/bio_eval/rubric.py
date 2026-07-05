"""bio_eval 多维评分引擎。

需求 4 的核心：分数不能只看「是否调用工具」，还要看：
  - 工具**结果是否被正确使用**（grounding：最终答复里的 ID 是否真的来自工具输出，不是编的）
  - 结论**是否被证据支持**（linter：挂的 PMID/NCT 真实存在）
  - **是否暴露不确定性**（五段面板 Known knowns / unknowns / Conflicts / Missing / Next experiment）

每个 case 带一个 `rubric` 规格，本模块把它拆成多个 [0,1] 维度，再加权合成。
幻觉（编造 ID）作为**乘法惩罚**，与项目"编 ID 零分、不给部分分作激励"的哲学一致。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional


# ---------- ID 抽取（final_text / tool_results 共用）----------
def extract_ids(text: str) -> Dict[str, set]:
    text = text or ""
    return {
        "pmid": {m.group(1) for m in re.finditer(r"(?:PMID|pmid)[\s:：\-]*(\d{4,9})", text)},
        "nct": {m.group(1).upper() for m in re.finditer(r"(NCT\d{8})", text, re.I)},
        "chembl": {m.group(1).upper() for m in re.finditer(r"(CHEMBL\d+)", text, re.I)},
        "gse": {m.group(1).upper() for m in re.finditer(r"(GSE\d{3,7})", text, re.I)},
        "doi": {m.group(0) for m in re.finditer(r"10\.\d{4,9}/\S+", text)},
    }


def _all_ids_flat(idmap: Dict[str, set]) -> set:
    out = set()
    for v in idmap.values():
        out |= v
    return out


# ---------- 各维度 ----------
def dim_tool_invoked(tool_calls: List[Dict[str, Any]], expect: List[str]) -> float:
    if not expect:
        return 1.0
    called = {c.get("name") for c in tool_calls or []}
    return sum(1 for t in expect if t in called) / len(expect)


_QUERY_ARG_KEYS = ("query", "condition", "intervention", "term", "text", "question")


def dim_query_relevance(tool_calls: List[Dict[str, Any]], primary_tool: Optional[str],
                        keywords: List[str]) -> float:
    """primary_tool 的检索参数是否包含期望关键词。命中一半以上=1，部分=0.6，完全跑题=0.3，没调=0。"""
    best = 0.0
    found_call = False
    for c in tool_calls or []:
        if primary_tool and c.get("name") != primary_tool:
            continue
        found_call = True
        args = c.get("input") or {}
        blob = " ".join(str(args.get(k, "")) for k in _QUERY_ARG_KEYS).lower()
        hits = sum(1 for k in keywords if k.lower() in blob)
        if hits >= max(1, len(keywords) // 2):
            best = max(best, 1.0)
        elif hits > 0:
            best = max(best, 0.6)
        else:
            best = max(best, 0.3)
    return best if found_call else 0.0


def dim_gold_match(final_text: str, gold: Dict[str, Any]) -> Optional[float]:
    """专家金标准匹配：答复是否命中专家标定的正确 ID / 必提实体。
    gold = {ids:[...], must_mention:[...], mode:"any"|"all"}。都空 → None。"""
    text = final_text or ""
    parts: List[float] = []
    ids = gold.get("ids") or []
    if ids:
        found = _all_ids_flat(extract_ids(text))
        hit = sum(1 for g in ids if g in found or g.upper() in {x.upper() for x in found})
        parts.append(hit / len(ids))
    mentions = gold.get("must_mention") or []
    if mentions:
        tl = text.lower()
        if gold.get("mode") == "all":
            parts.append(sum(1 for m in mentions if m.lower() in tl) / len(mentions))
        else:  # any-of: 命中任意一个即满分（如"KRAS G12C 药物"命中 sotorasib 或 adagrasib）
            parts.append(1.0 if any(m.lower() in tl for m in mentions) else 0.0)
    if not parts:
        return None
    return sum(parts) / len(parts)


# 语义 grounding：抽答复里的"事实原子"（数字事实 + 命名实体），看有多少真出现在工具结果里。
# 目的：抓"编造数字/实体"——ID 对但把 HR 0.65 编成 0.42 这类，ID-grounding 抓不到。
_NUM_FACT = re.compile(r"\b\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\b")
_ENTITY_TOK = re.compile(r"\b[A-Z][A-Za-z0-9\-]{2,}\b")
_ENTITY_STOP = {"The", "This", "PMID", "DOI", "NCT", "GSE", "CHEMBL", "RCT", "PFS", "OS",
                "ORR", "HR", "OR", "RR", "CI", "FDA", "USA", "II", "III", "IV", "JSON"}


def dim_semantic_grounding(final_text: str, tool_results: List[Dict[str, Any]]) -> Optional[float]:
    """答复里的数字事实 + 命名实体，有多少能在工具结果里找到出处。无可查事实 → None。"""
    text = final_text or ""
    results_blob = "\n".join(str(r.get("content", "")) for r in tool_results or [])
    if not results_blob:
        return None
    atoms: set = set()
    for m in _NUM_FACT.finditer(text):
        tok = m.group(0).replace(" ", "")
        # 跳过年份和小整数（噪声大）
        if re.fullmatch(r"(19|20)\d{2}", tok) or re.fullmatch(r"\d", tok):
            continue
        atoms.add(tok)
    for m in _ENTITY_TOK.finditer(text):
        tok = m.group(0)
        if tok in _ENTITY_STOP:
            continue
        atoms.add(tok)
    if not atoms:
        return None
    blob_low = results_blob.lower()
    grounded = sum(1 for a in atoms if a.replace(" ", "").lower() in blob_low)
    return grounded / len(atoms)


def dim_grounded(final_text: str, tool_results: List[Dict[str, Any]]) -> Optional[float]:
    """最终答复里的 ID 有多少比例真的出现在工具返回里（而非模型自己编的）。
    答复里没有任何 ID → None（本维度不适用，交给别的维度评）。"""
    final_ids = _all_ids_flat(extract_ids(final_text))
    if not final_ids:
        return None
    results_blob = "\n".join(str(r.get("content", "")) for r in tool_results or [])
    result_ids = _all_ids_flat(extract_ids(results_blob))
    grounded = sum(1 for i in final_ids if i in result_ids)
    return grounded / len(final_ids)


# 五段面板：中英双语标题都认。
_PANEL_SECTIONS = [
    ("known_knowns", [r"known\s*knowns", r"已知已确证", r"已知的已知"]),
    ("known_unknowns", [r"known\s*unknowns", r"已知的未知", r"已知未知"]),
    ("conflicts", [r"conflicts?", r"冲突", r"反证"]),
    ("missing_data", [r"missing\s*data", r"缺失数据", r"盲区"]),
    ("next_experiment", [r"next\s*experiments?", r"下一步实验", r"下一步"]),
]


def dim_uncertainty(final_text: str) -> float:
    tl = (final_text or "").lower()
    hit = 0
    for _key, pats in _PANEL_SECTIONS:
        if any(re.search(p, tl) for p in pats):
            hit += 1
    return hit / len(_PANEL_SECTIONS)


def dim_json_shape(final_text: str, shape: Dict[str, Any]) -> float:
    """从答复里挖 JSON（fenced 或整段），校验 shape。
    shape = {"root": "object"|"array", "require_keys": [...], "item_keys": [...]}."""
    candidates: List[str] = []
    for m in re.finditer(r"```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```", final_text or ""):
        candidates.append(m.group(1))
    candidates.append((final_text or "").strip())
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        return _check_shape(obj, shape)
    return 0.0


def _check_shape(obj: Any, shape: Dict[str, Any]) -> float:
    root = shape.get("root", "object")
    req = shape.get("require_keys", [])
    item_keys = shape.get("item_keys", [])
    if root == "object":
        if not isinstance(obj, dict):
            return 0.3
        got = sum(1 for k in req if k in obj)
        base = got / len(req) if req else 1.0
        # 若 require_keys 指向一个数组字段，检查其 item shape
        if item_keys and req:
            arr = obj.get(req[0])
            if isinstance(arr, list) and arr:
                item_ok = sum(1 for it in arr
                              if isinstance(it, dict) and all(k in it for k in item_keys)) / len(arr)
                return round(0.5 * base + 0.5 * item_ok, 3)
        return round(base, 3)
    if root == "array":
        if not isinstance(obj, list) or not obj:
            return 0.3
        ok = sum(1 for it in obj if isinstance(it, dict) and all(k in it for k in item_keys))
        return round(ok / len(obj), 3)
    return 0.0


# ---------- 合成 ----------
_DEFAULT_WEIGHTS = {
    "tool_invoked": 1.0,
    "query_relevance": 1.0,
    "grounded": 1.5,            # 用对结果比调用本身更重要
    "gold_match": 2.0,          # 命中专家金标准是最强信号
    "semantic_grounding": 1.5,  # 数字/实体不能编
    "uncertainty": 1.0,
    "json_valid": 1.5,
    "custom": 1.5,
}


def score_case(case: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {score, dims, notes, fake_ratio}。ctx 含 final_text / tool_calls / tool_results / lint。"""
    r = case.get("rubric", {}) or {}
    dims: Dict[str, float] = {}
    notes: List[str] = []

    expect = r.get("expect_tools", [])
    if expect or r.get("force_tool_dim"):
        dims["tool_invoked"] = dim_tool_invoked(ctx["tool_calls"], expect)

    kws = r.get("query_keywords")
    if kws:
        primary = r.get("primary_tool") or (expect[0] if expect else None)
        dims["query_relevance"] = dim_query_relevance(ctx["tool_calls"], primary, kws)

    if r.get("require_grounding"):
        g = dim_grounded(ctx["final_text"], ctx.get("tool_results") or [])
        if g is None:
            dims["grounded"] = 0.0
            notes.append("要求 grounding 但答复未给出任何可核对 ID")
        else:
            dims["grounded"] = g

    if r.get("gold"):
        gm = dim_gold_match(ctx["final_text"], r["gold"])
        if gm is not None:
            dims["gold_match"] = gm
            if gm < 1.0:
                notes.append(f"专家金标准未完全命中（{gm:.2f}）")

    if r.get("require_semantic"):
        sg = dim_semantic_grounding(ctx["final_text"], ctx.get("tool_results") or [])
        if sg is None:
            notes.append("无可核查的事实原子，语义 grounding 跳过")
        else:
            dims["semantic_grounding"] = sg

    if r.get("require_uncertainty"):
        dims["uncertainty"] = dim_uncertainty(ctx["final_text"])
        if dims["uncertainty"] < 1.0:
            notes.append(f"不确定性面板不完整（{int(dims['uncertainty']*5)}/5 段）")

    if r.get("json_shape"):
        dims["json_valid"] = dim_json_shape(ctx["final_text"], r["json_shape"])

    custom: Optional[Callable] = r.get("custom")
    if custom:
        try:
            dims["custom"] = float(custom(ctx))
        except Exception as e:  # noqa: BLE001
            dims["custom"] = 0.0
            notes.append(f"custom scorer 抛错: {e}")

    # 加权平均
    if dims:
        wsum = sum(_DEFAULT_WEIGHTS.get(k, 1.0) for k in dims)
        base = sum(v * _DEFAULT_WEIGHTS.get(k, 1.0) for k, v in dims.items()) / wsum
    else:
        base = 0.0

    # 门控：gate 里的维度若 < 0.5，整体封顶 0.4（例如该调工具却没调）
    for gd in r.get("gate", []):
        if dims.get(gd, 1.0) < 0.5:
            base = min(base, 0.4)
            notes.append(f"门控维度 {gd} 未过（{dims.get(gd)}）→ 封顶 0.4")

    # 幻觉惩罚（乘法）：编造 ID 比例越高扣越狠，全编直接归零。
    # 例外：反幻觉 case 会**故意**在 prompt 里埋一个假 ID，要求模型识别并撤回——
    # 模型为了说"这个不存在"必须提到它，这种提及不算幻觉。用 expected_fake_ids 排除。
    lint = ctx.get("lint") or {}
    expected_fake = set(r.get("expected_fake_ids") or [])
    details = lint.get("details") or []
    if details:
        considered = [d for d in details if d.get("id") not in expected_fake]
        n_total = len(considered)
        n_fake = sum(1 for d in considered if not d.get("exists"))
    else:
        n_total = lint.get("n_total", 0)
        n_fake = lint.get("n_fake", 0)
    fake_ratio = (n_fake / n_total) if n_total else 0.0
    final = max(0.0, base * (1.0 - fake_ratio))
    if fake_ratio > 0:
        notes.append(f"幻觉惩罚：{n_fake}/{n_total} 个 ID 不存在 → ×{1-fake_ratio:.2f}")

    return {
        "score": round(final, 3),
        "base_score": round(base, 3),
        "dims": {k: round(v, 3) for k, v in dims.items()},
        "fake_ratio": round(fake_ratio, 3),
        "notes": notes,
    }
