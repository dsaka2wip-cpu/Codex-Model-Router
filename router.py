"""Codex PreToolUse routing. Python standard library only; no network calls."""
import json
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROUTES = {
    "lookup": ("gpt-5.6-luna", "medium"),
    "implementation": ("gpt-5.6-sol", "medium"),
    "critical_review": ("gpt-5.6-sol", "high"),
    "hard_problem": ("gpt-6-astra", "high"),
}
MARKER = re.compile(r"\A\[codex-route:([a-z_]+)\]\r?\n")
TOOLS = {"spawn_agent", "Agent", "collaboration.spawn_agent", "functions.spawn_agent"}
POLICY = """Codex Model Router (subagents only): Do mechanical checks with tools directly.
When a useful independent subtask merits delegation, classify it using the whole
conversation, including short follow-ups. Prefix the child instruction with exactly
[codex-route:lookup], [codex-route:implementation], [codex-route:critical_review],
or [codex-route:hard_problem] on its own first line. lookup = exact location/field
extraction/formatting (Luna medium); implementation = call-flow analysis, bounded
coding/UI/tests (Sol medium); critical_review = deep debugging, security, integrity
or semantic evidence checks (Sol high); hard_problem = independent difficult
architecture/requirements conflicts (Astra high); usually resolve design yourself.
For automatic routing set the role's exact model and reasoning_effort explicitly
when spawning (lookup gpt-5.6-luna/medium; implementation gpt-5.6-sol/medium;
critical_review gpt-5.6-sol/high; hard_problem gpt-6-astra/high). The current V2
spawn path can bypass PreToolUse, so DO NOT rely on argument rewriting there.
Avoid named agent profiles whose own model settings override these arguments.
Use a fresh child
context only when you can supply the goal, constraints, paths/sources, write scope,
acceptance checks and desired return format. Never drop necessary source material.
On tools with fork_turns explicitly use 'none' (or an appropriate partial fork);
the hook NEVER converts a full-history fork. Explicit user model/effort wins: pass
the requested arguments and omit the route marker. Unknown roles keep defaults.
No classifier API, compulsory delegation, retry ladder, recursive fan-out or duplicate
parent work. Diagnose environment/input errors before escalating model difficulty.
Keep required tests, evidence, progress updates and the current main model settings.
"""


def route(event):
    """Return (hook response, non-sensitive decision metadata)."""
    if not isinstance(event, dict):
        return {}, {"decision": "invalid_event"}
    kind = event.get("hook_event_name")
    if kind == "SubagentStart":
        model = event.get("model")
        known_models = {pair[0] for pair in ROUTES.values()}
        effort = event.get("reasoning_effort")
        meta = {"decision": "child_started", "model": model if model in known_models else "unknown",
                "effort": effort if effort in {"low", "medium", "high", "xhigh", "max", "ultra"} else None}
        if isinstance(event.get("session_id"), str):
            meta["parent_fingerprint"] = hashlib.sha256(event["session_id"].encode()).hexdigest()[:16]
        return {}, meta
    if kind == "UserPromptSubmit":
        return {"hookSpecificOutput": {"hookEventName": kind,
                                      "additionalContext": POLICY}}, {"decision": "policy"}
    if kind != "PreToolUse" or event.get("tool_name") not in TOOLS:
        return {}, {"decision": "unrelated"}
    args = event.get("tool_input")
    meta = {"decision": "invalid_arguments"}
    if not isinstance(args, dict):
        return {}, meta
    # The local CLI uses message too; prompt is accepted for other documented adapters.
    fields = [key for key in ("message", "prompt") if isinstance(args.get(key), str)]
    if len(fields) != 1:
        return {}, meta
    match = MARKER.match(args[fields[0]])
    role = match.group(1) if match else None
    if role not in ROUTES:
        return {}, {"decision": "unclassified"}
    meta = {"decision": "preserved", "role": role}
    if any(args.get(key) is not None for key in
           ("model", "reasoning_effort", "model_reasoning_effort", "effort", "thinking")):
        return {}, {**meta, "reason": "explicit_model_or_effort"}
    if args.get("agent_type") or args.get("subagent_type"):
        return {}, {**meta, "reason": "named_profile"}
    # Desktop fork_turns defaults to all. Never remove inherited context to force a model.
    if event.get("tool_name") == "collaboration.spawn_agent" or "task_name" in args or "fork_turns" in args:
        fork = args.get("fork_turns", "all")
        if not isinstance(fork, str) or (fork != "none" and not re.fullmatch(r"[1-9][0-9]*", fork)):
            return {}, {**meta, "reason": "full_or_unknown_fork"}
    if args.get("fork_context") is True:
        return {}, {**meta, "reason": "full_fork_context"}
    model, effort = ROUTES[role]
    updated = dict(args)
    updated.update(model=model, reasoning_effort=effort)
    # Preserve the original task verbatim, including its harmless role marker.
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "allow",
                                  "updatedInput": updated}}, {
        "decision": "routed", "role": role, "model": model, "effort": effort}


def audit(meta):
    """One small file per UTC day; no prompts, paths, raw ids or tool outputs."""
    if meta["decision"] == "unrelated":
        return
    try:
        folder = ROOT / "logs"
        folder.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        line = json.dumps({"time": now.isoformat(), **meta}, ensure_ascii=True) + "\n"
        with (folder / (now.strftime("%Y-%m-%d") + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass  # Logging must never prevent an otherwise valid tool call.


def main():
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        result, meta = route(event)
    except (ValueError, UnicodeError, TypeError):
        result, meta = {}, {"decision": "invalid_json"}
    audit(meta)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
