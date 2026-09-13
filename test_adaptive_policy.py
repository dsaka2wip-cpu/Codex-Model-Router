"""Offline native policy checks: routing, precedence, wire preservation and privacy."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from adaptive_policy import AdaptivePolicy, DEFAULT, controls, escalate_selection

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
            {"model": "gpt-5.6-luna", "effort": "high", "subagents": [], "duration_ms": 123,
             "context_chars": 456, "source_context": "summary", "source_read_ms": 5000,
             "recent_turns_fetched": 3, "has_recent_context": True,
             "has_task_summary": True, "has_current_request": True,
             "usage": {"inputTokens": 50, "cachedInputTokens": 10, "outputTokens": 5,
                       "reasoningOutputTokens": 2, "totalTokens": 55}},
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
        records = [json.loads(line) for path in self.policy.audit_dir.glob("*.jsonl")
                   for line in path.read_text(encoding="utf-8").splitlines()]
        first = next(row for row in records if row["event"] == "classifier")
        self.assertEqual((first["duration_ms"], first["context_chars"], first["source_context"],
                          first["source_read_ms"], first["recent_turns_fetched"],
                          first["has_recent_context"], first["has_task_summary"],
                          first["has_current_request"], first["input_tokens"], first["cached_tokens"],
                          first["output_tokens"], first["reasoning_tokens"], first["total_tokens"]),
                         (123, 456, "summary", 5000, 3, True, True, True, 50, 10, 5, 2, 55))

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

    def test_classifier_failure_uses_local_route_without_fabricating_usage(self):
        def fail(*_args):
            raise TimeoutError("secret failure detail")
        self.policy.set_classifier(fail)
        request, routed = self.turn("What is JSON?")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]),
                         ("gpt-5.6-luna", "low"))
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "failed-classifier"}}})
        self.policy.on_server({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "same-thread", "turnId": "failed-classifier", "tokenUsage": {"last": {
                "inputTokens": 80, "cachedInputTokens": 20, "outputTokens": 20,
                "reasoningOutputTokens": 5, "totalTokens": 100}}}})
        self.assertEqual(self.policy.on_server({"method": "item/completed", "params": {
            "threadId": "same-thread", "turnId": "failed-classifier", "item": {
                "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "OK"}}}), [])
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "failed-classifier", "status": "completed"}}})[:-1]
        footer = rows[1]["params"]["item"]["text"]
        self.assertIn("판별: Sol / Medium 실패→로컬", footer)
        self.assertIn("이번 턴 관측 사용량 미수신", footer)
        self.assertIn("Astra/Ultra 기준 비용 절감 추정 미산출", footer)
        self.assertNotIn("판별 0", footer)
        audit = "\n".join(path.read_text(encoding="utf-8") for path in self.policy.audit_dir.glob("*.jsonl"))
        self.assertIn('"stage": "unknown"', audit)
        self.assertIn('"failure_kind": "timeout"', audit)
        self.assertNotIn("secret failure detail", audit)

    def test_classifier_failure_preserves_observed_partial_usage(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["classifier"] = {"model": "gpt-5.5", "effort": "medium"}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        error = TimeoutError("private")
        error.usage = {"inputTokens": 5, "cachedInputTokens": 0, "outputTokens": 1,
                       "reasoningOutputTokens": 0, "totalTokens": 6}
        def fail(*_args):
            raise error
        self.policy.set_classifier(fail)
        request, _ = self.turn("What is JSON?")
        classifier = self.policy.pending[("int", request["id"])]["classifier"]
        self.assertTrue(classifier["failed"])
        self.assertEqual(classifier["usage"]["totalTokens"], 6)
        self.policy.on_server({"id": request["id"], "result": {"turn": {"id": "partial"}}})
        self.policy.on_server({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "same-thread", "turnId": "partial", "tokenUsage": {"last": {
                "inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 2,
                "reasoningOutputTokens": 0, "totalTokens": 12}}}})
        self.assertEqual(self.policy.on_server({"method": "item/completed", "params": {
            "threadId": "same-thread", "turnId": "partial", "item": {
                "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "OK"}}}), [])
        rows = self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "partial", "status": "completed"}}})[:-1]
        footer = rows[1]["params"]["item"]["text"]
        self.assertIn("판별: gpt-5.5 / Medium 실패→로컬", footer)
        self.assertIn("이번 턴 관측 18 tokens (작업 12 + 판별 6)", footer)
        self.assertIn("Astra/Ultra 기준 비용 절감 추정 미산출", footer)

    def test_failed_turn_escalates_once_without_overriding_explicit_choice(self):
        self.policy.set_classifier(lambda *_args: {
            "model": "gpt-5.6-luna", "effort": "low", "subagents": [], "usage": None})
        first, _ = self.turn("First attempt")
        self.policy.on_server({"id": first["id"], "result": {"turn": {"id": "failed-turn"}}})
        self.policy.on_server({"method": "turn/completed", "params": {"threadId": "same-thread",
            "turn": {"id": "failed-turn", "status": "failed"}}})

        retry, routed = self.turn("Retry")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]),
                         ("gpt-5.6-luna", "medium"))
        self.assertEqual(self.policy.pending[("int", retry["id"])]["route"]["effort"], "medium")
        self.policy.on_server({"id": retry["id"], "error": {"code": -1}})
        explicit, routed = self.turn("[router model=luna effort=low]\nRetry explicitly")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]),
                         ("gpt-5.6-luna", "low"))
        self.accept(explicit)
        _, routed = self.turn("Next normal turn")
        self.assertEqual((routed["params"]["model"], routed["params"]["effort"]),
                         ("gpt-5.6-luna", "low"))

        self.assertEqual(escalate_selection(
            {"model": "gpt-5.6-luna", "effort": "max"}, self.policy.catalog),
            {"model": "gpt-5.6-terra", "effort": "max"})

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
        def classifier(_thread, prompt, *_args):
            return {"model": "gpt-5.6-luna" if "JSON" in prompt else "gpt-5.6-sol",
                    "effort": "low" if "JSON" in prompt else "medium", "subagents": [],
                    "usage": {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0,
                              "reasoningOutputTokens": 0, "totalTokens": 0}}
        self.policy.set_classifier(classifier)
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
        self.assertNotIn("판별:", rows[0]["params"]["delta"])
        self.assertIn("세션 1턴 · Luna 1 / Terra 0 / Sol 0 / Astra 0", rows[1]["params"]["item"]["text"])
        self.assertIn("이번 턴 관측 100 tokens (작업 100 + 판별 0)", rows[1]["params"]["item"]["text"])
        self.assertIn("관측 누적 100 tokens (1/1턴) · Astra/Ultra 기준 비용 절감 추정 98%",
                      rows[1]["params"]["item"]["text"])
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
        self.assertNotIn("판별:", footer)
        self.assertIn("직전 턴: FAST: Luna / Low", footer)
        self.assertIn("세션 2턴 · Luna 1 / Terra 0 / Sol 1 / Astra 0", footer)
        self.assertIn("이번 턴 관측 70 tokens (작업 70 + 판별 0)", footer)
        self.assertIn("관측 누적 170 tokens (2/2턴)", footer)

    def test_savings_require_complete_priced_usage(self):
        base = {"model": "gpt-5.6-sol", "effort": "medium", "classifier": None,
                "usage": {"inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 2,
                          "reasoningOutputTokens": 0}}
        self.assertIsNone(self.policy._estimate_units(base))
        priced = copy.deepcopy(base)
        priced["usage"]["totalTokens"] = 12
        self.assertIsNotNone(self.policy._estimate_units(priced))
        unpriced = copy.deepcopy(priced)
        unpriced["model"] = "gpt-5.5"
        self.assertIsNone(self.policy._estimate_units(unpriced))

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

    def test_audit_schema_tracks_policy_route_and_turn_without_content(self):
        self.policy.set_classifier(lambda *_args: {
            "model": "gpt-5.6-luna", "effort": "medium", "task_type": "code_edit",
            "subagents": [{"role": "implementation", "model": "gpt-5.6-sol", "effort": "medium"}],
            "usage": {"inputTokens": 20, "cachedInputTokens": 5, "outputTokens": 2,
                      "reasoningOutputTokens": 1, "totalTokens": 22},
        })
        request, _ = self.turn("[router model=sol]\nPRIVATE_PROMPT_MARKER")
        self.policy.on_server({"id": request["id"], "result": {
            "turn": {"id": "private-turn-id", "status": "inProgress"}}})
        self.policy.on_server({"method": "item/agentMessage/delta", "params": {
            "threadId": "same-thread", "turnId": "private-turn-id", "delta": "PRIVATE_DELTA"}})
        self.policy.on_server({"method": "item/commandExecution/requestApproval", "params": {
            "threadId": "same-thread", "turnId": "private-turn-id", "command": "PRIVATE_COMMAND"}})
        self.policy.on_server({"method": "item/completed", "params": {
            "threadId": "same-thread", "turnId": "private-turn-id", "item": {
                "id": "private-file", "type": "fileChange", "path": "PRIVATE_PATH"}}})
        self.policy.on_server({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "same-thread", "turnId": "private-turn-id", "tokenUsage": {"last": {
                "inputTokens": 50, "cachedInputTokens": 10, "outputTokens": 5,
                "reasoningOutputTokens": 2, "totalTokens": 55}}}})
        self.assertEqual(self.policy.on_server({"method": "item/completed", "params": {
            "threadId": "same-thread", "turnId": "private-turn-id", "item": {
                "id": "private-answer", "type": "agentMessage", "phase": "final_answer",
                "text": "PRIVATE_RESPONSE"}}}), [])
        self.policy.on_server({"method": "turn/completed", "params": {
            "threadId": "same-thread", "turn": {"id": "private-turn-id", "status": "completed"}}})

        records = [json.loads(line) for path in self.policy.audit_dir.glob("*.jsonl")
                   for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(row["schema_version"] == 2 and len(row["policy_fingerprint"]) == 16
                            for row in records))
        route = next(row for row in records if row["event"] == "route")
        self.assertEqual((route["task_type"], route["explicit_model"], route["explicit_effort"]),
                         ("code_edit", True, False))
        started = next(row for row in records if row["event"] == "turn_started")
        completed = next(row for row in records if row["event"] == "turn_completed")
        footer = next(row for row in records if row["event"] == "footer")
        self.assertEqual((started["turn"], completed["turn"], footer["turn"]),
                         (started["turn"], started["turn"], started["turn"]))
        self.assertIsInstance(completed["duration_ms"], int)
        self.assertIsInstance(completed["first_delta_ms"], int)
        self.assertGreater(footer["baseline_units"], 0)
        self.assertEqual((footer["total_tokens"], footer["classifier_tokens"],
                          footer["measured_total_tokens"], footer["session_measured_tokens"]),
                         (55, 22, 77, 77))
        log_text = json.dumps(records)
        for private in ("PRIVATE_PROMPT_MARKER", "PRIVATE_DELTA", "PRIVATE_COMMAND", "PRIVATE_PATH",
                        "PRIVATE_RESPONSE", "private-turn-id", "same-thread"):
            self.assertNotIn(private, log_text)

        old_fingerprint = self.policy.policy_fingerprint
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["tiers"]["NORMAL"]["effort"] = "high"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.policy._config()
        self.assertNotEqual(self.policy.policy_fingerprint, old_fingerprint)

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
