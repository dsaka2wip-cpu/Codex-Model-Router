"""Opt-in real subscription check: two MAIN turns, one ephemeral conversation."""
import json
import hashlib
from datetime import datetime
import time
from pathlib import Path
from main_router import MainRouter

ROOT = Path(__file__).resolve().parent


def main():
    router = MainRouter(ephemeral=True)
    report = {"scope": "two main turns, same ephemeral thread; no cost/savings estimate", "turns": []}
    try:
        prompts = [
            "다음 JSON에서 code의 값만 추출하고 이 대화에서 기억하세요: {\"code\":\"cedar-47\"}. "
            "답은 값만 쓰세요. 도구, 하위 에이전트, 웹, 파일 접근은 사용하지 마세요.",
            "Python 함수 구현: 앞선 질문의 code 값을 그대로 반환하는 get_code 함수를 작성하세요. "
            "코드만 답하고 도구, 하위 에이전트, 웹, 파일 접근은 사용하지 마세요.",
        ]
        for prompt in prompts:
            prior_ids = {m["id"] for m in router.snapshot()["messages"]}
            started_at = time.time()
            router.send({"prompt": prompt, "cwd": str(ROOT), "tier": "default"})
            deadline = time.monotonic() + 150
            while router.snapshot()["busy"] and time.monotonic() < deadline:
                time.sleep(0.2)
            state = router.snapshot()
            if state["busy"]:
                raise RuntimeError("Synthetic main turn timed out")
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["pending"]:
                raise RuntimeError("Unexpected permission request; check will not approve")
            reply = "\n".join(m["text"] for m in state["messages"]
                              if m["id"] not in prior_ids and m["role"] == "assistant")
            turn_report = {"requested": state["route"], "reply": reply, "usage": state["usage"],
                           "threadId": state["threadId"]}
            fingerprint = hashlib.sha256(state["threadId"].encode()).hexdigest()[:16]
            observed = []
            for path in (ROOT / "logs").glob("*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if (row.get("decision") == "policy" and row.get("session_fingerprint") == fingerprint
                                and datetime.fromisoformat(row["time"]).timestamp() >= started_at):
                            observed.append(row.get("model"))
                    except (ValueError, KeyError):
                        continue
            turn_report["runtime_hook_models"] = observed
            turn_report["effort_evidence"] = "Explicit supported turn/start effort acknowledged; hook does not expose runtime effort."
            report["turns"].append(turn_report)
            print(json.dumps(turn_report, ensure_ascii=True), flush=True)
        one, two = report["turns"]
        report["same_thread"] = one["threadId"] == two["threadId"]
        report["remembered"] = "cedar-47" in one["reply"] and "cedar-47" in two["reply"] and "get_code" in two["reply"]
        report["verified_runtime_models"] = all(expected in t["runtime_hook_models"]
                                                for expected, t in zip(["gpt-5.6-luna", "gpt-5.6-sol"], report["turns"]))
        report["runtime_effort_observed"] = False
        report["passed"] = all(report[k] for k in ("same_thread", "remembered", "verified_runtime_models"))
        (ROOT / "live-main-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "turns"}), flush=True)
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        router.close()


if __name__ == "__main__":
    main()
