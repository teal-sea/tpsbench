"""Subscription CLIs: one adapter per vendor client, run headless.

A plan's credentials are honoured only by the vendor's own client, so a plan is
measured by spawning that client (`claude -p`, …) with a stripped context and
reading its JSON stream. Everything vendor-specific lives in a `CliBackend`:
how to invoke it, what to strip from its environment, how to read its output,
how to ask whether it is signed in, and the words the terminal uses for it.
The timing loop, cancellation, plan usage, doctor, the `tps <vendor>` shortcut
and the reports are generic (`core.cli_once`, `cli.py`) — adding a client means
adding a backend here, plus offline tests against a recorded stream.

Rules every backend keeps (AGENTS.md):
- never read, store or replay the plan's tokens; never impersonate the client
- no retries by the client (failures are data)
- strip the user's setup (tools, MCP, project files) so the model is timed, not
  the prefill of someone's configuration
- only the first API call of a request is measured
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CliEvent:
    """One line of a client's output, reduced to what the timing loop needs.

    `usage` is Messages-API shaped: `input_tokens` (everything the model read,
    cached or not), `output_tokens`, `output_tokens_details.thinking_tokens`.
    """

    stream: bool = False  # part of the model's response (the first one ends startup)
    piece: str = ""  # answer text
    reasoning: str = ""  # streamed reasoning text ("" when the client hides it)
    usage: dict[str, Any] = field(default_factory=dict)
    finish: str | None = None  # "stop" | "length" | "refusal" | …
    stop: bool = False  # the first response is complete
    model: str | None = None  # the model id that served it, when the client says
    plan_window: str | None = None  # e.g. "five_hour"
    plan_utilization: float | None = None  # 0–1 of that window
    plan_rejected: bool = False  # the plan's limit refused the request
    error: str | None = None  # a final error the client reported
    status: int | None = None  # HTTP status behind that error, when known
    api_call: bool = False  # one model call finished (counted per request)
    final: bool = False  # nothing more worth reading: stop the client now
    send: list[dict[str, Any]] = field(default_factory=list)  # replies to write (JSON lines)


class CliSession:
    """One request's conversation with a client. The default writes the prompt
    to stdin, closes it and reads; clients that speak a protocol (Codex's
    app-server) keep stdin open and answer what they read through `send`."""

    close_stdin = True

    def __init__(self, backend: CliBackend, prompt: str, model: str, effort: str | None) -> None:
        self.backend, self.prompt, self.model, self.effort = backend, prompt, model, effort

    def opening(self) -> list[str]:
        """What to write first: raw text (the prompt) or JSON lines."""
        return [self.prompt]

    def parse(self, obj: dict[str, Any]) -> CliEvent | None:
        return self.backend.parse(obj)


@dataclass
class Auth:
    """What `doctor` and the headers say about a client's sign-in.

    `signed_in` None means unknown (the status could not be read), never "no".
    """

    signed_in: bool | None
    plan: str | None = None  # "Max", "Plus", …
    method: str | None = None
    subscription: bool = True  # False: signed in with an API key, billed per token
    why: str = ""  # why the status is unknown


class CliBackend:
    """A vendor client. Subclasses fill in the class attributes and methods."""

    api: str  # config value: api = "claude-code"
    command: str  # shortcut subcommand: tps claude
    binary: str  # executable on PATH
    vendor: str  # "Claude" in "Claude Max plan"
    efforts: tuple[str, ...]  # levels the client accepts
    default_models: tuple[str, ...]  # what `tps <command>` measures by default
    model_help: str  # one line for --help: which names it understands
    example_model: str  # for error messages
    examples: tuple[tuple[str, str], ...]  # (args, what it does) for --help
    # the client sends continuation requests after a capped response, so the
    # loop must stop it right after the first response
    continues_after_cap: bool = False
    # whether --max-tokens can be enforced; False: answers run their natural length
    caps_output: bool = True

    def session(self, prompt: str, model: str, effort: str | None) -> CliSession:
        return CliSession(self, prompt, model, effort)

    def defaults(self, path: str | None) -> tuple[tuple[str, ...], str]:
        """The models `tps <command>` measures when none are named → (models,
        "") or ((), why they couldn't be read)."""
        return self.default_models, ""

    def prepare(self, path: str, targets: list[Any]) -> None:
        """Once before a real run, untimed: resolve anything a request would
        otherwise look up inside its timing (Codex: each model's default effort)."""

    def argv(self, path: str, model: str, effort: str | None) -> list[str]:
        raise NotImplementedError

    def env(self, max_tokens: int) -> dict[str, str]:
        raise NotImplementedError

    def parse(self, obj: dict[str, Any]) -> CliEvent | None:
        """One JSON line → an event, or None for lines that don't matter."""
        raise NotImplementedError

    def auth(self, path: str) -> Auth:
        raise NotImplementedError


def _run_json(argv: list[str], what: str) -> tuple[dict[str, Any] | None, str]:
    """Run a status command that prints JSON → (object, "") or (None, why not)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None, f"`{what}` timed out after 20s"
    except OSError as exc:
        return None, f"`{what}` failed: {exc.strerror or exc}"
    try:
        status = json.loads(done.stdout)
    except ValueError:
        status = None
    if not isinstance(status, dict):
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        return None, f"`{what}` unreadable (exit {done.returncode}: {tail[0][:80]})"
    return status, ""


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

# Claude Code's default context (tools, MCP servers, skills, settings,
# CLAUDE.md) is ~245k tokens for a configured user; timing that would time the
# prefill of someone's setup, not the model. Strip it to one user message.
CLAUDE_SYSTEM_PROMPT = "You are a helpful assistant."
# credentials a child `claude` would prefer over the signed-in plan (`-p` uses an
# API key whenever one is set); dropped so the plan is what gets measured
CLAUDE_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Claude stop reasons in OpenAI's vocabulary, so one report covers HTTP and CLI
ANTHROPIC_STOP = {
    "max_tokens": "length",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


class ClaudeCode(CliBackend):
    api = "claude-code"
    command = "claude"
    binary = "claude"
    vendor = "Claude"
    efforts = ("low", "medium", "high", "xhigh", "max")
    default_models = ("opus", "sonnet", "haiku")
    model_help = "opus, sonnet, haiku (the latest of each), or full ids"
    example_model = "claude-opus-5-5"
    examples = (
        ("", "opus, sonnet and haiku at default effort"),
        ("opus sonnet -e low,high", "two models × two effort levels"),
        ("opus -e all", "one model, every effort level"),
        ("haiku -n 10", "more reps for a tighter number"),
        ("--dry-run", "what it would send, and how long it takes"),
    )
    # a capped response is followed by silent continuation requests
    continues_after_cap = True
    caps_output = True

    def argv(self, path: str, model: str, effort: str | None) -> list[str]:
        cmd = [
            path,
            "-p",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt",
            CLAUDE_SYSTEM_PROMPT,
        ]
        return cmd + (["--effort", effort] if effort else [])

    def env(self, max_tokens: int) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in CLAUDE_STRIP_ENV}
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
        # failures are data: the client must not retry on our behalf
        env["CLAUDE_CODE_MAX_RETRIES"] = "0"
        env["CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES"] = "0"
        return env

    def parse(self, obj: dict[str, Any]) -> CliEvent | None:
        kind = obj.get("type")
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            window = info.get("rateLimitType")
            level = ((info.get("unifiedWindows") or {}).get(window) or {}).get("utilization")
            return CliEvent(
                plan_window=window if isinstance(level, int | float) else None,
                plan_utilization=float(level) if isinstance(level, int | float) else None,
                plan_rejected=info.get("status") == "rejected",
            )
        if kind == "result":
            if not obj.get("is_error"):
                return None
            status = obj.get("api_error_status")
            return CliEvent(
                error=str(obj.get("result") or obj.get("subtype") or "error"),
                status=status if isinstance(status, int) else None,
            )
        event = obj.get("event")
        if kind != "stream_event" or not isinstance(event, dict):
            return None
        out = CliEvent(stream=True)
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            if isinstance(message.get("model"), str):
                out.model = message["model"]
            # output_tokens here is a placeholder (≈1); only message_delta's is final
            usage = {k: v for k, v in (message.get("usage") or {}).items() if k != "output_tokens"}
            out.usage = _claude_usage(usage)
        elif etype == "message_delta":
            out.usage = _claude_usage(event.get("usage") or {})
            stop = (event.get("delta") or {}).get("stop_reason")
            out.finish = ANTHROPIC_STOP.get(stop, stop)
        elif etype == "message_stop":
            out.stop = out.api_call = True
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                out.piece = delta.get("text") or ""
            elif delta.get("type") == "thinking_delta":
                out.reasoning = delta.get("thinking") or ""  # "" when omitted
        return out

    def auth(self, path: str) -> Auth:
        status, why = _run_json([path, "auth", "status", "--json"], "auth status --json")
        if status is None:
            return Auth(signed_in=None, why=why)
        method = status.get("authMethod")
        subscription = method == "claude.ai"
        plan = status.get("subscriptionType") if subscription else None
        return Auth(
            signed_in=bool(status.get("loggedIn")),
            plan=str(plan).capitalize() if plan else None,
            method=method,
            subscription=subscription,
        )


def _claude_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Claude Code sends the prompt with cache control, so most of what the model
    read is reported as cache creation/read: input_tokens counts all of it."""
    out = dict(usage)
    parts = [
        usage.get(k)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ]
    if any(isinstance(v, int) for v in parts):
        out["input_tokens"] = sum(v for v in parts if isinstance(v, int))
    return out


# --------------------------------------------------------------------------
# Codex (ChatGPT plans)
# --------------------------------------------------------------------------

# `codex exec --json` reports only whole messages, so it can't be timed per
# token; `codex app-server` (the protocol Codex's IDE integrations use) streams
# deltas. tps speaks it as itself (clientInfo "tps"), on the user's sign-in.
# Everything the user's setup adds is switched off for tps's process only:
CODEX_OFF = (
    "hooks",  # the user's own scripts would run on every request
    "apps",
    "plugins",
    "plugin_discovery_prefetch",
    "remote_plugin",
    "skill_search",
    "skill_mcp_dependency_install",
    "shell_tool",
    "shell_snapshot",
    "image_generation",
    "in_app_browser",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "multi_agent",
    "goals",
    "tool_suggest",
    "view_image",
    "sleep_tool",
)
# API keys a child `codex` would use instead of the signed-in plan
CODEX_STRIP_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")
CODEX_WINDOWS = {300: "five_hour", 10080: "seven_day"}
CODEX_LIMITED = {"usageLimitExceeded", "rateLimitExceeded"}


class Codex(CliBackend):
    api = "codex"
    command = "codex"
    binary = "codex"
    vendor = "ChatGPT"
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")
    default_models = ()  # the plan's own default, from `model/list`
    model_help = "model ids from your plan (default: the plan's default model)"
    example_model = "gpt-6.1-sol"
    examples = (
        ("", "your plan's default model at its default effort"),
        ("gpt-6.1-sol gpt-6-astra -e low,high", "two models × two effort levels"),
        ("gpt-6.1-sol -e all", "one model, every effort level"),
        ("--dry-run", "what it would send, and how long it takes"),
    )
    # no output cap exists in Codex: answers run to their natural length
    caps_output = False

    def argv(self, path: str, model: str, effort: str | None) -> list[str]:
        cmd = [path, "app-server", "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0"]
        cmd += ["-c", 'web_search="disabled"']
        for feature in CODEX_OFF:
            cmd += ["--disable", feature]
        return cmd

    def env(self, max_tokens: int) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in CODEX_STRIP_ENV}

    def session(self, prompt: str, model: str, effort: str | None) -> CliSession:
        return CodexSession(self, prompt, model, effort)

    def parse(self, obj: dict[str, Any]) -> CliEvent | None:  # pragma: no cover - via session
        return None

    def defaults(self, path: str | None) -> tuple[tuple[str, ...], str]:
        got, why = codex_query(path or self.binary, ["model/list"])
        listed = ((got or {}).get("model/list") or {}).get("data") or []
        default = [m.get("id") for m in listed if m.get("isDefault") and m.get("id")]
        if default:
            return tuple(default[:1]), ""
        return (), why or "the plan lists no default model"

    def prepare(self, path: str, targets: list[Any]) -> None:
        unresolved = [t for t in targets if not t.effort]
        if not unresolved:
            return
        got, _why = codex_query(path, ["model/list"])
        listed = ((got or {}).get("model/list") or {}).get("data") or []
        for target in unresolved:
            mine = next((m for m in listed if target.model in (m.get("id"), m.get("model"))), {})
            # unknown here → the session asks model/list itself (inside timing)
            target.default_effort = mine.get("defaultReasoningEffort")

    def auth(self, path: str) -> Auth:
        got, why = codex_query(path, ["account/read"])
        if got is None:
            return Auth(signed_in=None, why=why)
        account = (got.get("account/read") or {}).get("account")
        if not account:
            return Auth(signed_in=False)
        chatgpt = account.get("type") == "chatgpt"
        plan = account.get("planType") if chatgpt else None
        return Auth(
            signed_in=True,
            plan=str(plan).capitalize() if plan and plan != "unknown" else None,
            method=account.get("type"),
            subscription=chatgpt,
        )


class CodexSession(CliSession):
    """initialize → thread/start (+ model/list for the model's own default
    effort) → turn/start, then time the turn's streamed answer.

    Codex can make a model call that never shows (no item, no text) before the
    one that streams the answer. Every call is counted (`api_call`); the usage
    kept is the answering call's own (`tokenUsage.last`), so tok/s is that
    call's rate, and TTFT — to the first answer text — includes the hidden one.
    """

    close_stdin = False

    def __init__(self, backend: CliBackend, prompt: str, model: str, effort: str | None) -> None:
        super().__init__(backend, prompt, model, effort)
        self.thread: str | None = None
        self.turn_effort = effort  # None until model/list names the model's default
        self.last_usage: dict[str, Any] | None = None

    def opening(self) -> list[dict[str, Any]]:  # type: ignore[override]
        info = {"name": "tps", "version": _version()}
        return [{"id": 1, "method": "initialize", "params": {"clientInfo": info}}]

    def _turn(self) -> list[dict[str, Any]]:
        if not self.thread or not self.turn_effort:
            return []
        params = {
            "threadId": self.thread,
            "input": [{"type": "text", "text": self.prompt}],
            "effort": self.turn_effort,
        }
        return [{"id": 4, "method": "turn/start", "params": params}]

    def parse(self, obj: dict[str, Any]) -> CliEvent | None:
        rid, method, params = obj.get("id"), obj.get("method"), obj.get("params") or {}
        if rid is not None and method is None:  # a response to one of our requests
            if "error" in obj:
                message = (obj.get("error") or {}).get("message") or "request failed"
                return CliEvent(error=f"{_RID.get(rid, 'request')}: {message}", final=True)
            result = obj.get("result") or {}
            if rid == 1:
                start = {
                    "model": self.model,
                    "baseInstructions": CLAUDE_SYSTEM_PROMPT,
                    "ephemeral": True,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    # the standard tier, whatever the user's own config prefers
                    "serviceTier": "default",
                    # no cwd: the client already runs in tps's empty workdir
                }
                send = [{"method": "initialized"}]
                send.append({"id": 2, "method": "thread/start", "params": start})
                if not self.effort:
                    send.append({"id": 3, "method": "model/list", "params": {}})
                return CliEvent(send=send)
            if rid == 2:
                self.thread = (result.get("thread") or {}).get("id")
                return CliEvent(model=result.get("model"), send=self._turn())
            if rid == 3:
                listed = result.get("data") or []
                mine = next((m for m in listed if self.model in (m.get("id"), m.get("model"))), {})
                # the model's own default, not the user's configured effort
                self.turn_effort = mine.get("defaultReasoningEffort") or "medium"
                return CliEvent(send=self._turn())
            return None
        if method == "turn/started":
            # the client has the request: what came before was client startup.
            # (The reasoning item is only announced once reasoning is done, so it
            # would put the model's thinking inside "startup".)
            return CliEvent(stream=True)
        if method == "item/started":
            kind = (params.get("item") or {}).get("type")
            if kind not in ("userMessage", "reasoning", "agentMessage", None):
                return CliEvent(
                    error=f"the model used a tool ({kind}); not a single answer", final=True
                )
            return None
        if method == "item/agentMessage/delta":
            return CliEvent(stream=True, piece=params.get("delta") or "")
        if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            return CliEvent(stream=True, reasoning=params.get("delta") or "")
        if method == "thread/tokenUsage/updated":
            last = (params.get("tokenUsage") or {}).get("last") or {}
            usage = {
                "input_tokens": last.get("inputTokens"),
                "output_tokens": last.get("outputTokens"),
                "output_tokens_details": {"thinking_tokens": last.get("reasoningOutputTokens")},
            }
            # a call is counted when its usage first appears, not per update
            changed = last != self.last_usage
            self.last_usage = last
            return CliEvent(stream=True, api_call=changed, usage=usage)
        if method == "account/rateLimits/updated":
            limits = params.get("rateLimits") or {}
            window = limits.get("primary") or {}
            used = window.get("usedPercent")
            minutes = window.get("windowDurationMins")
            return CliEvent(
                plan_window=CODEX_WINDOWS.get(minutes, f"{minutes}_min") if minutes else None,
                plan_utilization=used / 100 if isinstance(used, int | float) else None,
                plan_rejected=bool(limits.get("rateLimitReachedType")),
            )
        if method == "error":
            err = params.get("error") or {}
            info = err.get("codexErrorInfo")
            code = info if isinstance(info, str) else next(iter(info or {}), None)
            message = err.get("message") or "error"
            if params.get("willRetry"):
                message += " (the client would retry; tps records the first failure)"
            return CliEvent(
                error=message, status=429 if code in CODEX_LIMITED else None, final=True
            )
        if method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("status") == "completed":
                return CliEvent(stream=True, stop=True, finish="stop", final=True)
            message = (turn.get("error") or {}).get("message") or f"turn {turn.get('status')}"
            return CliEvent(error=message, final=True)
        return None


_RID = {1: "initialize", 2: "thread/start", 3: "model/list", 4: "turn/start"}


def _version() -> str:
    from . import __version__

    return __version__


def codex_query(path: str, methods: list[str]) -> tuple[dict[str, Any] | None, str]:
    """Ask a short-lived `codex app-server` a few read-only questions
    (account/read, model/list) → ({method: result}, "") or (None, why not)."""
    argv = [path, "app-server"]
    for feature in ("hooks", "apps", "plugins"):
        argv += ["--disable", feature]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={k: v for k, v in os.environ.items() if k not in CODEX_STRIP_ENV},
        )
    except OSError as exc:
        return None, f"`codex app-server` failed: {exc.strerror or exc}"
    assert proc.stdin and proc.stdout
    ids = {10 + i: m for i, m in enumerate(methods)}
    got: dict[str, Any] = {}

    def send(obj: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    import threading

    timer = threading.Timer(20, proc.kill)
    timer.start()
    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "tps", "version": _version()}},
            }
        )
        for line in proc.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("id") == 1:
                send({"method": "initialized"})
                for rid, method in ids.items():
                    send({"id": rid, "method": method, "params": {}})
            elif obj.get("id") in ids:
                if "error" in obj:
                    return None, f"`{ids[obj['id']]}` failed: {(obj['error'] or {}).get('message')}"
                got[ids[obj["id"]]] = obj.get("result") or {}
                if len(got) == len(ids):
                    return got, ""
        return None, "`codex app-server` exited without answering (timed out after 20s?)"
    except (OSError, ValueError) as exc:
        return None, f"`codex app-server`: {exc}"
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

BACKENDS: dict[str, CliBackend] = {b.api: b for b in (ClaudeCode(), Codex())}
CLI_APIS = tuple(BACKENDS)


def backend(api: str) -> CliBackend:
    return BACKENDS[api]


def by_command() -> dict[str, CliBackend]:
    """Shortcut subcommand → backend (`tps claude` → ClaudeCode)."""
    return {b.command: b for b in BACKENDS.values()}
