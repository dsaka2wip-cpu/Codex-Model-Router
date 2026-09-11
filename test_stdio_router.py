import copy
import io
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_policy import AdaptivePolicy, DEFAULT
from classifier_eval import SnapshotClassifier, score_case, summarize
from stdio_router import (RECENT_CONTEXT_CHARS, RECENT_TURNS, STATE_SUMMARY_CHARS,
                          ClassifierFailure, InternalClassifier, _json_transform, _start_requested_eval,
                          RpcFailure, run_bridge)


class Policy:
    def __init__(self):
        self.client = []
        self.server = []
        self.audit_events = []
        self.classifier = None
        self.settings_updater = None

    def set_classifier(self, callback):
        self.classifier = callback

    def set_settings_updater(self, callback):
        self.settings_updater = callback

    def on_client(self, message):
        self.client.append(message)
        if message.get("method") != "turn/start":
            return message
        changed = dict(message)
        changed["params"] = dict(message["params"], model="routed-model", effort="high")
        return changed

    def on_server(self, message):
        self.server.append(message)

    def audit(self, event):
        self.audit_events.append(event)


class FooterPolicy(Policy):
    def on_server(self, message):
        if message.get("method") != "item/completed":
            return None
        changed = copy.deepcopy(message)
        changed["params"]["item"]["text"] += "\nfooter"
        return [{"method": "item/agentMessage/delta", "params": {"delta": "\nfooter"}}, changed]


class StdioRouterTests(unittest.TestCase):
    def bridge(self, payload, policy=None, *child_args, limit=16 * 1024 * 1024):
        output, errors = io.BytesIO(), io.BytesIO()
        code = run_bridge(
            sys.executable,
            [str(Path(__file__).resolve()), "--fake-child", *child_args, "app-server"],
            policy=policy,
            stdin=io.BytesIO(payload),
            stdout=output,
            stderr=errors,
            max_line_bytes=limit,
            classifier_sidecar=False,
        )
        return code, output.getvalue(), errors.getvalue()

    def test_bidirectional_protocol_is_transparent_except_turn_start(self):
        policy = Policy()
        raw = (
            b'{"id":"abc","method":"turn/start","params":{"input":"\xe2\x98\x83"}}\n'
            b'{"id":7,"result":{"decision":"accept"}}\n'
            b'{"method":"unknown/event","params":{"raw":true}}\n'
            b'\xff malformed\n'
        )
        code, output, errors = self.bridge(raw, policy, "--emit-event")
        lines = output.splitlines(keepends=True)
        routed = json.loads(lines[0])
        self.assertEqual((routed["id"], routed["params"]["model"], routed["params"]["effort"]),
                         ("abc", "routed-model", "high"))
        self.assertEqual(lines[1:4], raw.splitlines(keepends=True)[1:])
        self.assertEqual(json.loads(lines[4])["method"], "item/agentMessage/delta")
        self.assertEqual(policy.client[0]["id"], "abc")
        self.assertEqual(policy.server[-1]["method"], "item/agentMessage/delta")
        self.assertTrue(callable(policy.classifier))
        self.assertTrue(callable(policy.settings_updater))
        self.assertEqual(errors, b"child-stderr\x00\xff")
        self.assertEqual(code, 0)

    def test_non_utf8_json_is_preserved_byte_for_byte(self):
        raw = '{"id":1,"method":"turn/start","params":{}}\n'.encode("utf-16-be")
        policy = Policy()
        code, output, _ = self.bridge(raw, policy)
        self.assertEqual((code, output), (0, raw))
        self.assertEqual(policy.client, [])

    def test_opt_in_eval_uses_bounded_existing_runner(self):
        with patch.dict(os.environ, {"CODEX_ROUTER_CLASSIFIER_EVAL": "6"}), \
                patch("stdio_router.subprocess.Popen") as popen:
            process = _start_requested_eval()
        self.assertIs(process, popen.return_value)
        command = popen.call_args.args[0]
        self.assertEqual(command[-3:], ["--run", "--limit", "6"])
        self.assertTrue(command[1].endswith("classifier_eval.py"))
        with patch.dict(os.environ, {"CODEX_ROUTER_CLASSIFIER_EVAL": "6"}), \
                patch("stdio_router.subprocess.Popen", side_effect=OSError):
            self.assertIsNone(_start_requested_eval())

    def test_rpc_error_is_redacted_to_safe_category(self):
        error = RpcFailure({"code": -32602, "message": "Invalid params containing secret details"})
        self.assertEqual((error.rpc_code, error.error_kind), (-32602, "invalid_params"))
        self.assertNotIn("secret", str(error))

    def test_classifier_context_is_bounded_and_keeps_state_plus_recent_turns(self):
        turns = [{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": f"request-{i}-" + "x" * 2500}]},
            {"type": "agentMessage", "phase": "final_answer", "text": f"answer-{i}-" + "y" * 2500},
        ]} for i in range(8)]
        summary = "active goal and constraints"
        context = InternalClassifier._context({"turns": turns}, summary)
        self.assertIn("[Persistent task state]\n" + summary, context)
        self.assertNotIn("request-4-", context)
        self.assertIn("answer-7-", context)
        self.assertLessEqual(len(context), STATE_SUMMARY_CHARS + RECENT_CONTEXT_CHARS + 80)
        self.assertEqual(RECENT_TURNS, 3)

    def test_source_read_timeout_uses_existing_summary(self):
        classifier = InternalClassifier(io.BytesIO())
        timeouts = []
        classifier._call = lambda _method, _params, timeout=None: (
            timeouts.append(timeout), (_ for _ in ()).throw(TimeoutError()))[1]
        thread, context, mode, _duration = classifier._read_source("main", "Active state")
        self.assertEqual((thread, context, mode),
                         ({"turns": []}, "[Persistent task state]\nActive state", "summary"))
        self.assertEqual(timeouts, [5])

    def test_late_internal_response_after_timeout_stays_hidden(self):
        classifier = InternalClassifier(io.BytesIO(), timeout=0.001)
        with self.assertRaises(TimeoutError):
            classifier._call("thread/read", {})
        request_id = next(iter(classifier.ignored_responses))
        self.assertTrue(classifier.handle_server({"id": request_id, "result": {}}))
        self.assertFalse(classifier.ignored_responses)

    def test_sidecar_timeout_discards_it_until_next_turn(self):
        class Sidecar:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        sidecars = [Sidecar(), Sidecar()]
        classifier = InternalClassifier(io.BytesIO(), sidecar_factory=lambda: sidecars.pop(0))
        classifier._read_source = lambda *_args: ({"turns": []}, "", "read", 0)
        classifier._start_hidden = lambda *_args: (_ for _ in ()).throw(TimeoutError())
        first = sidecars[0]
        with self.assertRaises(ClassifierFailure) as raised:
            classifier.classify("main", "request", {"model": "gpt-5.6-sol", "effort": "medium"},
                                {"gpt-5.6-sol": {"efforts": ["medium"]}}, [])
        self.assertEqual(raised.exception.stage, "sidecar_start")
        self.assertTrue(first.closed)
        replacement = sidecars[0]
        self.assertIs(classifier._get_sidecar(), replacement)

    def test_classifier_uses_lightweight_sidecar_and_recreates_it_after_exit(self):
        class Process:
            returncode = None

            def poll(self):
                return self.returncode

        class Sidecar:
            def __init__(self):
                self.events, self.calls, self.process, self.closed = queue.Queue(), [], Process(), False

            def call(self, method, params, timeout=None):
                self.calls.append((method, params, timeout))
                if method == "thread/start":
                    return {"thread": {"id": "sidecar-thread", "ephemeral": True}}
                if method == "turn/start":
                    common = {"threadId": "sidecar-thread", "turnId": "sidecar-turn"}
                    self.events.put({"method": "item/completed", "params": {**common, "item": {
                        "type": "agentMessage", "phase": "final_answer",
                        "text": json.dumps({"model": "gpt-5.6-luna", "effort": "high",
                                            "subagents": [], "state_summary": "Keep active constraints."})}}})
                    self.events.put({"method": "thread/tokenUsage/updated", "params": {**common,
                        "tokenUsage": {"last": {"inputTokens": 900, "cachedInputTokens": 0,
                                                  "outputTokens": 30, "reasoningOutputTokens": 10,
                                                  "totalTokens": 930}}}})
                    self.events.put({"method": "turn/completed", "params": {**common,
                        "turn": {"id": "sidecar-turn", "status": "completed"}}})
                    return {"turn": {"id": "sidecar-turn", "status": "inProgress"}}
                if method == "thread/delete":
                    return {}
                raise AssertionError(method)

            def reply(self, *_args, **_kwargs):
                raise AssertionError("sidecar requested a tool")

            def close(self):
                self.closed = True

        sidecars = [Sidecar(), Sidecar()]
        classifier = InternalClassifier(io.BytesIO(), sidecar_factory=lambda: sidecars.pop(0))
        classifier._call = lambda method, _params, timeout=None: ({"thread": {
            "id": "main-thread", "cwd": os.getcwd(), "turns": [{"items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "Prior request"}]},
                {"type": "agentMessage", "phase": "final_answer", "text": "Prior answer"},
            ]}]}} if method == "thread/read" else (_ for _ in ()).throw(AssertionError(method)))
        catalog = {
            "gpt-5.6-luna": {"efforts": ["low", "high"]},
            "gpt-5.6-sol": {"efforts": ["medium"]},
        }
        first = sidecars[0]
        result = classifier.classify("main-thread", "Current request",
                                     {"model": "gpt-5.6-sol", "effort": "medium"}, catalog, [])
        self.assertEqual((result["model"], result["effort"], result["context_mode"],
                          result["usage"]["inputTokens"]),
                         ("gpt-5.6-luna", "high", "sidecar", 900))
        turn = next(params for method, params, _ in first.calls if method == "turn/start")
        self.assertIn("Current request", turn["input"][0]["text"])
        first.process.returncode = 1
        replacement = classifier._get_sidecar()
        self.assertTrue(first.closed)
        self.assertIsNot(replacement, first)
        classifier.close()
        self.assertTrue(replacement.closed)

    def test_eval_scorer_separates_acceptance_from_critical_underroute(self):
        case = {"id": "critical", "group": "review", "expected": {
            "models": ["gpt-5.6-sol", "gpt-6-astra"], "efforts": ["high", "xhigh"],
            "minimum_model": "gpt-5.6-sol", "minimum_effort": "high",
            "subagents": [1, 2], "roles": ["critical_review"]}}
        low = score_case(case, {"model": "gpt-5.6-luna", "effort": "low", "subagents": [],
                                "usage": {"totalTokens": 10}})
        good = score_case(case, {"model": "gpt-5.6-sol", "effort": "high", "subagents": [
            {"role": "critical_review", "model": "gpt-5.6-sol", "effort": "high"}],
                                 "usage": {"totalTokens": 20}})
        report = summarize([low, good], 2)
        self.assertFalse(low["accepted"])
        self.assertTrue(low["critical_underroute"])
        self.assertTrue(good["accepted"])
        self.assertEqual((report["critical_underroutes"], report["classifier_total_tokens"]), (1, 30))

    def test_eval_classifier_replaces_discarded_sidecar(self):
        class Sidecar:
            process = None

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        first, replacement = Sidecar(), Sidecar()
        classifier = SnapshotClassifier(first, {}, lambda: replacement)
        self.assertIs(classifier._get_sidecar(), first)
        classifier._discard_sidecar(first)
        self.assertTrue(first.closed)
        self.assertIs(classifier._get_sidecar(), replacement)

    def test_server_transform_can_inject_footer_delta_before_completed_item(self):
        transform = _json_transform(FooterPolicy(), "server")
        raw = b'{"method":"item/completed","params":{"item":{"text":"answer"}}}\r\n'
        rows = [json.loads(line) for line in transform(raw).splitlines()]
        self.assertEqual(rows[0]["method"], "item/agentMessage/delta")
        self.assertEqual(rows[1]["params"]["item"]["text"], "answer\nfooter")

    def test_server_transform_can_hold_a_final_item(self):
        class HoldPolicy(Policy):
            def on_server(self, _message):
                return []

        raw = b'{"method":"item/completed","params":{}}\n'
        self.assertEqual(_json_transform(HoldPolicy(), "server")(raw), b"")
    def test_policy_failure_fails_open_without_content_logging(self):
        class Broken(Policy):
            def on_client(self, message):
                raise ValueError(message["params"]["input"])

        policy = Broken()
        raw = b'{"id":1,"method":"turn/start","params":{"input":"secret"}}\n'
        code, output, errors = self.bridge(raw, policy)
        self.assertEqual((code, output, errors), (0, raw, b"child-stderr\x00\xff"))
        self.assertEqual(policy.audit_events, ["policy_error"])

    def test_oversized_line_bypasses_policy_and_is_not_truncated(self):
        policy = Policy()
        raw = b'{"method":"turn/start","params":{"input":"' + (b"x" * 200) + b'"}}\n'
        code, output, _ = self.bridge(raw, policy, limit=32)
        self.assertEqual((code, output), (0, raw))
        self.assertEqual(policy.client, [])

    def test_exit_code_and_non_app_server_passthrough(self):
        output, errors = io.BytesIO(), io.BytesIO()
        raw = b"--version bytes \xff\n"
        code = run_bridge(
            sys.executable,
            [str(Path(__file__).resolve()), "--fake-child", "--exit", "23"],
            stdin=io.BytesIO(raw), stdout=output, stderr=errors,
        )
        self.assertEqual((code, output.getvalue()), (23, raw))

    def test_child_receives_real_cli_path(self):
        output = io.BytesIO()
        code = run_bridge(
            sys.executable,
            [str(Path(__file__).resolve()), "--fake-child", "--emit-cli-path"],
            stdin=io.BytesIO(), stdout=output, stderr=io.BytesIO(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().decode(), str(Path(sys.executable).resolve()))

    def test_gui_arguments_are_forwarded_in_order(self):
        args = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]
        output = io.BytesIO()
        code = run_bridge(
            sys.executable,
            [str(Path(__file__).resolve()), "--fake-child", "--emit-argv", *args],
            policy=Policy(), stdin=io.BytesIO(), stdout=output, stderr=io.BytesIO(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), ["--emit-argv", *args])

    def test_adaptive_policy_becomes_ready_through_bridge_observation(self):
        class ObservedPolicy(AdaptivePolicy):
            def __init__(self, config, audit):
                self.auth_ready = threading.Event()
                self.catalog_ready = threading.Event()
                super().__init__(config, audit)

            def on_server(self, message):
                super().on_server(message)
                if self.auth:
                    self.auth_ready.set()
                if self.catalog:
                    self.catalog_ready.set()

        class SequencedInput:
            def __init__(self, lines, gates):
                self.lines, self.gates, self.index = lines, gates, 0

            def readline(self, _limit):
                if self.index and self.index < len(self.lines) and not self.gates[self.index - 1].wait(5):
                    raise TimeoutError("policy observation did not complete")
                if self.index >= len(self.lines):
                    return b""
                line = self.lines[self.index]
                self.index += 1
                return line

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps(DEFAULT), encoding="utf-8")
            policy = ObservedPolicy(config, root / "audit")
            requests = [
                b'{"id":1,"method":"account/read","params":{}}\n',
                b'{"id":"1","method":"model/list","params":{}}\n',
                b'{"id":3,"method":"turn/start","params":{"threadId":"t","model":null,"effort":null,"input":[{"type":"text","text":"What is JSON?"}],"collaborationMode":{"mode":"default","settings":{"model":"gpt-6-astra","reasoning_effort":"ultra","developer_instructions":"base"}}}}\n',
            ]
            output = io.BytesIO()
            code = run_bridge(
                sys.executable,
                [str(Path(__file__).resolve()), "--fake-child", "--adaptive-sequence", "--fork-active-writer", "app-server"],
                policy=policy,
                stdin=SequencedInput(requests, [policy.auth_ready, policy.catalog_ready]),
                stdout=output,
                stderr=io.BytesIO(),
                classifier_sidecar=False,
            )
            audit = [json.loads(line) for path in (root / "audit").glob("*.jsonl")
                     for line in path.read_text(encoding="utf-8").splitlines()]
        response = json.loads(output.getvalue().splitlines()[-1])
        received = response["result"]["received"]
        self.assertEqual(code, 0)
        self.assertEqual((received["params"]["model"], received["params"]["effort"]),
                         (None, None))
        settings = received["params"]["collaborationMode"]["settings"]
        self.assertEqual((settings["model"], settings["reasoning_effort"]),
                         ("gpt-5.6-luna", "high"))
        self.assertIn("[codex-route:lookup] gpt-5.6-luna / high",
                      settings["developer_instructions"])
        self.assertNotIn(b"classifier-thread", output.getvalue())
        measured = next(row for row in audit if row["event"] == "classifier")
        self.assertGreaterEqual(measured["duration_ms"], 0)
        self.assertEqual((measured["input_tokens"], measured["cached_tokens"],
                          measured["output_tokens"], measured["reasoning_tokens"],
                          measured["total_tokens"]), (50, 10, 5, 2, 55))


def fake_child(argv):
    exit_code = int(argv[argv.index("--exit") + 1]) if "--exit" in argv else 0
    if "--adaptive-sequence" in argv:
        for line in sys.stdin.buffer:
            request = json.loads(line)
            if request["method"] == "account/read":
                result = {"account": {"type": "chatgpt"}}
            elif request["method"] == "model/list":
                result = {"data": [
                    {"model": "gpt-5.6-luna", "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
                    {"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]},
                    {"model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
                ]}
            elif request["method"] == "thread/fork":
                if request["params"].get("excludeTurns") is not True:
                    result = None
                    error = {"code": -32600, "message": "paginated fork requires excludeTurns"}
                    sys.stdout.buffer.write(json.dumps({"id": request["id"], "error": error}).encode() + b"\n")
                    sys.stdout.buffer.flush()
                    continue
                if "--fork-active-writer" in argv:
                    error = {"code": -32600, "message": "thread already has an active writer"}
                    sys.stdout.buffer.write(json.dumps({"id": request["id"], "error": error}).encode() + b"\n")
                    sys.stdout.buffer.flush()
                    continue
                result = {"thread": {"id": "classifier-thread", "ephemeral": True,
                                      "forkedFromId": request["params"]["threadId"]}}
            elif request["method"] == "thread/read":
                result = {"thread": {"id": request["params"]["threadId"], "cwd": os.getcwd(), "turns": [{
                    "items": [
                        {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "Prior request"}]},
                        {"id": "a", "type": "agentMessage", "phase": "final_answer", "text": "Prior answer"},
                    ]}]}}
            elif request["method"] == "thread/start" and "threadId" not in request["params"]:
                result = {"thread": {"id": "classifier-thread", "ephemeral": True}}
            elif request["method"] == "turn/start" and request["params"]["threadId"] == "classifier-thread":
                result = {"turn": {"id": "classifier-turn", "status": "inProgress"}}
            elif request["method"] == "thread/delete":
                result = {}
            else:
                result = {"received": request}
            if request["method"] == "thread/fork":
                sys.stdout.buffer.write(json.dumps({"method": "thread/settings/updated", "params": {
                    "threadId": "other-thread", "threadSettings": {"model": "gpt-5.6-sol"}}}).encode() + b"\n")
            sys.stdout.buffer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
            if request["method"] == "thread/fork":
                sys.stdout.buffer.write(json.dumps({"method": "thread/started", "params": {
                    "thread": result["thread"]}}).encode() + b"\n")
            elif request["method"] == "thread/start" and "threadId" not in request["params"]:
                sys.stdout.buffer.write(json.dumps({"method": "thread/started", "params": {
                    "thread": result["thread"]}}).encode() + b"\n")
            elif request["method"] == "turn/start" and request["params"]["threadId"] == "classifier-thread":
                sys.stdout.buffer.write(json.dumps({"method": "item/completed", "params": {
                    "threadId": "classifier-thread", "turnId": "classifier-turn", "item": {
                        "id": "classifier-answer", "type": "agentMessage", "phase": "final_answer",
                        "text": '{"model":"gpt-5.6-luna","effort":"high","subagents":['
                                '{"role":"lookup","model":"gpt-5.6-luna","effort":"high"}],'
                                '"state_summary":"Keep the active routing goal and constraints."}'}}}).encode() + b"\n")
                sys.stdout.buffer.write(json.dumps({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": "classifier-thread", "turnId": "classifier-turn", "tokenUsage": {
                        "last": {"inputTokens": 50, "cachedInputTokens": 10, "outputTokens": 5,
                                 "reasoningOutputTokens": 2, "totalTokens": 55}}}}).encode() + b"\n")
                sys.stdout.buffer.write(json.dumps({"method": "turn/completed", "params": {
                    "threadId": "classifier-thread", "turn": {
                        "id": "classifier-turn", "status": "completed"}}}).encode() + b"\n")
            elif request["method"] == "thread/delete":
                sys.stdout.buffer.write(json.dumps({"method": "thread/deleted", "params": {
                    "threadId": "classifier-thread"}}).encode() + b"\n")
            sys.stdout.buffer.flush()
        return exit_code
    data = sys.stdin.buffer.read()
    sys.stdout.buffer.write(data)
    if "--emit-cli-path" in argv:
        sys.stdout.buffer.write(os.environ["CODEX_CLI_PATH"].encode())
    if "--emit-argv" in argv:
        sys.stdout.buffer.write(json.dumps(argv).encode())
    if "--emit-event" in argv:
        sys.stdout.buffer.write(b'{"method":"item/agentMessage/delta","params":{"delta":"stream"}}\n')
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(b"child-stderr\x00\xff")
    sys.stderr.buffer.flush()
    return exit_code


if __name__ == "__main__" and "--fake-child" in sys.argv:
    raise SystemExit(fake_child(sys.argv[2:]))
elif __name__ == "__main__":
    unittest.main()
