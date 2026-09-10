"""Local, authenticated main-model routing UI using the installed Codex login."""
import argparse
import base64
import copy
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app_server import AppServer
from main_policy import choose_route

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MAX_BODY = 4 * 1024 * 1024


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def approval_result(method, params, data):
    """Respond only to the exact pending request and only after a UI decision."""
    action = data.get("action")
    if action not in ("accept", "decline", "answer"):
        raise ValueError("승인 응답이 올바르지 않습니다.")
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        if action == "answer":
            raise ValueError("허용 또는 거절을 선택하세요.")
        if method == "item/fileChange/requestApproval" and action == "accept" and not params.get("_item", {}).get("changes"):
            raise ValueError("변경 내용을 확인할 수 없어 승인할 수 없습니다. 거절 후 기본 앱에서 확인하세요.")
        available = params.get("availableDecisions")
        if action == "accept" and available and "accept" not in available:
            raise ValueError("이 요청은 한 번 허용을 지원하지 않습니다. 거절 후 기본 앱에서 처리하세요.")
        return {"decision": action}
    if method == "item/permissions/requestApproval":
        if action == "answer":
            raise ValueError("허용 또는 거절을 선택하세요.")
        return {"permissions": params.get("permissions", {}) if action == "accept" else {}, "scope": "turn"}
    if method in ("item/tool/requestUserInput", "tool/requestUserInput"):
        if action == "decline":
            return {"answers": {}}
        answers = data.get("answers")
        if action != "answer" or not isinstance(answers, dict):
            raise ValueError("질문에 대한 답을 입력하세요.")
        ids = {q["id"] for q in params.get("questions", [])}
        if set(answers) != ids or any(not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v) for v in answers.values()):
            raise ValueError("모든 질문에 답해야 합니다.")
        return {"answers": {k: {"answers": v} for k, v in answers.items()}}
    if method == "mcpServer/elicitation/request":
        if action == "decline":
            return {"action": "decline"}
        if action == "answer":
            if not isinstance(data.get("content"), dict):
                raise ValueError("입력은 JSON 객체여야 합니다.")
            return {"action": "accept", "content": data["content"]}
        if params.get("mode") == "url":
            return {"action": "accept"}
        raise ValueError("요청한 입력값을 먼저 작성하세요.")
    raise ValueError("이 요청 유형은 현재 화면에서 지원하지 않습니다.")


class MainRouter:
    def __init__(self, cwd=None, client=None, state_file=None, ephemeral=False):
        self.client = client or AppServer(cwd=str(ROOT))
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.closed = False
        self.ephemeral = ephemeral
        self.state_file = Path(state_file) if state_file else STATE_DIR / "session.json"
        self.requests = {}
        self.approval_items = {}
        self.turn_id = None
        self.pending_user_id = None
        self.restore_error = False
        self.timeout_pending = False
        self.state = {"revision": 0, "busy": False, "threadId": None,
                      "cwd": str(Path(cwd or ROOT).resolve()), "models": [], "messages": [],
                      "route": None, "pending": [], "usage": None, "error": None}
        try:
            account = self.client.call("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") not in ("chatgpt", "chatgptAuthTokens"):
                raise RuntimeError("로그인된 ChatGPT 구독이 필요합니다. 터미널에서 codex login을 실행하세요. API 키 과금은 자동 사용하지 않습니다.")
            cursor = None
            while True:
                result = self.client.call("model/list", {"cursor": cursor, "includeHidden": False})
                self.state["models"].extend(result.get("data", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            if not ephemeral and self.state_file.exists():
                try:
                    saved = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
                    if saved.get("threadId"):
                        if cwd and saved.get("cwd") and os.path.normcase(str(Path(cwd).resolve())) != os.path.normcase(saved["cwd"]):
                            raise ValueError("지정한 폴더와 저장된 대화의 폴더가 다릅니다.")
                        self._resume(saved["threadId"])
                        if isinstance(saved.get("route"), dict):
                            self.state["route"] = saved["route"]
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    self.restore_error = True
                    self.state["error"] = "이전 대화 복구 실패. 새 대화 또는 작업 ID로 이어가기를 선택하세요: " + str(exc)
        except Exception:
            self.client.close()
            raise
        self.reader = threading.Thread(target=self._events, daemon=True)
        self.reader.start()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def _changed(self):
        self.state["revision"] += 1

    def _save(self):
        if not self.ephemeral:
            try:
                atomic_json(self.state_file, {k: self.state[k] for k in ("threadId", "cwd", "route")})
            except OSError as exc:
                self.state["error"] = "재개 정보 저장 실패. 현재 작업 ID를 보관하세요: " + str(exc)

    def preview(self, data):
        with self.lock:
            return choose_route(data.get("prompt"), self.state["route"], data.get("model", "auto"),
                                data.get("effort", "auto"), self.state["models"])

    def _message(self, message_id, role, text=None, **extra):
        existing = next((m for m in self.state["messages"] if m["id"] == message_id), None)
        if existing is None:
            existing = {"id": message_id, "role": role, "text": ""}
            self.state["messages"].append(existing)
        if text is not None:
            existing["text"] = text
        existing.update(extra)
        return existing

    def _hydrate(self, thread):
        self.state["messages"] = []
        previous = None
        for turn in thread.get("turns", []):
            for item in turn.get("items", []):
                if item.get("type") == "userMessage":
                    text = "\n".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "text")
                    self._message(item["id"], "user", text)
                    if text.strip():
                        previous = choose_route(text, previous)
                elif item.get("type") == "agentMessage":
                    self._message(item["id"], "assistant", item.get("text", ""), phase=item.get("phase"))
        self.state["route"] = previous

    def _resume(self, thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r"[-\w]{1,160}", thread_id):
            raise ValueError("유효한 작업 ID가 필요합니다.")
        read = self.client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        status = read["thread"].get("status", {})
        kind = status.get("type") if isinstance(status, dict) else status
        if kind in ("active", "running", "inProgress"):
            raise ValueError("진행 중인 작업입니다. 다른 화면에서 작업이 끝난 뒤 불러오세요.")
        result = self.client.call("thread/resume", {"threadId": thread_id})
        self.state.update(threadId=thread_id, cwd=result["cwd"], usage=None, error=None)
        self._hydrate(read["thread"])
        self._changed()

    def reset(self, data, resume=False):
        if not self.operation.acquire(blocking=False):
            raise ValueError("요청을 처리 중입니다.")
        try:
            with self.lock:
                if self.state["busy"]:
                    raise ValueError("진행 중인 답변이 끝나거나 중지된 뒤 변경하세요.")
                if resume:
                    self._resume(data.get("threadId"))
                else:
                    cwd = valid_cwd(data.get("cwd", self.state["cwd"]))
                    self.state.update(threadId=None, cwd=cwd, messages=[], route=None, usage=None, error=None)
                    self._changed()
                self.restore_error = False
                self._save()
        finally:
            self.operation.release()

    def send(self, data):
        if not self.operation.acquire(blocking=False):
            raise ValueError("요청을 처리 중입니다.")
        try:
            with self.lock:
                if self.state["busy"]:
                    raise ValueError("답변 중입니다. 끝난 뒤 보내거나 중지하세요.")
                if self.restore_error:
                    raise ValueError("이전 대화가 복구되지 않았습니다. 먼저 새 대화 또는 이어가기를 선택하세요.")
                route = self.preview(data)
                cwd = valid_cwd(data.get("cwd", self.state["cwd"]))
                if self.state["threadId"] and os.path.normcase(cwd) != os.path.normcase(self.state["cwd"]):
                    raise ValueError("기존 대화의 작업 폴더는 유지됩니다. 폴더를 바꾸려면 새 대화를 누르세요.")
                tier = data.get("tier", "inherit")
                if tier not in ("inherit", "default"):
                    raise ValueError("속도 등급이 올바르지 않습니다.")
                previous = copy.deepcopy(self.state["route"])
                self.timeout_pending = False
                self.state.update(busy=True, cwd=cwd, error=None, route=route)
                self._changed()
                worker = threading.Thread(target=self._send, args=(data["prompt"], route, tier, previous), daemon=True)
                worker.start()
        except Exception:
            self.operation.release()
            raise

    def _send(self, prompt, route, tier, previous):
        submitted = False
        user_id = None
        try:
            with self.lock:
                thread_id, cwd = self.state["threadId"], self.state["cwd"]
            if thread_id is None:
                params = {"cwd": cwd, "model": route["model"],
                          "config": {"model_reasoning_effort": route["effort"]}}
                if self.ephemeral:
                    params.update(ephemeral=True, sandbox="read-only")
                result = self.client.call("thread/start", params)
                thread_id = result["thread"]["id"]
                with self.lock:
                    self.state["threadId"] = thread_id
                    self._save()
            params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                      "model": route["model"], "effort": route["effort"]}
            if tier == "default":
                params["serviceTierForTurn"] = "default"
            # Reserve one UI message; the native item replaces its temporary id.
            with self.lock:
                self.pending_user_id = "user-" + secrets.token_hex(12)
                user_id = self.pending_user_id
                self._message(self.pending_user_id, "user", prompt,
                              **{k: route[k] for k in ("model", "effort", "reason")})
                self._changed()
            submitted = True
            result = self.client.call("turn/start", params)
            with self.lock:
                if self.state["busy"]:
                    self.turn_id = result["turn"]["id"]
                self._save()
                self._changed()
        except Exception as exc:
            with self.lock:
                if submitted and isinstance(exc, TimeoutError):
                    # A timeout is not proof of rejection: never enable a duplicate send.
                    if self.state["busy"]:
                        self.timeout_pending = True
                        self.state["error"] = "전송 결과 확인이 지연됩니다. 자동 재전송하지 않습니다. 기다리거나 중지/라우터 종료를 선택하세요."
                else:
                    self.state.update(error=str(exc), busy=False, route=previous)
                    if user_id:
                        self.state["messages"] = [m for m in self.state["messages"] if m["id"] != user_id]
                    self.turn_id = None
                    self._save()
                self._changed()
        finally:
            self.operation.release()

    def interrupt(self):
        with self.lock:
            thread_id, turn_id = self.state["threadId"], self.turn_id
            if not self.state["busy"]:
                return
        if not turn_id:
            raise ValueError("답변 시작을 기다리고 있습니다. 잠시 후 다시 중지하세요.")
        self.client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def respond(self, data):
        key = data.get("id")
        if not isinstance(key, str):
            raise ValueError("유효한 화면 요청 ID가 필요합니다.")
        with self.lock:
            event = self.requests.get(key)
            if not event:
                raise ValueError("이미 처리됐거나 만료된 요청입니다.")
            method, params = event["method"], event.get("params", {})
            if data.get("action") == "decline" and method not in SUPPORTED_REQUESTS:
                self.client.reply(event["id"], error={"code": -32601, "message": "Unsupported request declined by user"})
            else:
                result = approval_result(method, params, data)
                self.client.reply(event["id"], result=result)
            self.requests.pop(key, None)
            self.state["pending"] = [x for x in self.state["pending"] if x["id"] != key]
            self._changed()

    def _events(self):
        while not self.closed:
            try:
                event = self.client.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(event, BaseException):
                with self.lock:
                    self.state.update(busy=False, error=str(event))
                    self._changed()
                return
            method, params = event.get("method", ""), event.get("params", {})
            with self.lock:
                if method in ("item/started", "item/completed"):
                    item = params.get("item", {})
                    if item.get("type") in ("fileChange", "commandExecution"):
                        item_key = (params.get("threadId"), params.get("turnId"), item.get("id"))
                        self.approval_items[item_key] = item
                        for pending in self.state["pending"]:
                            p = pending["params"]
                            if (p.get("threadId"), p.get("turnId"), p.get("itemId")) == item_key:
                                p["_item"] = item
                                p["_reviewUnavailable"] = item.get("type") == "fileChange" and not item.get("changes")
                        self._changed()
                if "id" in event:
                    context = self.approval_items.get((params.get("threadId"), params.get("turnId"), params.get("itemId")))
                    if context:
                        params["_item"] = context
                    if method == "item/fileChange/requestApproval":
                        params["_reviewUnavailable"] = not (context or {}).get("changes")
                    key = json.dumps(event["id"], ensure_ascii=True)
                    self.requests[key] = event
                    self.state["pending"].append({"id": key, "method": method, "params": params})
                    self._changed()
                    continue
                if method == "serverRequest/resolved":
                    key = json.dumps(params.get("requestId"), ensure_ascii=True)
                    self.requests.pop(key, None)
                    self.state["pending"] = [x for x in self.state["pending"] if x["id"] != key]
                    self._changed()
                    continue
                if params.get("threadId") != self.state["threadId"]:
                    continue
                event_turn = params.get("turnId") or params.get("turn", {}).get("id")
                if method != "turn/started" and event_turn and self.turn_id and event_turn != self.turn_id:
                    continue
                if method == "turn/started":
                    self.turn_id = params["turn"]["id"]
                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    self.state["busy"] = False
                    self.turn_id = None
                    expired = [k for k, event in self.requests.items()
                               if event.get("params", {}).get("threadId") == self.state["threadId"]
                               and event.get("params", {}).get("turnId") == turn.get("id")]
                    for key in expired:
                        self.requests.pop(key, None)
                    self.state["pending"] = [x for x in self.state["pending"] if x["id"] not in expired]
                    if turn.get("error"):
                        self.state["error"] = json.dumps(turn["error"], ensure_ascii=False)
                    elif self.timeout_pending:
                        self.state["error"] = None
                    self.timeout_pending = False
                    self._save()

                elif method == "thread/tokenUsage/updated":
                    self.state["usage"] = params.get("tokenUsage")
                elif method == "model/rerouted":
                    if self.state["route"]:
                        self.state["route"]["runtimeModel"] = params.get("toModel")
                        self.state["route"]["runtimeReason"] = params.get("reason")
                    self._message("reroute-" + str(time.time_ns()), "system", "Codex 서버가 모델을 변경했습니다: " + str(params.get("toModel")))
                elif method == "item/agentMessage/delta":
                    item = self._message(params["itemId"], "assistant")
                    item["text"] += params.get("delta", "")
                elif method in ("item/started", "item/completed"):
                    item = params.get("item", {})
                    kind, iid = item.get("type"), item.get("id", "")
                    if kind == "userMessage":
                        pending = next((m for m in self.state["messages"] if m["id"] == self.pending_user_id), None)
                        if pending:
                            pending["id"] = iid
                            self.pending_user_id = None
                        else:
                            text = "\n".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "text")
                            self._message(iid, "user", text)
                    elif kind == "agentMessage":
                        self._message(iid, "assistant", item.get("text", ""), phase=item.get("phase"),
                                      **{k: (self.state["route"] or {}).get(k) for k in ("model", "effort")})
                    elif kind not in ("reasoning", None):
                        # UI preview only; full tool outputs remain in Codex's native transcript.
                        display = {k: v for k, v in item.items() if k not in ("content", "summary", "reasoning")}
                        text = json.dumps(display, ensure_ascii=False, indent=2)
                        if len(text) > 24000:
                            text = text[:24000] + "\n[화면 미리보기 생략: 전체 결과는 Codex 기록에 보존됩니다.]"
                        self._message(iid, "tool", text)
                elif method == "error":
                    self.state["error"] = json.dumps(params.get("error", params), ensure_ascii=False)
                self._changed()

    def close(self):
        self.closed = True
        try:
            self.interrupt()
        except (RuntimeError, ValueError, TimeoutError):
            pass
        self.client.close()


SUPPORTED_REQUESTS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "item/tool/requestUserInput",
    "tool/requestUserInput", "mcpServer/elicitation/request",
}


def valid_cwd(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("작업 폴더는 절대 경로로 입력하세요.")
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError("작업 폴더가 존재하지 않습니다.")
    return str(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log prompts, tokens, URLs, or approval data.

    def _reply(self, status, value, content_type="application/json; charset=utf-8"):
        data = json.dumps(value, ensure_ascii=False).encode() if content_type.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", self.server.csp)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _allowed(self, auth=True):
        if self.headers.get("Host") != self.server.authority:
            self._reply(403, {"error": "Invalid host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self._reply(403, {"error": "Invalid origin"})
            return False
        token = self.headers.get("X-Router-Token", "")
        if auth and not secrets.compare_digest(token, self.server.token):
            self._reply(403, {"error": "라우터 실행 링크로 화면을 열어주세요."})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._allowed(auth=path != "/"):
            return
        if path == "/":
            self._reply(200, self.server.html, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._reply(200, self.server.router.snapshot())
        else:
            self._reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self._allowed():
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("JSON 요청이 필요합니다.")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("요청 크기는 1바이트~4MB여야 합니다. 내용을 자동으로 잘라 보내지 않습니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다.")
            path = urlsplit(self.path).path
            router = self.server.router
            if path == "/api/preview":
                self._reply(200, router.preview(data))
                return
            if path == "/api/send":
                router.send(data)
            elif path == "/api/interrupt":
                router.interrupt()
            elif path == "/api/respond":
                router.respond(data)
            elif path == "/api/new":
                router.reset(data)
            elif path == "/api/resume":
                router.reset(data, resume=True)
            elif path == "/api/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._reply(404, {"error": "Not found"})
                return
            self._reply(200, {"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            self._reply(400, {"error": str(exc)})
        except (RuntimeError, OSError, TimeoutError) as exc:
            self._reply(503, {"error": str(exc)})


def make_server(router, port=8765, token=None):
    html = (ROOT / "main_ui.html").read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")
    scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s).digest()).decode() + "'" for s in scripts)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.router, server.token, server.html = router, token or secrets.token_urlsafe(32), html
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    server.csp = f"default-src 'none'; script-src {hashes or chr(39)+'none'+chr(39)}; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cwd", default=None, help="Workspace for a new conversation; mismatched saved threads require explicit recovery")
    parser.add_argument("--open", action="store_true", help="Open the local router in the default browser")
    args = parser.parse_args()
    router = MainRouter(cwd=valid_cwd(args.cwd) if args.cwd else None)
    try:
        server = make_server(router, args.port)
    except Exception:
        router.close()
        raise
    url = server.origin + "/#token=" + server.token
    runtime = STATE_DIR / "runtime.json"
    try:
        atomic_json(runtime, {"pid": os.getpid(), "url": url, "port": server.server_port})
        print(f"Codex Model Router listening on {server.origin}. No model call until you send a prompt.", flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        router.close()
        if runtime.exists():
            try:
                if json.loads(runtime.read_text(encoding="utf-8"))["pid"] == os.getpid():
                    runtime.unlink()
            except (OSError, ValueError, KeyError):
                pass


if __name__ == "__main__":
    main()
