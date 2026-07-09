"""BioCSSwitch Ultra orchestration layer.

The first production-safe version is deliberately conservative:
  - disabled unless CSSWITCH_ULTRA_MODE is set;
  - full orchestration is non-streaming only;
  - subagents are local structured guardrails by default, not autonomous tool
    executors.
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import fallback_policy
import task_router
import debate_arena
import knowledge_ingest
import anthropic_compat
import provider_policy
import provider_registry


@dataclass
class Attempt:
    profile_id: str
    profile_name: str
    provider: str
    model: str
    status: int
    outcome: str
    reason: str = ""
    route_source: str = ""
    probes: List[Dict[str, str]] = field(default_factory=list)
    subagents: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UltraResult:
    handled: bool
    status: int = 0
    body: Dict[str, Any] = field(default_factory=dict)
    task_id: str = "general"
    attempts: List[Attempt] = field(default_factory=list)
    route_plan: Dict[str, Any] = field(default_factory=dict)


def ultra_enabled(mode: Optional[str]) -> bool:
    return str(mode or "").lower() not in ("", "0", "false", "off", "normal")


def request_has_phi(req: Dict[str, Any]) -> bool:
    return _text_has_phi(task_router.request_text(req))


def _text_has_phi(text: str) -> bool:
    patterns = [
        r"\bMRN[:#\s]*\d{5,}\b",
        r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN-like
        r"(?:DOB|出生日期)[:：#\s]*\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b",
        r"(?:身份证|身份证号)[:：#\s]*[0-9Xx]{15,18}\b",
        r"(?:住院号|病案号)[:：#\s]*\d{5,}\b",
    ]
    return any(re.search(p, text, re.I) for p in patterns)


def preflight_guard(req: Dict[str, Any], ctx: Dict[str, Any], config: Dict[str, Any]) -> fallback_policy.Failure:
    if config.get("sensitive_mode") and request_has_phi(req):
        allowed = config.get("local_endpoint_hosts") or []
        if not task_router.is_local_or_allowed(ctx, allowed):
            return fallback_policy.Failure(
                fallback_policy.SENSITIVE_VIOLATION,
                400,
                "sensitive mode blocks PHI from leaving local/allowed endpoints",
            )
    return fallback_policy.Failure(fallback_policy.OK, 200, "")


def run_subagents(req: Dict[str, Any], resp: Dict[str, Any], task_id: str,
                  mode: str = "ultra_conservative") -> Dict[str, Any]:
    """Run local structured subagent guardrails.

    These are intentionally deterministic in v1. They establish the protocol and
    safety gates; future versions can replace individual roles with upstream
    model calls under the same JSON contract.
    """
    return _run_subagents_v2(req, resp, task_id, mode)


def _run_subagents_v2(req: Dict[str, Any], resp: Dict[str, Any], task_id: str,
                      mode: str) -> Dict[str, Any]:
    text = fallback_policy.response_text(resp)
    findings: List[Dict[str, Any]] = []
    roles_run: List[str] = []

    if _verifier_enabled(req, task_id):
        roles_run.append("verifier")
        grounding_text = _verifier_grounding_text(req)
        for cid in _citation_ids(text):
            if cid["needle"] and cid["needle"].lower() not in grounding_text.lower():
                findings.append({
                    "agent_id": "verifier",
                    "severity": "high",
                    "issue": "ungrounded_citation_id",
                    "citation": cid["raw"],
                    "recommendation": "Use only IDs found in tool results or remove the citation.",
                })
        if (task_id == "phi-sensitive" or request_has_phi(req)) and _text_has_phi(text):
            findings.append({
                "agent_id": "verifier",
                "severity": "high",
                "issue": "phi_echo",
                "recommendation": "Redact direct identifiers and answer only with de-identified placeholders.",
            })

    if _critic_enabled(text, task_id):
        roles_run.append("critic")
        findings.extend(_critic_findings(text, task_id, mode))

    plan: List[str] = []
    if _deep_ultra(mode):
        if fallback_policy.request_forces_tool(req) and not fallback_policy.response_has_tool_use(resp):
            roles_run.append("toolsmith")
            findings.append({
                "agent_id": "toolsmith",
                "severity": "high",
                "issue": "tool_use_missing",
                "recommendation": "Retry with a tool-stable profile or DSML repair.",
            })

        low = text.lower()
        if task_id == "omics-code" and "```" not in text and "python" not in low and "rscript" not in low:
            roles_run.append("coder")
            findings.append({
                "agent_id": "coder",
                "severity": "low",
                "issue": "no_code_skeleton",
                "recommendation": "Add a reproducible script or command skeleton.",
            })

        if task_id in ("lit-review", "target-discovery", "clinical-trials"):
            roles_run.append("planner")
            plan = ["classify question", "retrieve evidence", "audit citations", "state uncertainty"]

    verdict = "fail" if any(f["severity"] == "high" for f in findings) else ("warn" if findings else "pass")
    return {
        "verdict": verdict,
        "findings": findings,
        "planner": {"steps": plan},
        "roles_run": sorted(set(roles_run)),
    }


def _deep_ultra(mode: str) -> bool:
    return str(mode or "").lower() in {"ultra_deep", "deep_ultra", "opus4.8ultra", "opus48ultra", "deep"}


def _verifier_enabled(req: Dict[str, Any], task_id: str) -> bool:
    return task_id in {"clinical-trials", "evidence-check", "phi-sensitive"} or request_has_phi(req)


def _critic_enabled(text: str, task_id: str) -> bool:
    if task_id in {"clinical-trials", "evidence-check", "target-discovery", "lit-review"}:
        return True
    low = text.lower()
    return any(x in low for x in ("clinical", "patient", "pmid", "nct", "mouse", "mice", "in vitro", "cancer", "tumor", "gene"))


def _citation_ids(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in re.finditer(r"\bPMID\s*[:#]?\s*(\d{4,9})\b", text or "", re.I):
        out.append({"kind": "pmid", "raw": m.group(0), "needle": m.group(1)})
    for m in re.finditer(r"\bNCT\d{8}\b", text or "", re.I):
        out.append({"kind": "nct", "raw": m.group(0), "needle": m.group(0).upper()})
    for m in re.finditer(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text or "", re.I):
        out.append({"kind": "doi", "raw": m.group(0), "needle": m.group(0).lower()})
    seen = set()
    unique: List[Dict[str, str]] = []
    for item in out:
        key = (item["kind"], item["needle"].lower())
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def _verifier_grounding_text(req: Dict[str, Any]) -> str:
    return "\n".join([task_router.request_text(req), _tool_result_text(req)])


def _critic_findings(text: str, task_id: str, mode: str) -> List[Dict[str, Any]]:
    modules = _load_critique_modules()
    if modules:
        return _critic_findings_from_rules(text, task_id, mode, modules)
    return _critic_findings_heuristic(text)


def _load_critique_modules() -> Optional[Dict[str, Any]]:
    try:
        from packs._lib.counter_experiment import design_counter_experiment
        from packs._lib.critique_scoring import score_believability
        from packs._lib.extrapolation_checker import (
            check_extrapolations,
            concern_level,
            detect_methodology_flags,
            infer_asserted_from_text,
            infer_boundary_from_text,
        )
        return {
            "check_extrapolations": check_extrapolations,
            "concern_level": concern_level,
            "detect_methodology_flags": detect_methodology_flags,
            "infer_asserted_from_text": infer_asserted_from_text,
            "infer_boundary_from_text": infer_boundary_from_text,
            "score_believability": score_believability,
            "design_counter_experiment": design_counter_experiment,
        }
    except Exception:
        return None


def _critic_findings_from_rules(text: str, task_id: str, mode: str, modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    intensity = "aggressive" if _deep_ultra(mode) else "standard"
    for claim in _split_claims(text)[:5]:
        boundary = modules["infer_boundary_from_text"](claim)
        asserted = modules["infer_asserted_from_text"](claim)
        species = set(boundary.get("species") or [])
        if asserted.get("species") == "human" and ({"animal", "in-vitro"} & species):
            boundary["species"] = sorted(species & {"animal", "in-vitro"})
        extrapolations = modules["check_extrapolations"](
            asserted=asserted,
            boundary=boundary,
            claim_text=claim,
            intensity=intensity,
        )
        methodology_flags = modules["detect_methodology_flags"](
            boundary=boundary,
            claim_text=claim,
            asserted=asserted,
            intensity=intensity,
        )
        if not extrapolations and not methodology_flags:
            continue
        level = modules["concern_level"](extrapolations, methodology_flags)
        severity = "high" if level in {"red", "orange"} else "medium"
        score = modules["score_believability"](
            evidence_level="text-only heuristic; evidence_graph not run",
            verdict="contested",
            extrapolations=extrapolations,
            methodology_flags=methodology_flags,
            language="en",
        )
        counter = None
        if extrapolations:
            counter = modules["design_counter_experiment"](
                claim,
                extrapolations=extrapolations,
                boundary=boundary,
                language="en",
            )
        findings.append({
            "agent_id": "critic",
            "severity": severity,
            "issue": "evidence_boundary_risk",
            "claim": claim[:240],
            "concern_level": level,
            "rule_ids": [x.get("rule_id") for x in extrapolations if x.get("rule_id")],
            "methodology_ids": [x.get("check_id") for x in methodology_flags if x.get("check_id")],
            "believability": {
                "score": score.get("score"),
                "score_100": score.get("score_100"),
                "key_concern": score.get("key_concern"),
            },
            "counter_experiment": counter,
            "recommendation": "Narrow the claim to the evidence boundary or add bridging validation.",
        })
    return findings or _critic_findings_heuristic(text)


def _critic_findings_heuristic(text: str) -> List[Dict[str, Any]]:
    low = text.lower()
    if any(x in low for x in ("mouse", "mice", "murine", "in vitro", "cell line")) and any(
        x in low for x in ("clinically effective", "treats patients", "human efficacy")
    ):
        return [{
            "agent_id": "critic",
            "severity": "medium",
            "issue": "possible_extrapolation",
            "recommendation": "Separate preclinical support from clinical efficacy claims.",
        }]
    return []


def _split_claims(text: str) -> List[str]:
    raw = re.split(r"[\n.;!?]+", text or "")
    claims = []
    for part in raw:
        p = part.strip(" -\t\r")
        if len(p) >= 12:
            claims.append(p)
    return claims or ([text.strip()] if text.strip() else [])


def quality_gate(req: Dict[str, Any], resp: Dict[str, Any], task_id: str,
                 subagents: Dict[str, Any]) -> fallback_policy.Failure:
    q = fallback_policy.classify_quality(req, resp, task_id)
    if q.kind != fallback_policy.OK:
        return q
    if subagents.get("verdict") == "fail":
        return fallback_policy.Failure(
            fallback_policy.QUALITY_GATE_FAIL,
            200,
            "verifier/critic guardrail failed",
        )
    return fallback_policy.Failure(fallback_policy.OK, 200, "")


def handle_request(req: Dict[str, Any], active_ctx: Dict[str, Any], config: Dict[str, Any],
                   http_post: Callable[..., Tuple[bytes, str]], ledger_path: Optional[str],
                   log_fn: Callable[[str], None] = lambda _msg: None,
                   mode: str = "ultra_conservative") -> Optional[UltraResult]:
    """Handle a non-streaming Ultra request. Return None to use legacy path."""
    if req.get("stream"):
        # SSE mid-stream retries are intentionally left to the legacy path in v1.
        return None

    task_id = task_router.detect_task(req)
    policy = fallback_policy.policy_for(config, task_id)
    route_plan = task_router.route_plan(config, task_id, active_ctx)
    route_events = [_ledger_route_plan(route_plan)]
    contexts = list(route_plan.get("contexts") or [])
    if not contexts:
        return None

    contexts, sensitive_skips = _filter_sensitive_contexts(req, contexts, config)
    if sensitive_skips:
        route_events.append({"kind": "sensitive_filter", "skipped": sensitive_skips})

    ledger = fallback_policy.FallbackLedger(ledger_path, extra_secrets=[c.get("key", "") for c in contexts])
    attempts: List[Attempt] = []
    final_failure = fallback_policy.Failure(fallback_policy.TRANSPORT_ERROR, 0, "no attempts")

    if not contexts:
        final_failure = fallback_policy.Failure(
            fallback_policy.SENSITIVE_VIOLATION,
            400,
            "sensitive mode has no local/allowed profile for PHI",
        )
        ledger.write(_ledger_entry(task_id, attempts, final_failure.kind, route_events, mode, policy))
        return UltraResult(True, 400, fallback_policy.error_body(final_failure), task_id, attempts, {"events": route_events})

    if debate_arena.should_run_debate(req, task_id, mode, config):
        ultra_cfg = config.get("ultra") if isinstance(config.get("ultra"), dict) else {}
        debate_cfg = ultra_cfg.get("debate") if isinstance(ultra_cfg.get("debate"), dict) else {}

        def _debate_call(role_req: Dict[str, Any], ctx: Dict[str, Any],
                         _role: Dict[str, Any], _round_index: int):
            pre = preflight_guard(role_req, ctx, config)
            model = resolve_model(role_req.get("model"), ctx)
            if pre.kind != fallback_policy.OK:
                return 400, fallback_policy.error_body(pre), pre, model
            return call_once(role_req, ctx, http_post)

        debate = debate_arena.run_debate(
            req,
            contexts,
            _debate_call,
            task_id=task_id,
            max_agents=debate_cfg.get("max_agents", 4),
            rounds=debate_cfg.get("rounds", 2),
        )
        route_events.append({
            "kind": "scientific_debate",
            "rounds": debate.get("structured_debate_record", {}).get("rounds", 0),
            "successful_turns": debate.get("structured_debate_record", {}).get("successful_turns", 0),
            "evidence_grade": (debate.get("evidence_grade") or {}).get("grade", ""),
        })
        body_obj = debate_arena.debate_body(req.get("model") or "bio-debate-arena", debate)
        kg_event = _maybe_ingest_kg(req, body_obj, task_id, config, "debate_arena")
        if kg_event:
            route_events.append(kg_event)
        if debate.get("structured_debate_record", {}).get("successful_turns", 0) <= 0:
            final_failure = fallback_policy.Failure(
                fallback_policy.QUALITY_GATE_FAIL,
                502,
                "scientific debate produced no successful turns",
            )
            ledger.write(_ledger_entry(task_id, attempts, final_failure.kind, route_events, mode, policy))
            return UltraResult(True, 502, fallback_policy.error_body(final_failure), task_id, attempts, {"events": route_events})
        ledger.write(_ledger_entry(task_id, attempts, "debate_pass", route_events, mode, policy))
        return UltraResult(True, 200, body_obj, task_id, attempts, {"events": route_events})

    idx = 0
    while idx < len(contexts) and len(attempts) < policy.max_attempts:
        ctx = contexts[idx]
        pre = preflight_guard(req, ctx, config)
        if pre.kind != fallback_policy.OK:
            attempts.append(_attempt(ctx, req, 400, pre))
            final_failure = pre
            break

        status, body_obj, failure, target_model = call_once(req, ctx, http_post)
        sub: Dict[str, Any] = {}
        if status == 200 and isinstance(body_obj, dict):
            sub = run_subagents(req, body_obj, task_id, mode=mode)
            failure = quality_gate(req, body_obj, task_id, sub)
        attempts.append(_attempt(ctx, req, status, failure, target_model, subagents=sub))
        final_failure = failure

        if failure.kind == fallback_policy.OK:
            kg_event = _maybe_ingest_kg(
                req,
                body_obj,
                task_id,
                config,
                f"ultra:{ctx.get('profile_id') or ctx.get('profile_name') or 'active'}",
            )
            if kg_event:
                route_events.append(kg_event)
            ledger.write(_ledger_entry(task_id, attempts, "pass", route_events, mode, policy))
            log_fn(f"  <- ultra OK task={task_id} attempts={len(attempts)} final={ctx.get('profile_name')}")
            return UltraResult(True, 200, body_obj, task_id, attempts, {"events": route_events})

        remaining_budget = policy.max_attempts - len(attempts)
        if remaining_budget <= 0 or not fallback_policy.should_fallback(failure, remaining_budget, policy):
            break

        next_plan = task_router.route_plan(config, task_id, active_ctx, failure_kind=failure.kind)
        route_events.append(_ledger_route_plan(next_plan))
        next_contexts, next_sensitive_skips = _filter_sensitive_contexts(
            req, list(next_plan.get("contexts") or []), config)
        if next_sensitive_skips:
            route_events.append({"kind": "sensitive_filter", "skipped": next_sensitive_skips})

        attempted_keys = {_context_key(c) for c in contexts[:idx + 1]}
        reordered: List[Dict[str, Any]] = []
        queued_keys = set()
        for new_ctx in next_contexts + contexts[idx + 1:]:
            key = _context_key(new_ctx)
            if key not in attempted_keys and key not in queued_keys:
                reordered.append(new_ctx)
                queued_keys.add(key)
        contexts = contexts[:idx + 1] + reordered
        if not reordered:
            break
        log_fn(f"  ~ ultra fallback task={task_id} failure={failure.kind} next_attempt={idx + 2}")
        idx += 1

    ledger.write(_ledger_entry(task_id, attempts, final_failure.kind, route_events, mode, policy))
    return UltraResult(
        True,
        _failure_http_status(final_failure),
        fallback_policy.error_body(final_failure),
        task_id,
        attempts,
        {"events": route_events},
    )


def call_once(req: Dict[str, Any], ctx: Dict[str, Any],
              http_post: Callable[..., Tuple[bytes, str]]) -> Tuple[int, Dict[str, Any], fallback_policy.Failure, str]:
    upstream, compat_ctx = anthropic_compat.transform_request(req, _provider_state(ctx))
    target_model = compat_ctx.target_model
    try:
        if ctx.get("provider") == "openai-responses":
            omit_tools = {"web_search"} if "dashscope.aliyuncs.com" in (ctx.get("url") or "") else set()
            payload = anthropic_compat.anthropic_to_openai_responses(
                upstream,
                target_model,
                max_output_tokens=upstream.get("max_tokens"),
                omit_tool_names=omit_tools,
            )
            raw, _ct = http_post(ctx["url"], json.dumps(payload).encode(), auth_headers(ctx))
            body = anthropic_compat.openai_responses_to_anthropic(
                json.loads(raw),
                req.get("model") or target_model,
                default_id="msg_ultra_proxy",
            )
        elif ctx.get("mode") == "openai" or ctx.get("provider") == "qwen":
            payload = anthropic_compat.anthropic_to_openai(
                upstream,
                target_model,
                upstream.get("max_tokens"),
            )
            raw, _ct = http_post(ctx["url"], json.dumps(payload).encode(), auth_headers(ctx))
            body = openai_to_anthropic(json.loads(raw), req.get("model") or target_model)
        else:
            payload = upstream
            raw, _ct = http_post(ctx["url"], json.dumps(payload).encode(), auth_headers(ctx))
            body = json.loads(raw)
        return 200, body, fallback_policy.Failure(fallback_policy.OK, 200, ""), target_model
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        f = fallback_policy.classify_status(e.code, detail)
        return e.code, fallback_policy.error_body(f), f, target_model
    except Exception as e:  # noqa: BLE001
        f = fallback_policy.classify_status(None, "", e)
        return 0, fallback_policy.error_body(f), f, target_model


def _failure_http_status(failure: fallback_policy.Failure) -> int:
    status = int(failure.status or 0)
    if 400 <= status <= 599:
        return status
    return 502


def resolve_model(name: Optional[str], ctx: Dict[str, Any]) -> str:
    return provider_policy.resolve_model(name, _provider_state(ctx))


def _provider_state(ctx: Dict[str, Any]) -> provider_policy.ProviderState:
    provider = str(ctx.get("provider") or "deepseek")
    definition = provider_registry.PROVIDERS.get(provider)
    if definition is None:
        fallback_name = "openai-custom" if ctx.get("mode") == "openai" else "relay"
        definition = provider_registry.PROVIDERS[fallback_name]
    return provider_policy.ProviderState(
        policy=provider_policy.policy_from_prov(definition),
        prov_name=provider,
        relay_force_model=ctx.get("model") or None,
        relay_models=(),
        relay_thinking=ctx.get("thinking_policy") or None,
        shim_mode="off",
    )


def openai_to_anthropic(resp: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    return anthropic_compat.openai_to_anthropic(
        resp, model_id, default_id="msg_ultra_proxy")


def auth_headers(ctx: Dict[str, Any]) -> Dict[str, str]:
    style = ctx.get("auth_style", "x-api-key")
    key = ctx.get("key", "")
    headers = {"Content-Type": "application/json"}
    if ctx.get("mode") != "openai":
        headers["anthropic-version"] = "2023-06-01"
    if style in ("x-api-key", "both"):
        headers["x-api-key"] = key
    if style in ("bearer", "both"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def clamp_max_tokens(v: Any, ctx: Dict[str, Any], model: str) -> Any:
    return provider_policy.clamp_max_tokens(v, model, _provider_state(ctx))


def _tool_result_text(req: Dict[str, Any]) -> str:
    parts = []
    for msg in req.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _filter_sensitive_contexts(req: Dict[str, Any], contexts: List[Dict[str, Any]],
                               config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not (config.get("sensitive_mode") and request_has_phi(req)):
        return contexts, []
    allowed = config.get("local_endpoint_hosts") or []
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for ctx in contexts:
        if task_router.is_local_or_allowed(ctx, allowed):
            kept.append(ctx)
        else:
            skipped.append({
                "profile_id": ctx.get("profile_id", ""),
                "profile_name": ctx.get("profile_name", ""),
                "provider": ctx.get("provider", ""),
                "host": task_router.host_from_context(ctx),
                "reason": "sensitive_mode_phi_requires_local_or_allowed_host",
            })
    return kept, skipped


def _context_key(ctx: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(ctx.get("profile_id") or ""),
        str(ctx.get("provider") or ""),
        str(ctx.get("url") or ""),
        str(ctx.get("model") or ""),
    )


def _attempt(ctx: Dict[str, Any], req: Dict[str, Any], status: int,
             failure: fallback_policy.Failure, model: Optional[str] = None,
             subagents: Optional[Dict[str, Any]] = None) -> Attempt:
    return Attempt(
        profile_id=ctx.get("profile_id", ""),
        profile_name=ctx.get("profile_name", ""),
        provider=ctx.get("provider", ""),
        model=model or resolve_model(req.get("model"), ctx),
        status=status,
        outcome=failure.kind,
        reason=failure.reason,
        route_source=ctx.get("_route_source", ""),
        probes=ctx.get("_probe_results", []),
        subagents=subagents or {},
    )


def _ledger_route_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "route_plan",
        "task_id": plan.get("task_id", ""),
        "failure_kind": plan.get("failure_kind", ""),
        "required_probes": plan.get("required_probes", []),
        "candidates": plan.get("candidates", []),
        "skipped": plan.get("skipped", []),
        "policy": plan.get("policy", {}),
    }


def _policy_for_ledger(policy: fallback_policy.Policy) -> Dict[str, Any]:
    return {
        "max_attempts": policy.max_attempts,
        "fallback_on": sorted(policy.fallback_on),
        "stop_on": sorted(policy.stop_on),
        "failure_routes": policy.failure_routes,
    }


def _maybe_ingest_kg(req: Dict[str, Any], resp: Dict[str, Any], task_id: str,
                     config: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    try:
        if request_has_phi(req):
            if knowledge_ingest.auto_ingest_enabled(config):
                return {"kind": "knowledge_graph_ingest", "skipped": "phi_detected"}
            return None
        return knowledge_ingest.ingest_exchange(req, resp, task_id, source, config)
    except Exception as e:  # noqa: BLE001
        return {"kind": "knowledge_graph_ingest", "error": str(e)[:200]}


def _ledger_entry(task_id: str, attempts: List[Attempt], final: str,
                  route_events: List[Dict[str, Any]], mode: str,
                  policy: fallback_policy.Policy) -> Dict[str, Any]:
    return {
        "request_id": f"ultra_{id(attempts) & 0xffffff:x}",
        "mode": mode,
        "task_id": task_id,
        "final_outcome": final,
        "policy": _policy_for_ledger(policy),
        "route_events": route_events,
        "attempts": [a.__dict__ for a in attempts],
    }
