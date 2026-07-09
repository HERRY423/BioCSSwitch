"""Optional proxy-side ingestion into the local BioCSSwitch causal KG."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import fallback_policy
import task_router


def _load_kg_module():
    from packs._lib import causal_kg

    return causal_kg


def auto_ingest_enabled(config: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> bool:
    env = env or os.environ
    kg = config.get("knowledge_graph") if isinstance(config.get("knowledge_graph"), dict) else {}
    raw = kg.get("auto_ingest")
    if raw is None:
        raw = env.get("CSSWITCH_KG_AUTO") or env.get("BIOCSSWITCH_KG_AUTO")
    return str(raw).lower() in {"1", "true", "yes", "on", "auto"}


def graph_path(config: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> str:
    env = env or os.environ
    kg = config.get("knowledge_graph") if isinstance(config.get("knowledge_graph"), dict) else {}
    if kg.get("path"):
        return str(kg["path"])
    causal_kg = _load_kg_module()
    return causal_kg.default_graph_path(env)


def ingest_exchange(
    req: Dict[str, Any],
    resp: Dict[str, Any],
    task_id: str,
    source: str,
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not auto_ingest_enabled(config):
        return None
    text = "\n".join([task_router.request_text(req), fallback_policy.response_text(resp)])
    if not text.strip():
        return None
    causal_kg = _load_kg_module()
    kg_cfg = config.get("knowledge_graph") if isinstance(config.get("knowledge_graph"), dict) else {}
    context = str(kg_cfg.get("default_context") or task_id or "")
    triples = causal_kg.extract_triples(text, context=context, source=source)
    if not triples:
        return {"kind": "knowledge_graph_ingest", "path": graph_path(config), "extracted": 0, "written": 0}
    write = causal_kg.append_triples(graph_path(config), triples)
    return {
        "kind": "knowledge_graph_ingest",
        "path": write["path"],
        "extracted": len(triples),
        "written": write["written"],
        "source": source,
    }
