"""Offline contract and preservation checks: python -m unittest -v"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install
import router


class RouterChecks(unittest.TestCase):
    def event(self, role="lookup", **args):
        return {"hook_event_name": "PreToolUse", "tool_name": "collaboration.spawn_agent",
                "tool_input": {"task_name": "test", "message": f"[codex-route:{role}]\n한글 Task",
                               "fork_turns": "none", "unknown_future_field": [1, 2], **args}}

    def test_routes_preserve_all_original_arguments_and_input(self):
        for role, pair in router.ROUTES.items():
            with self.subTest(role=role):
                event = self.event(role)
                original = copy.deepcopy(event)
                out, meta = router.route(event)
                selected = out["hookSpecificOutput"]
                self.assertEqual(selected["permissionDecision"], "allow")
                self.assertEqual(selected["updatedInput"], {**event["tool_input"], "model": pair[0], "reasoning_effort": pair[1]})
                self.assertEqual(event, original)
                self.assertEqual(meta["decision"], "routed")

    def test_explicit_choices_profiles_and_full_context_are_never_rewritten(self):
        cases = [{"model": "custom"}, {"reasoning_effort": "low"}, {"thinking": "max"},
                 {"agent_type": "reviewer"}, {"fork_turns": "all"}, {"fork_turns": None},
                 {"fork_turns": "0"}, {"fork_turns": 2}, {"fork_context": True}]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(router.route(self.event(**args))[0], {})
        missing = self.event()
        del missing["tool_input"]["fork_turns"]
        self.assertEqual(router.route(missing)[0], {})

    def test_no_keyword_guessing_or_nested_marker(self):
        for message in ("lookup this", "Text\n[codex-route:lookup]\nsource", "[codex-route:nope]\nX"):
            self.assertEqual(router.route(self.event(message=message))[0], {})
        self.assertEqual(router.route([])[0], {})
        self.assertEqual(router.route({"hook_event_name": "PreToolUse", "tool_name": "Bash"})[0], {})

    def test_cli_shape_and_partial_fork(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                 "tool_input": {"message": "[codex-route:implementation]\nTask"}}
        self.assertEqual(router.route(event)[1]["decision"], "routed")
        self.assertEqual(router.route(self.event(fork_turns="2"))[1]["decision"], "routed")

    def test_observer_keeps_only_known_model_metadata(self):
        out, meta = router.route({"hook_event_name": "SubagentStart", "model": "gpt-5.6-luna",
                                  "transcript_path": "private-path", "prompt": "private-text"})
        self.assertEqual(out, {})
        self.assertEqual(meta, {"decision": "child_started", "model": "gpt-5.6-luna", "effort": None})
        _, correlated = router.route({"hook_event_name": "SubagentStart", "session_id": "private-id"})
        self.assertEqual(len(correlated["parent_fingerprint"]), 16)
        self.assertNotIn("private-id", json.dumps(correlated))

    def test_policy_and_malformed_stdin_fail_open(self):
        out, _ = router.route({"hook_event_name": "UserPromptSubmit", "prompt": "응 진행해"})
        self.assertIn("whole", out["hookSpecificOutput"]["additionalContext"])
        result = subprocess.run([sys.executable, str(router.ROOT / "router.py")], input=b"bad json",
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(result.stderr, b"")

    def test_install_is_idempotent_and_uninstall_preserves_other_hooks(self):
        other = {"description": "keep", "hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "existing-hook"}]}], "Stop": [{"hooks": []}]}}
        installed = install.merge(other)
        self.assertEqual(install.merge(installed), installed)
        self.assertEqual(install.merge(installed, remove=True), other)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            home.mkdir()
            before = json.dumps(other).encode()
            (home / "hooks.json").write_bytes(before)
            with patch.object(install, "ROOT", root):
                first = install.update(home)
                self.assertTrue(first["changed"])
                self.assertEqual((Path(first["backup"]) / "hooks.json").read_bytes(), before)
                self.assertFalse(install.update(home)["changed"])
                install.update(home, remove=True)
            self.assertEqual(json.loads((home / "hooks.json").read_text()), other)


if __name__ == "__main__":
    unittest.main()
