"""Explicit one-shot Codex subscription smoke test; never part of hook execution."""
import json
import hashlib
import queue
import time
from collections import Counter
from pathlib import Path

from manage import Codex

ROOT = Path(__file__).resolve().parent
PROMPT = """Run exactly one synthetic integration check of the installed Codex Model Router.
Delegate the role lookup to exactly one fresh-context child called router_smoke_lookup.
Follow the installed routing policy, including its model/effort instructions. The child
message must start with [codex-route:lookup] on its own first line. Its complete task:
From the supplied JSON {"alpha":7,"beta":11}, return only the value of beta. No file,
shell, web, MCP or further agent calls. Wait for that child to finish; return its answer.
Do not solve it yourself, retry, or change files. This is a synthetic routing test."""


def main():
    client = Codex(standalone=True)
    report = {"methods": {}, "hooks": [], "items": [], "children": []}
    observed_since = time.time()
    counts = Counter()
    child_ids = set()
    parent = None
    turn_id = None
    complete = False
    try:
        started = client.call("thread/start", {"cwd": str(ROOT), "model": "gpt-5.6-sol",
                              "config": {"model_reasoning_effort": "medium"},
                              "sandbox": "read-only", "ephemeral": True})
        parent = started["thread"]["id"]
        report["parent"] = {"model": started["model"], "effort": started.get("reasoningEffort")}
        turn = client.call("turn/start", {"threadId": parent, "input": [{"type": "text", "text": PROMPT}]})
        turn_id = turn["turn"]["id"]
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                event = client.messages.get(timeout=1)
            except queue.Empty:
                continue
            if event is None:
                break
            method, params = event.get("method", ""), event.get("params", {})
            counts[method] += 1
            if method == "turn/started" and params.get("threadId") != parent:
                child_ids.add(params["threadId"])
            if "id" in event:
                client.send({"id": event["id"], "error": {"code": -32601, "message": "Non-interactive check"}})
            if method == "thread/started":
                child = params.get("thread", {}).get("id")
                if child and child != parent:
                    child_ids.add(child)
            if method == "hook/completed":
                hook = params.get("run", {})
                report["hooks"].append({k: hook.get(k) for k in ("eventName", "status", "durationMs")})
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") in ("collabAgentToolCall", "agentMessage"):
                    report["items"].append(item)
                    child_ids.update(item.get("receiverThreadIds", []))
            if method == "thread/tokenUsage/updated":
                report["usage_reported_by_runtime"] = params.get("tokenUsage")
            if method == "turn/completed" and params.get("threadId") == parent:
                report["turn_status"] = params.get("turn", {}).get("status")
                complete = True
                break
        for child in child_ids:
            try:
                state = client.call("thread/resume", {"threadId": child})
                report["children"].append({"id": child, "model": state["model"],
                                           "effort": state.get("reasoningEffort")})
            except RuntimeError as error:
                report["children"].append({"id": child, "state_error": str(error)})
        report["methods"] = dict(counts)
        report["completed"] = complete
        report["verified_luna_medium"] = any(c.get("model") == "gpt-5.6-luna" and
                                             c.get("effort") == "medium" for c in report["children"])
        from datetime import datetime
        observations = []
        for path in (ROOT / "logs").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if (row.get("decision") == "child_started" and
                    row.get("parent_fingerprint") == hashlib.sha256(parent.encode()).hexdigest()[:16] and
                    datetime.fromisoformat(row["time"]).timestamp() >= observed_since):
                    observations.append(row)
        report["subagent_start_observations"] = observations
        report["verified_luna_start"] = any(row.get("model") == "gpt-5.6-luna" for row in observations)
        (ROOT / "live-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        if parent and not complete:
            # No background test should survive a timeout or failed check.
            try:
                if turn_id:
                    client.call("turn/interrupt", {"threadId": parent, "turnId": turn_id})
            except RuntimeError:
                pass
        client.close()


if __name__ == "__main__":
    main()
