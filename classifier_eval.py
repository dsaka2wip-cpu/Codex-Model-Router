"""Opt-in, metadata-only evaluation of the production routing classifier."""

import argparse
import json
import msvcrt
import os
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app_server import AppServer
from stdio_router import EFFORT_ORDER, InternalClassifier, SIDECAR_ARGS, _real_codex


ROOT = Path(__file__).resolve().parent
MODEL_ORDER = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra")
CASES_PATH = ROOT / "classifier_eval_cases.json"
REPORT_PATH = ROOT / "state" / "classifier-eval-latest.json"
LOCK_PATH = ROOT / "state" / "classifier-eval.lock"


@contextmanager
def evaluation_lock(path=LOCK_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def write_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(pending, path)


class SnapshotClassifier(InternalClassifier):
    def __init__(self, sidecar, snapshots, sidecar_factory):
        super().__init__(None, sidecar_factory=sidecar_factory)
        self.sidecar = sidecar
        self.snapshots = snapshots

    def _call(self, method, params, timeout=None):
        if method != "thread/read":
            raise RuntimeError("evaluation only permits synthetic thread reads")
        return {"thread": self.snapshots[params["threadId"]]}


def load_cases(path=CASES_PATH):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation cases must be a non-empty list")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or len(set(ids)) != len(ids) or not all(isinstance(x, str) for x in ids):
        raise ValueError("evaluation case ids must be unique strings")
    return cases


def score_case(case, decision):
    model, effort = decision["model"], decision["effort"]
    subagents = decision.get("subagents") or []
    expected = case["expected"]
    count_ok = expected["subagents"][0] <= len(subagents) <= expected["subagents"][1]
    roles_ok = all(item.get("role") in expected["roles"] for item in subagents)
    critical_under = (
        MODEL_ORDER.index(model) < MODEL_ORDER.index(expected["minimum_model"])
        or EFFORT_ORDER.index(effort) < EFFORT_ORDER.index(expected["minimum_effort"])
    )
    return {
        "case_id": case["id"],
        "group": case["group"],
        "model": model,
        "effort": effort,
        "subagents": [{"role": item["role"], "model": item["model"], "effort": item["effort"]}
                      for item in subagents],
        "accepted": (model in expected["models"] and effort in expected["efforts"]
                     and count_ok and roles_ok),
        "critical_underroute": critical_under,
        "unexpected_subagents": not (count_ok and roles_ok),
        "context_chars": decision.get("context_chars"),
        "duration_ms": decision.get("duration_ms"),
        "usage": {key: (decision.get("usage") or {}).get(key) for key in (
            "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")},
    }


def summarize(rows, requested):
    completed = [row for row in rows if "accepted" in row]
    tokens = [row["usage"]["totalTokens"] for row in completed
              if type(row["usage"]["totalTokens"]) is int]
    return {
        "requested": requested,
        "completed": len(completed),
        "accepted": sum(row["accepted"] for row in completed),
        "critical_underroutes": sum(row["critical_underroute"] for row in completed),
        "unexpected_subagents": sum(row["unexpected_subagents"] for row in completed),
        "classifier_total_tokens": sum(tokens),
        "average_classifier_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        "selections": dict(Counter(f'{row["model"]}/{row["effort"]}' for row in completed)),
        "passed": (len(completed) == requested and all(row["accepted"] for row in completed)
                   and not any(row["critical_underroute"] for row in completed)),
    }


def _snapshot(case):
    turns = []
    for prior in case.get("prior", []):
        items = [{"type": "userMessage", "content": [{"type": "text", "text": prior["user"]}]}]
        if prior.get("assistant"):
            items.append({"type": "agentMessage", "phase": "final_answer", "text": prior["assistant"]})
        turns.append({"items": items})
    return {"id": case["id"], "cwd": str(ROOT), "turns": turns}


def _catalog(client):
    rows = client.call("model/list", {"includeHidden": False}).get("data", [])
    return {row["model"]: {"efforts": {item["reasoningEffort"]
            for item in row.get("supportedReasoningEfforts", []) if isinstance(item, dict)}}
            for row in rows if isinstance(row, dict) and isinstance(row.get("model"), str)}


def run(cases, report_path=REPORT_PATH):
    report_path = Path(report_path)
    report = {"version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "classifier routing only; no task answers", "cases": []}
    snapshots = {case["id"]: _snapshot(case) for case in cases}
    def new_sidecar():
        return AppServer(executable=_real_codex(), arguments=SIDECAR_ARGS)

    sidecar = new_sidecar()
    classifier = SnapshotClassifier(sidecar, snapshots, new_sidecar)
    try:
        account = sidecar.call("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise RuntimeError("ChatGPT authentication required")
        catalog = _catalog(sidecar)
        for case in cases:
            try:
                decision = classifier.classify(
                    case["id"], case["prompt"], {"model": "gpt-5.6-sol", "effort": "medium"},
                    catalog, [{"type": "text", "text": case["prompt"]}],
                )
                row = score_case(case, decision)
            except Exception as error:
                row = {"case_id": case["id"], "group": case["group"],
                       "failure": type(error).__name__}
                if isinstance(error, ClassifierFailure):
                    row.update({"failure_stage": error.stage,
                                "failure_kind": error.failure_kind,
                                "rpc_code": error.rpc_code,
                                "error_kind": error.error_kind,
                                "duration_ms": error.duration_ms})
            report["cases"].append(row)
            report["summary"] = summarize(report["cases"], len(cases))
            write_report(report, report_path)
            print(json.dumps(row, separators=(",", ":")), flush=True)
            if "failure" in row or row.get("critical_underroute"):
                break
    finally:
        classifier.close()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = summarize(report["cases"], len(cases))
    write_report(report, report_path)
    print(json.dumps(report["summary"], separators=(",", ":")), flush=True)
    return 0 if report["summary"]["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description="Run bounded Codex classifier canaries.")
    parser.add_argument("--run", action="store_true", help="acknowledge ChatGPT subscription usage")
    parser.add_argument("--limit", type=int, default=6, choices=range(1, 13))
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required because this invokes the configured Sol/medium classifier")
    cases = load_cases()
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["id"] in wanted]
        if len(cases) != len(wanted):
            parser.error("unknown --case id")
    else:
        cases = cases[:args.limit]
    with evaluation_lock() as acquired:
        if not acquired:
            print('{"stopped":"AlreadyRunning"}', flush=True)
            return 3
        try:
            return run(cases)
        except Exception as error:
            print(json.dumps({"stopped": type(error).__name__}, separators=(",", ":")), flush=True)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
