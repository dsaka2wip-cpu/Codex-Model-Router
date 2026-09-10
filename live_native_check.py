"""Opt-in synthetic real Codex test through the Python bridge (NOT a GUI test)."""
import json
import queue
import sys
import time
from pathlib import Path

from app_server import AppServer

ROOT = Path(__file__).resolve().parent
PLAN_ONLY = "--plan-only" in sys.argv
REPORT = ROOT / "state" / ("native-plan-check.json" if PLAN_ONLY else "native-live-check.json")


def run():
    metadata = json.loads((ROOT / "state/native-runtime.json").read_text(encoding="utf-8-sig"))
    cwd = ROOT / "state" / "native-test-repo"
    cwd.mkdir(parents=True, exist_ok=True)
    result = {"surface": "python_stdio_bridge", "gui_verified": False,
              "same_thread": False, "turns": [], "passed": False}
    with AppServer(cwd=str(cwd), executable=metadata["python"],
                   arguments=["-u", "-B", str(ROOT / "stdio_router.py"),
                              "-c", "features.code_mode_host=true",
                              "app-server", "--analytics-default-enabled"]) as client:
        account = client.call("account/read", {"refreshToken": False})
        acct = account.get("account") or {}
        result["auth_type"] = acct.get("type")
        result["plan_type"] = acct.get("planType")
        if result["auth_type"] != "chatgpt":
            raise RuntimeError("chatgpt_auth_required")
        client.call("model/list", {"includeHidden": False})
        thread = client.call("thread/start", {
            "cwd": str(cwd), "ephemeral": True, "model": "gpt-6-astra",
            "config": {"model_reasoning_effort": "ultra"},
            "approvalPolicy": "on-request", "sandbox": "read-only",
            "baseInstructions": "This is a bounded synthetic router test. Reply briefly. Do not use tools or spawn subagents.",
        })
        thread_id = thread["thread"]["id"]
        cases = [
            ("FAST", "What is JSON? Reply in one sentence. Remember code maple-62.", "gpt-5.6-luna", "low", False),
            ("NORMAL", "Implement a Python function token() returning the code from my previous message. Return code only; do not run commands.", "gpt-5.6-sol", "medium", False),
            ("DEEP_PLAN", "Design a distributed system architecture with two services and one queue. Give only two short plan steps. Do not edit files or use tools.", "gpt-6-astra", "high", True),
            ("GUI_PASSTHROUGH", "[router off]\nWhat is JSON? Reply with OK only.", "gpt-5.6-sol", "high", False),
        ]
        if PLAN_ONLY:
            cases = cases[2:3]
        for label, prompt, expected_model, expected_effort, plan in cases:
            while True:
                try:
                    client.events.get_nowait()
                except queue.Empty:
                    break
            params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                      "model": "gpt-6-astra", "effort": "ultra"}
            if plan:
                params.update(model=None, effort=None, collaborationMode={"mode": "plan", "settings": {
                    "model": "gpt-6-astra", "reasoning_effort": "ultra", "developer_instructions": None}})
            if label == "GUI_PASSTHROUGH":
                params.update(model="gpt-5.6-sol", effort="high", collaborationMode={"mode": "default", "settings": {
                    "model": "gpt-5.6-sol", "reasoning_effort": "high", "developer_instructions": None}})
            response = client.call("turn/start", params, timeout=45)
            turn_id = response["turn"]["id"]
            item = {"case": label, "expected_model": expected_model, "expected_effort": expected_effort,
                    "settings": [], "stream_deltas": 0, "plan_deltas": 0, "token_usage_updates": 0,
                    "footer_present": False, "status": None, "mode": "plan" if plan else "default"}
            text = ""
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                event = client.events.get(timeout=max(0.01, deadline - time.monotonic()))
                if isinstance(event, Exception):
                    raise event
                method, data = event.get("method"), event.get("params") or {}
                if "id" in event and method:
                    # This test never approves tools or permissions.
                    client.reply(event["id"], error={"code": -32000, "message": "Synthetic test does not grant approval."})
                if data.get("threadId") != thread_id:
                    continue
                if method == "thread/settings/updated":
                    settings = data["threadSettings"]
                    nested = (settings.get("collaborationMode") or {}).get("settings") or {}
                    item["settings"].append({"model": settings.get("model"), "effort": settings.get("effort"),
                        "nested_model": nested.get("model"), "nested_effort": nested.get("reasoning_effort"),
                        "mode": (settings.get("collaborationMode") or {}).get("mode")})
                elif method == "item/plan/delta":
                    item["stream_deltas"] += 1
                    item["plan_deltas"] += 1
                    text += data.get("delta", "")
                elif method == "item/agentMessage/delta":
                    item["stream_deltas"] += 1
                    text += data.get("delta", "")
                elif method == "item/completed" and (data.get("item") or {}).get("type") == "agentMessage":
                    text = (data["item"].get("text") or text)
                elif method == "item/completed" and (data.get("item") or {}).get("type") == "plan":
                    text = (data["item"].get("text") or text)
                elif method == "thread/tokenUsage/updated" and data.get("turnId") == turn_id:
                    item["token_usage_updates"] += 1
                elif method == "turn/completed" and (data.get("turn") or {}).get("id") == turn_id:
                    item["status"] = data["turn"]["status"]
                    break
            item["remembered"] = "maple-62" in text if label == "NORMAL" else None
            item["footer_present"] = "Router · 이번 턴:" in text
            item["actual_settings_match"] = any(
                s["model"] == expected_model and s["effort"] == expected_effort
                and s["nested_model"] == expected_model and s["nested_effort"] == expected_effort
                and s["mode"] == item["mode"] for s in item["settings"])
            result["turns"].append(item)
            print(json.dumps(item), flush=True)
            REPORT.write_text(json.dumps(result, indent=2), encoding="utf-8")
            if item["status"] != "completed":
                break
        result["same_thread"] = len(result["turns"]) == len(cases)
        result["passed"] = result["same_thread"] and all(t["status"] == "completed" and t["actual_settings_match"]
            and t["stream_deltas"] > 0 and t["footer_present"] for t in result["turns"]) \
            and (PLAN_ONLY or result["turns"][1]["remembered"])
    REPORT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "same_thread": result["same_thread"], "gui_verified": False}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as error:
        print(json.dumps({"test_failed": type(error).__name__}), flush=True)
        raise SystemExit(2)
