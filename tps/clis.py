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
ANTHROPIC_STOP = {"max_tokens": "length", "end_turn": "stop", "stop_sequence": "stop"}


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
            out.stop = True
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
# registry
# --------------------------------------------------------------------------

BACKENDS: dict[str, CliBackend] = {b.api: b for b in (ClaudeCode(),)}
CLI_APIS = tuple(BACKENDS)


def backend(api: str) -> CliBackend:
    return BACKENDS[api]


def by_command() -> dict[str, CliBackend]:
    """Shortcut subcommand → backend (`tps claude` → ClaudeCode)."""
    return {b.command: b for b in BACKENDS.values()}
