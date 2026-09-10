"""Inspect/activate the exact installed hook definitions via Codex App Server."""
import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import install
import router


class Codex:
    def __init__(self, standalone=False):
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("codex executable not found on PATH")
        args = [executable, "app-server", "--stdio"] if standalone else [executable, "app-server", "proxy"]
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "codex_model_router", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass
        self.messages.put(None)

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            try:
                value = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"Codex timed out: {method}") from None
            if value is None:
                raise RuntimeError("Codex connection closed; try --standalone if no daemon is running")
            if value.get("id") == request_id and "method" not in value:
                if "error" in value:
                    raise RuntimeError(f"{method}: {value['error']}")
                return value["result"]
            if "method" in value and "id" in value:
                self.send({"id": value["id"], "error": {"code": -32601, "message": "No interactive handler"}})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()  # Only our stdio/proxy client, never the daemon or user sessions.
        self.process.wait(timeout=5)


def owned_hooks(client):
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    result = client.call("hooks/list", {"cwds": [str(install.ROOT)]})
    entry = result["data"][0]
    if entry.get("errors"):
        raise RuntimeError(f"Codex hook loading errors: {entry['errors']}")
    hooks = [h for h in entry["hooks"] if
             Path(h["sourcePath"]).resolve() == home / "hooks.json" and
             (h.get("statusMessage") or "").startswith(install.TAG)]
    if len(hooks) != 3:
        raise RuntimeError(f"Expected exactly three router hooks, found {len(hooks)}")
    if {h["eventName"] for h in hooks} != {"preToolUse", "userPromptSubmit", "subagentStart"}:
        raise RuntimeError("Expected one routing, policy and observation hook")
    for hook in hooks:
        routing = hook["eventName"] == "preToolUse"
        expected = {"command": install.command(), "handlerType": "command", "async": False,
                    "matcher": install.MATCHER if routing else None, "timeoutSec": 5,
                    "statusMessage": install.TAG + {"preToolUse": "route", "userPromptSubmit": "policy",
                                                     "subagentStart": "observe"}[hook["eventName"]], "isManaged": False}
        if any(hook.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Installed hook differs from the reviewed local definition")
    return home, hooks, entry.get("warnings", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "activate"))
    parser.add_argument("--standalone", action="store_true", help="Use own app-server instead of the existing daemon proxy")
    args = parser.parse_args()
    client = Codex(args.standalone)
    try:
        home, hooks, warnings = owned_hooks(client)
        catalog = client.call("model/list", {})
        supported = {m["model"]: {r["reasoningEffort"] for r in m["supportedReasoningEfforts"]}
                     for m in catalog["data"]}
        if any(effort not in supported.get(model, set()) for model, effort in router.ROUTES.values()):
            raise RuntimeError("A routed model/effort is missing from the current catalog")
        backup = None
        if args.action == "activate":
            # The user must have authorized installation/activation. This reviews only these
            # exact commands; it never bypasses trust globally or trusts other hooks.
            changes = []
            for hook in hooks:
                if not hook["enabled"] or hook["trustStatus"] != "trusted":
                    prefix = "hooks.state." + json.dumps(hook["key"])
                    changes += [{"keyPath": prefix + ".enabled", "value": True, "mergeStrategy": "replace"},
                                {"keyPath": prefix + ".trusted_hash", "value": hook["currentHash"], "mergeStrategy": "replace"}]
            if changes:
                config = home / "config.toml"
                if config.exists():
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                    backup = config.with_name("config.toml.router-backup-" + stamp)
                    shutil.copy2(config, backup)
                config_read = client.call("config/read", {"includeLayers": True})
                if config_read["config"].get("features", {}).get("hooks") is False:
                    raise RuntimeError("Hooks are disabled in the effective configuration")
                version = next((layer["version"] for layer in config_read.get("layers", [])
                                if layer["name"].get("type") == "user" and
                                Path(layer["name"].get("file", "")).resolve() == config), None)
                if version is None:
                    raise RuntimeError("User config version could not be determined")
                client.call("config/batchWrite", {"edits": changes, "reloadUserConfig": True,
                            "expectedVersion": version, "filePath": str(config)})
                home, hooks, warnings = owned_hooks(client)
            if any(not h["enabled"] or h["trustStatus"] != "trusted" for h in hooks):
                raise RuntimeError("Activation was not confirmed by Codex")
        print(json.dumps({"hooks": [{key: h.get(key) for key in
                          ("eventName", "enabled", "trustStatus", "currentHash", "sourcePath")} for h in hooks],
                          "warnings": warnings, "config_backup": str(backup) if backup else None}, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
