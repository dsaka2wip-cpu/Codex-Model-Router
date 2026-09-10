"""Offline routing, same-thread, protocol and localhost boundary checks."""
import copy
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from main_policy import choose_route
from main_router import MainRouter, approval_result, make_server

CATALOG = [
    {"model": model, "defaultReasoningEffort": "medium",
     "supportedReasoningEfforts": [{"reasoningEffort": x} for x in efforts]}
    for model, efforts in [("gpt-5.6-luna", ["low", "medium", "high"]),
                           ("gpt-5.6-sol", ["low", "medium", "high"]),
                           ("gpt-6-astra", ["low", "medium", "high", "ultra"])]
]


class FakeCodex:
    def __init__(self):
        self.events = queue.Queue()
        self.calls = []
        self.replies = []
        self.history = []
        self.closed = False
        self.turn = 0

    def call(self, method, params, timeout=30):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "model/list":
            return {"data": CATALOG, "nextCursor": None}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "model": params["model"]}
        if method in ("thread/read", "thread/resume"):
            return {"thread": {"id": params["threadId"], "turns": self.history, "status": {"type": "idle"}},
                    "cwd": str(Path.cwd())}
        if method == "turn/start":
            self.turn += 1
            tid = str(self.turn)
            self.events.put({"method": "turn/started", "params": {"threadId": "thread-1", "turn": {"id": tid}}})
            return {"turn": {"id": tid}}
        if method == "turn/interrupt":
            self.complete(params["turnId"], "interrupted")
            return {}
        raise AssertionError(method)

    def complete(self, turn_id=None, status="completed"):
        self.events.put({"method": "turn/completed", "params": {"threadId": "thread-1",
                         "turn": {"id": turn_id or str(self.turn), "status": status}}})

    def reply(self, request_id, result=None, error=None):
        self.replies.append((request_id, result, error))

    def close(self):
        self.closed = True


def wait_for(check):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


class MainRoutingTests(unittest.TestCase):
    def test_rules_context_explicit_and_source_preservation(self):
        cases = [
            ("하위 작업이 정확히 뭐야?", "lookup"),
            ("로그인 버튼 UI 구현해줘", "implementation"),
            ("결제 코드의 권한 취약점을 검토해줘", "critical_review"),
            ("분산 시스템 아키텍처를 설계해줘", "hard_problem"),
            ("뜻이 뭐야? 그리고 이 코드를 수정해줘", "implementation"),
            ("아키텍처가 뭐야?", "lookup"),
            ("작업 좀 해줘", "implementation"),
        ]
        for prompt, role in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(choose_route(prompt, catalog=CATALOG)["role"], role)
        previous = choose_route("분산 시스템 아키텍처를 설계해줘")
        self.assertEqual(choose_route("그럼 구현해", previous)["role"], "hard_problem")
        self.assertEqual(choose_route("원래 목적을 달성해야지", previous)["role"], "hard_problem")
        self.assertEqual(choose_route("새 질문: 아키텍처가 뭐야?", previous)["role"], "lookup")
        self.assertEqual(choose_route("다음 중 2+2는 무엇인가요?", previous)["role"], "lookup")
        self.assertEqual(choose_route("/model luna low\n간단한 설명", previous, catalog=CATALOG)["effort"], "low")
        self.assertEqual(choose_route("Luna로 답해줘. 안녕", catalog=CATALOG)["model"], "gpt-5.6-luna")
        self.assertEqual(choose_route("Sol이 뭐야?", catalog=CATALOG)["model"], "gpt-5.6-luna")
        with self.assertRaises(ValueError):
            choose_route("안녕", model="luna", effort="ultra", catalog=CATALOG)
        with self.assertRaises(ValueError):
            choose_route("안녕", model="unknown", catalog=CATALOG)
        self.assertEqual(choose_route("추출해줘\n" + "x" * 4000)["role"], "implementation")
        self.assertEqual(choose_route("SQL injection 취약점이 뭐야?")["role"], "critical_review")
        self.assertEqual(choose_route("Translate the word authentication")["role"], "lookup")
        fenced = chr(96) * 3 + "\narchitecture design\n" + chr(96) * 3 + "\ntranslate this"
        self.assertEqual(choose_route(fenced)["role"], "lookup")

    def setUp(self):
        self.client = FakeCodex()
        self.router = MainRouter(client=self.client, ephemeral=True)
        self.addCleanup(self.router.close)

    def send(self, prompt):
        self.router.send({"prompt": prompt, "cwd": str(Path.cwd())})
        wait_for(lambda: self.router.turn_id is not None)

    def test_two_main_models_same_thread_no_classifier_or_subagent(self):
        first = "하위 작업이 뭐야?\n기억할 단어는 cedar-47."
        second = "Python 함수 구현해줘. 앞선 단어를 반환해."
        self.send(first)
        self.client.complete()
        wait_for(lambda: not self.router.snapshot()["busy"])
        self.send(second)
        calls = [(m, p) for m, p in self.client.calls if m == "turn/start"]
        self.assertEqual(len(calls), 2)
        self.assertEqual([p["model"] for _, p in calls], ["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual([p["effort"] for _, p in calls], ["medium", "medium"])
        self.assertEqual([p["threadId"] for _, p in calls], ["thread-1", "thread-1"])
        self.assertEqual([p["input"][0]["text"] for _, p in calls], [first, second])
        self.assertEqual(sum(m == "thread/start" for m, _ in self.client.calls), 1)
        self.assertFalse(any("spawn" in m for m, _ in self.client.calls))
        self.assertEqual([m["text"] for m in self.router.snapshot()["messages"] if m["role"] == "user"], [first, second])
        self.client.complete()
        wait_for(lambda: not self.router.snapshot()["busy"])

    def test_busy_does_not_duplicate_send_and_stale_completion_ignored(self):
        self.send("하위 작업이 뭐야?")
        with self.assertRaises(ValueError):
            self.router.send({"prompt": "중복 요청"})
        self.client.complete("stale")
        time.sleep(0.03)
        self.assertTrue(self.router.snapshot()["busy"])
        self.router.interrupt()
        wait_for(lambda: not self.router.snapshot()["busy"])
        self.assertEqual(sum(m == "turn/start" for m, _ in self.client.calls), 1)

    def test_pending_request_requires_exact_user_reply_including_child(self):
        self.send("하위 작업이 뭐야?")
        self.client.events.put({"id": 91, "method": "item/permissions/requestApproval",
                                "params": {"threadId": "child-1", "turnId": "child-turn",
                                           "permissions": {"network": {"enabled": True}}}})
        wait_for(lambda: bool(self.router.snapshot()["pending"]))
        self.assertEqual(self.client.replies, [])
        self.router.respond({"id": "91", "action": "accept"})
        self.assertEqual(self.client.replies[0][1], {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
        with self.assertRaises(ValueError):
            self.router.respond({"id": "91", "action": "accept"})
        self.client.complete()

    def test_numeric_and_string_rpc_ids_are_distinct(self):
        for rid in (91, "91"):
            self.client.events.put({"id": rid, "method": "item/commandExecution/requestApproval", "params": {"command": "echo test"}})
        wait_for(lambda: len(self.router.snapshot()["pending"]) == 2)
        keys = [p["id"] for p in self.router.snapshot()["pending"]]
        self.assertNotEqual(*keys)
        self.router.respond({"id": keys[0], "action": "decline"})
        self.router.respond({"id": keys[1], "action": "accept"})
        self.assertIsInstance(self.client.replies[0][0], int)
        self.assertIsInstance(self.client.replies[1][0], str)

    def test_child_request_expiry_removes_approval_card(self):
        self.client.events.put({"id": "child-request", "method": "item/fileChange/requestApproval",
                                "params": {"threadId": "child-thread"}})
        wait_for(lambda: bool(self.router.snapshot()["pending"]))
        key = self.router.snapshot()["pending"][0]["id"]
        self.client.events.put({"method": "serverRequest/resolved",
                                "params": {"threadId": "child-thread", "requestId": "child-request"}})
        wait_for(lambda: not self.router.snapshot()["pending"])
        with self.assertRaises(ValueError):
            self.router.respond({"id": key, "action": "accept"})

    def test_timeout_does_not_enable_duplicate_turn(self):
        original = self.client.call
        def call(method, params, timeout=30):
            if method == "turn/start":
                raise TimeoutError("ack delayed")
            return original(method, params, timeout)
        self.client.call = call
        self.router.send({"prompt": "안녕"})
        wait_for(lambda: self.router.snapshot()["error"] is not None)
        self.assertTrue(self.router.snapshot()["busy"])
        with self.assertRaises(ValueError):
            self.router.send({"prompt": "duplicate"})
        self.client.complete("late-success")
        wait_for(lambda: not self.router.snapshot()["busy"])
        self.assertIsNone(self.router.snapshot()["error"])

    def test_rejected_turn_restores_previous_route_and_removes_phantom(self):
        original = self.client.call
        before = choose_route("분산 시스템 아키텍처를 설계해줘")
        self.router.state["route"] = before
        def call(method, params, timeout=30):
            if method == "turn/start":
                raise RuntimeError("rejected")
            return original(method, params, timeout)
        self.client.call = call
        self.router.send({"prompt": "안녕"})
        wait_for(lambda: self.router.snapshot()["error"] is not None)
        self.assertFalse(self.router.snapshot()["busy"])
        self.assertEqual(self.router.snapshot()["route"], before)
        self.assertEqual(self.router.snapshot()["messages"], [])

    def test_child_patch_must_be_available_before_approval(self):
        params = {"threadId": "child-thread", "turnId": "child-turn", "itemId": "patch-1"}
        self.client.events.put({"id": "patch-request", "method": "item/fileChange/requestApproval", "params": params})
        wait_for(lambda: bool(self.router.snapshot()["pending"]))
        key = self.router.snapshot()["pending"][0]["id"]
        with self.assertRaises(ValueError):
            self.router.respond({"id": key, "action": "accept"})
        self.client.events.put({"method": "item/started", "params": {"threadId": "child-thread", "turnId": "child-turn",
                               "item": {"id": "patch-1", "type": "fileChange", "changes": [{"path": "a.py", "diff": "+safe"}]}}})
        wait_for(lambda: not self.router.snapshot()["pending"][0]["params"]["_reviewUnavailable"])
        self.assertEqual(self.router.snapshot()["pending"][0]["params"]["_item"]["changes"][0]["diff"], "+safe")
        self.router.respond({"id": key, "action": "accept"})
        self.assertEqual(self.client.replies[-1][1], {"decision": "accept"})

    def test_approval_wire_contracts(self):
        self.assertEqual(approval_result("item/fileChange/requestApproval", {}, {"action": "decline"}), {"decision": "decline"})
        questions = {"questions": [{"id": "choice"}]}
        result = approval_result("item/tool/requestUserInput", questions, {"action": "answer", "answers": {"choice": ["one"]}})
        self.assertEqual(result, {"answers": {"choice": {"answers": ["one"]}}})
        with self.assertRaises(ValueError):
            approval_result("item/tool/requestUserInput", questions, {"action": "answer", "answers": {}})
        with self.assertRaises(ValueError):
            approval_result("unsupported", {}, {"action": "accept"})

    def test_corrupt_saved_state_requires_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "session.json"
            state_file.write_text("invalid", encoding="utf-8")
            router = MainRouter(client=FakeCodex(), state_file=state_file)
            try:
                self.assertIn("복구 실패", router.snapshot()["error"])
                with self.assertRaises(ValueError):
                    router.send({"prompt": "안녕"})
                router.reset({"cwd": str(Path.cwd())})
                self.assertIsNone(router.snapshot()["error"])
            finally:
                router.close()

    def test_local_http_auth_origin_host_and_body_validation(self):
        server = make_server(self.router, port=0, token="test-only-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(path, headers=None, body=None):
            return urllib.request.urlopen(urllib.request.Request(server.origin + path, data=body, headers=headers or {}), timeout=3)

        with self.assertRaises(urllib.error.HTTPError) as denied:
            request("/api/state")
        self.assertEqual(denied.exception.code, 403)
        headers = {"X-Router-Token": server.token}
        with request("/api/state", headers) as response:
            self.assertIn("models", json.load(response))
            self.assertIn("sha256-", response.headers["Content-Security-Policy"])
        for extra in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/api/state", {**headers, **extra})
            self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            request("/api/send", {**headers, "Content-Type": "application/json"}, b"[]")
        self.assertEqual(denied.exception.code, 400)
        self.assertFalse(any(m == "turn/start" for m, _ in self.client.calls))


if __name__ == "__main__":
    unittest.main()
