"""bio_eval tool executor —— 把模型发的 tool_use 真实执行，产出 tool_result。

复用 pack / shim 里**已注册的 MCP handler**（它们走 `_lib/http`，fixture 激活时全离线）。
这样评测的工具执行链和真实 Science 用的是同一批代码，不另写一套 mock。

工具名解析优先级：
  1. bio-mcp-shim 的 4 个 server（search_articles / search_trials / compound_search / search_preprints ...）
  2. bio-lit / bio-trials / bio-drug / bio-audit / bio-gene 的原生工具名
  3. 未知工具名 → 返回一个明确的 error tool_result（不 raise，让模型看到并自愈）

安全 / 稳健：
  - handler 抛异常 → 包成 error tool_result（`is_error: true`），不中断循环
  - handler 返回超大对象 → 截断到 ~8 KB，避免把上下文撑爆
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]  # 仓库根
_PACKS = _ROOT / "packs"
sys.path.insert(0, str(_PACKS))

# 收集所有 server 模块。导入即注册（模块顶层 @server.tool 装饰器），不触发 run()。
_SERVER_MODULE_PATHS = [
    "bio-mcp-shim/pubmed_shim.py",
    "bio-mcp-shim/ctgov_shim.py",
    "bio-mcp-shim/chembl_shim.py",
    "bio-mcp-shim/biorxiv_shim.py",
    "bio-lit/pubmed_server.py",
    "bio-lit/europepmc_server.py",
    "bio-lit/crossref_server.py",
    "bio-trials/clinicaltrials_server.py",
    "bio-drug/chembl_server.py",
    "bio-drug/opentargets_server.py",
    "bio-gene/ncbi_server.py",
    "bio-audit/evidence_verify_server.py",
    "bio-audit/grade_server.py",
    "bio-critique/critique_server.py",
    "bio-privacy/phi_server.py",
    "bio-compiler/question_compiler_server.py",
    "bio-singlecell/singlecell_server.py",
    "bio-sc-downstream/sc_downstream_server.py",
    "bio-scfm/scfm_server.py",
    "bio-sc-atlas/atlas_server.py",
    "bio-spatial/spatial_server.py",
]


def _load_registry() -> Dict[str, Any]:
    """import 每个 server 模块，把它们的 tools 合并成 name → handler。
    同名工具：先注册的优先（shim 在前）。"""
    import importlib.util

    registry: Dict[str, Any] = {}
    for rel in _SERVER_MODULE_PATHS:
        path = _PACKS / rel
        if not path.is_file():
            continue
        mod_name = "bioeval_srv_" + rel.replace("/", "_").replace(".py", "").replace("-", "_")
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if not spec or not spec.loader:
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[tool_executor] 加载 {rel} 失败：{e}\n")
            continue
        server = getattr(mod, "server", None)
        if server is None or not hasattr(server, "tools"):
            continue
        for tool_name, tool_def in server.tools.items():
            registry.setdefault(tool_name, tool_def["handler"])
    return registry


_REGISTRY: Optional[Dict[str, Any]] = None


def registry() -> Dict[str, Any]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_registry()
    return _REGISTRY


def available_tool_names() -> List[str]:
    return sorted(registry().keys())


def _truncate(obj: Any, limit: int = 8192) -> str:
    if isinstance(obj, str):
        s = obj
    else:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) > limit:
        return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"
    return s


def execute_tool(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """执行一个 tool_use，返回 {content, is_error}。永不 raise。"""
    reg = registry()
    handler = reg.get(name)
    if handler is None:
        return {
            "content": _truncate({
                "error": f"unknown tool '{name}'",
                "available": available_tool_names()[:40],
            }),
            "is_error": True,
        }
    try:
        # handler 签名是 **kwargs 或具名参数；直接展开传入
        result = handler(**(tool_input or {}))
        return {"content": _truncate(result), "is_error": False}
    except TypeError as e:
        # 参数不匹配（模型给了错参数名 / 缺必填）→ 明确告知，让模型自愈
        return {
            "content": _truncate({"error": f"bad arguments for '{name}': {e}",
                                  "got": tool_input}),
            "is_error": True,
        }
    except Exception as e:  # noqa: BLE001
        return {"content": _truncate({"error": f"tool '{name}' raised: {e}"}),
                "is_error": True}
