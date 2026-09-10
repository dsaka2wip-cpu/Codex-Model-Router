"""Merge/remove only this router's hooks; backups precede every real change."""
import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAG = "Codex Model Router: "
MATCHER = r"^(Agent|spawn_agent|collaboration\.spawn_agent|functions\.spawn_agent)$"


def command():
    # Quoting works under the Windows command runner even when Python/path contains spaces.
    python = str(Path(sys.executable).resolve()).replace("'", "''")
    script = str(ROOT / "router.py").replace("'", "''")
    if os.name == "nt":
        if any(c in python + script for c in ('"', '\r', '\n')):
            raise ValueError("Unsupported command path")
        return f'powershell.exe -NoProfile -NonInteractive -Command "& \'{python}\' \'{script}\'"'
    import shlex
    return shlex.join([sys.executable, str(ROOT / "router.py")])


def merge(document, remove=False):
    result = copy.deepcopy(document)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for event in ("UserPromptSubmit", "PreToolUse", "SubagentStart"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{event} must be an array")
        clean = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid {event} matcher group")
            remaining = [h for h in group["hooks"]
                         if not (isinstance(h, dict) and str(h.get("statusMessage", "")).startswith(TAG))]
            if remaining or not group["hooks"]:
                clean.append({**group, "hooks": remaining})
        if not remove:
            handler = {"type": "command", "command": command(), "timeout": 5,
                       "statusMessage": TAG + {"UserPromptSubmit": "policy", "PreToolUse": "route",
                                                "SubagentStart": "observe"}[event]}
            clean.append({**({"matcher": MATCHER} if event == "PreToolUse" else {}), "hooks": [handler]})
        if clean:
            hooks[event] = clean
        else:
            hooks.pop(event, None)
    return result


def update(home, remove=False):
    path = home / "hooks.json"
    before = path.read_bytes() if path.exists() else None
    document = json.loads(before.decode("utf-8-sig")) if before is not None else {}
    if not isinstance(document, dict):
        raise ValueError("hooks.json must be an object")
    result = merge(document, remove)
    if result == document or (before is None and remove):
        return {"changed": False, "path": str(path)}
    home.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = ROOT / "backups" / stamp
    backup.mkdir(parents=True)
    if before is not None:
        (backup / "hooks.json").write_bytes(before)
    (backup / "state.json").write_text(json.dumps({"hooks_existed": before is not None}), encoding="utf-8")
    # No auth/config secrets are copied into the desktop project.
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=home, delete=False) as stream:
            name = stream.name
            stream.write(data)
        # Refuse to overwrite another process's concurrent edit.
        current = path.read_bytes() if path.exists() else None
        if current != before:
            raise RuntimeError("hooks.json changed concurrently; retry after reviewing it")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return {"changed": True, "path": str(path), "backup": str(backup)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    args = parser.parse_args()
    print(json.dumps(update(args.codex_home.resolve(), args.action == "uninstall"), ensure_ascii=True))
