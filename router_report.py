"""Summarize Router audit metadata without reading prompt or response text."""

import argparse
import json
from collections import Counter
from math import ceil
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "state" / "adaptive-logs"
TASK_TYPES = ("chat", "lookup", "research", "code_edit", "debugging",
              "design", "review", "ops", "mixed")


def load_records(log_dir=LOG_DIR, *, latest=False):
    paths = sorted(Path(log_dir).glob("adaptive-*.jsonl"), key=lambda path: path.stat().st_mtime_ns)
    for path in (paths[-1:] if latest else paths):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event"), str):
                yield row


def current_policy_records(records):
    records = list(records)
    fingerprint = next((row.get("policy_fingerprint") for row in reversed(records)
                        if row.get("policy_fingerprint")), None)
    return ([row for row in records if row.get("policy_fingerprint") == fingerprint]
            if fingerprint else [])


def _percentile(values, percent):
    values = sorted(values)
    return values[max(0, ceil(len(values) * percent / 100) - 1)] if values else None


def _rate(count, total):
    return round(100 * count / total, 1) if total else None


def summarize(records):
    records = list(records)
    routes = ([row for row in records if row["event"] == "turn_started"]
              + [row for row in records if row["event"] == "route"
                 and row.get("schema_version") != 2])
    classifiers = [row for row in records if row["event"] == "classifier"]
    fallbacks = [row for row in records if row["event"] == "classifier_fallback"]
    completed = [row for row in records if row["event"] == "turn_completed"]
    footers = [row for row in records if row["event"] == "footer"]
    durations = [row["duration_ms"] for row in classifiers if type(row.get("duration_ms")) is int]
    tokens = [row["total_tokens"] for row in classifiers if type(row.get("total_tokens")) is int]
    saved = [row["saved_percent"] for row in footers if type(row.get("saved_percent")) is int]
    unit_pairs = [(row["actual_units"], row["baseline_units"]) for row in footers
                  if type(row.get("actual_units")) in (int, float)
                  and type(row.get("baseline_units")) in (int, float)
                  and row["baseline_units"] > 0]
    weighted_saved = (round(100 * (1 - sum(actual for actual, _ in unit_pairs)
                                        / sum(baseline for _, baseline in unit_pairs)))
                      if unit_pairs else None)
    route_tasks = Counter(row.get("task_type", "unknown") for row in routes)
    idle_ok = len([row for row in records if row["event"] == "idle_restored"])
    idle_failed = len([row for row in records if row["event"] == "idle_restore_failed"])
    escalations = len([row for row in records if row["event"] == "escalated"])
    completed_ok = sum(row.get("status") == "completed" for row in completed)
    completed_failed = sum(row.get("status") == "failed" for row in completed)
    classifier_latencies = [row["duration_ms"] for row in classifiers
                            if type(row.get("duration_ms")) is int]
    turn_latencies = [row["duration_ms"] for row in completed
                      if type(row.get("duration_ms")) is int]
    first_delta_latencies = [row["first_delta_ms"] for row in completed
                             if type(row.get("first_delta_ms")) is int]
    answer_tokens = [row["total_tokens"] for row in footers
                     if type(row.get("total_tokens")) is int]
    approvals = [row for row in records if row["event"] == "approval"]
    file_changes = [row for row in records if row["event"] == "file_change"]
    tier_counts = Counter(row.get("tier") for row in routes)
    readiness = {
        "ready": (completed_ok >= 200
                  and all(tier_counts[tier] >= 25 for tier in ("FAST", "NORMAL", "DEEP"))
                  and all(route_tasks[task] >= 20 for task in TASK_TYPES)),
        "completed_turns": completed_ok,
        "required_completed_turns": 200,
        "missing_tiers": [tier for tier in ("FAST", "NORMAL", "DEEP") if tier_counts[tier] < 25],
        "missing_task_types": [task for task in TASK_TYPES if route_tasks[task] < 20],
    }
    return {
        "records": len(records),
        "routes": dict(Counter(f'{row.get("model")}/{row.get("effort")}' for row in routes)),
        "tiers": dict(Counter(row.get("tier") for row in routes)),
        "classifier": {
            "successes": len(classifiers),
            "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "average_total_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
            "planned_subagents": sum(row.get("count", 0) for row in classifiers if type(row.get("count")) is int),
        },
        "fallbacks": dict(Counter(row.get("stage", "unknown") for row in fallbacks)),
        "turn_statuses": dict(Counter(row.get("status", "unknown") for row in completed)),
        "escalations": escalations,
        "idle_restore_failures": idle_failed,
        "average_saved_percent": weighted_saved if weighted_saved is not None else round(median(saved)) if saved else None,
        "metrics": {
            "task_routes": dict(Counter(
                f'{row.get("task_type", "unknown")}:{row.get("model")}/{row.get("effort")}'
                for row in routes)),
            "classifier_latency_ms": {"p50": round(median(classifier_latencies)) if classifier_latencies else None,
                                      "p95": _percentile(classifier_latencies, 95)},
            "turn_latency_ms": {"p50": round(median(turn_latencies)) if turn_latencies else None,
                                "p95": _percentile(turn_latencies, 95)},
            "first_delta_ms": {"p50": round(median(first_delta_latencies)) if first_delta_latencies else None,
                               "p95": _percentile(first_delta_latencies, 95)},
            "classifier_tokens": {"p50": round(median(tokens)) if tokens else None,
                                  "p95": _percentile(tokens, 95)},
            "turn_tokens": {"p50": round(median(answer_tokens)) if answer_tokens else None,
                            "p95": _percentile(answer_tokens, 95)},
            "fallback_rate_percent": _rate(len(fallbacks), len(classifiers) + len(fallbacks)),
            "turn_failure_rate_percent": _rate(completed_failed, len(completed)),
            "idle_restore_failure_rate_percent": _rate(idle_failed, idle_ok + idle_failed),
            "escalation_rate_percent": _rate(escalations, len(routes)),
            "approval_events": len(approvals),
            "file_change_events": len(file_changes),
            "saved_estimate": {"percent": weighted_saved if weighted_saved is not None else round(median(saved)) if saved else None,
                               "samples": len(unit_pairs) if weighted_saved is not None else len(saved),
                               "method": "weighted_units" if weighted_saved is not None else "median_snapshot"},
        },
        "readiness": readiness,
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize non-sensitive Adaptive Router audit metadata.")
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="include prior Router process logs")
    scope.add_argument("--current-policy", action="store_true", help="include all logs for the latest policy fingerprint")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    records = load_records(args.log_dir, latest=not args.all and not args.current_policy)
    report = summarize(current_policy_records(records) if args.current_policy else records)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f'Router report: {report["records"]} audit records')
    print(f'Routes: {report["routes"] or "none"}')
    print(f'Classifier: {report["classifier"]["successes"]} successes, '
          f'{report["classifier"]["average_duration_ms"]}ms avg, '
          f'{report["classifier"]["average_total_tokens"]} tokens avg, '
          f'{report["classifier"]["planned_subagents"]} subagents planned')
    print(f'Fallbacks: {report["fallbacks"] or "none"}; turns: {report["turn_statuses"] or "none"}')
    print(f'Escalations: {report["escalations"]}; idle restore failures: {report["idle_restore_failures"]}; '
          f'average saved estimate: {report["average_saved_percent"]}%')
    print(f'Metrics: {report["metrics"]}')
    print(f'Tuning ready: {report["readiness"]["ready"]} '
          f'({report["readiness"]["completed_turns"]}/200 completed turns)')


if __name__ == "__main__":
    main()
