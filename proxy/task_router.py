"""Task-aware routing for BioCSSwitch Ultra.

The desktop app already stores task_routes and probe_results in config.json.
This module reads that runtime state and turns it into provider contexts the
proxy can call without restarting the app.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


TASK_RULES = {
    "scientific-debate": [
        "scientific debate", "debate arena", "multi-agent debate", "adversarial review",
        "argue both sides", "opposing agents", "structured debate", "uncertainty quantification",
        "科学辩论", "多智能体辩论", "对抗性论证", "正反方", "不确定性量化",
    ],
    "lit-review": [
        "literature review", "systematic review", "pubmed", "meta-analysis",
        "文献综述", "系统综述", "荟萃", "meta 分析",
    ],
    "clinical-trials": [
        "clinicaltrials", "clinical trial", "nct", "endpoint", "eligibility",
        "临床试验", "试验终点", "入排标准",
    ],
    "target-discovery": [
        "target discovery", "drug repurposing", "opentargets", "chembl",
        "靶点", "老药新用", "药物再利用", "成药性",
    ],
    "omics-code": [
        "deseq2", "scanpy", "seurat", "limma", "clusterprofiler", "pipeline",
        "组学", "差异表达", "脚本", "代码", "流程",
    ],
    "spatial-omics": [
        "visium", "xenium", "cosmx", "merfish", "slide-seq", "spatial",
        "空间转录组", "空间组学", "稀有细胞",
    ],
    "long-context-pdf": [
        "pdf", "full text", "supplement", "long context", "全文", "长上下文", "附件",
    ],
    "tool-heavy": [
        "use tools", "call tool", "mcp", "multi-source", "工具调用", "多源", "检索",
    ],
    "evidence-check": [
        "evidence audit", "verify citation", "doi", "pmid", "grade", "sof",
        "证据审计", "引用", "核验", "可信度", "反证",
    ],
    "phi-sensitive": [
        "phi", "patient", "mrn", "dob", "病历", "患者", "住院号", "身份证", "脱敏",
    ],
}

TASK_PROBES = {
    "scientific-debate": ["json_stable", "long_ctx"],
    "clinical-trials": ["tool_use"],
    "target-discovery": ["tool_use"],
    "tool-heavy": ["tool_use"],
    "evidence-check": ["json_stable", "tool_use"],
    "long-context-pdf": ["long_ctx"],
    "lit-review": ["long_ctx"],
}


def normalize_openai_base(base: str) -> str:
    root = str(base or "").strip().rstrip("/")
    for suffix in (
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/responses",
        "/responses",
        "/v1/models",
        "/models",
    ):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
    return root


def _ends_with_version_segment(base: str) -> bool:
    return bool(re.search(r"/v\d+(?:\.\d+)?$", base))


def openai_endpoint(base: str, suffix: str) -> str:
    root = normalize_openai_base(base)
    if not _ends_with_version_segment(root):
        root = root + "/v1"
    return root + suffix


TEMPLATE_RUNTIME = {
    "deepseek": {
        "adapter": "deepseek",
        "mode": "anthropic",
        "url": "https://api.deepseek.com/anthropic/v1/messages",
        "key_env": "DEEPSEEK_API_KEY",
        "thinking_policy": "",
    },
    "qwen": {
        "adapter": "qwen",
        "mode": "openai",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key_env": "DASHSCOPE_API_KEY",
        "thinking_policy": "",
    },
    "glm": {"adapter": "relay", "thinking_policy": "adaptive"},
    "xiaomi": {"adapter": "relay", "thinking_policy": "adaptive"},
    "siliconflow": {"adapter": "relay", "thinking_policy": "adaptive"},
    "kimi": {"adapter": "relay", "thinking_policy": "enabled"},
    "minimax": {"adapter": "relay", "thinking_policy": "adaptive"},
    "openrouter": {"adapter": "relay", "thinking_policy": "adaptive"},
    "custom-openai": {
        "adapter": "openai-custom",
        "mode": "openai",
        "key_env": "CSSWITCH_OPENAI_KEY",
        "thinking_policy": "",
    },
    "custom-openai-responses": {
        "adapter": "openai-responses",
        "mode": "openai",
        "key_env": "CSSWITCH_OPENAI_KEY",
        "thinking_policy": "",
    },
    "custom": {"adapter": "relay", "thinking_policy": "adaptive"},
}


def request_text(req: Dict[str, Any]) -> str:
    parts: List[str] = []
    if isinstance(req.get("system"), str):
        parts.append(req["system"])
    elif isinstance(req.get("system"), list):
        parts.extend(b.get("text", "") for b in req["system"] if isinstance(b, dict))
    for msg in req.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("text", "tool_result"):
                    txt = block.get("text") or block.get("content") or ""
                    if not isinstance(txt, str):
                        txt = json.dumps(txt, ensure_ascii=False, default=str)
                    parts.append(txt)
    return "\n".join(parts)


def detect_task(req: Dict[str, Any], override: Optional[str] = None) -> str:
    if override:
        return override
    text = request_text(req).lower()
    best = ("tool-heavy" if req.get("tools") else "", 0)
    for task, needles in TASK_RULES.items():
        score = sum(1 for n in needles if n.lower() in text)
        if score > best[1]:
            best = (task, score)
    return best[0] or "general"


def default_config_path(env: Optional[Dict[str, str]] = None) -> str:
    env = env or os.environ
    return (
        env.get("CSSWITCH_CONFIG_PATH")
        or env.get("CSSWITCH_ULTRA_CONFIG")
        or os.path.join(os.path.expanduser("~"), ".csswitch", "config.json")
    )


def load_runtime_config(path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    path = path or default_config_path(env)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def profile_by_id(config: Dict[str, Any], profile_id: str) -> Optional[Dict[str, Any]]:
    for profile in config.get("profiles") or []:
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            return profile
    return None


def context_from_profile(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tid = profile.get("template_id") or "custom"
    rt = TEMPLATE_RUNTIME.get(tid, TEMPLATE_RUNTIME["custom"])
    adapter = rt["adapter"]
    key = profile.get("api_key") or profile.get("key") or ""
    if not key:
        return None
    ctx = {
        "profile_id": profile.get("id") or "",
        "profile_name": profile.get("name") or profile.get("id") or "profile",
        "template_id": tid,
        "provider": adapter,
        "mode": rt.get("mode") or ("openai" if adapter == "qwen" else "anthropic"),
        "key": key,
        "key_env": rt.get("key_env") or ("CSSWITCH_RELAY_KEY" if adapter == "relay" else ""),
        "model": profile.get("model") or "",
        "thinking_policy": rt.get("thinking_policy", ""),
        "base_url": profile.get("base_url") or "",
    }
    if adapter == "relay":
        base = (ctx["base_url"] or "").rstrip("/")
        if not re.match(r"^https?://", base):
            return None
        ctx["url"] = base + "/v1/messages"
        ctx["models_url"] = base + "/v1/models"
        ctx["auth_style"] = "both"
    elif adapter in {"openai-custom", "openai-responses"}:
        base = normalize_openai_base(ctx["base_url"])
        if not re.match(r"^https?://", base):
            return None
        suffix = "/responses" if adapter == "openai-responses" else "/chat/completions"
        ctx["url"] = openai_endpoint(base, suffix)
        ctx["models_url"] = openai_endpoint(base, "/models")
        ctx["auth_style"] = "bearer"
    else:
        ctx["url"] = rt["url"]
        ctx["auth_style"] = "bearer" if adapter == "qwen" else "x-api-key"
    return ctx


def current_context(provider: str, prov: Dict[str, Any], key: str,
                    force_model: Optional[str] = None, thinking_policy: Optional[str] = None) -> Dict[str, Any]:
    return {
        "profile_id": "active",
        "profile_name": provider or "active",
        "template_id": provider or "active",
        "provider": provider,
        "mode": prov.get("mode", "anthropic"),
        "url": prov.get("url"),
        "models_url": prov.get("models_url"),
        "key": key,
        "model": force_model or "",
        "thinking_policy": thinking_policy or "",
        "auth_style": prov.get("auth_style", "x-api-key" if provider != "qwen" else "bearer"),
        "base_url": "",
        "prov": prov,
    }


def probe_verdict(config: Dict[str, Any], profile_id: str, probe: str) -> str:
    raw = (config.get("probe_results") or {}).get(f"{profile_id}:{probe}")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return ""
    if isinstance(raw, dict):
        return str(raw.get("verdict") or "")
    return ""


def profile_probe_report(config: Dict[str, Any], profile_id: str, task_id: str) -> Tuple[List[Dict[str, str]], bool]:
    report: List[Dict[str, str]] = []
    hard_fail = False
    for probe in TASK_PROBES.get(task_id, []):
        verdict = probe_verdict(config, profile_id, probe) or "missing"
        report.append({"probe": probe, "verdict": verdict})
        if verdict == "fail":
            hard_fail = True
    return report, hard_fail


def profile_passes_task_probes(config: Dict[str, Any], profile_id: str, task_id: str) -> bool:
    _report, hard_fail = profile_probe_report(config, profile_id, task_id)
    return not hard_fail


def _route_ids_from_value(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, dict):
        out: List[str] = []
        for key in ("profile_id", "primary_profile_id"):
            if value.get(key):
                out.append(str(value[key]))
        for key in ("profiles", "profile_ids", "fallback_profile_ids"):
            vals = value.get(key)
            if isinstance(vals, list):
                out.extend(str(x) for x in vals if x)
            elif isinstance(vals, str):
                out.append(vals)
        return out
    return []


def _add_ordered(target: List[Dict[str, str]], profile_ids: Iterable[str], source: str) -> None:
    for pid in profile_ids:
        if pid:
            target.append({"profile_id": str(pid), "source": source})


def route_plan(config: Dict[str, Any], task_id: str, active_ctx: Dict[str, Any],
               failure_kind: Optional[str] = None) -> Dict[str, Any]:
    """Return route candidates plus diagnostics derived from config state."""
    routes = config.get("task_routes") or {}
    task_policies = ((config.get("ultra") or {}).get("task_policies") or {})
    policy = task_policies.get(task_id) or {}
    route_value = routes.get(task_id)
    ordered: List[Dict[str, str]] = []

    route_ids = _route_ids_from_value(route_value)
    primary = policy.get("primary_profile_id") or (route_ids[0] if route_ids else None) or config.get("active_id")
    _add_ordered(ordered, [primary], "primary")

    failure_routes = policy.get("failure_routes") if isinstance(policy.get("failure_routes"), dict) else {}
    if failure_kind:
        _add_ordered(ordered, _route_ids_from_value(failure_routes.get(failure_kind)), f"failure_route:{failure_kind}")
        _add_ordered(ordered, _route_ids_from_value(failure_routes.get("*")), "failure_route:*")

    _add_ordered(ordered, policy.get("fallback_profile_ids") or [], "policy_fallback")
    _add_ordered(ordered, route_ids[1:], "task_route")

    # Add other configured profiles as last-resort candidates when they pass probes.
    for profile in config.get("profiles") or []:
        pid = profile.get("id") if isinstance(profile, dict) else None
        if pid:
            _add_ordered(ordered, [pid], "last_resort")

    out: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen = set()
    for item in ordered:
        pid = item["profile_id"]
        if pid in seen:
            continue
        seen.add(pid)
        probes, hard_fail = profile_probe_report(config, pid, task_id)
        if hard_fail:
            skipped.append({
                "profile_id": pid,
                "source": item["source"],
                "reason": "probe_fail",
                "probes": probes,
            })
            continue
        prof = profile_by_id(config, pid)
        ctx = context_from_profile(prof) if prof else None
        if not ctx:
            skipped.append({
                "profile_id": pid,
                "source": item["source"],
                "reason": "profile_missing_or_unusable",
                "probes": probes,
            })
            continue
        degraded = any(p.get("verdict") == "degraded" for p in probes)
        ctx["_route_source"] = item["source"]
        ctx["_probe_results"] = probes
        candidates.append({
            "profile_id": pid,
            "profile_name": ctx.get("profile_name", ""),
            "source": item["source"],
            "probe_status": "degraded" if degraded else "ok",
            "probes": probes,
        })
        out.append(ctx)

    if not out and active_ctx:
        active = dict(active_ctx)
        active["_route_source"] = "runtime_active"
        active["_probe_results"] = []
        out.append(active)
        candidates.append({
            "profile_id": active.get("profile_id", "active"),
            "profile_name": active.get("profile_name", "active"),
            "source": "runtime_active",
            "probe_status": "unknown",
            "probes": [],
        })

    return {
        "task_id": task_id,
        "failure_kind": failure_kind or "",
        "contexts": out,
        "candidates": candidates,
        "skipped": skipped,
        "required_probes": TASK_PROBES.get(task_id, []),
        "policy": {
            "primary_profile_id": primary or "",
            "fallback_profile_ids": policy.get("fallback_profile_ids") or [],
            "failure_routes": failure_routes,
        },
    }


def route_contexts(config: Dict[str, Any], task_id: str, active_ctx: Dict[str, Any],
                   failure_kind: Optional[str] = None) -> List[Dict[str, Any]]:
    return route_plan(config, task_id, active_ctx, failure_kind=failure_kind)["contexts"]


def host_from_context(ctx: Dict[str, Any]) -> str:
    url = ctx.get("url") or ctx.get("base_url") or ""
    m = re.match(r"^https?://([^/:?#]+)", url)
    return (m.group(1) if m else "").lower()


def is_local_or_allowed(ctx: Dict[str, Any], allowed_hosts: Iterable[str]) -> bool:
    host = host_from_context(ctx)
    allowed = {h.strip().lower() for h in allowed_hosts or [] if h.strip()}
    if host in allowed:
        return True
    return (
        host in ("localhost", "127.0.0.1", "::1")
        or host.startswith("10.")
        or host.startswith("192.168.")
        or bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host))
    )
