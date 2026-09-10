"""Small concurrent JSONL client for ``codex app-server --stdio``."""

import json
import queue
import shutil
import subprocess
import threading


class AppServer:
    """Own one App Server child process and expose its JSON-RPC-like protocol."""

    def __init__(self, cwd=None, executable=None, arguments=None):
        executable = executable or shutil.which("codex")
        if not executable:
            raise RuntimeError("codex executable not found on PATH")

        self.events = queue.Queue()
        self._pending = {}
        self._next_id = 0
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._failure = None
        self._closed = False
        self.process = subprocess.Popen(
            [executable, *(arguments if arguments is not None else ["app-server", "--stdio"])],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read, name="codex-app-server", daemon=True)
        self._reader.start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "codex_model_router", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def call(self, method, params, timeout=30):
        waiter = queue.Queue(maxsize=1)
        with self._state_lock:
            if self._failure is not None:
                raise RuntimeError(str(self._failure))
            if self._closed:
                raise RuntimeError("Codex App Server is closed")
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = waiter

        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                message = waiter.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"Codex App Server timed out: {method}") from None
            if isinstance(message, BaseException):
                raise RuntimeError(str(message)) from message
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def reply(self, request_id, result=None, error=None):
        message = {"id": request_id}
        if error is None:
            message["result"] = result
        else:
            message["error"] = error
        self._send(message)

    def _send(self, message):
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._failure is not None:
                    raise RuntimeError(str(self._failure))
                if self._closed:
                    raise RuntimeError("Codex App Server is closed")
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._fail(RuntimeError("Codex App Server connection closed"))
                raise RuntimeError("Codex App Server connection closed") from exc

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    self._fail(RuntimeError("Codex App Server sent invalid JSON"))
                    return

                if "id" in message and "method" not in message:
                    with self._state_lock:
                        waiter = self._pending.get(message["id"])
                    if waiter is not None:
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                elif "method" in message:
                    self.events.put(message)
            self._fail(RuntimeError("Codex App Server connection closed"))
        except Exception as exc:
            self._fail(RuntimeError(f"Codex App Server reader failed: {type(exc).__name__}"))

    def _fail(self, error):
        with self._state_lock:
            if self._failure is not None or self._closed:
                return
            self._failure = error
            waiters = list(self._pending.values())
        self.events.put(error)
        for waiter in waiters:
            try:
                waiter.put_nowait(error)
            except queue.Full:
                pass

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            waiters = list(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(RuntimeError("Codex App Server is closed"))
            except queue.Full:
                pass

        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
