"""Adaptive native-GUI policy with a hidden, fixed-model routing classifier."""
import copy
import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from datetime import datetime, timezone

from main_policy import choose_route, ALIASES

ROOT = Path(__file__).resolve().parent
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
TIERS = {
    "FAST": {"model": "gpt-5.6-luna", "effort": "low"},
    "NORMAL": {"model": "gpt-5.6-sol", "effort": "medium"},
    "DEEP": {"model": "gpt-6-astra", "effort": "high"},
}
ROLE_TIER = {"lookup": "FAST", "implementation": "NORMAL",
             "critical_review": "DEEP", "hard_problem": "DEEP"}
DEFAULT = {"enabled": True, "model": "auto", "effort": "auto", "tiers": TIERS,
           "classifier": {"model": "gpt-5.6-sol", "effort": "medium"}}
MODEL_LABELS = {"gpt-5.6-luna": "Luna", "gpt-5.6-terra": "Terra",
                "gpt-5.6-sol": "Sol", "gpt-6-astra": "Astra"}
# OpenAI Work/Codex token-based rate-card ratios: input, cached input, output.
CREDIT_RATES = {"gpt-5.6-luna": (5.0, 0.5, 30.0), "gpt-5.6-sol": (100.0, 10.0, 500.0),
                "gpt-5.6-terra": (50.0, 5.0, 300.0), "gpt-6-astra": (250.0, 25.0, 1250.0)}
# No fixed public effort multiplier exists. Only the Astra/Ultra counterfactual reasoning size uses this heuristic.
EFFORT_REASONING_RATIO = {"none": 0.20, "minimal": 0.30, "low": 0.60, "medium": 0.75,
                          "high": 0.90, "xhigh": 0.95, "max": 1.00, "ultra": 1.00}
AVERAGE_MESSAGE_CREDITS = {"gpt-5.6-luna": 1.0, "gpt-5.6-terra": 5.0,
                           "gpt-5.6-sol": 11.0, "gpt-6-astra": 16.0}
EFFORT_RANK = {name: rank for rank, name in enumerate(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))}


def rpc_id(value):
    return (type(value).__name__, value) if type(value) in (str, int) else None


def safe_model(value):
    return value if isinstance(value, str) and value in set(ALIASES.values()) | {"gpt-5.5", "gpt-5.3-codex-spark"} else "other"


def tier_for_selection(model, effort):
    rank = EFFORT_RANK.get(effort, 0)
    if model == "gpt-5.6-luna" and rank <= EFFORT_RANK["medium"]:
        return "FAST"
    if (model == "gpt-6-astra"
            or model == "gpt-5.6-sol" and rank >= EFFORT_RANK["high"]
            or model == "gpt-5.6-terra" and rank >= EFFORT_RANK["xhigh"]):
        return "DEEP"
    return "NORMAL"


def route_for_selection(model, effort):
    tier = tier_for_selection(model, effort)
    if model == "gpt-5.6-luna":
        role = "lookup"
    elif model == "gpt-6-astra":
        role = "hard_problem"
    elif model == "gpt-5.6-sol" and EFFORT_RANK.get(effort, 0) >= EFFORT_RANK["high"]:
        role = "critical_review"
    else:
        role = "implementation"
    return {"role": role, "model": model, "effort": effort, "reason": "LLM classifier",
            "explicit": False, "explicit_model": False, "explicit_effort": False}, tier


def add_subagent_plan(params, routes):
    """Add trusted route metadata without copying classifier-authored prose."""
    if not routes:
        return False
    collaboration = params.get("collaborationMode")
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    if not isinstance(settings, dict):
        return False
    lines = [
        "Adaptive Router subagent plan (fixed Sol/medium classifier):",
        "Create these independent lanes promptly and run them in parallel when still useful.",
        "Derive each concrete task from the user's request; do not delegate overlapping writes.",
        "Use collaboration.spawn_agent with fork_turns=none and the exact model/effort below.",
    ]
    for index, item in enumerate(routes, 1):
        lines.append(f"{index}. [codex-route:{item['role']}] {item['model']} / {item['effort']}")
    lines.append("Each child must stay bounded, must not spawn children, and must return a concise result.")
    previous = settings.get("developer_instructions")
    settings["developer_instructions"] = ((previous.rstrip() + "\n\n") if isinstance(previous, str) and previous else "") + "\n".join(lines)
    return True


def selection(params, fallback=None):
    fallback = fallback or {}
    nested = params.get("collaborationMode")
    if isinstance(nested, dict):
        settings = nested.get("settings")
        if not isinstance(settings, dict) or nested.get("mode") not in ("default", "plan"):
            raise ValueError("unknown_collaboration_mode")
        return settings.get("model") or fallback.get("model"), settings.get("reasoning_effort", fallback.get("effort"))
    return params.get("model") or fallback.get("model"), params.get("effort") or fallback.get("effort")


def controls(prompt):
    """Only a literal first-line control; never remove it from the wire prompt."""
    first, _, rest = prompt.partition("\n")
    match = re.fullmatch(r"\[router\s+([^\]\r\n]+)\]\s*", first.strip(), re.I)
    if not match:
        return {}, prompt
    tokens = match.group(1).lower().split()
    if tokens == ["auto"]:
        return {"model": "auto", "effort": "auto"}, rest
    if tokens == ["off"]:
        return {"model": "gui", "effort": "gui"}, rest
    result = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep != "=" or key not in ("model", "effort", "tier") or key in result:
            raise ValueError("invalid_control")
        if key == "model":
            value = ALIASES.get(value, value)
            if value not in ("auto", "gui") and not re.fullmatch(r"gpt-[a-z0-9.-]+", value):
                raise ValueError("invalid_model_control")
        elif key == "effort" and value not in EFFORTS | {"auto", "gui"}:
            raise ValueError("invalid_effort_control")
        elif key == "tier":
            value = value.upper()
            if value not in TIERS:
                raise ValueError("invalid_tier_control")
        result[key] = value
    return result, rest


class AdaptivePolicy:
    """Mutate only main turn selection fields; all control-plane packets pass through.

    ponytail: process-local role/control memory is bounded to 256 threads. On restart
    it returns to file defaults; native Codex owns durable conversation history.
    """
    def __init__(self, path=None, audit_dir=None):
        self.path = Path(path) if path else ROOT / "state" / "adaptive-config.json"
        self.audit_dir = Path(audit_dir) if audit_dir else ROOT / "state" / "adaptive-logs"
        self.lock = threading.RLock()
        self.pending = OrderedDict()
        self.threads = OrderedDict()
        self.turns = OrderedDict()
        self.catalog = {}
        self.auth = None
        self.classifier = None
        self.settings_updater = None
        self.audit("bridge_started")

    def set_classifier(self, callback):
        self.classifier = callback

    def set_settings_updater(self, callback):
        self.settings_updater = callback

    def _restore_idle(self, params):
        thread_id = params["threadId"]
        try:
            self.settings_updater(params)
            self.audit("idle_restored", thread_id, model=params["model"], effort=params["effort"])
        except Exception:
            self.audit("idle_restore_failed", thread_id)

    def audit(self, event, thread_id=None, **fields):
        """Closed allowlist: no prompt, token, path, command, error text or raw ID."""
        if event not in {"bridge_started", "policy_error", "config_error", "control_error",
                         "passthrough", "route", "settings", "turn_completed", "rpc_error",
                         "stream", "approval", "file_change", "model_rerouted", "auth", "footer",
                          "classifier", "classifier_fallback", "idle_restored", "idle_restore_failed"}:
            return
        record = {"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "event": event}
        if isinstance(thread_id, str):
            record["thread"] = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
        for key, value in fields.items():
            if key in ("model", "from_model", "to_model"):
                record[key] = safe_model(value)
            elif key == "effort" and value in EFFORTS:
                record[key] = value
            elif key == "tier" and value in TIERS:
                record[key] = value
            elif key == "mode" and value in ("default", "plan"):
                record[key] = value
            elif key == "status" and value in ("completed", "failed", "interrupted", "inProgress"):
                record[key] = value
            elif key == "reason" and value in {"disabled", "not_ready", "no_text", "unknown_mode",
                      "unsupported_selection", "invalid_control", "gui", "custom_provider",
                      "background", "invalid_packet", "unavailable", "failed"}:
                record[key] = value
            elif key == "auth" and value in ("chatgpt", "chatgptAuthTokens", "apiKey", "other"):
                record[key] = value
            elif key in ("count", "code") and type(value) is int:
                record[key] = value
            elif key in ("turns", "luna", "terra", "sol", "astra", "input_tokens", "cached_tokens",
                         "output_tokens", "reasoning_tokens", "total_tokens", "saved_percent",
                         "duration_ms") and type(value) is int:
                record[key] = value
            elif key == "usage_source" and value in ("total_delta", "last", "equal_turn"):
                record[key] = value
            elif key == "stage" and value in ("prepare", "fork", "read", "turn_start", "turn_wait", "result", "unknown"):
                record[key] = value
            elif key == "context_mode" and value in ("fork", "read"):
                record[key] = value
            elif key == "error_kind" and value in ("invalid_params", "not_found", "busy", "permission",
                                                     "unsupported", "other"):
                record[key] = value
            elif key == "failure_kind" and value in ("timeout", "rpc", "invalid_json", "invalid_result",
                                                       "internal"):
                record[key] = value
            elif key == "rpc_code" and type(value) in (str, int):
                record[key] = value
        try:
            with self.lock:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                target = self.audit_dir / ("adaptive-%s.jsonl" % os.getpid())
                # A capped per-process metadata log; no destructive rotation.
                if target.exists() and target.stat().st_size > 2_000_000:
                    return
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError:
            pass  # Logging failure must never break the native connection.

    def _thread(self, thread_id):
        if thread_id not in self.threads:
            if len(self.threads) >= 256:
                self.threads.popitem(last=False)
            self.threads[thread_id] = {
                "controls": {}, "previous": None, "settings": {}, "stream": 0,
                "active_turn": None, "usage_total": None, "last_footer": None,
                "stats": {"turns": 0, "models": {"Luna": 0, "Terra": 0, "Sol": 0, "Astra": 0},
                          "actual_units": 0.0, "baseline_units": 0.0},
            }
        self.threads.move_to_end(thread_id)
        return self.threads[thread_id]

    def _config(self):
        if not self.path.exists():
            merged = copy.deepcopy(DEFAULT)
            merged["enabled"] = False
            return merged
        config = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or set(config) - set(DEFAULT):
            raise ValueError("invalid_config")
        merged = copy.deepcopy(DEFAULT)
        merged.update(config)
        if type(merged["enabled"]) is not bool:
            raise ValueError("invalid_config")
        for axis in ("model", "effort"):
            value = merged[axis]
            if not isinstance(value, str):
                raise ValueError("invalid_config")
            if axis == "effort" and value not in EFFORTS | {"auto", "gui"}:
                raise ValueError("invalid_config")
            if axis == "model":
                merged[axis] = ALIASES.get(value, value)
        if not isinstance(merged["tiers"], dict) or set(merged["tiers"]) != set(TIERS):
            raise ValueError("invalid_config")
        for row in merged["tiers"].values():
            if not isinstance(row, dict) or set(row) != {"model", "effort"}:
                raise ValueError("invalid_config")
            if not isinstance(row["model"], str) or row["effort"] not in EFFORTS:
                raise ValueError("invalid_config")
        classifier = merged["classifier"]
        if (not isinstance(classifier, dict) or set(classifier) != {"model", "effort"}
                or not isinstance(classifier["model"], str) or classifier["effort"] not in EFFORTS):
            raise ValueError("invalid_config")
        classifier["model"] = ALIASES.get(classifier["model"], classifier["model"])
        return merged

    def on_client(self, message):
        if not isinstance(message, dict):
            return message
        with self.lock:
            method, params = message.get("method"), message.get("params")
            key = rpc_id(message.get("id"))
            if method in ("account/login/start", "account/logout"):
                self.auth = None
            if method in ("account/read", "model/list", "thread/start", "thread/resume", "thread/fork", "turn/start") and key:
                if len(self.pending) >= 1024:
                    self.pending.popitem(last=False)
                self.pending[key] = {"method": method, "thread": params.get("threadId") if isinstance(params, dict) else None}
            if method != "turn/start" or not isinstance(params, dict):
                return message
            thread_id = params.get("threadId")
            if not isinstance(thread_id, str) or not key:
                return message
            state = self._thread(thread_id)
            try:
                config = self._config()
            except (ValueError, OSError, TypeError):
                self.audit("config_error", thread_id)
                return message
            if not config["enabled"]:
                self.audit("passthrough", thread_id, reason="disabled")
                return message
            if self.auth not in ("chatgpt", "chatgptAuthTokens") or not self.catalog:
                self.audit("passthrough", thread_id, reason="not_ready")
                return message
            if params.get("toolOutput") is not None:
                return message
            if state["settings"].get("modelProvider") not in (None, "openai"):
                self.audit("passthrough", thread_id, reason="custom_provider")
                return message
            inputs = params.get("input")
            if not isinstance(inputs, list) or any(not isinstance(x, dict) for x in inputs):
                return message
            prompt = "\n".join(x["text"] for x in inputs if x.get("type") == "text" and isinstance(x.get("text"), str))
            if not prompt.strip():
                self.audit("passthrough", thread_id, reason="no_text")
                return message
            try:
                control, prose = controls(prompt)
                settings = {**config, **state["controls"], **control}
                # Tier is turn-local; model/effort control persists for this process/thread.
                if not prose.strip():
                    state["controls"].update({k: v for k, v in control.items() if k != "tier"})
                    return message
                old_model, old_effort = selection(params, state["settings"])
                if settings["model"] == settings["effort"] == "gui":
                    state["controls"].update({k: v for k, v in control.items() if k != "tier"})
                    if old_model in self.catalog and old_effort in self.catalog[old_model]["efforts"]:
                        self.pending[key].update(tier="GUI", model=old_model, effort=old_effort)
                    self.audit("passthrough", thread_id, reason="gui", model=old_model, effort=old_effort)
                    return message
                fallback = choose_route(prose, state["previous"])
                decision = None
                needs_classifier = (settings.get("tier") is None and self.classifier is not None
                                    and any(settings[axis] == "auto" and not fallback["explicit_" + axis]
                                            for axis in ("model", "effort")))
                if needs_classifier:
                    try:
                        # Let the stdout pump process unrelated GUI events while the hidden turn runs.
                        self.lock.release()
                        try:
                            decision = self.classifier(thread_id, prose, config["classifier"],
                                                       copy.deepcopy(self.catalog), copy.deepcopy(inputs))
                        finally:
                            self.lock.acquire()
                        route, tier = route_for_selection(decision["model"], decision["effort"])
                        usage = decision.get("usage") or {}
                        self.audit("classifier", thread_id, model=decision["model"], effort=decision["effort"],
                                   count=len(decision.get("subagents", [])),
                                   context_mode=decision.get("context_mode"),
                                   duration_ms=decision.get("duration_ms"),
                                   input_tokens=usage.get("inputTokens"),
                                   cached_tokens=usage.get("cachedInputTokens"),
                                   output_tokens=usage.get("outputTokens"),
                                   reasoning_tokens=usage.get("reasoningOutputTokens"),
                                   total_tokens=usage.get("totalTokens"))
                    except Exception as error:
                        decision = None
                        failure_kind = getattr(error, "failure_kind", None)
                        if failure_kind is None:
                            failure_kind = "timeout" if isinstance(error, TimeoutError) else "internal"
                        self.audit("classifier_fallback", thread_id, reason="failed",
                                   stage=getattr(error, "stage", "unknown"),
                                   rpc_code=getattr(error, "rpc_code", None),
                                   error_kind=getattr(error, "error_kind", None),
                                   failure_kind=failure_kind,
                                   duration_ms=getattr(error, "duration_ms", None))
                if decision is None:
                    route = fallback
                    tier = settings.get("tier", ROLE_TIER[route["role"]])
                    preferred = config["tiers"][tier]
                else:
                    preferred = decision
                chosen = {}
                for axis, old in (("model", old_model), ("effort", old_effort)):
                    selector = settings[axis]
                    if selector == "gui":
                        chosen[axis] = old
                    elif route.get("explicit_" + axis) and axis not in control:
                        chosen[axis] = route[axis]
                    else:
                        chosen[axis] = preferred[axis] if selector == "auto" else selector
                entry = self.catalog.get(chosen["model"])
                if not entry or chosen["effort"] not in entry["efforts"]:
                    self.audit("passthrough", thread_id, reason="unsupported_selection")
                    return message
                if settings.get("tier") is None:
                    tier = tier_for_selection(chosen["model"], chosen["effort"])
                updated = copy.deepcopy(message)
                target = updated["params"]
                nested = target.get("collaborationMode")
                idle = config["tiers"]["NORMAL"]
                restore = {"threadId": thread_id, "model": idle["model"], "effort": idle["effort"]}
                if isinstance(nested, dict):
                    restore["collaborationMode"] = copy.deepcopy(params["collaborationMode"])
                    idle_settings = restore["collaborationMode"]["settings"]
                    idle_settings["model"] = idle["model"]
                    idle_settings["reasoning_effort"] = idle["effort"]
                for axis in ("model", "effort"):
                    if settings[axis] == "gui":
                        continue
                    if isinstance(nested, dict):
                        nested["settings"]["model" if axis == "model" else "reasoning_effort"] = chosen[axis]
                        # Keep existing redundant non-null top-level values consistent.
                        if target.get(axis) is not None:
                            target[axis] = chosen[axis]
                    else:
                        target[axis] = chosen[axis]
                if decision is not None:
                    add_subagent_plan(target, decision.get("subagents", []))
                self.pending[key].update(previous=state["previous"], controls=state["controls"].copy(),
                                         route=route, tier=tier, model=chosen["model"], effort=chosen["effort"],
                                         restore=restore,
                                         classifier=(decision and {"model": config["classifier"]["model"],
                                                                  "effort": config["classifier"]["effort"],
                                                                  "usage": decision.get("usage")}),
                                         new_controls={k: v for k, v in control.items() if k != "tier"})
                # Commit policy state only after the backend accepts turn/start.
                self.audit("route", thread_id, tier=tier, model=chosen["model"],
                           effort=chosen["effort"], mode=(nested or {}).get("mode", "default"))
                return updated
            except (ValueError, TypeError, KeyError):
                self.audit("control_error", thread_id, reason="invalid_control")
                return message

    def _observe_settings(self, thread_id, settings):
        if not isinstance(thread_id, str) or not isinstance(settings, dict):
            return
        state = self._thread(thread_id)
        for name in ("model", "effort", "modelProvider"):
            if name in settings:
                state["settings"][name] = settings[name]
        mode = (settings.get("collaborationMode") or {}).get("mode")
        turn = self.turns.get(state.get("active_turn"))
        if turn:
            turn["model"] = state["settings"].get("model") or turn["model"]
            turn["effort"] = state["settings"].get("effort") or turn["effort"]
        self.audit("settings", thread_id, model=state["settings"].get("model"),
                    effort=state["settings"].get("effort"), mode=mode)

    @staticmethod
    def _usage_delta(total, start):
        if not isinstance(total, dict) or not isinstance(start, dict):
            return None
        keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
        if any(type(total.get(k)) is not int or type(start.get(k)) is not int for k in keys):
            return None
        return {k: max(0, total[k] - start[k]) for k in keys}

    @staticmethod
    def _estimate_units(turn):
        usage = turn.get("usage")
        model, effort = turn["model"], turn["effort"]
        classifier_units = 0.0
        classifier = turn.get("classifier")
        if classifier:
            classifier_usage = classifier.get("usage")
            values = ([classifier_usage.get(k) for k in
                       ("inputTokens", "cachedInputTokens", "outputTokens")]
                      if isinstance(classifier_usage, dict) else [])
            if len(values) == 3 and all(type(x) is int and x >= 0 for x in values):
                input_tokens, cached, output = values
                rate_in, rate_cached, rate_out = CREDIT_RATES[classifier["model"]]
                classifier_units = (max(0, input_tokens - cached) * rate_in
                                    + cached * rate_cached + output * rate_out) / 1_000_000
            else:
                classifier_units = (AVERAGE_MESSAGE_CREDITS[classifier["model"]]
                                    * EFFORT_REASONING_RATIO.get(classifier["effort"], 1.0))
        if isinstance(usage, dict):
            values = [usage.get(k) for k in ("inputTokens", "cachedInputTokens", "outputTokens",
                                               "reasoningOutputTokens")]
            if all(type(x) is int and x >= 0 for x in values):
                input_tokens, cached, output, reasoning = values
                write = usage.get("cacheWriteInputTokens", 0)
                write = write if type(write) is int and write > 0 else 0
                uncached = max(0, input_tokens - cached - write)
                rate_in, rate_cached, rate_out = CREDIT_RATES.get(model, CREDIT_RATES["gpt-6-astra"])
                actual = ((uncached * rate_in + cached * rate_cached + write * rate_in * 1.25
                           + output * rate_out) / 1_000_000 + classifier_units)
                ratio = EFFORT_REASONING_RATIO.get(effort, 1.0)
                baseline_output = max(0, output - reasoning) + reasoning / ratio
                astra_in, astra_cached, astra_out = CREDIT_RATES["gpt-6-astra"]
                baseline = (uncached * astra_in + cached * astra_cached + write * astra_in * 1.25
                            + baseline_output * astra_out) / 1_000_000
                if baseline > 0:
                    return actual, baseline
        ratio = EFFORT_REASONING_RATIO.get(effort, 1.0)
        return AVERAGE_MESSAGE_CREDITS.get(model, 16.0) * ratio + classifier_units, 16.0

    def _footer_messages(self, message, thread_id, turn_id, item):
        turn = self.turns.get(turn_id)
        if not turn or turn.get("footer_done"):
            return None
        state = self._thread(thread_id)
        usage = turn.get("usage")
        actual_units, baseline_units = self._estimate_units(turn)
        stats = state["stats"]
        label = MODEL_LABELS.get(turn["model"], turn["model"])
        stats["turns"] += 1
        if label in stats["models"]:
            stats["models"][label] += 1
        stats["actual_units"] += actual_units
        stats["baseline_units"] += baseline_units
        saved = 100 * (1 - stats["actual_units"] / stats["baseline_units"])
        current = f'{turn["tier"]}: {label} / {turn["effort"].capitalize()}'
        previous = state["last_footer"] or "없음"
        counts = stats["models"]
        classifier = turn.get("classifier")
        classifier_label = (f'{MODEL_LABELS.get(classifier["model"], classifier["model"])} / '
                            f'{classifier["effort"].capitalize()}' if classifier else "로컬/수동")
        footer = (f'Router · 판별: {classifier_label} · 이번 턴: {current} · 직전 턴: {previous}\n'
                  f'세션 {stats["turns"]}턴 · Luna {counts["Luna"]} / Terra {counts["Terra"]} / '
                  f'Sol {counts["Sol"]} / '
                  f'Astra {counts["Astra"]} · 사용량 절감 추정 {saved:.0f}%')
        usage = usage if isinstance(usage, dict) else {}
        self.audit("footer", thread_id, tier=turn["tier"] if turn["tier"] in TIERS else None,
                   model=turn["model"], effort=turn["effort"], turns=stats["turns"],
                   luna=counts["Luna"], terra=counts["Terra"], sol=counts["Sol"], astra=counts["Astra"],
                   input_tokens=usage.get("inputTokens"), cached_tokens=usage.get("cachedInputTokens"),
                   output_tokens=usage.get("outputTokens"), reasoning_tokens=usage.get("reasoningOutputTokens"),
                   total_tokens=usage.get("totalTokens"), saved_percent=round(saved),
                   usage_source=turn.get("usage_source", "equal_turn"))
        suffix = "\n\n---\n" + footer
        updated = copy.deepcopy(message)
        updated["params"]["item"]["text"] = item["text"] + suffix
        delta_method = "item/plan/delta" if item["type"] == "plan" else "item/agentMessage/delta"
        delta = {"method": delta_method, "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": item["id"], "delta": suffix}}
        turn["footer_done"] = True
        state["last_footer"] = current
        return [delta, updated]

    def on_server(self, message):
        if not isinstance(message, dict):
            return
        with self.lock:
            method, params = message.get("method"), message.get("params")
            key = rpc_id(message.get("id"))
            if method is None and key in self.pending:
                request = self.pending.pop(key)
                thread_id = request["thread"]
                if "error" in message:
                    code = (message.get("error") or {}).get("code")
                    self.audit("rpc_error", thread_id, code=code)
                    return
                result = message.get("result") or {}
                if not isinstance(result, dict):
                    return
                if request["method"] == "account/read":
                    self.auth = (result.get("account") or {}).get("type")
                    self.audit("auth", auth=self.auth if self.auth in ("chatgpt", "chatgptAuthTokens", "apiKey") else "other")
                elif request["method"] == "model/list":
                    for row in result.get("data", []):
                        if isinstance(row, dict) and isinstance(row.get("model"), str):
                            efforts = {x.get("reasoningEffort") for x in row.get("supportedReasoningEfforts", []) if isinstance(x, dict)}
                            self.catalog[row["model"]] = {"efforts": efforts}
                elif request["method"] in ("thread/start", "thread/resume", "thread/fork"):
                    thread = result.get("thread") or {}
                    self._observe_settings(thread.get("id"), {**result, "effort": result.get("reasoningEffort")})
                elif request["method"] == "turn/start" and isinstance(thread_id, str):
                    state = self._thread(thread_id)
                    if "route" in request:
                        state["previous"] = request["route"]
                        state["controls"].update(request["new_controls"])
                    if "tier" in request:
                        turn_id = (result.get("turn") or {}).get("id")
                        if isinstance(turn_id, str):
                            if len(self.turns) >= 1024:
                                self.turns.popitem(last=False)
                            self.turns[turn_id] = {
                                "thread": thread_id, "tier": request["tier"], "model": request["model"],
                                "effort": request["effort"], "usage_start": copy.deepcopy(state["usage_total"]),
                                "restore": copy.deepcopy(request.get("restore")),
                                "classifier": request.get("classifier"),
                            }
                            state["active_turn"] = turn_id
                    state["stream"] = 0
                return
            if not isinstance(params, dict):
                return
            thread_id = params.get("threadId")
            if method == "account/updated":
                self.auth = params.get("authMode")
            elif method == "thread/settings/updated":
                self._observe_settings(thread_id, params.get("threadSettings"))
            elif method == "turn/completed":
                state = self._thread(thread_id) if isinstance(thread_id, str) else {}
                turn_data = params.get("turn") or {}
                turn_id = turn_data.get("id")
                self.audit("turn_completed", thread_id, status=turn_data.get("status"))
                self.audit("stream", thread_id, count=state.get("stream", 0))
                turn = self.turns.get(turn_id)
                footer_rows = None
                if turn and turn.get("pending_final"):
                    final_message = turn.pop("pending_final")
                    final_item = final_message["params"]["item"]
                    footer_rows = self._footer_messages(final_message, thread_id, turn_id, final_item)
                if state.get("active_turn") == turn_id:
                    state["active_turn"] = None
                restore = turn.get("restore") if turn else None
                if self.settings_updater and isinstance(restore, dict):
                    threading.Thread(target=self._restore_idle, args=(restore,),
                                     name="codex-router-idle-restore", daemon=True).start()
                if footer_rows:
                    return [*footer_rows, message]
            elif method == "item/agentMessage/delta" and isinstance(thread_id, str):
                self._thread(thread_id)["stream"] += 1
            elif method == "thread/tokenUsage/updated" and isinstance(thread_id, str):
                state = self._thread(thread_id)
                token_usage = params.get("tokenUsage") or {}
                total = token_usage.get("total")
                turn = self.turns.get(params.get("turnId"))
                if turn:
                    delta = self._usage_delta(total, turn.get("usage_start"))
                    turn["usage"] = delta or token_usage.get("last")
                    turn["usage_source"] = "total_delta" if delta else "last"
                if isinstance(total, dict):
                    state["usage_total"] = copy.deepcopy(total)
            elif method == "item/completed" and isinstance(thread_id, str):
                item = params.get("item") or {}
                if ((item.get("type") == "agentMessage" and item.get("phase") == "final_answer")
                        or item.get("type") == "plan"):
                    if (isinstance(item.get("id"), str) and isinstance(item.get("text"), str)):
                        turn = self.turns.get(params.get("turnId"))
                        if turn and not turn.get("footer_done"):
                            turn["pending_final"] = copy.deepcopy(message)
                            return []
                if item.get("type") == "fileChange":
                    self.audit("file_change", thread_id)
            elif method == "model/rerouted":
                turn = self.turns.get(params.get("turnId"))
                if turn and isinstance(params.get("toModel"), str):
                    turn["model"] = params["toModel"]
                self.audit("model_rerouted", thread_id, from_model=params.get("fromModel"), to_model=params.get("toModel"))
            elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"):
                self.audit("approval", thread_id)
