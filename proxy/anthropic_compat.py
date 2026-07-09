"""Anthropic 透传路径「兼容层」（S1a）：暴露三入口给薄骨架。内部调 provider_policy + dsml_shim。

依赖方向：骨架 → 本模块 → provider_policy；本模块不反向 import csswitch_proxy（无循环依赖）。
三入口无状态可序列化 + nonce 可注入 + ProviderState 显式传参 → 为 S1b 跨语言接缝铺路。
"""
import json
from dataclasses import dataclass

import dsml_shim
import provider_policy


def map_tool_choice(tool_choice, tools):
    """Translate Anthropic tool_choice into the OpenAI Chat shape."""
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    if choice_type == "any":
        names = [
            tool["name"]
            for tool in (tools or [])
            if isinstance(tool, dict) and tool.get("name")
        ]
        if len(names) == 1:
            return {"type": "function", "function": {"name": names[0]}}
        return "required"
    return None


def anthropic_to_openai(body, target_model, max_tokens=None):
    """Translate an Anthropic Messages request to OpenAI Chat Completions."""
    messages = []
    system_prompt = body.get("system")
    if isinstance(system_prompt, list):
        system_prompt = "\n".join(
            block.get("text", "")
            for block in system_prompt
            if isinstance(block, dict)
        )
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        text_parts = []
        tool_calls = []
        tool_results = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif block_type == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "".join(
                        item.get("text", "")
                        for item in result
                        if isinstance(item, dict)
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                })

        if role == "assistant" and tool_calls:
            messages.append({
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": tool_calls,
            })
        elif tool_results:
            messages.extend(tool_results)
            if text_parts:
                messages.append({"role": role, "content": "".join(text_parts)})
        else:
            messages.append({"role": role, "content": "".join(text_parts)})

    out = {"model": target_model, "messages": messages, "stream": False}
    effective_max_tokens = body.get("max_tokens") if max_tokens is None else max_tokens
    if effective_max_tokens:
        out["max_tokens"] = effective_max_tokens
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("tools"):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
            for tool in body["tools"]
            if isinstance(tool, dict) and tool.get("name")
        ]
    mapped_choice = map_tool_choice(body.get("tool_choice"), body.get("tools"))
    if mapped_choice is not None:
        out["tool_choice"] = mapped_choice
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    return out


def openai_to_anthropic(body, model_id, default_id="msg_proxy"):
    """Translate an OpenAI Chat Completions response to Anthropic Messages."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function", {})
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id"),
            "name": function.get("name"),
            "input": arguments,
        })
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }.get(choice.get("finish_reason"), "end_turn")
    usage = body.get("usage", {})
    return {
        "id": body.get("id", default_id),
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def map_responses_tool_choice(tool_choice, tools):
    """Translate Anthropic tool_choice into a broadly compatible Responses value."""
    if isinstance(tool_choice, str):
        choice_type = tool_choice
    elif isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
    else:
        choice_type = None
    if choice_type == "auto":
        return "auto"
    if choice_type == "none":
        return "none"
    if tools:
        # Some Responses-compatible providers reject required or named choices.
        return "auto"
    return None


def normalize_responses_tool_parameters(schema):
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


def _responses_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item.get("text", "")
            for item in value
            if isinstance(item, dict)
        )
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def anthropic_to_openai_responses(
    body,
    target_model,
    max_output_tokens=None,
    omit_tool_names=(),
):
    """Translate an Anthropic Messages request to the OpenAI Responses shape."""
    system_prompt = body.get("system")
    if isinstance(system_prompt, list):
        system_prompt = "\n".join(
            block.get("text", "")
            for block in system_prompt
            if isinstance(block, dict)
        )

    items = []
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue

        text_parts = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                if text_parts:
                    items.append({"role": role, "content": "".join(text_parts)})
                    text_parts = []
                items.append({
                    "type": "function_call",
                    "call_id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                })
            elif block_type == "tool_result":
                if text_parts:
                    items.append({"role": role, "content": "".join(text_parts)})
                    text_parts = []
                items.append({
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id"),
                    "output": _responses_text(block.get("content")),
                })
        if text_parts:
            items.append({"role": role, "content": "".join(text_parts)})

    out = {"model": target_model, "input": items, "stream": False}
    if system_prompt:
        out["instructions"] = system_prompt
    if max_output_tokens:
        out["max_output_tokens"] = max_output_tokens
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]

    omitted = set(omit_tool_names or ())
    tools = [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": normalize_responses_tool_parameters(tool.get("input_schema", {})),
        }
        for tool in body.get("tools") or []
        if isinstance(tool, dict) and tool.get("name") and tool.get("name") not in omitted
    ]
    if tools:
        out["tools"] = tools
    mapped_choice = map_responses_tool_choice(body.get("tool_choice"), tools)
    if mapped_choice is not None:
        out["tool_choice"] = mapped_choice
    return out


def openai_responses_to_anthropic(body, model_id, default_id="msg_proxy"):
    """Translate an OpenAI Responses result to Anthropic Messages."""
    blocks = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text = "".join(
                content.get("text", "")
                for content in item.get("content") or []
                if isinstance(content, dict) and content.get("type") in ("output_text", "text")
            )
            if text:
                blocks.append({"type": "text", "text": text})
        elif item_type == "function_call":
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            blocks.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id"),
                "name": item.get("name"),
                "input": arguments,
            })
    if not blocks and body.get("output_text"):
        blocks.append({"type": "text", "text": body.get("output_text", "")})

    usage = body.get("usage", {})
    stop_reason = "tool_use" if any(block.get("type") == "tool_use" for block in blocks) else "end_turn"
    if body.get("status") == "incomplete":
        stop_reason = "max_tokens"
    return {
        "id": body.get("id", default_id),
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }


@dataclass
class Ctx:
    """transform_request 产出、传给 rewrite_nonstream / make_stream_rewriter 的请求级上下文。"""
    src_model: str
    target_model: str
    known_tools: dict
    nonce: str
    shim_mode: str
    provider: str


def _filter_upstream_tools(upstream, target_model, provider):
    """Provider-specific tool compatibility before sending to upstream.

    Kimi's Anthropic endpoint treats a tool named ``web_search`` as its own server tool and
    streams ``server_tool_use`` / ``web_search_tool_result`` blocks. The local client path
    expects ordinary client tools, so those server-tool blocks make the stream
    retry. Keep the original known_tools in ctx, but do not advertise this one tool upstream.
    """
    if provider == "relay" and "kimi" in (target_model or "").lower():
        tools = upstream.get("tools")
        if isinstance(tools, list):
            filtered = [t for t in tools if not (isinstance(t, dict) and t.get("name") == "web_search")]
            if len(filtered) != len(tools):
                if filtered:
                    upstream["tools"] = filtered
                else:
                    upstream.pop("tools", None)


def transform_request(body, state):
    """(body, ProviderState) -> (upstream_body, Ctx)。纯函数：无网络、无全局读取。
    等价于旧 _handle_anthropic 的 :695-702 + :714-718。"""
    src = body.get("model", "?")
    target = provider_policy.resolve_model(src, state)
    upstream = dict(body)
    upstream["model"] = target
    if upstream.get("max_tokens"):
        upstream["max_tokens"] = provider_policy.clamp_max_tokens(
            upstream["max_tokens"], target, state)
    provider_policy.normalize_thinking(upstream, state.prov_name, state.relay_thinking)
    _filter_upstream_tools(upstream, target, state.prov_name)
    known_tools = {t["name"]: (t.get("input_schema") or {})
                   for t in (body.get("tools") or [])
                   if isinstance(t, dict) and t.get("name")}
    ctx = Ctx(src_model=src, target_model=target, known_tools=known_tools,
              nonce=state.nonce_factory(), shim_mode=state.shim_mode, provider=state.prov_name)
    return upstream, ctx


def _shim_on(ctx):
    return ctx.shim_mode in ("detect", "rewrite") and bool(ctx.known_tools)


def rewrite_nonstream(body_bytes, ctx):
    """(body_bytes, Ctx) -> (body_bytes, stats)。等价于旧 :771-780。
    off / 无工具：(原 bytes, {})；detect：(原 bytes, {"found": bool})；
    rewrite：(改写 bytes, {"rewritten": bool})。"""
    if not _shim_on(ctx):
        return body_bytes, {}
    if ctx.shim_mode == "rewrite":
        new = dsml_shim.rewrite_nonstream_body(body_bytes, ctx.known_tools, nonce=ctx.nonce)
        return new, {"rewritten": new != body_bytes}
    det = dsml_shim.DsmlDetector()
    det.feed(body_bytes)
    return body_bytes, {"found": det.found}


class _RewriteFilter:
    """rewrite 模式的流式 filter：包 DsmlStreamRewriter，暴露统一 feed/finalize/stats。"""

    def __init__(self, known_tools, nonce):
        self._rw = dsml_shim.DsmlStreamRewriter(known_tools, nonce=nonce)

    def feed(self, chunk):
        return self._rw.feed(chunk)

    def finalize(self):
        return self._rw.finalize()

    def stats(self):
        return {"synthesized": self._rw.synthesized, "tool_n": self._rw.tool_n}


class _DetectFilter:
    """detect 模式的流式 filter：原样透传 + 内部记 stats。"""

    def __init__(self):
        self._det = dsml_shim.DsmlDetector()

    def feed(self, chunk):
        self._det.feed(chunk)
        return chunk

    def finalize(self):
        return b""

    def stats(self):
        return {"found": self._det.found}


class _KimiServerToolFilter:
    """Drop Kimi server-tool SSE blocks that the local client cannot consume.

    Kimi may emit Anthropic server-tool blocks (currently web search) even when CSSwitch
    does not advertise that tool upstream. The client-tool path expects ordinary
    content blocks with contiguous indexes, so we remove those blocks and compact indexes.
    """

    _DROP_TYPES = {"server_tool_use", "web_search_tool_result"}

    def __init__(self):
        self._buf = b""
        self._skip = set()
        self._index_map = {}
        self._next_index = 0
        self._dropped = 0

    @staticmethod
    def _split_frame(buf):
        candidates = [(buf.find(b"\n\n"), 2), (buf.find(b"\r\n\r\n"), 4)]
        candidates = [(i, n) for i, n in candidates if i >= 0]
        if not candidates:
            return None, None, buf
        i, n = min(candidates, key=lambda x: x[0])
        return buf[:i], buf[i:i + n], buf[i + n:]

    @staticmethod
    def _event_and_data(frame):
        event = None
        data = []
        for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
            if line.startswith(b"event:"):
                event = line.split(b":", 1)[1].strip()
            elif line.startswith(b"data:"):
                data.append(line.split(b":", 1)[1].lstrip())
        return event, b"\n".join(data)

    @staticmethod
    def _render(event, obj):
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if event:
            return b"event: " + event + b"\n" + b"data: " + data + b"\n\n"
        return b"data: " + data + b"\n\n"

    def _mapped_index(self, idx):
        if idx not in self._index_map:
            self._index_map[idx] = self._next_index
            self._next_index += 1
        return self._index_map[idx]

    def _rewrite_frame(self, frame, sep):
        event, data = self._event_and_data(frame)
        if not data:
            return frame + sep
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            return frame + sep
        if not isinstance(obj, dict):
            return frame + sep

        t = obj.get("type")
        if t == "content_block_start":
            idx = obj.get("index")
            block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
            if block.get("type") in self._DROP_TYPES:
                self._skip.add(idx)
                self._dropped += 1
                return b""
            obj = dict(obj)
            obj["index"] = self._mapped_index(idx)
            return self._render(event, obj)
        if t in ("content_block_delta", "content_block_stop"):
            idx = obj.get("index")
            if idx in self._skip:
                return b""
            if idx in self._index_map:
                obj = dict(obj)
                obj["index"] = self._index_map[idx]
                return self._render(event, obj)
        return frame + sep

    def feed(self, chunk):
        self._buf += chunk
        out = []
        while True:
            frame, sep, rest = self._split_frame(self._buf)
            if frame is None:
                break
            self._buf = rest
            out.append(self._rewrite_frame(frame, sep))
        return b"".join(out)

    def finalize(self):
        if not self._buf:
            return b""
        frame = self._buf
        self._buf = b""
        return self._rewrite_frame(frame, b"\n\n")

    def stats(self):
        return {"dropped_kimi_server_tool_blocks": self._dropped}


class _PipelineFilter:
    def __init__(self, filters):
        self._filters = filters

    def feed(self, chunk):
        out = chunk
        for f in self._filters:
            out = f.feed(out)
        return out

    def finalize(self):
        out = b""
        for f in self._filters:
            out = f.feed(out) + f.finalize()
        return out

    def stats(self):
        out = {}
        for f in self._filters:
            out.update(f.stats())
        return out


def make_stream_rewriter(ctx):
    """(Ctx) -> stream_filter | None。off / 无工具 → None（骨架直接透传，零开销）。
    filter 统一接口：feed(chunk)->bytes / finalize()->bytes / stats()。等价于旧 :735-737。"""
    filters = []
    if ctx.provider == "relay" and "kimi" in (ctx.target_model or "").lower():
        filters.append(_KimiServerToolFilter())
    if _shim_on(ctx):
        if ctx.shim_mode == "rewrite":
            filters.append(_RewriteFilter(ctx.known_tools, ctx.nonce))
        else:
            filters.append(_DetectFilter())
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return _PipelineFilter(filters)
