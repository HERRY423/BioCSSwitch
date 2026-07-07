"""WellFallback policy primitives for BioCSSwitch Ultra.

This module is intentionally pure and small: it classifies failures, decides
whether fallback is allowed, and writes a redacted local ledger. It does not
know how to call upstream models.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set


OK = "ok"
AUTH_ERROR = "auth_error"
RATE_LIMIT = "rate_limit"
TRANSPORT_ERROR = "transport_error"
CONTEXT_OVERFLOW = "context_overflow"
INVALID_REQUEST = "invalid_request"
MODEL_UNAVAILABLE = "model_unavailable"
PROVIDER_OVERLOADED = "provider_overloaded"
TOOL_USE_DEGRADED = "tool_use_degraded"
JSON_UNSTABLE = "json_unstable"
QUALITY_GATE_FAIL = "quality_gate_fail"
SENSITIVE_VIOLATION = "sensitive_violation"
UPSTREAM_ERROR = "upstream_error"

STOP_KINDS = {AUTH_ERROR, SENSITIVE_VIOLATION, INVALID_REQUEST}
FALLBACK_KINDS = {
    RATE_LIMIT,
    TRANSPORT_ERROR,
    CONTEXT_OVERFLOW,
    MODEL_UNAVAILABLE,
    PROVIDER_OVERLOADED,
    TOOL_USE_DEGRADED,
    JSON_UNSTABLE,
    QUALITY_GATE_FAIL,
    UPSTREAM_ERROR,
}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bcr_[A-Za-z0-9_\-]{8,}\b"),
]


@dataclass
class Failure:
    kind: str
    status: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class Policy:
    max_attempts: int
    fallback_on: Set[str]
    stop_on: Set[str]
    failure_routes: Dict[str, Any]


def redact(value: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Redact likely secrets in strings/lists/dicts before writing ledgers."""
    if isinstance(value, dict):
        return {k: redact(v, extra_secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, extra_secrets) for v in value]
    if not isinstance(value, str):
        return value

    out = value
    for secret in extra_secrets:
        if secret:
            out = out.replace(secret, "****")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "****", out)
    return out


def classify_status(status: Optional[int], body: Any = "", exc: Optional[BaseException] = None) -> Failure:
    """Classify an upstream result into a WellFallback failure kind."""
    if exc is not None:
        msg = str(exc)
        reason = "upstream timeout" if "timed out" in msg.lower() or "timeout" in msg.lower() else msg
        return Failure(TRANSPORT_ERROR, status, reason)
    if status is None or status == 0:
        return Failure(TRANSPORT_ERROR, status, "no upstream response")
    if 200 <= int(status) < 300:
        return Failure(OK, status, "")
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    low = text.lower()
    if status in (401, 403):
        return Failure(AUTH_ERROR, status, "upstream rejected credentials")
    if status == 429 or any(x in low for x in ("rate limit", "too many requests", "quota exceeded")):
        return Failure(RATE_LIMIT, status, "rate limited")
    if status in (408, 504):
        return Failure(TRANSPORT_ERROR, status, "upstream timeout")
    if status in (400, 413, 422) and any(
        x in low for x in ("context", "token", "max_tokens", "too long", "length", "payload too large")
    ):
        return Failure(CONTEXT_OVERFLOW, status, "context or token limit")
    if status in (400, 404, 422) and any(
        x in low for x in ("model", "deployment", "not found", "does not exist", "unsupported")
    ):
        return Failure(MODEL_UNAVAILABLE, status, "model or deployment unavailable")
    if status == 400:
        return Failure(INVALID_REQUEST, status, "invalid upstream request")
    if status in (503, 529) or any(x in low for x in ("overloaded", "temporarily unavailable", "capacity")):
        return Failure(PROVIDER_OVERLOADED, status, "provider overloaded or unavailable")
    if 500 <= int(status) <= 599:
        return Failure(UPSTREAM_ERROR, status, "upstream server error")
    return Failure(UPSTREAM_ERROR, status, f"upstream HTTP {status}")


def request_forces_tool(req: Dict[str, Any]) -> bool:
    tc = req.get("tool_choice")
    return isinstance(tc, dict) and tc.get("type") in ("any", "tool")


def response_has_tool_use(resp: Dict[str, Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in resp.get("content") or []
    )


def response_text(resp: Dict[str, Any]) -> str:
    parts = []
    for block in resp.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def classify_quality(req: Dict[str, Any], resp: Dict[str, Any], task_id: str = "") -> Failure:
    """Run cheap local quality gates that are safe to apply before fallback."""
    if request_forces_tool(req) and not response_has_tool_use(resp):
        return Failure(TOOL_USE_DEGRADED, 200, "forced tool request returned no tool_use block")

    text = response_text(resp).strip()
    wants_json = task_id == "evidence-check" or "return only compact json" in _request_text(req).lower()
    if wants_json and text:
        try:
            json.loads(text)
        except Exception:
            return Failure(JSON_UNSTABLE, 200, "response text is not strict JSON")

    return Failure(OK, 200, "")


def policy_for(config: Optional[Dict[str, Any]], task_id: str) -> Policy:
    """Build the effective fallback policy from global and task config."""
    config = config or {}
    ultra = config.get("ultra") or {}
    task_policies = ultra.get("task_policies") or {}
    raw: Dict[str, Any] = {}
    for src in (ultra.get("default_policy") or {}, task_policies.get(task_id) or {}):
        if isinstance(src, dict):
            raw.update(src)

    def _set(name: str, default: Set[str]) -> Set[str]:
        value = raw.get(name)
        if value is None:
            return set(default)
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {str(x) for x in value}
        return set(default)

    try:
        max_attempts = int(raw.get("max_attempts", ultra.get("max_attempts", 3)))
    except Exception:
        max_attempts = 3
    return Policy(
        max_attempts=max(1, min(max_attempts, 8)),
        fallback_on=_set("fallback_on", FALLBACK_KINDS),
        stop_on=_set("stop_on", STOP_KINDS),
        failure_routes=raw.get("failure_routes") if isinstance(raw.get("failure_routes"), dict) else {},
    )


def should_fallback(failure: Failure, remaining_attempts: int, policy: Optional[Policy] = None) -> bool:
    if remaining_attempts <= 0:
        return False
    policy = policy or Policy(3, set(FALLBACK_KINDS), set(STOP_KINDS), {})
    if failure.kind in policy.stop_on:
        return False
    return failure.kind in policy.fallback_on


def error_body(failure: Failure) -> Dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": failure.reason or failure.kind,
            "csswitch_failure_kind": failure.kind,
            "status": failure.status,
        },
    }


class FallbackLedger:
    def __init__(self, path: Optional[str], extra_secrets: Iterable[str] = ()):
        self.path = path
        self.extra_secrets = tuple(extra_secrets or ())

    def write(self, entry: Dict[str, Any]) -> None:
        if not self.path:
            return
        safe = redact({"ts": int(time.time() * 1000), **entry}, self.extra_secrets)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")


def _request_text(req: Dict[str, Any]) -> str:
    parts = []
    for msg in req.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)
