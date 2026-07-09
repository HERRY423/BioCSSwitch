import json
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))
import csswitch_proxy as cs          # 复用 PROVIDERS 作配置真源
import provider_policy as pp
import anthropic_compat as ac

P2 = "｜｜"   # 双全角竖线 U+FF5C（issue #8 实测泄漏形态）
# 一段「模型本想调用 web_search、却泄漏成纯文本」的 DSML。
DSML_TEXT = (
    "<" + P2 + "DSML" + P2 + "tool_calls> "
    "<" + P2 + "DSML" + P2 + 'invoke name="web_search">'
    "<" + P2 + "DSML" + P2 + 'parameter name="query" string="true">GSE207177</' + P2 + "DSML" + P2 + "parameter>"
    "</" + P2 + "DSML" + P2 + "invoke> "
    "</" + P2 + "DSML" + P2 + "tool_calls>")
WEB_SEARCH = {"web_search": {"type": "object", "properties": {"query": {"type": "string"}}}}


def _state(prov, prov_name, nonce="n", **over):
    return pp.ProviderState(
        policy=pp.policy_from_prov(prov), prov_name=prov_name,
        relay_force_model=over.get("relay_force_model"),
        relay_models=over.get("relay_models", []),
        relay_thinking=over.get("relay_thinking"),
        shim_mode=over.get("shim_mode", "off"),
        nonce_factory=lambda: nonce)


def _ctx(**over):
    base = dict(src_model="claude-opus-4-8", target_model="deepseek-v4-pro",
                known_tools={}, nonce="fixed", shim_mode="off", provider="deepseek")
    base.update(over)
    return ac.Ctx(**base)


def _dsml_json_body():
    return json.dumps({
        "id": "msg", "type": "message", "role": "assistant", "model": "up",
        "content": [{"type": "text", "text": DSML_TEXT}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 5},
    }, ensure_ascii=False).encode("utf-8")


class OpenAIChatTranslation(unittest.TestCase):
    def test_request_translation_preserves_tools_and_sampling(self):
        body = {
            "system": [{"type": "text", "text": "be precise"}],
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100000,
            "temperature": 0.2,
            "top_p": 0.8,
            "stop_sequences": ["STOP"],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "lookup"},
        }
        out = ac.anthropic_to_openai(body, "qwen-plus", max_tokens=8192)
        self.assertEqual(out["model"], "qwen-plus")
        self.assertEqual(out["max_tokens"], 8192)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "be precise"})
        self.assertEqual(out["tool_choice"],
                         {"type": "function", "function": {"name": "lookup"}})
        self.assertEqual(out["stop"], ["STOP"])
        self.assertEqual(out["top_p"], 0.8)

    def test_response_translation_preserves_tool_calls_and_usage(self):
        body = {
            "choices": [{
                "message": {
                    "content": "checking",
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {"name": "lookup", "arguments": '{"q":"TP53"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        out = ac.openai_to_anthropic(body, "claude-opus-4-8")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][1]["input"], {"q": "TP53"})
        self.assertEqual(out["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_invalid_tool_arguments_degrade_to_empty_object(self):
        body = {"choices": [{"message": {"tool_calls": [{
            "id": "call_1",
            "function": {"name": "lookup", "arguments": "{bad json"},
        }]}}]}
        out = ac.openai_to_anthropic(body, "m")
        self.assertEqual(out["content"][0]["input"], {})


class TransformRequest(unittest.TestCase):
    def test_deepseek_maps_model_clamps_normalizes_thinking(self):
        st = _state(cs.PROVIDERS["deepseek"], "deepseek")
        body = {"model": "claude-opus-4-8", "max_tokens": 100000,
                "thinking": {"type": "auto"},
                "messages": [{"role": "user", "content": "hi"}]}
        up, ctx = ac.transform_request(body, st)
        self.assertEqual(up["model"], "deepseek-v4-pro")       # 映射
        self.assertEqual(up["max_tokens"], 65536)               # clamp 到 pro cap
        self.assertEqual(up["thinking"]["type"], "adaptive")    # auto → adaptive
        self.assertEqual(ctx.src_model, "claude-opus-4-8")
        self.assertEqual(ctx.target_model, "deepseek-v4-pro")
        self.assertEqual(ctx.provider, "deepseek")
        # 不改原 body（副本语义）。
        self.assertEqual(body["model"], "claude-opus-4-8")

    def test_relay_passthrough_no_clamp(self):
        st = _state(dict(cs.PROVIDERS["relay"]), "relay")
        up, ctx = ac.transform_request(
            {"model": "claude-opus-4-8", "max_tokens": 1000000, "messages": []}, st)
        self.assertEqual(up["model"], "claude-opus-4-8")        # 透传不映射
        self.assertEqual(up["max_tokens"], 1000000)             # relay 不夹

    def test_nonce_injected_from_factory(self):
        st = _state(cs.PROVIDERS["deepseek"], "deepseek", nonce="deadbeef")
        _up, ctx = ac.transform_request({"model": "claude-opus-4-8", "messages": []}, st)
        self.assertEqual(ctx.nonce, "deadbeef")

    def test_known_tools_extracted(self):
        st = _state(cs.PROVIDERS["deepseek"], "deepseek")
        body = {"model": "claude-opus-4-8", "messages": [],
                "tools": [{"name": "web_search", "input_schema": {"type": "object"}},
                          {"no_name": True}]}
        _up, ctx = ac.transform_request(body, st)
        self.assertEqual(list(ctx.known_tools), ["web_search"])

    def test_kimi_relay_does_not_advertise_web_search_server_tool(self):
        st = _state(dict(cs.PROVIDERS["relay"]), "relay",
                    relay_force_model="kimi-k2.7-code", relay_thinking="enabled")
        body = {"model": "claude-opus-4-8", "messages": [], "max_tokens": 1000,
                "tools": [{"name": "web_search", "input_schema": {"type": "object"}},
                          {"name": "bash", "input_schema": {"type": "object"}}]}
        up, ctx = ac.transform_request(body, st)
        self.assertEqual(up["model"], "kimi-k2.7-code")
        self.assertEqual([t["name"] for t in up["tools"]], ["bash"])
        self.assertEqual(set(ctx.known_tools), {"web_search", "bash"})

    def test_non_kimi_relay_keeps_web_search_tool(self):
        st = _state(dict(cs.PROVIDERS["relay"]), "relay",
                    relay_force_model="glm-5.2", relay_thinking="adaptive")
        body = {"model": "claude-opus-4-8", "messages": [],
                "tools": [{"name": "web_search", "input_schema": {"type": "object"}}]}
        up, _ctx = ac.transform_request(body, st)
        self.assertEqual([t["name"] for t in up["tools"]], ["web_search"])

    def test_no_max_tokens_left_absent(self):
        st = _state(cs.PROVIDERS["deepseek"], "deepseek")
        up, _ctx = ac.transform_request({"model": "claude-opus-4-8", "messages": []}, st)
        self.assertNotIn("max_tokens", up)


class RewriteNonstream(unittest.TestCase):
    def test_off_returns_verbatim_empty_stats(self):
        body = _dsml_json_body()
        out, stats = ac.rewrite_nonstream(body, _ctx(shim_mode="off", known_tools=WEB_SEARCH))
        self.assertEqual(out, body)
        self.assertEqual(stats, {})

    def test_no_tools_returns_verbatim(self):
        body = _dsml_json_body()
        out, stats = ac.rewrite_nonstream(body, _ctx(shim_mode="rewrite", known_tools={}))
        self.assertEqual(out, body)
        self.assertEqual(stats, {})

    def test_detect_reports_found_without_changing_bytes(self):
        body = _dsml_json_body()
        out, stats = ac.rewrite_nonstream(body, _ctx(shim_mode="detect", known_tools=WEB_SEARCH))
        self.assertEqual(out, body)
        self.assertTrue(stats["found"])

    def test_rewrite_synthesizes_tool_use_with_fixed_nonce(self):
        body = _dsml_json_body()
        out, stats = ac.rewrite_nonstream(
            body, _ctx(shim_mode="rewrite", known_tools=WEB_SEARCH, nonce="fixed"))
        self.assertTrue(stats["rewritten"])
        obj = json.loads(out)
        tu = next(b for b in obj["content"] if b["type"] == "tool_use")
        self.assertEqual(tu["id"], "toolu_dsml_fixed_1")       # 固定 nonce → 稳定 id
        self.assertEqual(tu["name"], "web_search")
        self.assertEqual(tu["input"], {"query": "GSE207177"})
        self.assertEqual(obj["stop_reason"], "tool_use")

    def test_rewrite_no_leak_reports_not_rewritten(self):
        clean = json.dumps({"content": [{"type": "text", "text": "hello"}],
                            "stop_reason": "end_turn"}).encode()
        out, stats = ac.rewrite_nonstream(
            clean, _ctx(shim_mode="rewrite", known_tools=WEB_SEARCH))
        self.assertEqual(out, clean)
        self.assertFalse(stats["rewritten"])


class MakeStreamRewriter(unittest.TestCase):
    def test_off_returns_none(self):
        self.assertIsNone(ac.make_stream_rewriter(_ctx(shim_mode="off", known_tools=WEB_SEARCH)))

    def test_no_tools_returns_none(self):
        self.assertIsNone(ac.make_stream_rewriter(_ctx(shim_mode="rewrite", known_tools={})))

    def test_detect_filter_passes_through_and_reports_found(self):
        f = ac.make_stream_rewriter(_ctx(shim_mode="detect", known_tools=WEB_SEARCH))
        chunk = DSML_TEXT.encode("utf-8")
        self.assertEqual(f.feed(chunk), chunk)                 # detect 原样透传
        self.assertEqual(f.finalize(), b"")
        self.assertTrue(f.stats()["found"])

    def test_kimi_filter_drops_server_tool_blocks_and_compacts_indexes(self):
        f = ac.make_stream_rewriter(_ctx(provider="relay", target_model="kimi-k2.7-code"))
        sse = b"".join([
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"server_tool_use","name":"web_search"}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta"}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":2,"content_block":{"type":"web_search_tool_result","content":[]}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":2}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":3,"content_block":{"type":"thinking","thinking":"","signature":""}}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":3}\n\n',
            b'event: content_block_start\ndata: {"type":"content_block_start","index":4,"content_block":{"type":"text","text":""}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":4,"delta":{"type":"text_delta","text":"OK"}}\n\n',
        ])
        out = f.feed(sse) + f.finalize()
        self.assertNotIn(b"server_tool_use", out)
        self.assertNotIn(b"web_search_tool_result", out)
        self.assertIn(b'"type":"thinking","thinking":"","signature":""', out)
        self.assertIn(b'"index":1', out)
        self.assertIn(b'"index":2', out)
        self.assertIn(b'"text":"OK"', out)
        self.assertEqual(f.stats()["dropped_kimi_server_tool_blocks"], 2)

    def test_rewrite_filter_synthesizes_tool_use(self):
        f = ac.make_stream_rewriter(
            _ctx(shim_mode="rewrite", known_tools=WEB_SEARCH, nonce="fixed"))
        # 逐帧喂一段含 DSML 的最小 SSE，finalize 收尾。
        out = b""
        for frame in _min_sse_frames():
            out += f.feed(frame)
        out += f.finalize()
        text = out.decode("utf-8", "replace")
        self.assertIn('"type":"tool_use"', text.replace(" ", ""))
        self.assertTrue(f.stats()["synthesized"])
        self.assertGreaterEqual(f.stats()["tool_n"], 1)


def _min_sse_frames():
    def sse(event, obj):
        return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
    return [
        sse("message_start", {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "up",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}}),
        sse("content_block_start", {"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
        sse("content_block_delta", {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": DSML_TEXT}}),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse("message_delta", {"type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5}}),
        sse("message_stop", {"type": "message_stop"}),
    ]


if __name__ == "__main__":
    unittest.main()
