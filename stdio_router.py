"""Transparent stdio bridge between the Codex desktop app and codex.exe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path


MAX_LINE_BYTES = 16 * 1024 * 1024


def _pump_lines(source, destination, transform, limit):
    """Copy binary lines, bypassing inspection when a line exceeds *limit*."""
    while True:
        chunk = source.readline(limit + 1)
        if not chunk:
            break
        if len(chunk) > limit:
            destination.write(chunk)
            while chunk and not chunk.endswith(b"\n"):
                chunk = source.readline(limit + 1)
                destination.write(chunk)
            destination.flush()
            continue
        destination.write(transform(chunk))
        destination.flush()


def _json_transform(policy, direction):
    callback = policy.on_client if direction == "client" else policy.on_server

    def transform(raw):
        try:
            message = json.loads(raw.decode("utf-8"))
            if not isinstance(message, dict):
                return raw
            if direction == "server":
                rewritten = callback(message)
                if rewritten is None:
                    return raw
                if rewritten == []:
                    return b""
                rows = rewritten if isinstance(rewritten, list) else [rewritten]
                newline = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
                encoded = [json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                           for row in rows]
                return newline.join(encoded) + (newline if raw.endswith((b"\n", b"\r\n")) else b"")
            rewritten = callback(message)
            if message.get("method") != "turn/start" or not isinstance(rewritten, dict) or rewritten == message:
                return raw
            newline = b"\r\n" if raw.endswith(b"\r\n") else (b"\n" if raw.endswith(b"\n") else b"")
            return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + newline
        except Exception:
            try:
                policy.audit("policy_error")
            except Exception:
                pass
            return raw

    return transform


def _copy_chunks(source, destination):
    read = getattr(source, "read1", source.read)
    while True:
        chunk = read(64 * 1024)
        if not chunk:
            break
        destination.write(chunk)
        destination.flush()


def _stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_bridge(executable, args, policy=None, *, stdin=None, stdout=None, stderr=None,
               max_line_bytes=MAX_LINE_BYTES):
    """Run *executable* with original *args* and return its exact exit code."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    stderr = stderr or sys.stderr.buffer
    executable = os.path.abspath(os.fspath(executable))
    child_env = os.environ.copy()
    child_env["CODEX_CLI_PATH"] = executable
    is_app_server = "app-server" in args
    if is_app_server and policy is None:
        try:
            from adaptive_policy import AdaptivePolicy
            policy = AdaptivePolicy()
        except Exception:
            policy = None

    process = subprocess.Popen(
        [executable, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    use_policy = is_app_server and policy is not None
    client_transform = _json_transform(policy, "client") if use_policy else (lambda raw: raw)
    server_transform = _json_transform(policy, "server") if use_policy else (lambda raw: raw)

    def client_pump():
        try:
            if is_app_server:
                _pump_lines(stdin, process.stdin, client_transform, max_line_bytes)
            else:
                _copy_chunks(stdin, process.stdin)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def output_pump(source, destination, transform=None):
        try:
            if transform is None:
                _copy_chunks(source, destination)
            else:
                _pump_lines(source, destination, transform, max_line_bytes)
        except (BrokenPipeError, OSError, ValueError):
            _stop(process)

    threads = [
        threading.Thread(target=client_pump, name="codex-router-stdin", daemon=True),
        threading.Thread(target=output_pump,
                         args=(process.stdout, stdout, server_transform if is_app_server else None),
                         name="codex-router-stdout", daemon=True),
        threading.Thread(target=output_pump, args=(process.stderr, stderr),
                         name="codex-router-stderr", daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait()
        threads[1].join()
        threads[2].join()
        process.stdout.close()
        process.stderr.close()
        return returncode
    except (KeyboardInterrupt, SystemExit):
        _stop(process)
        raise


def _real_codex():
    configured = os.environ.get("ROUTER_REAL_CODEX")
    if not configured:
        metadata = Path(__file__).resolve().parent / "state" / "native-runtime.json"
        try:
            configured = json.loads(metadata.read_text(encoding="utf-8-sig"))["real_codex"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("real Codex executable is not configured") from exc
    path = Path(configured)
    root = Path(__file__).resolve().parent
    router_paths = {
        Path(__file__).resolve(),
        Path(sys.argv[0]).resolve(),
        (root / "state" / "bin" / "AdaptiveCodexRouter.exe").resolve(),
    }
    if not path.is_absolute() or not path.is_file() or path.resolve() in router_paths:
        raise RuntimeError("real Codex executable must be an absolute, existing, non-router file")
    return str(path.resolve())


def main():
    try:
        return run_bridge(_real_codex(), sys.argv[1:])
    except RuntimeError as exc:
        print(f"Codex Model Router: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
