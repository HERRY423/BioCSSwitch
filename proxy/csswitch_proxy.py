#!/usr/bin/env python3
"""CSSwitch 代理：把 Claude Science 的推理转发到第三方模型（provider 可切）。

Providers:
  deepseek (默认)：https://api.deepseek.com/anthropic —— DeepSeek 原生 Anthropic 端点，
                   代理只做「透传 + 改模型名 + 换鉴权头 + max_tokens 夹取 + 连接重试」，
                   thinking/tool_use 全部原生保真（不翻译协议）。
  qwen           ：DashScope compatible-mode —— Anthropic↔OpenAI 双向翻译（流式以 SSE 回放保真 tool_use）。
  openai-custom  ：任意 OpenAI Chat Completions 兼容端点，base_url + token + model。
                   代理拼 /chat/completions 与 /models，并复用 qwen 的 Anthropic↔OpenAI 翻译链。
  openai-responses：任意 OpenAI Responses 兼容端点，base_url + token + model。
                   代理拼 /responses 与 /models，并做 Anthropic↔Responses 翻译。
  relay          ：任意「中转站」（Anthropic 兼容端点，base_url + token）。原生透传、【不重映射模型】，
                   /v1/models 回源直拉让 Science 选择器自动铺满中转站真实模型。base_url 经
                   CSSWITCH_RELAY_BASE_URL 提供、token 经 CSSWITCH_RELAY_KEY 提供。

安全约束：
  - 入站 Authorization / x-api-key（Science 带来的 OAuth Bearer）一律剥离，不记录、不转发。
  - 上游只用本地环境变量里的 provider key，值只驻内存，不打印、不写日志。
  - 只监听回环地址；除所选 provider 端点外不外连。

用法：
  DEEPSEEK_API_KEY=... python3 csswitch_proxy.py --provider deepseek --port 18991
  DASHSCOPE_API_KEY=... python3 csswitch_proxy.py --provider qwen --port 18991
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import logging.handlers
import os
import queue
import re
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import dsml_shim
import task_router
import ultra_orchestrator
import provider_policy
import anthropic_compat
import provider_registry

# ---------- provider 注册表 ----------
PROVIDERS = provider_registry.PROVIDERS

LOGGER = logging.getLogger("csswitch.proxy")

@dataclass
class RequestContext:
    """One proxy instance's immutable configuration and request-shared caches.

    The server owns this object and handlers obtain it from ``self.server``.
    This removes the former process-wide PROV/KEY/RELAY_* mutation, keeps tests
    isolated, and makes concurrent proxy instances safe inside one interpreter.
    """

    prov_name: str
    prov: dict[str, Any]
    key: str
    auth_secret: str | None = None
    shim_mode: str = "off"
    ultra_mode: str = "off"
    ultra_ledger: str | None = None
    relay_models: list[str] = field(default_factory=list)
    relay_force_model: str | None = None
    relay_thinking: str | None = None
# 出站 User-Agent：部分中转站的 WAF 把默认的 "Python-urllib/x.y" 判为 bot 直接 403
# （byteswarm 实测），故所有上游请求统一带一个非 bot 的 UA。
UPSTREAM_UA = "CSSwitch/0.2 (+https://github.com/SuperJJ007/CSSwitch)"
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    OSError,
    http.client.HTTPException,
)

# ---------- #3: targeted fast-fail（沙箱「Switching organization」卡死修复） ----------
# 沙箱 Science 启动时会对 claude.ai/api/oauth/profile 发【阻塞式】请求解析组织；
# 在到不了 claude.ai 的网络上超时重试 → UI 卡在 "Switching organization"。
# 起沙箱时把 http(s)_proxy 指向本代理（见 launch-virtual-sandbox.sh），do_CONNECT
# 对下列 Anthropic 域名的 CONNECT 立即短路，其余域名正常隧道透传（保留装包 / MCP 等外联）。
# 推理仍走 127.0.0.1（no_proxy 直连本地）。
#
# 【为何回 401 而非 403】operon 的 claudeAiFetch 读的是 CONNECT 的状态码：
#   - 401 Unauthorized = 「你没登录」→ operon 打日志 `treating as logged-out` 并秒过（实测/根因确认）。
#   - 403 Forbidden    = 「登录了但没权限」→ operon 当成组织/权限问题【反复重试】→ 一直卡
#     "Switching organization"（v0.1.4 实机复现：server 日志固定 `/api/oauth/profile → 403`，
#     无 `treating as logged-out`）。
# 虚拟登录本就该表现为「未登录」，故用 401。
_BLOCKED_SUFFIXES = ("anthropic.com", "claude.ai", "claude.com")


def _is_blocked_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in _BLOCKED_SUFFIXES)


def configure_logging(log_path: str | None = None) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    LOGGER.addHandler(stream)

    if log_path:
        parent = os.path.dirname(os.path.abspath(log_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        LOGGER.addHandler(file_handler)


def log(msg: str, level: int = logging.INFO) -> None:
    if not LOGGER.handlers:
        configure_logging()
    LOGGER.log(level, msg)


def load_key(prov: dict[str, Any], args: argparse.Namespace) -> str | None:
    env = prov["key_env"]
    if os.environ.get(env):
        return os.environ[env].strip()
    if args.env_file and os.path.isfile(args.env_file):
        for raw in open(args.env_file):
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            if k.strip() == env:
                return v.strip().strip('"').strip("'")
    return None


def _provider_state(ctx: RequestContext) -> provider_policy.ProviderState:
    """Snapshot one server context so a request observes one coherent policy."""
    return provider_policy.ProviderState(
        policy=provider_policy.policy_from_prov(ctx.prov),
        prov_name=ctx.prov_name,
        relay_force_model=ctx.relay_force_model,
        relay_models=tuple(ctx.relay_models),
        relay_thinking=ctx.relay_thinking,
        shim_mode=ctx.shim_mode,
    )


def http_post(
    url: str,
    data: bytes,
    headers: dict[str, str],
    attempts: int = 4,
    timeout: float = 300,
) -> tuple[bytes, str]:
    """POST 上游；重试覆盖【连接 + 完整读体】（含 SSL EOF、握手超时、对端断开、IncompleteRead），
    对服务端明确响应（HTTPError，如 400）不重试。返回 (body_bytes, content_type)。"""
    headers = {"User-Agent": UPSTREAM_UA, **headers}
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError:
            raise
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            if i < attempts - 1:
                log(f"  ~ 上游连接抖动，重试 {i + 1}/{attempts - 1}: {e}")
                time.sleep(0.8 * (i + 1))
                continue
            raise


def open_stream(
    url: str,
    data: bytes,
    headers: dict[str, str],
    attempts: int = 4,
    timeout: float = 300,
) -> tuple[Any, bytes, str]:
    """打开上游流式连接并预读首行（把「200 但立刻空体」这种抖动也纳入重试）。
    返回 (resp, first_chunk, content_type)；首字节到手后不再重试。"""
    headers = {"User-Agent": UPSTREAM_UA, **headers}
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            first = r.readline(65536)
            if not first:
                r.close()
                raise ConnectionError("上游 200 但立刻空体")
            return r, first, r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError:
            raise
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            if i < attempts - 1:
                log(f"  ~ 上游连接抖动，重试 {i + 1}/{attempts - 1}: {e}")
                time.sleep(0.8 * (i + 1))
                continue
            raise


def _open_stream_with_keepalive(
    write_chunk: Callable[[bytes], None],
    url: str,
    data: bytes,
    headers: dict[str, str],
) -> tuple[Any, bytes, str]:
    """等待上游首帧时持续给下游发 SSE 注释心跳。

    下游主请求可能带大量工具定义；部分上游首帧 TTFT 较长时，如果下游在这段
    时间完全收不到 body 字节，会先断开并重试。注释帧是合法 SSE，客户端应忽略内容，
    但能证明连接仍活着。"""
    q = queue.Queue(maxsize=1)

    def _open():
        try:
            q.put(("ok", open_stream(url, data, headers)))
        except BaseException as e:
            q.put(("err", e))

    threading.Thread(target=_open, daemon=True).start()
    keepalive = b": csswitch-keepalive\n\n"
    while True:
        try:
            kind, payload = q.get(timeout=1.0)
            if kind == "err":
                raise payload
            return payload
        except queue.Empty:
            write_chunk(keepalive)


def http_get_json(
    url: str,
    headers: dict[str, str],
    attempts: int = 3,
    timeout: float = 30,
) -> Any:
    """GET 上游并解析 JSON（relay 回源拉 /v1/models 用）。连接抖动重试，服务端明确响应不重试。"""
    headers = {"User-Agent": UPSTREAM_UA, **headers}
    for i in range(attempts):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            if i < attempts - 1:
                log(f"  ~ 上游连接抖动，重试 {i + 1}/{attempts - 1}: {e}")
                time.sleep(0.6 * (i + 1))
                continue
            raise


def normalize_openai_base(base: str | None) -> str:
    """OpenAI 兼容端点存 base root。用户若误填到 /chat/completions 或 /models，
    这里收敛回 root，避免后续双拼。"""
    b = (base or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions",
                   "/v1/responses", "/responses", "/v1/models", "/models"):
        if b.endswith(suffix):
            b = b[: -len(suffix)].rstrip("/")
    return b


def _ends_with_version_segment(base: str) -> bool:
    return bool(re.search(r"/v\d+(?:\.\d+)?$", base))


def openai_endpoint(base: str, suffix: str) -> str:
    root = normalize_openai_base(base)
    if not _ends_with_version_segment(root):
        root = root + "/v1"
    return root + suffix


def _upstream_auth_headers(ctx: RequestContext) -> dict[str, str]:
    """上游鉴权头：按当前 provider 的 auth_style 装 x-api-key / bearer / both。
    deepseek 未设 → 默认 x-api-key（保持原状）；relay = both。"""
    style = ctx.prov.get("auth_style", "x-api-key")
    h = {}
    if style in ("x-api-key", "both"):
        h["x-api-key"] = ctx.key
    if style in ("bearer", "both"):
        h["Authorization"] = f"Bearer {ctx.key}"
    return h


def fetch_relay_models(ctx: RequestContext) -> list[dict[str, Any]]:
    """回源拉上游模型列表，归一化成 Science 可消费的模型列表。relay 会刷新
    RELAY_MODELS 缓存（供 resolve_model 贴合）；openai-custom 仅用于模型发现。返回 list（可空）。"""
    murl = ctx.prov.get("models_url")
    if not murl:
        return []
    headers = dict(_upstream_auth_headers(ctx))
    if ctx.prov_name == "relay":
        headers["anthropic-version"] = "2023-06-01"
    raw = http_get_json(murl, headers)
    data = raw.get("data") if isinstance(raw, dict) else raw
    out, ids = [], []
    for m in data or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid:
            continue
        ids.append(mid)
        # 能力位：从上游 supported_parameters 推断，绝不臆测（无该字段 → None）。
        sp = m.get("supported_parameters") if isinstance(m, dict) else None
        supports_tools = ("tools" in sp) if isinstance(sp, list) else None
        out.append({"type": "model", "id": mid,
                    "display_name": (m.get("display_name") if isinstance(m, dict) else None) or mid,
                    "supports_tools": supports_tools,
                    "created_at": "2026-01-01T00:00:00Z"})
    if ids and ctx.prov_name == "relay":
        ctx.relay_models[:] = ids
    return out


def build_models_response(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
    """装配 /v1/models 响应，返回 (状态码, body dict)。协议锁定（修评审 P2-2）：
      - relay/openai-custom 回源成功 → (200, {data:[…含 supports_tools…]})。
      - 回源 HTTPError → (上游同状态码, {error_kind:"upstream", upstream_status, message})，
        绝不吞成 200+静态（否则掩盖坏 key）。builtin 兜底交 Rust 命令决定。
      - 网络异常 → (502, {error_kind:"network", upstream_status:None, message})。
      - 非 relay（无 models_url，deepseek/qwen）→ (200, {静态选择器列表})，行为不变。"""
    if ctx.prov.get("models_url"):
        if ctx.relay_force_model:
            # force（Science 常驻代理）：只返回一个壳，Science 主列表显示真实模型名。
            # 出站由 resolve_model 的 force 分支覆盖，无需 model_map。app 的 fetch_models
            # 不设 RELAY_FORCE_MODEL，故仍走下面回源拿真实 id 供用户选（两个消费者切分）。
            shell = [{"type": "model", "id": "claude-opus-4-8",
                      "display_name": ctx.relay_force_model, "supports_tools": None,
                      "created_at": "2026-01-01T00:00:00Z"}]
            log(f"GET /v1/models -> {ctx.prov_name}(force 借壳): {ctx.relay_force_model}")
            return 200, {"data": shell, "has_more": False,
                         "first_id": "claude-opus-4-8", "last_id": "claude-opus-4-8"}
        try:
            data = fetch_relay_models(ctx)
            log(f"GET /v1/models -> {ctx.prov_name}(回源): {len(data)} 个模型")
            return 200, {"data": data, "has_more": False,
                         "first_id": data[0]["id"] if data else None,
                         "last_id": data[-1]["id"] if data else None}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            log(f"GET /v1/models -> {ctx.prov_name} 回源 HTTP {e.code}（保留状态码，不回静态）")
            return e.code, {"error_kind": "upstream", "upstream_status": e.code,
                            "message": f"upstream {e.code}: {detail}"}
        except Exception as e:
            log(f"GET /v1/models -> {ctx.prov_name} 回源网络异常，本地回 502: {e}")
            return 502, {"error_kind": "network", "upstream_status": None, "message": str(e)}
    # 非 relay：静态选择器列表（deepseek/qwen）。
    data = [{"type": "model", "id": mid, "display_name": disp, "supports_tools": None,
             "created_at": "2026-01-01T00:00:00Z"} for mid, disp in ctx.prov["models"]]
    return 200, {"data": data, "has_more": False,
                 "first_id": data[0]["id"] if data else None,
                 "last_id": data[-1]["id"] if data else None}


# ---------- Anthropic -> OpenAI 翻译（qwen 路径） ----------
def anthropic_to_openai(req: dict[str, Any], ctx: RequestContext) -> dict[str, Any]:
    state = _provider_state(ctx)
    target_model = provider_policy.resolve_model(req.get("model"), state)
    max_tokens = provider_policy.clamp_max_tokens(
        req.get("max_tokens"), target_model, state)
    return anthropic_compat.anthropic_to_openai(req, target_model, max_tokens)


def map_tool_choice(tc: Any, tools: list[dict[str, Any]] | None) -> Any:
    return anthropic_compat.map_tool_choice(tc, tools)


def map_responses_tool_choice(tc: Any, tools: list[dict[str, Any]] | None) -> str | None:
    """把 Anthropic tool_choice 译成 Responses API 取值。
    千问 Responses 在 thinking mode 下会拒绝 required/object 形态；Science 会在
    reviewing/agent 路径发送强制工具选择。这里保守降级为 auto，宁可不强制工具，
    也不要让整个请求 400。"""
    has_tools = bool(tools)
    if isinstance(tc, str):
        t = tc
    elif isinstance(tc, dict):
        t = tc.get("type")
    else:
        t = None
    if t == "auto":
        return "auto"
    if t == "none":
        return "none"
    if has_tools:
        return "auto"
    return None


def responses_max_output_tokens(
    req: dict[str, Any],
    model: str,
    state: provider_policy.ProviderState,
    has_tools: bool,
    ctx: RequestContext,
) -> int | None:
    value = provider_policy.clamp_max_tokens(req.get("max_tokens"), model, state)
    if not value:
        return value
    # DashScope Responses accepts 65536 for simple prompts, but real Science tool
    # requests with very large inbound max_tokens can still fail its compatibility
    # layer. Keep tool calls on the conservative budget used by the existing Qwen
    # chat path.
    if has_tools and "dashscope.aliyuncs.com" in (ctx.prov.get("url") or ""):
        return min(int(value), 8192)
    return value


def _is_dashscope_responses(ctx: RequestContext) -> bool:
    return (
        ctx.prov.get("api_format") == "openai_responses"
        and "dashscope.aliyuncs.com" in (ctx.prov.get("url") or "")
    )


def normalize_responses_tool_parameters(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if "properties" in out and not out.get("type"):
        out["type"] = "object"
    if out.get("type") != "object":
        return {"type": "object", "properties": {}}
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    return out


def map_responses_tools(
    tools: list[dict[str, Any]] | None, ctx: RequestContext
) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        name = t.get("name")
        if not name:
            continue
        # DashScope Responses currently rejects this function schema in
        # compatibility mode; omit it rather than failing the entire request.
        if _is_dashscope_responses(ctx) and name == "web_search":
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": t.get("description", ""),
            "parameters": normalize_responses_tool_parameters(t.get("input_schema", {})),
        })
    return out


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(x.get("text", "") for x in value if isinstance(x, dict))
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def anthropic_to_openai_responses(
    req: dict[str, Any], ctx: RequestContext
) -> dict[str, Any]:
    sys_prompt = req.get("system")
    if isinstance(sys_prompt, list):
        sys_prompt = "\n".join(b.get("text", "") for b in sys_prompt if isinstance(b, dict))

    items = []
    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue

        text_parts = []
        for blk in content or []:
            t = blk.get("type")
            if t == "text":
                text_parts.append(blk.get("text", ""))
            elif t == "tool_use":
                if text_parts:
                    items.append({"role": role, "content": "".join(text_parts)})
                    text_parts = []
                items.append({
                    "type": "function_call",
                    "call_id": blk.get("id"),
                    "name": blk.get("name"),
                    "arguments": json.dumps(blk.get("input", {}), ensure_ascii=False),
                })
            elif t == "tool_result":
                if text_parts:
                    items.append({"role": role, "content": "".join(text_parts)})
                    text_parts = []
                items.append({
                    "type": "function_call_output",
                    "call_id": blk.get("tool_use_id"),
                    "output": _as_text(blk.get("content")),
                })
        if text_parts:
            items.append({"role": role, "content": "".join(text_parts)})

    _st = _provider_state(ctx)
    out = {
        "model": provider_policy.resolve_model(req.get("model"), _st),
        "input": items,
        "stream": False,
    }
    if sys_prompt:
        out["instructions"] = sys_prompt
    tools = map_responses_tools(req.get("tools"), ctx)
    max_output_tokens = responses_max_output_tokens(
        req, out["model"], _st, bool(tools), ctx
    )
    if max_output_tokens:
        out["max_output_tokens"] = max_output_tokens
    if req.get("temperature") is not None:
        out["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        out["top_p"] = req["top_p"]
    if tools:
        out["tools"] = tools
    tcm = map_responses_tool_choice(req.get("tool_choice"), tools)
    if tcm is not None:
        out["tool_choice"] = tcm
    return out


def openai_to_anthropic(resp: dict[str, Any], model_id: str) -> dict[str, Any]:
    return anthropic_compat.openai_to_anthropic(resp, model_id)


def _responses_output_text(item: dict[str, Any]) -> str:
    parts = []
    for c in item.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") in ("output_text", "text"):
            parts.append(c.get("text", ""))
    return "".join(parts)


def openai_responses_to_anthropic(resp: dict[str, Any], model_id: str) -> dict[str, Any]:
    blocks = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "message":
            text = _responses_output_text(item)
            if text:
                blocks.append({"type": "text", "text": text})
        elif t == "function_call":
            raw_args = item.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id"),
                "name": item.get("name"),
                "input": args,
            })
    if not blocks and resp.get("output_text"):
        blocks.append({"type": "text", "text": resp.get("output_text", "")})
    usage = resp.get("usage", {})
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    if resp.get("status") == "incomplete":
        stop = "max_tokens"
    return {
        "id": resp.get("id", "msg_proxy"), "type": "message", "role": "assistant", "model": model_id,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("input_tokens", 0),
                  "output_tokens": usage.get("output_tokens", 0)},
    }


class ContextHTTPServer(ThreadingHTTPServer):
    """Threading server carrying all runtime state for its request handlers."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        context: RequestContext,
    ) -> None:
        self.context = context
        super().__init__(server_address, handler_class)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "csswitch-proxy"

    @property
    def ctx(self) -> RequestContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, *a: Any) -> None:
        pass

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            # 主动关闭连接时显式告知客户端，避免其在已关闭的 socket 上复用连接。
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, event: str, data: dict[str, Any]) -> None:
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        self.wfile.write(hex(len(chunk))[2:].encode() + b"\r\n" + chunk + b"\r\n")

    def _sse_error_and_terminate(self, msg: str) -> None:
        frame = ("event: error\ndata: " + json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": msg}},
            ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(hex(len(frame))[2:].encode() + b"\r\n" + frame + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _auth_ok(self) -> bool:
        if not self.ctx.auth_secret:
            return True
        prefix = "/" + self.ctx.auth_secret
        if self.path == prefix or self.path.startswith(prefix + "/"):
            self.path = self.path[len(prefix):] or "/"
            return True
        # 鉴权失败时请求体（POST）尚未读取，若保持长连接，服务端下一轮会从残留
        # body 中间开始解析下一个请求，产出的畸形 400 错误页会把残留字节和下一条
        # 请求行拼在一起回显给客户端，可能带出路径里的 secret。这里主动关连接
        # 阻断该复用路径；_send_json 会据 close_connection 追加 Connection: close。
        self.close_connection = True
        self._send_json(403, {"type": "error", "error": {
            "type": "permission_error", "message": "forbidden"}})
        return False

    def do_GET(self) -> None:
        if not self._auth_ok():
            return
        if self.path.startswith("/v1/models"):
            code, body = build_models_response(self.ctx)
            self._send_json(code, body)
        elif self.path.startswith("/health"):
            self._send_json(200, {"status": "ok", "provider": self.ctx.prov_name})
        else:
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    def do_POST(self) -> None:
        if not self._auth_ok():
            return
        # Content-Length 解析放在保护内：畸形头（如 "oops" / 负数）应回规范 400，
        # 不能让 int() 抛 ValueError 击穿 handler、给客户端一个空响应。
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0:
                raise ValueError("negative length")
        except (ValueError, TypeError):
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": "invalid Content-Length"}})
            return
        raw = self.rfile.read(n) if n else b"{}"
        if not self.path.startswith("/v1/messages"):
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            return
        try:
            areq = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}})
            return
        # 结构校验（修 P1 GPT 复审）：顶层必须是对象且 messages 是数组，否则回规范 400。
        # 否则 []/"hello"/{"messages":null} 会在下游 .get / 迭代处抛 AttributeError/TypeError，
        # 击穿线程 → 客户端拿到空响应而非 400。
        if not isinstance(areq, dict) or not isinstance(areq.get("messages"), list):
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "request body must be a JSON object with a 'messages' array"}})
            return
        _dd = os.environ.get("PROXY_DUMP_REQ")
        if _dd:
            try:
                with open(os.path.join(_dd, f"req_{areq.get('model','x')}_{len(raw)}.json"), "w") as _f:
                    json.dump({"model": areq.get("model"), "thinking": areq.get("thinking"),
                               "tool_choice": areq.get("tool_choice"),
                               "n_tools": len(areq.get("tools") or [])}, _f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        if ultra_orchestrator.ultra_enabled(self.ctx.ultra_mode):
            if self._handle_ultra(areq):
                return
        if self.ctx.prov["mode"] == "anthropic":
            self._handle_anthropic(areq)
        else:
            self._handle_openai(areq)

    # ---- HTTP CONNECT 隧道：Anthropic 域名 fast-fail、其余透传（修 #3） ----
    def do_CONNECT(self) -> None:
        # operon 用 https_proxy 走到这里；self.path 形如 "host:port"。
        # 【为何不走 _auth_ok】CONNECT 把目标放在请求行、没有可嵌 path-secret 的位置，
        # operon 的 https_proxy 也带不上 secret。此处不鉴权的实际风险面很小：
        #   - 只监听回环（127.0.0.1），本机进程本就能自行外连，隧道不给它任何新能力；
        #   - 隧道是裸 TCP 转发，不注入上游 key、不经推理端点（那两条仍受 secret 保护）。
        #   即 path-secret 真正守护的边界（第三方 key + 推理端点）未被削弱。
        # 进一步收紧可让 launch 把 secret 放进 https_proxy 的 userinfo 再校验
        # Proxy-Authorization，但需先实测 operon 是否会带该头（否则误伤透传），留待整链联调。
        target = self.path
        host = target.rsplit(":", 1)[0].strip("[]").lower()
        if _is_blocked_host(host):
            # 401（未登录）而非 403（禁止）：让 operon 判 logged-out 秒过，而非当组织问题反复重试。
            log(f"CONNECT {target} -> 401 未登录（Anthropic 域名 fast-fail）")
            self._connect_reply(401)
            return
        try:
            port = int(target.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            self._connect_reply(400)
            return
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except Exception as e:
            log(f"CONNECT {target} -> 502 上游连不上: {e}")
            self._connect_reply(502)
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass
        log(f"CONNECT {target} -> 隧道建立，透传")
        try:
            self._tunnel(self.connection, upstream)
        finally:
            try:
                upstream.close()
            except Exception:
                pass
        self.close_connection = True

    def _connect_reply(self, code: int) -> None:
        """CONNECT 的短响应（拒绝/错误）：空体 + 主动关连接。"""
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    @staticmethod
    def _tunnel(client: socket.socket, upstream: socket.socket) -> None:
        """在两个已连接 socket 间双向搬字节，直到任一侧 EOF / 出错。"""
        socks = [client, upstream]
        while True:
            try:
                r, _, _ = select.select(socks, [], [])
            except Exception:
                return
            for s in r:
                other = upstream if s is client else client
                try:
                    data = s.recv(65536)
                except Exception:
                    return
                if not data:  # 对端 EOF
                    return
                try:
                    other.sendall(data)
                except Exception:
                    return

    # ---- Ultra：任务路由 + WellFallback + 子代理 guardrails（非流式 v1） ----
    def _handle_ultra(self, areq: dict[str, Any]) -> bool:
        active_ctx = task_router.current_context(
            self.ctx.prov_name,
            self.ctx.prov,
            self.ctx.key,
            self.ctx.relay_force_model,
            self.ctx.relay_thinking,
        )
        cfg = task_router.load_runtime_config()
        result = ultra_orchestrator.handle_request(
            areq,
            active_ctx,
            cfg,
            http_post,
            self.ctx.ultra_ledger,
            log,
            mode=self.ctx.ultra_mode,
        )
        if result is None:
            return False
        self._send_json(result.status, result.body)
        return True

    # ---- DeepSeek：Anthropic 原生透传（改模型名+换鉴权+夹 max_tokens+重试） ----
    def _handle_anthropic(self, areq: dict[str, Any]) -> None:
        state = _provider_state(self.ctx)
        upstream_body, ctx = anthropic_compat.transform_request(areq, state)
        stream = bool(upstream_body.get("stream"))
        n_tools = len(upstream_body.get("tools") or [])
        log(f"POST /v1/messages  {ctx.src_model}->{ctx.target_model} stream={stream} "
            f"tools={n_tools} msgs={len(upstream_body.get('messages') or [])}  "
            f"(入站鉴权已剥离, 直连 {self.ctx.prov_name})")
        # 鉴权头按 provider 的 auth_style（deepseek x-api-key / relay both）。KEY 只驻内存、不入日志。
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        headers.update(_upstream_auth_headers(self.ctx))
        data = json.dumps(upstream_body).encode()
        headers_sent = False
        try:
            if stream:
                # Deliberate stream tradeoff: open the downstream SSE response before
                # upstream TTFT, then send comment keepalives while waiting. This keeps
                # the client from retrying long tool-heavy requests, but upstream
                # HTTP errors after this point must be represented as SSE error frames
                # because the HTTP status line is already committed.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                headers_sent = True

                def _wc(b: bytes) -> None:
                    if b:
                        self.wfile.write(hex(len(b))[2:].encode() + b"\r\n" + b + b"\r\n")
                        self.wfile.flush()

                r, first, _ct = _open_stream_with_keepalive(
                    _wc, self.ctx.prov["url"], data, headers
                )
                with r:
                    # off / 无工具 → None（骨架直接透传，零开销）；detect / rewrite → 统一 filter。
                    f = anthropic_compat.make_stream_rewriter(ctx)
                    # 第一帧同样要过 filter（状态机 / 检测器必须从第 0 字节按序看到全部上游数据）。
                    if f is not None:
                        _wc(f.feed(first))
                    else:
                        _wc(first)
                    while True:
                        try:
                            chunk = r.readline(65536)
                        except Exception as e:
                            log(f"  !! 流中断（头已发），SSE error 收尾: {e}")
                            self._sse_error_and_terminate(str(e))
                            return
                        if not chunk:
                            break
                        if f is not None:
                            _wc(f.feed(chunk))
                        else:
                            _wc(chunk)
                    if f is not None:
                        _wc(f.finalize())
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                st = f.stats() if f is not None else {}
                if st.get("synthesized"):
                    log(f"  <- {self.ctx.prov_name} 流式 DSML 改写 OK（合成 tool_use×{st['tool_n']}）")
                elif st.get("found"):
                    log(f"  <- {self.ctx.prov_name} 流式透传 OK（!! detect：本响应含 DSML 泄漏，未改写）")
                else:
                    log(f"  <- {self.ctx.prov_name} 流式透传 OK")
            else:
                body_bytes, ct = http_post(self.ctx.prov["url"], data, headers)
                body_bytes, stats = anthropic_compat.rewrite_nonstream(body_bytes, ctx)
                if stats.get("rewritten"):
                    log(f"  <- {self.ctx.prov_name} 非流式 DSML 改写 OK（展开 tool_use）")
                elif stats.get("found"):
                    log(f"  !! detect：非流式响应含 DSML 泄漏，未改写")
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                headers_sent = True
                self.wfile.write(body_bytes)
                if "rewritten" not in stats:
                    log(f"  <- {self.ctx.prov_name} 非流式透传 OK")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            log(f"  !! 上游 HTTP {e.code}: {detail}")
            if not headers_sent:
                # 上游鉴权 / 额度类状态码原样透传（401/403/429），其余归一化 502。
                code = e.code if e.code in (401, 403, 429) else 502
                self._send_json(code, {"type": "error", "error": {
                    "type": "api_error", "message": f"upstream {e.code}: {detail}"}})
            else:
                try:
                    self._sse_error_and_terminate(f"upstream {e.code}: {detail}")
                except Exception:
                    pass
        except Exception as e:
            log(f"  !! 代理异常: {e}")
            if headers_sent:
                try:
                    self._sse_error_and_terminate(str(e))
                except Exception:
                    pass
            else:
                self._send_json(502, {"type": "error", "error": {
                    "type": "api_error", "message": str(e)}})

    # ---- Qwen：翻译到 OpenAI，非流式取全再按需 SSE 回放 ----
    def _handle_openai(self, areq: dict[str, Any]) -> None:
        model_id = areq.get("model", "claude-sonnet-5")
        stream = bool(areq.get("stream"))
        if self.ctx.prov.get("api_format") == "openai_responses":
            oreq = anthropic_to_openai_responses(areq, self.ctx)
            msg_count = len(oreq.get("input") or [])
        else:
            oreq = anthropic_to_openai(areq, self.ctx)
            msg_count = len(oreq.get("messages") or [])
        n_tools = len(oreq.get("tools", []))
        log(f"POST /v1/messages  {model_id}->{oreq['model']} stream={stream} tools={n_tools} "
            f"msgs={msg_count}  (入站鉴权已剥离, {self.ctx.prov_name})")
        headers = {
            "Authorization": f"Bearer {self.ctx.key}",
            "Content-Type": "application/json",
        }
        data = json.dumps(oreq).encode()
        try:
            raw, _ct = http_post(self.ctx.prov["url"], data, headers)
            oresp = json.loads(raw)
            if self.ctx.prov.get("api_format") == "openai_responses":
                aresp = openai_responses_to_anthropic(oresp, model_id)
            else:
                aresp = openai_to_anthropic(oresp, model_id)
            if stream:
                self._replay_as_sse(aresp)
            else:
                self._send_json(200, aresp)
            log(f"  <- {self.ctx.prov_name} OK (blocks={len(aresp['content'])} stop={aresp['stop_reason']})")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            log(f"  !! 上游 HTTP {e.code}: {detail}")
            # 修 P2（GPT 复审）：OpenAI 翻译路径（qwen 等）同样保留上游 401/403/429，
            # 别一律归一化 502——否则 verify_key 无法准确提示「key 无效」。其余仍归 502。
            code = e.code if e.code in (401, 403, 429) else 502
            self._send_json(code, {"type": "error", "error": {"type": "api_error",
                                   "message": f"upstream {e.code}: {detail}"}})
        except Exception as e:
            log(f"  !! 代理异常: {e}")
            self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": str(e)}})

    def _replay_as_sse(self, aresp: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        blocks = aresp.get("content") or [{"type": "text", "text": ""}]
        self._sse("message_start", {"type": "message_start", "message": {
            "id": aresp.get("id", "msg_proxy"), "type": "message", "role": "assistant",
            "model": aresp.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": aresp.get("usage", {"input_tokens": 0, "output_tokens": 0})}})
        self._sse("ping", {"type": "ping"})
        for idx, blk in enumerate(blocks):
            if blk.get("type") == "tool_use":
                self._sse("content_block_start", {"type": "content_block_start", "index": idx,
                          "content_block": {"type": "tool_use", "id": blk.get("id"),
                                            "name": blk.get("name"), "input": {}}})
                self._sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                          "delta": {"type": "input_json_delta",
                                    "partial_json": json.dumps(blk.get("input", {}), ensure_ascii=False)}})
            else:
                self._sse("content_block_start", {"type": "content_block_start", "index": idx,
                          "content_block": {"type": "text", "text": ""}})
                self._sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                          "delta": {"type": "text_delta", "text": blk.get("text", "")}})
            self._sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        self._sse("message_delta", {"type": "message_delta",
                  "delta": {"stop_reason": aresp.get("stop_reason", "end_turn"), "stop_sequence": None},
                  "usage": {"output_tokens": aresp.get("usage", {}).get("output_tokens", 0)}})
        self._sse("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")


def build_context(args: argparse.Namespace) -> RequestContext:
    """Build one validated server context from CLI arguments and environment."""
    prov_name = args.provider
    prov = dict(PROVIDERS[prov_name])
    key = load_key(prov, args)
    relay_force_model = None
    relay_thinking = None

    # relay：按中转站 base_url 装配上游端点（base + /v1/messages、base + /v1/models）。
    if prov_name == "relay":
        base = (
            os.environ.get("CSSWITCH_RELAY_BASE_URL") or args.relay_base or ""
        ).strip().rstrip("/")
        if not base or not re.match(r"^https?://", base):
            raise ValueError(
                "relay requires an http(s) base_url; set --relay-base or CSSWITCH_RELAY_BASE_URL"
            )
        prov["url"] = base + "/v1/messages"
        prov["models_url"] = base + "/v1/models"
        relay_force_model = (
            os.environ.get("CSSWITCH_RELAY_MODEL") or ""
        ).strip() or None
        relay_thinking = (
            os.environ.get("CSSWITCH_RELAY_THINKING") or ""
        ).strip() or None
    elif prov_name in ("openai-custom", "openai-responses"):
        base = normalize_openai_base(
            os.environ.get("CSSWITCH_OPENAI_BASE_URL") or args.openai_base or ""
        )
        if not base or not re.match(r"^https?://", base):
            raise ValueError(
                f"{prov_name} requires an http(s) OpenAI base root; "
                "set --openai-base or CSSWITCH_OPENAI_BASE_URL"
            )
        forced = (os.environ.get("CSSWITCH_OPENAI_MODEL") or "").strip()
        suffix = (
            "/responses"
            if prov.get("api_format") == "openai_responses"
            else "/chat/completions"
        )
        prov["url"] = openai_endpoint(base, suffix)
        prov["models_url"] = openai_endpoint(base, "/models")
        # 模型发现 scratch 只需要 /models，不能要求 CSSWITCH_OPENAI_MODEL；
        # 正式推理由 Rust 侧 relay_missing_model + Message 探针保证 model 必填。
        if forced:
            prov["default_model"] = forced
            relay_force_model = forced

    upstream_override = os.environ.get("CSSWITCH_UPSTREAM_URL")
    if upstream_override:
        prov["url"] = upstream_override
    if not key:
        raise ValueError(
            f"missing {prov['key_env']}; set the environment variable or pass --env-file"
        )

    return RequestContext(
        prov_name=prov_name,
        prov=prov,
        key=key,
        auth_secret=os.environ.get("CSSWITCH_AUTH_TOKEN") or args.auth_token,
        shim_mode=dsml_shim.shim_mode(prov_name, prov),
        ultra_mode=os.environ.get("CSSWITCH_ULTRA_MODE", "off").strip() or "off",
        ultra_ledger=os.environ.get("CSSWITCH_ULTRA_LEDGER")
        or os.path.join(
            os.path.expanduser("~"),
            ".csswitch",
            "logs",
            "fallback-ledger.jsonl",
        ),
        relay_force_model=relay_force_model,
        relay_thinking=relay_thinking,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=os.environ.get("CSSWITCH_PROVIDER", "deepseek"),
                    choices=list(PROVIDERS.keys()))
    ap.add_argument("--port", type=int, default=18991)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--auth-token", default=None)
    ap.add_argument("--relay-base", default=None,
                    help="relay provider 的中转站 base_url（也可用环境变量 CSSWITCH_RELAY_BASE_URL）")
    ap.add_argument("--openai-base", default=None,
                    help="openai-custom provider 的 OpenAI base root（也可用环境变量 CSSWITCH_OPENAI_BASE_URL）")
    args = ap.parse_args()
    configure_logging(args.log)
    try:
        context = build_context(args)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    log(f"CSSwitch 代理启动 127.0.0.1:{args.port}  provider={context.prov_name}  "
        f"key=已加载(未显示)  上游={context.prov['url']}  "
        f"dsml_shim={context.shim_mode}  ultra={context.ultra_mode}")
    # 绑定重试：上次会话遗留的孤儿代理可能还占着端口（app 侧会主动清，但退干净需一点时间）。
    # 重试 ~3s 等端口释放，避免一次绑不上就直接失败（Errno 48）。
    srv = None
    for attempt in range(10):
        try:
            srv = ContextHTTPServer(("127.0.0.1", args.port), H, context)
            break
        except OSError as e:
            if attempt == 9:
                LOGGER.error(
                    "port %s cannot be bound: %s. It may already be in use; stop the owner or choose another port.",
                    args.port,
                    e,
                )
                return 2
            time.sleep(0.3)
    assert srv is not None
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
