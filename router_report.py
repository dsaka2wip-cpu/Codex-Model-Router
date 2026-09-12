"""Summarize Router audit metadata without reading prompt or response text."""

import argparse
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "state" / "adaptive-logs"


def load_records(log_dir=LOG_DIR, *, latest=False):
    paths = sorted(Path(log_dir).glob("adaptive-*.jsonl"))
    for path in (paths[-1:] if latest else paths):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("event"), str):
                yield row


def summarize(records):
    records = list(records)
    routes = [row for row in records if row["event"] == "route"]
    classifiers = [row for row in records if row["event"] == "classifier"]
    fallbacks = [row for row in records if row["event"] == "classifier_fallback"]
    completed = [row for row in records if row["event"] == "turn_completed"]
    footers = [row for row in records if row["event"] == "footer"]
    durations = [row["duration_ms"] for row in classifiers if type(row.get("duration_ms")) is int]
    tokens = [row["total_tokens"] for row in classifiers if type(row.get("total_tokens")) is int]
    saved = [row["saved_percent"] for row in footers if type(row.get("saved_percent")) is int]
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
        "escalations": len([row for row in records if row["event"] == "escalated"]),
        "idle_restore_failures": len([row for row in records if row["event"] == "idle_restore_failed"]),
        "average_saved_percent": round(sum(saved) / len(saved)) if saved else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize non-sensitive Adaptive Router audit metadata.")
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--all", action="store_true", help="include prior Router process logs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = summarize(load_records(args.log_dir, latest=not args.all))
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


if __name__ == "__main__":
    main()
