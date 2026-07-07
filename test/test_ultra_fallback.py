import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import fallback_policy as fp
import task_router
import ultra_orchestrator as ultra


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


if __name__ == "__main__":
    unittest.main()
