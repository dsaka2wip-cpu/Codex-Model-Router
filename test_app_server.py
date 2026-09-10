import json
import queue
import threading
import unittest
from unittest import mock

from app_server import AppServer


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line

    def send(self, message):
        self.lines.put(json.dumps(message) + "\n")

    def close(self):
        self.lines.put(None)


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, value):
        if self.closed:
            raise ValueError("closed")
        message = json.loads(value)
        self.process.writes.put(message)
        if message.get("method") == "initialize":
            self.process.stdout.send({"id": message["id"], "result": {}})

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.stdout = FakeOutput()
        self.writes = queue.Queue()
        self.stdin = FakeInput(self)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.stdout.close()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout=None):
        return self.returncode


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        patcher = mock.patch("app_server.subprocess.Popen", return_value=self.process)
        self.addCleanup(patcher.stop)
        self.popen = patcher.start()
        self.server = AppServer(executable="codex-test")
        self.addCleanup(self.server.close)
        args, options = self.popen.call_args
        self.assertEqual(args[0], ["codex-test", "app-server", "--stdio"])
        self.assertFalse(options["shell"])
        initialized = self.process.writes.get(timeout=1)
        self.assertEqual(initialized["method"], "initialize")
        self.assertTrue(initialized["params"]["capabilities"]["experimentalApi"])
        self.assertEqual(self.process.writes.get(timeout=1), {"method": "initialized"})

    def test_out_of_order_replies_preserve_events(self):
        results = {}
        threads = [
            threading.Thread(target=lambda: results.setdefault("one", self.server.call("one", {}))),
            threading.Thread(target=lambda: results.setdefault("two", self.server.call("two", {}))),
        ]
        for thread in threads:
            thread.start()
        requests = [self.process.writes.get(timeout=1), self.process.writes.get(timeout=1)]
        by_method = {request["method"]: request for request in requests}
        notification = {"method": "turn/started", "params": {"turn": "t"}}
        server_request = {"id": 91, "method": "item/commandExecution/requestApproval", "params": {}}
        self.process.stdout.send(notification)
        self.process.stdout.send(server_request)
        self.process.stdout.send({"id": by_method["two"]["id"], "result": 2})
        self.process.stdout.send({"id": by_method["one"]["id"], "result": 1})
        for thread in threads:
            thread.join(1)
        self.assertEqual(results, {"one": 1, "two": 2})
        self.assertEqual(self.server.events.get(timeout=1), notification)
        self.assertEqual(self.server.events.get(timeout=1), server_request)

    def test_reply_does_not_auto_approve(self):
        request = {"id": 17, "method": "item/fileChange/requestApproval", "params": {}}
        self.process.stdout.send(request)
        self.assertEqual(self.server.events.get(timeout=1), request)
        self.assertTrue(self.process.writes.empty())
        error = {"code": -32000, "message": "denied"}
        self.server.reply(17, error=error)
        self.assertEqual(self.process.writes.get(timeout=1), {"id": 17, "error": error})

    def test_timeout_removes_waiter(self):
        with self.assertRaisesRegex(TimeoutError, "timed out: never"):
            self.server.call("never", {}, timeout=0.01)
        self.assertFalse(self.server._pending)

    def test_eof_wakes_pending_call(self):
        errors = []
        thread = threading.Thread(target=lambda: self._capture_error(errors))
        thread.start()
        self.process.writes.get(timeout=1)
        self.process.stdout.close()
        thread.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIn("connection closed", str(errors[0]))
        self.assertIn("connection closed", str(self.server.events.get(timeout=1)))

    def _capture_error(self, errors):
        try:
            self.server.call("wait", {}, timeout=1)
        except Exception as exc:
            errors.append(exc)

    def test_close_terminates_only_owned_child_and_is_idempotent(self):
        self.server.close()
        self.server.close()
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.process.killed)


if __name__ == "__main__":
    unittest.main()
