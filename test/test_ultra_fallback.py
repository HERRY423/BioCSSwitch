import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import fallback_policy as fp
import anthropic_compat
import task_router
import ultra_orchestrator as ultra
import debate_arena


def anthropic_msg(text):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


class FallbackPolicyTests(unittest.TestCase):
    def test_auth_error_never_fallbacks(self):
        f = fp.classify_status(401, "bad key")
        self.assertEqual(f.kind, fp.AUTH_ERROR)
        self.assertFalse(fp.should_fallback(f, remaining_attempts=2))

    def test_rate_limit_can_fallback(self):
        f = fp.classify_status(429, "slow down")
        self.assertEqual(f.kind, fp.RATE_LIMIT)
        self.assertTrue(fp.should_fallback(f, remaining_attempts=1))

    def test_context_and_model_failures_are_classified(self):
        self.assertEqual(fp.classify_status(413, "payload too large").kind, fp.CONTEXT_OVERFLOW)
        self.assertEqual(fp.classify_status(404, "model not found").kind, fp.MODEL_UNAVAILABLE)
        self.assertEqual(fp.classify_status(503, "overloaded").kind, fp.PROVIDER_OVERLOADED)
        self.assertEqual(fp.classify_status(400, "bad schema").kind, fp.INVALID_REQUEST)

    def test_ledger_redacts_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            fp.FallbackLedger(path, extra_secrets=["sk-secret-123456"]).write({
                "message": "Authorization: Bearer sk-secret-123456",
                "key": "sk-secret-123456",
            })
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("sk-secret-123456", text)
            self.assertIn("****", text)


class TaskRouterTests(unittest.TestCase):
    def test_detects_clinical_trials_task(self):
        req = {"messages": [{"role": "user", "content": "Find NCT clinical trial endpoints for GBM"}]}
        self.assertEqual(task_router.detect_task(req), "clinical-trials")

    def test_route_filters_failed_probe(self):
        cfg = {
            "active_id": "p1",
            "task_routes": {"clinical-trials": "p1"},
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1", "name": "bad tool"},
                {"id": "p2", "template_id": "qwen", "api_key": "sk-p2", "name": "tool ok"},
            ],
            "probe_results": {
                "p1:tool_use": json.dumps({"verdict": "fail"}),
                "p2:tool_use": json.dumps({"verdict": "ok"}),
            },
        }
        active = task_router.current_context("deepseek", {"mode": "anthropic", "url": "u"}, "sk-active")
        routes = task_router.route_contexts(cfg, "clinical-trials", active)
        self.assertEqual(routes[0]["profile_id"], "p2")

    def test_route_plan_uses_failure_route_and_probe_diagnostics(self):
        cfg = {
            "active_id": "p1",
            "task_routes": {"clinical-trials": "p1"},
            "ultra": {"task_policies": {
                "clinical-trials": {"failure_routes": {fp.RATE_LIMIT: ["p2"]}}
            }},
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1", "name": "primary"},
                {"id": "p2", "template_id": "qwen", "api_key": "sk-p2", "name": "rate-limit fallback"},
                {"id": "p3", "template_id": "qwen", "api_key": "sk-p3", "name": "last resort"},
            ],
            "probe_results": {"p3:tool_use": json.dumps({"verdict": "degraded"})},
        }
        active = task_router.current_context("deepseek", {"mode": "anthropic", "url": "u"}, "sk-active")
        plan = task_router.route_plan(cfg, "clinical-trials", active, failure_kind=fp.RATE_LIMIT)
        self.assertEqual([c["profile_id"] for c in plan["contexts"][:2]], ["p1", "p2"])
        self.assertEqual(plan["candidates"][2]["probe_status"], "degraded")

    def test_custom_openai_profile_uses_openai_chat_context(self):
        profile = {
            "id": "p-openai",
            "template_id": "custom-openai",
            "api_key": "sk-openai",
            "base_url": "https://example.com/v1/chat/completions",
            "model": "gpt-5",
            "name": "openai chat",
        }
        ctx = task_router.context_from_profile(profile)
        self.assertEqual(ctx["provider"], "openai-custom")
        self.assertEqual(ctx["mode"], "openai")
        self.assertEqual(ctx["url"], "https://example.com/v1/chat/completions")
        self.assertEqual(ctx["models_url"], "https://example.com/v1/models")
        self.assertEqual(ctx["auth_style"], "bearer")

    def test_custom_openai_responses_profile_uses_responses_context(self):
        profile = {
            "id": "p-responses",
            "template_id": "custom-openai-responses",
            "api_key": "sk-openai",
            "base_url": "https://example.com/v1",
            "model": "gpt-5.1",
            "name": "openai responses",
        }
        ctx = task_router.context_from_profile(profile)
        self.assertEqual(ctx["provider"], "openai-responses")
        self.assertEqual(ctx["mode"], "openai")
        self.assertEqual(ctx["url"], "https://example.com/v1/responses")
        self.assertEqual(ctx["models_url"], "https://example.com/v1/models")
        self.assertEqual(ctx["auth_style"], "bearer")

    def test_chinese_requests_are_classified(self):
        cases = [
            ("请做一轮科学辩论并量化不确定性", "scientific-debate"),
            ("系统综述这个靶点的临床证据", "lit-review"),
            ("整理临床试验终点与入排标准", "clinical-trials"),
            ("核验这些引用并做证据审计", "evidence-check"),
            ("为单细胞组学写 Scanpy 流程", "omics-code"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                req = {"messages": [{"role": "user", "content": text}]}
                self.assertEqual(task_router.detect_task(req), expected)


class DebateArenaTests(unittest.TestCase):
    def test_grade_ties_break_conservatively(self):
        grade = debate_arena._aggregate_grade(
            {"High": 1, "Moderate": 0, "Low": 0, "Very Low": 1},
            ["PMID:12345678"],
            [],
        )
        self.assertEqual(grade["grade"], "Very Low")

    def test_equal_votes_stay_conservative(self):
        grade = debate_arena._aggregate_grade(
            {"High": 1, "Moderate": 1, "Low": 1, "Very Low": 1},
            ["PMID:12345678"],
            [],
        )
        self.assertEqual(grade["grade"], "Very Low")


class UltraOrchestratorTests(unittest.TestCase):
    def test_rate_limit_falls_back_to_second_profile(self):
        cfg = {
            "active_id": "p1",
            "task_routes": {"clinical-trials": "p1"},
            "ultra": {"task_policies": {
                "clinical-trials": {"fallback_profile_ids": ["p2"]}
            }},
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1", "name": "primary"},
                {"id": "p2", "template_id": "qwen", "api_key": "sk-p2", "name": "fallback"},
            ],
        }
        req = {"model": "claude-opus-4-8", "max_tokens": 32,
               "messages": [{"role": "user", "content": "clinical trial NCT endpoint landscape"}]}
        calls = []

        def fake_post(url, data, headers):
            calls.append((url, headers))
            if headers.get("x-api-key") == "sk-p1":
                raise urllib.error.HTTPError(url, 429, "rate", {}, io.BytesIO(b"rate limit"))
            return json.dumps({
                "id": "chatcmpl",
                "choices": [{"message": {"content": "fallback answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            }).encode(), "application/json"

        active = task_router.current_context("deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)
        self.assertTrue(result.handled)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[0].outcome, fp.RATE_LIMIT)
        self.assertEqual(result.attempts[1].profile_id, "p2")
        self.assertIn("fallback answer", json.dumps(result.body))

    def test_sensitive_mode_blocks_phi_to_cloud(self):
        cfg = {
            "sensitive_mode": True,
            "local_endpoint_hosts": ["127.0.0.1"],
            "active_id": "p1",
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1", "name": "cloud"},
            ],
        }
        req = {"messages": [{"role": "user", "content": "Patient DOB: 1970-01-02 MRN 1234567"}]}

        def never_called(url, data, headers):
            raise AssertionError("upstream should not be called")

        active = task_router.current_context("deepseek", {"mode": "anthropic", "url": "https://api.deepseek.com/v1/messages"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, never_called, ledger_path=None)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.body["error"]["csswitch_failure_kind"], fp.SENSITIVE_VIOLATION)
        self.assertEqual(result.attempts, [])

    def test_streaming_requests_fall_back_to_legacy_path(self):
        req = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
        active = task_router.current_context("deepseek", {"mode": "anthropic", "url": "u"}, "sk")
        self.assertIsNone(ultra.handle_request(req, active, {}, lambda *_: None, None))

    def test_ultra_uses_shared_provider_model_policy(self):
        qwen = task_router.current_context(
            "qwen", {"mode": "openai", "url": "u"}, "sk")
        self.assertEqual(
            ultra.resolve_model("claude-opus-4-8", qwen), "qwen-max")
        self.assertEqual(
            ultra.clamp_max_tokens(100000, qwen, "qwen-max"), 8192)

    def test_ultra_relay_thinking_uses_shared_normalization(self):
        relay = task_router.current_context(
            "relay",
            {"mode": "anthropic", "url": "u", "auth_style": "both"},
            "sk",
            force_model="kimi-k2.5",
            thinking_policy="enabled",
        )
        body, _ctx = anthropic_compat.transform_request({
            "model": "claude-opus-4-8",
            "max_tokens": 2048,
            "tools": [{"name": "lookup"}],
            "tool_choice": {"type": "tool", "name": "lookup"},
        }, ultra._provider_state(relay))
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["thinking"]["type"], "enabled")

    def test_verifier_only_runs_for_clinical_evidence_or_phi(self):
        req = {"messages": [{"role": "user", "content": "verify citation"}]}
        resp = anthropic_msg("This is supported by PMID: 12345678.")
        lit = ultra.run_subagents(req, resp, "lit-review")
        self.assertNotIn("verifier", lit["roles_run"])
        sub = ultra.run_subagents(req, resp, "clinical-trials")
        f = ultra.quality_gate(req, resp, "clinical-trials", sub)
        self.assertEqual(f.kind, fp.QUALITY_GATE_FAIL)
        self.assertEqual(sub["verdict"], "fail")

    def test_verifier_allows_grounded_pmid(self):
        req = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "content": "PMID 12345678 title sample"}
        ]}]}
        resp = anthropic_msg("This is supported by PMID: 12345678.")
        sub = ultra.run_subagents(req, resp, "evidence-check")
        self.assertEqual(sub["verdict"], "pass")

    def test_critic_uses_rule_engine_for_extrapolation(self):
        req = {"messages": [{"role": "user", "content": "critique conclusion"}]}
        resp = anthropic_msg("Mouse xenograft data show this therapy is clinically effective for human patients.")
        sub = ultra.run_subagents(req, resp, "evidence-check")
        self.assertIn("critic", sub["roles_run"])
        self.assertTrue(any(f.get("agent_id") == "critic" for f in sub["findings"]))

    def test_deep_ultra_enables_planner_coder_toolsmith(self):
        req = {
            "tool_choice": {"type": "any"},
            "tools": [{"name": "search", "input_schema": {}}],
            "messages": [{"role": "user", "content": "clinical trial NCT endpoint landscape"}],
        }
        resp = anthropic_msg("I will summarize the clinical trial landscape.")
        conservative = ultra.run_subagents(req, resp, "clinical-trials", mode="ultra_conservative")
        deep = ultra.run_subagents(req, resp, "clinical-trials", mode="ultra_deep")
        self.assertNotIn("toolsmith", conservative["roles_run"])
        self.assertIn("toolsmith", deep["roles_run"])
        self.assertIn("planner", deep["roles_run"])


class UltraEndToEndAcceptanceTests(unittest.TestCase):
    def test_responses_profile_round_trips_through_ultra(self):
        cfg = {
            "active_id": "responses",
            "profiles": [{
                "id": "responses",
                "template_id": "custom-openai-responses",
                "api_key": "sk-responses",
                "base_url": "https://example.test/v1",
                "model": "gpt-5.1",
            }],
        }
        req = {
            "model": "claude-opus-4-8",
            "max_tokens": 256,
            "system": "Be precise.",
            "messages": [{"role": "user", "content": "Summarize the result."}],
        }
        captured = {}

        def fake_post(url, data, headers):
            captured.update(url=url, payload=json.loads(data), headers=headers)
            return json.dumps({
                "id": "resp_123",
                "status": "completed",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "responses answer"}],
                }],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }).encode(), "application/json"

        active = task_router.current_context(
            "deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)

        self.assertEqual(result.status, 200)
        self.assertEqual(captured["url"], "https://example.test/v1/responses")
        self.assertEqual(captured["payload"]["model"], "gpt-5.1")
        self.assertIn("input", captured["payload"])
        self.assertNotIn("messages", captured["payload"])
        self.assertEqual(captured["payload"]["max_output_tokens"], 256)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-responses")
        self.assertIn("responses answer", json.dumps(result.body))

    def test_failure_specific_route_is_tried_before_generic_fallback(self):
        cfg = {
            "active_id": "p1",
            "ultra": {"task_policies": {"general": {
                "fallback_profile_ids": ["p3"],
                "failure_routes": {fp.RATE_LIMIT: ["p2"]},
            }}},
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1"},
                {"id": "p2", "template_id": "qwen", "api_key": "sk-p2"},
                {"id": "p3", "template_id": "qwen", "api_key": "sk-p3"},
            ],
        }
        req = {"messages": [{"role": "user", "content": "hello"}]}
        calls = []

        def fake_post(url, data, headers):
            key = headers.get("x-api-key") or headers.get("Authorization")
            calls.append(key)
            if key == "sk-p1":
                raise urllib.error.HTTPError(url, 429, "rate", {}, io.BytesIO(b"rate limit"))
            if key == "Bearer sk-p3":
                raise AssertionError("generic fallback ran before the rate-limit route")
            return json.dumps({
                "id": "chatcmpl",
                "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
            }).encode(), "application/json"

        active = task_router.current_context(
            "deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)

        self.assertEqual(result.status, 200)
        self.assertEqual(calls, ["sk-p1", "Bearer sk-p2"])
        self.assertEqual([attempt.profile_id for attempt in result.attempts], ["p1", "p2"])

    def test_exhausted_quality_gate_returns_gateway_error(self):
        cfg = {
            "active_id": "p1",
            "ultra": {"max_attempts": 1},
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1"},
            ],
        }
        req = {
            "messages": [{"role": "user", "content": "核验 PMID 12345678 并做证据审计"}],
        }

        def fake_post(url, data, headers):
            return json.dumps(anthropic_msg("not strict json")).encode(), "application/json"

        active = task_router.current_context(
            "deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)

        self.assertEqual(result.status, 502)
        self.assertEqual(result.body["error"]["csswitch_failure_kind"], fp.JSON_UNSTABLE)
        self.assertEqual(result.attempts[0].status, 200)

    def test_chinese_phi_is_filtered_to_local_profile(self):
        cfg = {
            "sensitive_mode": True,
            "active_id": "cloud",
            "profiles": [
                {"id": "cloud", "template_id": "deepseek", "api_key": "sk-cloud"},
                {
                    "id": "local",
                    "template_id": "custom-openai",
                    "api_key": "sk-local",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "local-model",
                },
            ],
        }
        req = {
            "messages": [{
                "role": "user",
                "content": "患者出生日期：1970-01-02，住院号：123456，请脱敏后总结。",
            }],
        }
        calls = []

        def fake_post(url, data, headers):
            calls.append(url)
            return json.dumps({
                "id": "chatcmpl",
                "choices": [{"message": {"content": "已脱敏"}, "finish_reason": "stop"}],
            }, ensure_ascii=False).encode(), "application/json"

        active = task_router.current_context(
            "deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)

        self.assertTrue(ultra.request_has_phi(req))
        self.assertEqual(result.status, 200)
        self.assertEqual(calls, ["http://127.0.0.1:11434/v1/chat/completions"])
        sensitive_events = [
            event for event in result.route_plan["events"]
            if event.get("kind") == "sensitive_filter"
        ]
        self.assertTrue(sensitive_events)
        self.assertEqual(sensitive_events[0]["skipped"][0]["profile_id"], "cloud")

    def test_debate_path_returns_structured_scientific_result(self):
        cfg = {
            "active_id": "p1",
            "ultra": {
                "force_scientific_debate": True,
                "debate": {"max_agents": 2, "rounds": 1},
            },
            "profiles": [
                {"id": "p1", "template_id": "deepseek", "api_key": "sk-p1"},
                {"id": "p2", "template_id": "qwen", "api_key": "sk-p2"},
            ],
        }
        req = {
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "Debate whether this mechanism is causal."}],
        }
        calls = []

        def fake_post(url, data, headers):
            calls.append(url)
            text = (
                "claim_summary: plausible\n"
                "strongest_evidence: perturbation\n"
                "weakest_link: missing replication\n"
                "uncertainty_0_to_1: 0.4\n"
                "evidence_grade: Moderate\n"
                "decisive_next_experiment: validate with a powered assay"
            )
            if "dashscope" in url:
                return json.dumps({
                    "id": "chatcmpl",
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                }).encode(), "application/json"
            return json.dumps(anthropic_msg(text)).encode(), "application/json"

        active = task_router.current_context(
            "deepseek", {"mode": "anthropic", "url": "unused"}, "sk-active")
        result = ultra.handle_request(req, active, cfg, fake_post, ledger_path=None)

        self.assertEqual(result.status, 200)
        self.assertEqual(len(calls), 2)
        debate = json.loads(result.body["content"][0]["text"])
        self.assertEqual(debate["schema"], "bio-debate/scientific-debate/1")
        self.assertEqual(debate["structured_debate_record"]["successful_turns"], 2)
        self.assertIn(debate["integrated_judgment"]["verdict"], {
            "provisionally_supported", "plausible_but_contested", "not_ready_for_strong_claim",
        })
        self.assertTrue(any(
            event.get("kind") == "scientific_debate"
            for event in result.route_plan["events"]
        ))


if __name__ == "__main__":
    unittest.main()
