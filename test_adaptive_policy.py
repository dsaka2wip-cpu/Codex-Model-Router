"""Offline native policy checks: routing, precedence, wire preservation and privacy."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from adaptive_policy import AdaptivePolicy, DEFAULT, controls

CATALOG = [{"model": model, "supportedReasoningEfforts": [{"reasoningEffort": x} for x in efforts]}
           for model, efforts in (
               ("gpt-5.6-luna", ["low", "medium", "high", "xhigh", "max"]),
               ("gpt-5.6-terra", ["low", "medium", "high", "xhigh", "max", "ultra"]),
               ("gpt-5.6-sol", ["low", "medium", "high", "xhigh", "max", "ultra"]),
               ("gpt-6-astra", ["low", "medium", "high", "xhigh", "max", "ultra"]))]


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.config.write_text(json.dumps(DEFAULT), encoding="utf-8")
        self.policy = AdaptivePolicy(self.config, root / "audit")
        self.sequence = 10
        self.ready()

    def exchange(self, method, result, request_id, params=None):
        request = {"id": request_id, "method": method, "params": params or {}}
        self.assertIs(self.policy.on_client(request), request)
        self.policy.on_server({"id": request_id, "result": result})

    def ready(self):
        self.exchange("account/read", {"account": {"type": "chatgpt", "email": "never-log@example.invalid"}}, 1)
        self.exchange("model/list", {"data": CATALOG}, "1")

    def turn(self, text, thread="same-thread", **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": "turn/start", "params": {
            "threadId": thread, "model": "gpt-6-astra", "effort": "ultra",
            "input": [{"type": "text", "text": text, "text_elements": []}],
            "approvalPolicy": "on-request", "sandboxPolicy": {"type": "readOnly"}, **params}}
        before = copy.deepcopy(request)
        routed = self.policy.on_client(request)
        self.assertEqual(request, before)
        self.assertEqual(routed["params"]["input"], request["params"]["input"])
        self.assertEqual(routed["params"]["approvalPolicy"], request["params"]["approvalPolicy"])
        self.assertEqual(routed["params"]["sandboxPolicy"], request["params"]["sandboxPolicy"])
        return request, routed

    def accept(self, request):
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "turn", "status": "inProgress"}}})

    def test_three_tiers_and_same_thread_history_untouched(self):
        prompts = ["What is JSON?", "Implement a Python calculator function.",
                   "Design a distributed system architecture with tradeoffs."]
        actual = []
        for prompt in prompts:
            request, routed = self.turn(prompt)
            self.assertEqual(routed["params"]["threadId"], "same-thread")
            actual.append((routed["params"]["model"], routed["params"]["effort"]))
            self.accept(request)
        self.assertEqual(actual, [("gpt-5.6-luna", "low"), ("gpt-5.6-sol", "medium"), ("gpt-6-astra", "high")])

    def test_classifier_can_choose_model_and_effort_independently(self):
        decisions = iter([
            {"model": "gpt-5.6-luna", "effort": "high", "subagents": [], "usage": None},
            {"model": "gpt-5.6-terra", "effort": "max", "subagents": [], "usage": None},
            {"model": "gpt-6-astra", "effort": "ultra", "subagents": [], "usage": None},
        ])
        self.policy.set_classifier(lambda *_args: next(decisions))
        actual = []
        for prompt in ("Carefully extract one value.", "Implement this bounded change.",
                       "Resolve this exceptionally hard architecture conflict."):
            request, routed = self.turn(prompt)
            actual.append((routed["params"]["model"], routed["params"]["effort"]))
            self.accept(request)
        self.assertEqual(actual, [
            ("gpt-5.6-luna", "high"),
            ("gpt-5.6-terra", "max"),
            ("gpt-6-astra", "ultra"),
        ])

    def test_all_23_live_catalog_pairs_are_accepted(self):
        pairs = [(row["model"], effort["reasoningEffort"])
                 for row in CATALOG for effort in row["supportedReasoningEfforts"]]
        self.assertEqual(len(pairs), 23)
        decisions = iter({"model": model, "effort": effort, "subagents": [], "usage": None}
                         for model, effort in pairs)
        self.policy.set_classifier(lambda *_args: next(decisions))
        actual = []
        for index in range(len(pairs)):
            request, routed = self.turn(f"Case {index}")
            actual.append((routed["params"]["model"], routed["params"]["effort"]))
            self.accept(request)
        self.assertEqual(actual, pairs)

    def test_display_tier_is_recomputed_after_axis_override(self):
        self.policy.set_classifier(lambda *_args: {
            "model": "gpt-5.6-luna", "effort": "high", "subagents": [], "usage": None})
        request, routed = self.turn("[router model=sol]\nCareful narrow task.")
        pending = self.policy.pending[("int", request["id"])]
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"], pending["tier"]),
                         ("gpt-5.6-sol", "high", "DEEP"))

    def test_classifier_subagent_routes_are_injected_without_generated_task_text(self):
        self.policy.set_classifier(lambda *_args: {
            "model": "gpt-5.6-sol", "effort": "medium", "usage": None,
            "subagents": [
                {"role": "lookup", "model": "gpt-5.6-luna", "effort": "high"},
                {"role": "critical_review", "model": "gpt-6-astra", "effort": "ultra"},
            ],
        })
        collaboration = {"mode": "default", "settings": {"model": "gpt-5.6-sol",
            "reasoning_effort": "medium", "developer_instructions": "Keep this."}}
        _, routed = self.turn("Research and review these independent areas.",
                              model=None, effort=None, collaborationMode=collaboration)
        instructions = routed["params"]["collaborationMode"]["settings"]["developer_instructions"]
        self.assertIn("Keep this.", instructions)
        self.assertIn("[codex-route:lookup] gpt-5.6-luna / high", instructions)
        self.assertIn("[codex-route:critical_review] gpt-6-astra / ultra", instructions)

    def test_classifier_failure_uses_local_route(self):
        def fail(*_args):
            raise TimeoutError
        self.policy.set_classifier(fail)
        _, routed = self.turn("What is JSON?")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]),
                         ("gpt-5.6-luna", "low"))
        audit = "\n".join(path.read_text(encoding="utf-8") for path in self.policy.audit_dir.glob("*.jsonl"))
        self.assertIn('"stage": "unknown"', audit)

    def test_independent_axes_and_user_control_no_provenance_guess(self):
        request, routed = self.turn("[router model=gui effort=low]\nImplement a calculator.")
        self.assertEqual(routed["params"]["model"], "gpt-6-astra")
        self.assertEqual(routed["params"]["effort"], "low")
        self.accept(request)
        _, routed = self.turn("What is JSON?", model="gpt-5.6-sol", effort="high")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]), ("gpt-5.6-sol", "low"))
        request, routed = self.turn("[router off]\nWhat is JSON?", model="gpt-5.6-sol", effort="high")
        self.assertIs(routed, request)
        request, routed = self.turn("What is JSON?", model="gpt-6-astra", effort="ultra")
        self.assertIs(routed, request)
        request, routed = self.turn("[router auto]\nWhat is JSON?")
        self.assertEqual(routed["params"]["model"], "gpt-5.6-luna")

    def test_gui_settings_update_does_not_fabricate_manual_intent(self):
        update = {"method": "thread/settings/update", "id": 9, "params": {
            "threadId": "same-thread", "model": "gpt-6-astra", "effort": "high"}}
        self.assertIs(self.policy.on_client(update), update)
        _, routed = self.turn("What is JSON?")
        self.assertEqual(routed["params"]["model"], "gpt-5.6-luna")

    def test_explicit_prompt_model_effort_and_fixed_axis(self):
        _, routed = self.turn("/model sol high\nWhat is JSON?")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]), ("gpt-5.6-sol", "high"))
        _, routed = self.turn("[router model=sol effort=auto]\nWhat is JSON?")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]), ("gpt-5.6-sol", "low"))
        _, routed = self.turn("[router effort=gui]\nWhat is JSON?", effort="high")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]), ("gpt-5.6-luna", "high"))

    def test_plan_nested_precedence_preserves_mode_and_instructions(self):
        collaboration = {"mode": "plan", "settings": {"model": "gpt-6-astra",
            "reasoning_effort": "ultra", "developer_instructions": "Never change this marker."}}
        request, routed = self.turn("What is JSON?", model=None, effort=None, collaborationMode=collaboration)
        nested = routed["params"]["collaborationMode"]
        self.assertEqual(nested["mode"], "plan")
        self.assertEqual(nested["settings"]["developer_instructions"], collaboration["settings"]["developer_instructions"])
        self.assertEqual((nested["settings"]["model"], nested["settings"]["reasoning_effort"]), ("gpt-5.6-luna", "low"))
        self.assertIsNone(routed["params"]["model"])
        self.assertIsNone(routed["params"]["effort"])
        request, routed = self.turn("[router model=gui effort=low]\nWhat is JSON?", collaborationMode=collaboration)
        self.assertEqual(routed["params"]["collaborationMode"]["settings"]["model"], "gpt-6-astra")

    def test_completed_auto_turn_restores_idle_sol_medium_and_preserves_mode(self):
        restored, done = [], threading.Event()
        self.policy.set_settings_updater(lambda params: (restored.append(copy.deepcopy(params)), done.set()))
        collaboration = {"mode": "plan", "settings": {"model": "gpt-6-astra",
            "reasoning_effort": "ultra", "developer_instructions": "Keep this."}}
        request, routed = self.turn("Design a distributed system architecture.", model=None, effort=None,
                                    collaborationMode=collaboration)
        self.assertEqual(routed["params"]["collaborationMode"]["settings"]["model"], "gpt-6-astra")
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "restore-turn"}}})
        self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "restore-turn", "status": "completed"}}})
        self.assertTrue(done.wait(1))
        self.assertEqual((restored[0]["model"], restored[0]["effort"]),
                         ("gpt-5.6-sol", "medium"))
        nested = restored[0]["collaborationMode"]
        self.assertEqual((nested["mode"], nested["settings"]["model"],
                          nested["settings"]["reasoning_effort"],
                          nested["settings"]["developer_instructions"]),
                         ("plan", "gpt-5.6-sol", "medium", "Keep this."))

        restored.clear()
        off, routed = self.turn("[router off]\nWhat is JSON?", model="gpt-5.6-sol", effort="high")
        self.assertIs(off, routed)
        self.policy.on_server({"id": off["id"], "result": {"turn": {"id": "off-turn"}}})
        self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "off-turn", "status": "completed"}}})
        self.assertEqual(restored, [])

    def test_resume_effort_inheritance_and_independent_model_auto(self):
        self.exchange("thread/resume", {"thread": {"id": "same-thread"},
            "model": "gpt-6-astra", "reasoningEffort": "high", "modelProvider": "openai"},
            99, {"threadId": "same-thread"})
        _, routed = self.turn("[router model=auto effort=gui]\nWhat is JSON?", effort=None)
        self.assertEqual(routed["params"]["model"], "gpt-5.6-luna")
        self.assertIsNone(routed["params"]["effort"])
    def test_genuine_notifications_observed_without_rewriting(self):
        notification = {"method": "thread/settings/updated", "params": {
            "threadId": "same-thread", "threadSettings": {"model": "gpt-5.6-sol", "effort": "medium",
                "modelProvider": "openai", "collaborationMode": {"mode": "plan", "settings": {
                    "model": "gpt-5.6-sol", "reasoning_effort": "medium"}}}}}
        original = copy.deepcopy(notification)
        self.assertIsNone(self.policy.on_server(notification))
        self.assertEqual(notification, original)
        self.assertEqual(self.policy.threads["same-thread"]["settings"]["model"], "gpt-5.6-sol")

    def test_final_answer_gets_local_footer_and_real_usage_weight(self):
        first, _ = self.turn("What is JSON?")
        self.policy.on_server({"id": first["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        self.policy.on_server({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "same-thread", "turnId": "turn-1", "tokenUsage": {
                "last": {"inputTokens": 80, "cachedInputTokens": 20, "outputTokens": 20,
                         "reasoningOutputTokens": 5, "totalTokens": 100},
                "total": {"inputTokens": 80, "cachedInputTokens": 20, "outputTokens": 20,
                          "reasoningOutputTokens": 5, "totalTokens": 100}}}})
        completed = {"method": "item/completed", "params": {"threadId": "same-thread",
            "turnId": "turn-1", "item": {"id": "answer-1", "type": "agentMessage",
                "phase": "final_answer", "text": "JSON is data."}}}
        original = copy.deepcopy(completed)
        self.assertEqual(self.policy.on_server(completed), [])
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "turn-1", "status": "completed"}}})[:-1]
        self.assertEqual(completed, original)
        self.assertEqual(rows[0]["method"], "item/agentMessage/delta")
        self.assertIn("이번 턴: FAST: Luna / Low", rows[0]["params"]["delta"])
        self.assertIn("세션 1턴 · Luna 1 / Terra 0 / Sol 0 / Astra 0", rows[1]["params"]["item"]["text"])
        self.assertIn("사용량 절감 추정 98%", rows[1]["params"]["item"]["text"])
        self.assertIsNone(self.policy.on_server(completed))

        second, _ = self.turn("Implement a calculator.")
        self.policy.on_server({"id": second["id"], "result": {"turn": {"id": "turn-2", "status": "inProgress"}}})
        completed = {"method": "item/completed", "params": {"threadId": "same-thread",
            "turnId": "turn-2", "item": {"id": "answer-2", "type": "agentMessage",
                "phase": "final_answer", "text": "Done."}}}
        self.assertEqual(self.policy.on_server(completed), [])
        self.policy.on_server({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "same-thread", "turnId": "turn-2", "tokenUsage": {
                "last": {"inputTokens": 60, "cachedInputTokens": 10, "outputTokens": 10,
                         "reasoningOutputTokens": 2, "totalTokens": 70},
                "total": {"inputTokens": 140, "cachedInputTokens": 30, "outputTokens": 30,
                          "reasoningOutputTokens": 7, "totalTokens": 170}}}})
        self.assertEqual(self.policy.turns["turn-2"]["usage_source"], "total_delta")
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "turn-2", "status": "completed"}}})[:-1]
        footer = rows[1]["params"]["item"]["text"]
        self.assertIn("이번 턴: NORMAL: Sol / Medium", footer)
        self.assertIn("직전 턴: FAST: Luna / Low", footer)
        self.assertIn("세션 2턴 · Luna 1 / Terra 0 / Sol 1 / Astra 0", footer)

    def test_commentary_and_server_history_are_not_modified(self):
        request, _ = self.turn("Implement a calculator.")
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "turn", "status": "inProgress"}}})
        commentary = {"method": "item/completed", "params": {"threadId": "same-thread",
            "turnId": "turn", "item": {"id": "note", "type": "agentMessage",
                "phase": "commentary", "text": "Working."}}}
        self.assertIsNone(self.policy.on_server(commentary))
        self.assertEqual(commentary["params"]["item"]["text"], "Working.")

    def test_plan_footer_uses_plan_delta_and_gui_passthrough_is_counted(self):
        collaboration = {"mode": "plan", "settings": {"model": "gpt-6-astra",
            "reasoning_effort": "ultra", "developer_instructions": None}}
        request, _ = self.turn("Design a distributed system architecture.", model=None, effort=None,
                               collaborationMode=collaboration)
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "plan-turn"}}})
        self.assertEqual(self.policy.on_server({"method": "item/completed", "params": {"threadId": "same-thread",
            "turnId": "plan-turn", "item": {"id": "plan", "type": "plan", "text": "1. Plan"}}}), [])
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "plan-turn", "status": "completed"}}})[:-1]
        self.assertEqual(rows[0]["method"], "item/plan/delta")
        self.assertIn("DEEP: Astra / High", rows[1]["params"]["item"]["text"])

        off, routed = self.turn("[router off]\nWhat is JSON?", model="gpt-5.6-sol", effort="high")
        self.assertIs(off, routed)
        self.policy.on_server({"id": off["id"], "result": {"turn": {"id": "gui-turn"}}})
        self.assertEqual(self.policy.on_server({"method": "item/completed", "params": {"threadId": "same-thread",
            "turnId": "gui-turn", "item": {"id": "answer", "type": "agentMessage",
                "phase": "final_answer", "text": "OK"}}}), [])
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "gui-turn", "status": "completed"}}})[:-1]
        self.assertIn("GUI: Sol / High", rows[1]["params"]["item"]["text"])

    def test_auth_catalog_unavailable_custom_provider_and_bad_combo_passthrough(self):
        self.policy.auth = None
        original, routed = self.turn("What is JSON?")
        self.assertIs(original, routed)
        self.policy.auth = "apiKey"
        original, routed = self.turn("What is JSON?")
        self.assertIs(original, routed)
        self.ready()
        self.policy.threads["same-thread"]["settings"]["modelProvider"] = "custom"
        original, routed = self.turn("What is JSON?")
        self.assertIs(original, routed)
        self.policy.threads["same-thread"]["settings"]["modelProvider"] = "openai"
        original, routed = self.turn("[router effort=gui]\nWhat is JSON?", effort="ultra")
        self.assertIs(original, routed)

    def test_rejection_does_not_commit_role_or_control(self):
        request, _ = self.turn("[router model=sol]\nDesign a distributed system architecture.")
        self.policy.on_server({"id": request["id"], "error": {"code": -1, "message": "private"}})
        _, routed = self.turn("What is JSON?")
        self.assertEqual(routed["params"]["model"], "gpt-5.6-luna")

    def test_no_tools_approvals_steering_or_account_packets_changed(self):
        for method in ["turn/steer", "turn/interrupt", "thread/resume", "thread/settings/update",
                       "account/login/start", "command/exec", "custom/future"]:
            message = {"id": 7, "method": method, "params": {"threadId": "same-thread", "token": "secret-token",
                       "input": [{"type": "text", "text": "What is JSON?"}]}}
            self.assertIs(self.policy.on_client(message), message)
        response = {"id": 7, "result": {"decision": "accept"}}
        self.assertIs(self.policy.on_client(response), response)

    def test_privacy_invalid_config_and_control_are_fail_open(self):
        self.turn("What is JSON? PRIVATE_PROMPT_MARKER")
        self.policy.on_server({"id": self.sequence, "error": {"code": -1, "message": "PRIVATE_ERROR_TOKEN"}})
        self.policy.audit("policy_error", command="PRIVATE_COMMAND", token="PRIVATE_KEY", model="API_KEY_VALUE")
        self.config.write_text("{bad config", encoding="utf-8")
        original, routed = self.turn("What is JSON?")
        self.assertIs(original, routed)
        logs = "\n".join(p.read_text() for p in self.policy.audit_dir.glob("*.jsonl"))
        for private in ("PRIVATE_PROMPT_MARKER", "PRIVATE_ERROR_TOKEN", "PRIVATE_COMMAND", "PRIVATE_KEY",
                        "never-log@example.invalid", "same-thread", "API_KEY_VALUE"):
            self.assertNotIn(private, logs)
        for line in logs.splitlines():
            json.loads(line)
        with self.assertRaises(ValueError):
            controls("[router effort=invalid]\nhello")


if __name__ == "__main__":
    unittest.main()
