#!/usr/bin/env python3
"""Self-test: exercises the real streaming code path against a local SSE server.

`--mock` bypasses the network layer entirely, so it would not catch a broken
SSE parser or bad TTFT math. This does: it speaks OpenAI-style chat-completions
streaming over real sockets (including the ways real providers misbehave), then
asserts on what was measured — and drives the CLI as a subprocess to hold the
exit-code and JSON contracts in place. No network egress, no API keys.

    python tests/selftest.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import httpx  # noqa: E402

    from tps import core  # noqa: E402
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"selftest: `{exc.name}` is not installed — set up the dev environment first "
        "(CONTRIBUTING.md):\n\n"
        '    python -m venv .venv && . .venv/bin/activate && pip install -e ".[http2,dev]"\n'
    ) from None

TOKENS = 32
FIRST_DELAY = 0.05
TOKEN_DELAY = 0.01
BIG_CHUNK = 32  # tokens per chunk for the "big-chunks" model
BIG_DELAY = 0.02
THINK = 300  # the "thinker" model reasons this long before it answers
HIDDEN = 40  # thinking tokens the Claude-like models bill but never stream

INFLIGHT = {"n": 0}
INFLIGHT_LOCK = threading.Lock()
SEEN: list[tuple[str, int | None, bool]] = []  # (model, max_tokens, sent temperature?)
AUTH: list[str] = []  # how each Claude API request authenticated
BODIES: dict[str, dict] = {}  # the last request body per model
BETA: dict[str, str | None] = {}  # the last anthropic-beta header per model


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):  # silence
        pass

    def do_POST(self):
        with INFLIGHT_LOCK:
            INFLIGHT["n"] += 1
            inflight = INFLIGHT["n"]
        try:
            self._handle(inflight)
        finally:
            with INFLIGHT_LOCK:
                INFLIGHT["n"] -= 1

    def _json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, inflight: int) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "")
        SEEN.append((model, body.get("max_tokens"), "temperature" in body))
        BODIES[model] = body
        BETA[model] = self.headers.get("anthropic-beta")
        if self.path.rstrip("/").endswith("/messages"):
            self._claude(body)
            return

        if model == "echo-key":  # some providers echo the submitted key in errors
            # header key near the start; the bearer key straddles the 600-char cut
            auth = self.headers.get("Authorization")
            message = f"hdr={self.headers.get('x-api-key')} " + "x" * 523 + f" invalid: {auth}"
            self._json(401, {"error": {"message": message}})
            return
        if self.headers.get("Authorization") == "Bearer bad-key":
            self._json(429, {"error": {"message": "rate limited", "code": 429}})
            return
        if model == "rl-conc" and inflight > 1:
            self._json(429, {"error": {"message": "concurrency limit", "code": 429}})
            return

        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if body.get("stream") is False:
            self._non_streaming(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-openrouter-provider", "MockUpstream")
        self.end_headers()

        if model == "single-chunk":
            time.sleep(FIRST_DELAY)
            self._emit({"choices": [{"delta": {"content": "all of it at once " * 10}}]})
            self._emit({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 40}})
            self._done()
            return
        if model in ("reasoning", "reasoning-nousage"):
            time.sleep(FIRST_DELAY)
            for i in range(5):
                self._emit({"choices": [{"delta": {"reasoning": f"thinking step {i} "}}]})
                time.sleep(TOKEN_DELAY)
            if model == "reasoning":
                time.sleep(0.1)
                for i in range(10):
                    self._emit({"choices": [{"delta": {"content": f"answer{i} "}}]})
                    time.sleep(TOKEN_DELAY)
                if want_usage:
                    self._emit(
                        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 15}}
                    )
            self._done()
            return
        if model == "thinker":
            # below a THINK-token cap it never gets to the answer, like a
            # reasoning model at max_tokens 256
            cap = int(body.get("max_tokens") or 0)
            answers = cap > THINK
            time.sleep(FIRST_DELAY)
            for i in range(6):
                self._emit({"choices": [{"delta": {"reasoning": f"step {i} "}}]})
                time.sleep(TOKEN_DELAY)
            for i in range(6 if answers else 0):
                self._emit({"choices": [{"delta": {"content": f"answer{i} "}}]})
                time.sleep(TOKEN_DELAY)
            reason = "stop" if answers else "length"
            self._emit({"choices": [{"delta": {}, "finish_reason": reason}]})
            usage = {
                "prompt_tokens": 11,
                "completion_tokens": THINK + 6 if answers else cap,
                "completion_tokens_details": {"reasoning_tokens": min(THINK, cap)},
            }
            if want_usage:
                self._emit({"choices": [], "usage": usage})
            self._done()
            return
        if model == "bursty":
            # a proxy coalescing the stream: 8 flushes of 4 one-token chunks
            time.sleep(FIRST_DELAY)
            for burst in range(TOKENS // 4):
                for i in range(4):
                    self._emit({"choices": [{"delta": {"content": f"tok{burst * 4 + i} "}}]})
                time.sleep(4 * TOKEN_DELAY)
            if want_usage:
                self._emit(
                    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": TOKENS}}
                )
            self._done()
            return
        if model == "midstream-error":
            for i in range(5):
                self._emit({"choices": [{"delta": {"content": f"tok{i} "}}]})
            self._emit({"error": {"message": "upstream died mid-stream", "code": 502}})
            self._done()
            return
        if model == "big-chunks":
            time.sleep(FIRST_DELAY)
            for _ in range(8):
                self._emit({"choices": [{"delta": {"content": "word " * BIG_CHUNK}}]})
                time.sleep(BIG_DELAY)
            self._emit(
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 8 * BIG_CHUNK}}
            )
            self._done()
            return

        if model in ("filtered", "silent-stop"):
            # billed and finished, but nothing streamed: a filter, or an empty reply
            reason = "content_filter" if model == "filtered" else "stop"
            self._emit({"choices": [{"delta": {}, "finish_reason": reason}]})
            self._emit({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 5}})
            self._done()
            return

        delay = 0.05 if model == "slow" else TOKEN_DELAY
        time.sleep(FIRST_DELAY)
        # role-only chunk: must NOT count as the first token
        self._emit({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        for i in range(TOKENS):
            time.sleep(delay)
            self._emit(
                {
                    "choices": [
                        {"delta": {"content": f"tok{i} "}, "index": 0, "finish_reason": None}
                    ],
                    "provider": "MockUpstream",
                }
            )
        if want_usage:
            usage = {
                "prompt_tokens": 11,
                "completion_tokens": TOKENS,
                "total_tokens": 11 + TOKENS,
            }
            if model == "hidden-reasoner":  # o-series style: billed, never streamed
                usage["completion_tokens"] += HIDDEN
                usage["completion_tokens_details"] = {"reasoning_tokens": HIDDEN}
            self._emit({"choices": [], "usage": usage})
        self._done()

    def _claude(self, body: dict) -> None:
        """The native Claude Messages API, as Claude Opus 5.5 behaves on it:
        thinking is billed in output_tokens_details but streams as empty
        deltas, and any sampling parameter is a 400."""
        AUTH.append("x-api-key" if self.headers.get("x-api-key") else "bearer")
        if "temperature" in body:
            self._json(
                400,
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "temperature is deprecated",
                    },
                },
            )
            return
        model, cap = body.get("model", ""), int(body.get("max_tokens") or 0)
        answers = model != "claude-capped"
        out = HIDDEN + TOKENS if answers else cap
        usage = {
            "input_tokens": 11,
            "output_tokens": out,
            "output_tokens_details": {"thinking_tokens": HIDDEN if answers else cap},
        }
        stop = "end_turn" if answers else "max_tokens"
        if model == "claude-refusal":
            stop = "refusal"
        text = " ".join(f"tok{i}" for i in range(TOKENS))
        if not body.get("stream"):
            content = [{"type": "thinking", "thinking": ""}]
            content += [{"type": "text", "text": text}] if answers else []
            self._json(200, {"content": content, "stop_reason": stop, "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        start = {"usage": {"input_tokens": 11, "output_tokens": 1}}
        self._emit({"type": "message_start", "message": start})
        thinking = {"type": "thinking", "thinking": ""}
        self._emit({"type": "content_block_start", "index": 0, "content_block": thinking})
        time.sleep(FIRST_DELAY)  # the hidden thinking
        delta = {"type": "thinking_delta", "thinking": ""}
        self._emit({"type": "content_block_delta", "index": 0, "delta": delta})
        for i in range(TOKENS if answers else 0):
            time.sleep(TOKEN_DELAY)
            delta = {"type": "text_delta", "text": f"tok{i} "}
            self._emit({"type": "content_block_delta", "index": 1, "delta": delta})
        if model == "claude-cut":
            return  # a proxy closing the stream early: no message_delta, no message_stop
        self._emit({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": usage})
        self._emit({"type": "message_stop"})

    def _emit(self, obj: dict) -> None:
        try:
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise  # client hung up (timeout test); let the handler unwind

    def _done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _non_streaming(self, body: dict) -> None:
        """Deterministic greedy completion, for the fingerprint tests.

        `test-model` and `other-model` agree for 40 tokens then diverge, which
        mimics 'same base, different quantization' well enough to assert on.
        """
        model = body.get("model", "")
        if model == "fp-fail":
            self._json(500, {"error": {"message": "upstream down"}})
            return
        if model == "fp-slow":
            time.sleep(0.4)
        if model == "text-only-model" and body.get("logprobs"):
            self._json(
                400,
                {"error": {"message": "logprobs are not supported for this model", "code": 400}},
            )
            return
        if model == "capped-logprobs" and (body.get("top_logprobs") or 0) > 5:
            self._json(400, {"error": {"message": "top_logprobs must be between 0 and 5"}})
            return
        if model == "reasoner-empty":  # thought through the whole cap, answered nothing
            message = {"role": "assistant", "content": "", "reasoning": "let me think" * 20}
            self._json(
                200,
                {
                    "choices": [{"message": message, "finish_reason": "length"}],
                    "usage": {
                        "prompt_tokens": 78,
                        "completion_tokens": 160,
                        "completion_tokens_details": {"reasoning_tokens": 160},
                    },
                },
            )
            return

        tokens = [f"t{i}" for i in range(120)]
        if model == "other-model":
            tokens = tokens[:40] + [f"x{i}" for i in range(80)]
        content = " ".join(tokens)
        choice: dict = {
            "message": {"role": "assistant", "content": content},
            "finish_reason": "length",
        }
        if body.get("logprobs"):
            k = min(int(body.get("top_logprobs") or 0), 3)
            choice["logprobs"] = {
                "content": [
                    {
                        "token": t,
                        "logprob": -0.01,
                        "top_logprobs": [
                            {"token": t, "logprob": -0.01},
                            *({"token": f"alt{j}", "logprob": -5.0 - j} for j in range(k - 1)),
                        ],
                    }
                    for t in tokens
                ]
            }
        self._json(
            200,
            {
                "choices": [choice],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": len(tokens),
                    "total_tokens": 11 + len(tokens),
                },
            },
        )


class Server(ThreadingHTTPServer):
    """Broken pipes are expected here: the timeout test disconnects on purpose."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve() -> tuple[Server, str]:
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


def tps(
    args: list[str], cwd: Path, timeout: float = 120, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "tps", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# A stand-in for the `claude` CLI: replays Claude Code's stream-json (Messages
# API events wrapped in `stream_event`) by model name, and records what tps
# passed it. No network, no account.
FAKE_CLAUDE = r"""
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                      "email": "someone@example.com"})); sys.exit(0)
model = args[args.index("--model") + 1]
prompt = sys.stdin.read()
with open(os.environ["TPS_FAKE_LOG"], "a") as log:
    log.write(json.dumps({"model": model, "argv": args, "prompt": prompt,
                          "env": {k: os.environ.get(k) for k in (
                              "CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")},
                          "cwd": os.getcwd()}) + "\n")
def out(obj):
    print(json.dumps(obj), flush=True)
def ev(event):
    out({"type": "stream_event", "event": event})
out({"type": "system", "subtype": "init", "tools": []})
if model == "cc-crash":
    print("boom: something broke", file=sys.stderr); sys.exit(3)
if model == "cc-ratelimit":
    out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
         "result": "API Error: Request rejected (429)"}); sys.exit(1)
time.sleep(0.05)  # client startup
usage = {"input_tokens": 3, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0}
def message(n, stop, think=5):
    ev({"type": "message_start", "message": {"usage": {**usage, "output_tokens": 1}}})
    time.sleep(0.05)  # hidden thinking, never streamed
    ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    for _ in range(n):
        ev({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "word"}})
        time.sleep(0.01)
    ev({"type": "message_delta", "delta": {"stop_reason": stop},
        "usage": {**usage, "output_tokens": n + think,
                  "output_tokens_details": {"thinking_tokens": think}}})
    ev({"type": "message_stop"})
if model == "cc-capped":
    message(20, "max_tokens")
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
         "rateLimitType": "five_hour",
         "unifiedWindows": {"five_hour": {"utilization": 0.5}}}})
    time.sleep(1.0)
    # Claude Code would now send a continuation request; tps must be gone by then
    open(os.environ["TPS_FAKE_LOG"] + ".continued", "w").close()
    message(20, "max_tokens")
    out({"type": "result", "is_error": True, "result": "exceeded the output token maximum"})
    sys.exit(1)
message(20, "end_turn")
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
     "rateLimitType": "five_hour",
     "unifiedWindows": {"five_hour": {"utilization": 0.25, "status": "allowed"}}}})
out({"type": "result", "subtype": "success", "is_error": False,
     "usage": {**usage, "output_tokens": 25, "output_tokens_details": {"thinking_tokens": 5}}})
"""


def target_block(name: str, base: str, model: str, extra: str = "") -> str:
    return f'[[targets]]\nname = "{name}"\nbase_url = "{base}"\nmodel = "{model}"\n{extra}\n'


async def main() -> int:
    server, base = serve()
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "ok  " if condition else "FAIL"
        print(f"  [{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(name)

    cfg = core.RunConfig(targets=[], prompts={}, reps=1, concurrency=[1])

    def tgt(model: str, name: str = "local", **kw) -> core.Target:
        return core.Target(
            name=name, base_url=base, model=model, api_key=kw.pop("api_key", "good-key"), **kw
        )

    target = tgt("test-model", price_in=1.0, price_out=2.0)

    try:
        async with httpx.AsyncClient(http2=False, timeout=30.0) as client:

            async def once(
                t: core.Target, batch: int = 0, stream_options: bool = True
            ) -> core.Sample:
                return await core.stream_once(
                    client, t, "hello", "prose-256", 64, cfg, 1, batch, stream_options
                )

            print("streaming (usage reported):")
            sample = await once(target)
            check("ok", sample.ok, str(sample.error))
            check(
                "ttft >= role-chunk delay",
                sample.ttft_s is not None and sample.ttft_s >= FIRST_DELAY * 0.9,
                f"ttft={sample.ttft_s}",
            )
            # Streamed, not buffered: the first token lands while the server
            # still has (TOKENS - 1) × TOKEN_DELAY of sleeping to do. Measured
            # against the same request's end, not a wall-clock budget, so a cold
            # first connection shifts both numbers and can't fail it.
            check(
                "first token arrives long before the stream ends (no buffering)",
                sample.ttft_s is not None
                and sample.e2e_s - sample.ttft_s >= 0.5 * (TOKENS - 1) * TOKEN_DELAY,
                f"ttft={sample.ttft_s} e2e={sample.e2e_s}",
            )
            check("token count from usage", sample.out_tokens == TOKENS, str(sample.out_tokens))
            check("prompt tokens from usage", sample.in_tokens == 11, str(sample.in_tokens))
            check("count source", sample.count_source == "usage", sample.count_source)
            check("provider attribution", sample.served_by == "MockUpstream", str(sample.served_by))
            expected_tps = 1 / TOKEN_DELAY
            check(
                "out tok/s within 20% of truth",
                sample.out_tps is not None
                and abs(sample.out_tps - expected_tps) / expected_tps < 0.2,
                f"{sample.out_tps} vs {expected_tps:.1f}",
            )
            check(
                "itl ~ token delay",
                sample.itl_p50_ms is not None
                and 0.8 * TOKEN_DELAY * 1000 <= sample.itl_p50_ms <= 3 * TOKEN_DELAY * 1000,
                f"{sample.itl_p50_ms}",
            )

            print("streaming (no usage block -> chars/4 fallback):")
            fallback = await once(target, 1, stream_options=False)
            check("ok", fallback.ok, str(fallback.error))
            check("fallback flagged", fallback.count_source == "chars/4", fallback.count_source)
            check(
                "fallback token estimate plausible",
                fallback.out_tokens is not None and fallback.out_tokens > 0,
                str(fallback.out_tokens),
            )
            check(
                "input tokens estimated too (cost needs them)",
                fallback.in_tokens is not None and fallback.in_tokens > 0,
                str(fallback.in_tokens),
            )

            print("429 handling:")
            denied = await once(tgt("m", name="bad", api_key="bad-key"), 2)
            check("not ok", not denied.ok)
            check("status 429", denied.status == 429, str(denied.status))
            check(
                "tagged rate_limited",
                (denied.error or "").startswith("rate_limited"),
                str(denied.error),
            )

            echoed = await once(
                tgt(
                    "echo-key",
                    api_key="sk-selftest-secret-123456",
                    headers={"x-api-key": "sk-header-SECRET-0123456789"},
                )
            )
            check(
                "echoed keys redacted (header keys, and across the truncation cut)",
                "sk-" not in (echoed.error or "") and "[redacted]" in (echoed.error or ""),
                str(echoed.error)[-120:],
            )

            print("timeout handling:")
            async with httpx.AsyncClient(http2=False, timeout=httpx.Timeout(0.02)) as impatient:
                hung = await core.stream_once(
                    impatient, tgt("m", name="slow"), "hello", "prose-256", 64, cfg, 1, 3, True
                )
            check("timeout recorded", hung.error == "timeout", str(hung.error))

            print("misbehaving providers:")
            single = await once(tgt("single-chunk"))
            check("single chunk: ok (tokens arrived)", single.ok, str(single.error))
            check("single chunk: no invented tok/s", single.out_tps is None, str(single.out_tps))
            midstream = await once(tgt("midstream-error"))
            check("in-band stream error is a failure", not midstream.ok, str(midstream.error))
            check(
                "in-band stream error classified",
                (midstream.error or "").startswith("stream_error"),
                str(midstream.error),
            )
            big = await once(tgt("big-chunks"))
            # exact, timing-independent: the window is credited with the 7 chunks
            # that arrived inside it (224 tokens), not all 256
            credited = big.out_tps * big.decode_s if big.out_tps and big.decode_s else None
            check(
                "big chunks: first chunk not credited to the window",
                credited is not None and abs(credited - 7 * BIG_CHUNK) < 1,
                f"credited {credited} tokens, expected {7 * BIG_CHUNK}",
            )
            reasoning = await once(tgt("reasoning"))
            check("reasoning: ok", reasoning.ok, str(reasoning.error))
            check(
                "reasoning: first answer token recorded after ttft",
                reasoning.first_content_s is not None
                and reasoning.ttft_s is not None
                and reasoning.first_content_s - reasoning.ttft_s > 0.08,
                f"ttft={reasoning.ttft_s} first_content={reasoning.first_content_s}",
            )
            thinking = await once(tgt("reasoning-nousage"))
            check(
                "reasoning-only stream: chars/4 counts reasoning text",
                thinking.out_tokens is not None
                and thinking.out_tokens > 5
                and thinking.count_source == "chars/4",
                f"{thinking.out_tokens} {thinking.count_source}",
            )
            capped = await once(tgt("thinker"))
            check(
                "token cap while reasoning: finish_reason and reasoning tokens recorded",
                capped.ok
                and capped.finish_reason == "length"
                and capped.reasoning_tokens == 64
                and capped.first_content_s is None
                and capped.itl_p50_ms is None,
                f"{capped.finish_reason} {capped.reasoning_tokens} {capped.first_content_s}",
            )

            def note_for(*samples: core.Sample) -> str:
                stats = core.summarize([s.as_dict() for s in samples])
                return (
                    core.reasoning_note(
                        core.bucket_stats(stats["buckets"][next(iter(stats["buckets"]))])
                    )
                    or ""
                )

            check(
                "all-reasoning row says it measured thinking, not answering",
                "measured thinking" in note_for(capped) and "--max-tokens" in note_for(capped),
                note_for(capped),
            )
            check(
                "mixed row counts the reasoning-only responses",
                note_for(capped, reasoning).startswith("1/2 responses were reasoning only"),
                note_for(capped, reasoning),
            )
            check("answered row has no note", note_for(reasoning) == "", note_for(reasoning))

            print("chunk delivery (ITL honesty):")
            burst = await once(tgt("bursty"))
            check(
                "bursty delivery detected (median gap far below the mean)",
                core.is_bursty(burst.as_dict()) and burst.tokens_per_chunk == 1.0,
                f"p50={burst.itl_p50_ms} mean={burst.itl_mean_ms} tpc={burst.tokens_per_chunk}",
            )
            check(
                "a steady stream is not flagged",
                not core.is_bursty(sample.as_dict()),
                f"p50={sample.itl_p50_ms} mean={sample.itl_mean_ms}",
            )
            stall = [0.02] * 30 + [5.0]  # steady 20 ms, one 5 s stall (review #10)
            stalled = {"itl_p50_ms": 20.0, "itl_pace_ms": core.itl_pace(stall) * 1000}
            check(
                "one long stall in a steady stream is not called a burst",
                not core.is_bursty(stalled),
                str(stalled),
            )
            check(
                "tokens per chunk counts what each chunk carried",
                big.tokens_per_chunk == BIG_CHUNK,
                str(big.tokens_per_chunk),
            )
            burst_stats = core.bucket_stats(
                next(iter(core.summarize([burst.as_dict()])["buckets"].values()))
            )
            check(
                "a bursty row says its ITL measures flushing, not the model",
                any("arrived in bursts" in n for n in core.row_notes(burst_stats))
                and burst_stats["bursty"] == 1
                and burst_stats["itl_n"] == 1,
                str(core.row_notes(burst_stats)),
            )

            print("native Claude API (hidden thinking, no sampling params):")
            claude_key = "sk-ant-api03-selftest-0123456789"
            claude = await once(tgt("claude-sim", api="anthropic", api_key=claude_key))
            check("claude: ok, answer streamed", claude.ok and claude.first_content_s is not None)
            check(
                "claude: no temperature sent by default (it's a 400 there)",
                SEEN[-1] == ("claude-sim", 64, False),
                str(SEEN[-1]),
            )
            check(
                "claude: API key goes in x-api-key, stop_reason mapped",
                AUTH[-1] == "x-api-key" and claude.finish_reason == "stop",
                f"{AUTH[-1]} {claude.finish_reason}",
            )
            check(
                "claude: billed thinking tokens recorded as hidden",
                claude.out_tokens == HIDDEN + TOKENS
                and claude.reasoning_tokens == HIDDEN
                and claude.hidden_reasoning_tokens == HIDDEN,
                f"{claude.out_tokens} {claude.reasoning_tokens} {claude.hidden_reasoning_tokens}",
            )
            check(
                "claude: tok/s counts only streamed tokens (hidden thinking not credited)",
                claude.out_tps is not None
                and abs(claude.out_tps - expected_tps) / expected_tps < 0.2,
                f"{claude.out_tps} vs {expected_tps:.1f}",
            )
            await once(tgt("claude-sim", api="anthropic", api_key="sk-ant-oat01-subscription"))
            check("claude: an OAuth/subscription token is sent as a bearer", AUTH[-1] == "bearer")
            silent = await once(tgt("claude-capped", api="anthropic", api_key=claude_key))
            check(
                "claude: thinking that ate the whole cap is a result, not an empty stream",
                silent.ok
                and silent.error is None
                and silent.finish_reason == "length"
                and silent.ttft_s is None
                and silent.out_tps is None,
                f"{silent.ok} {silent.error} {silent.finish_reason}",
            )
            check(
                "claude: and its note says the model never answered",
                "hit the token cap while reasoning" in note_for(silent),
                note_for(silent),
            )
            hot = core.RunConfig(targets=[], prompts={}, reps=1, concurrency=[1], temperature=0.5)
            sent = await core.stream_once(
                client,
                tgt("claude-sim", api="anthropic", api_key=claude_key),
                "hello",
                "prose-256",
                64,
                hot,
                1,
                0,
                True,
            )
            check(
                "claude: an explicit temperature is still sent (and its 400 reported)",
                not sent.ok and (sent.error or "").startswith("http_400"),
                str(sent.error),
            )
            o_series = await once(tgt("hidden-reasoner"))
            check(
                "OpenAI-format hidden reasoning: not credited to tok/s either",
                o_series.hidden_reasoning_tokens == HIDDEN
                and o_series.out_tps is not None
                and abs(o_series.out_tps - expected_tps) / expected_tps < 0.2,
                f"{o_series.hidden_reasoning_tokens} {o_series.out_tps}",
            )
            stats = core.summarize([claude.as_dict(), claude.as_dict()])
            claude_notes = core.row_notes(core.bucket_stats(next(iter(stats["buckets"].values()))))
            check(
                "claude: the row says TTFT includes hidden thinking",
                any(f"~{HIDDEN} thinking tokens" in n for n in claude_notes),
                str(claude_notes),
            )

            print("responses that are not answers (review fixes):")
            for model, api, cls in (
                ("filtered", "openai", "content_filter"),
                ("claude-refusal", "anthropic", "refusal"),
                ("silent-stop", "openai", "no_content"),
            ):
                declined = await once(tgt(model, api=api, api_key=claude_key))
                check(
                    f"{model}: not ok, classified {cls} (never 'reasoning only')",
                    not declined.ok and core.error_class(declined.as_dict()) == cls,
                    f"{declined.ok} {declined.error}",
                )
            cut = await once(tgt("claude-cut", api="anthropic", api_key=claude_key))
            check(
                "claude: a stream cut before message_stop fails, keeping no placeholder count",
                not cut.ok and (cut.error or "").startswith("incomplete_stream"),
                f"{cut.ok} {cut.error} {cut.out_tokens}",
            )
            await once(tgt("claude-sim", api="anthropic", api_key="sk-ant-oat01-subscription"))
            oauth_beta = BETA.get("claude-sim")
            await once(tgt("claude-sim", api="anthropic", api_key="sk-ant-si-session"))
            check(
                "claude: an OAuth token carries the oauth beta header; other tokens don't",
                oauth_beta == core.ANTHROPIC_OAUTH_BETA and BETA.get("claude-sim") is None,
                f"{oauth_beta} / {BETA.get('claude-sim')}",
            )
            vllm = {"stream_options": {"include_usage": True, "continuous_usage_stats": True}}
            await once(tgt("test-model", extra_body=vllm))
            check(
                "extra_body still wins over the stream_options tps adds",
                BODIES["test-model"].get("stream_options") == vllm["stream_options"],
                str(BODIES["test-model"].get("stream_options")),
            )

        print("report pipeline:")
        run_dir = Path(tempfile.mkdtemp(prefix="tps-selftest-"))
        rows = [sample.as_dict(), fallback.as_dict(), denied.as_dict()]
        for row in rows:
            row["batch_wall_s"] = row.get("e2e_s")
        (run_dir / "raw.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "created": "selftest",
                    "mock": False,
                    "reps": 2,
                    "concurrency": [1],
                    "temperature": 0.0,
                    "targets": [
                        {
                            "name": "local",
                            "model": "test-model",
                            "base_url": base,
                            "pin": None,
                            "price_in": 1.0,
                            "price_out": 2.0,
                        }
                    ],
                    "prompts": ["prose-256"],
                    "http2": False,
                }
            ),
            encoding="utf-8",
        )
        samples, meta = core.load_samples(run_dir)
        report = core.write_report(run_dir, samples, meta)
        check("report has table row", "| local |" in report)
        check("report shows error count", "rate_limited" in report)
        check("report flags estimated counts", "≈" in report)
        check(
            "p90 tok/s is the slow tail", core.p90([10, 20, 30, 40, 50], higher_is_better=True) < 20
        )

        # unknown spend must say *why*: an unpriced success, not "no prices"
        unknown_dir = Path(tempfile.mkdtemp(prefix="tps-nocost-"))
        unknown_samples = [
            {
                "target": "down",
                "prompt": "p",
                "concurrency": 1,
                "batch": 0,
                "ok": False,
                "error": "rate_limited: x",
            },
            {
                "target": "free",
                "prompt": "p",
                "concurrency": 1,
                "batch": 0,
                "ok": True,
                "in_tokens": 10,
                "out_tokens": 10,
                "count_source": "usage",
            },
        ]
        unknown_meta = {
            "created": "selftest",
            "reps": 1,
            "concurrency": [1],
            "temperature": 0.0,
            "http2": False,
            "prompts": ["p"],
            "targets": [
                {"name": "down", "model": "m", "price_in": 1.0, "price_out": 2.0},
                {"name": "free", "model": "m"},
            ],
        }
        (unknown_dir / "raw.jsonl").write_text(
            "\n".join(json.dumps(s) for s in unknown_samples) + "\n", encoding="utf-8"
        )
        (unknown_dir / "meta.json").write_text(json.dumps(unknown_meta), encoding="utf-8")
        unknown_report = core.write_report(unknown_dir, unknown_samples, unknown_meta)
        unknown_line = next(
            (
                line
                for line in unknown_report.splitlines()
                if line.startswith("Run spend is unknown")
            ),
            "",
        )
        check(
            "unknown spend names the unpriced target and the real reason",
            "no successful request" in unknown_line and "free" in unknown_line,
            unknown_line,
        )
        priced = {"targets": [{"name": "local", "price_in": 1.0, "price_out": 2.0}]}
        bill = core.spend(
            [
                {
                    "target": "local",
                    "prompt": "p",
                    "concurrency": 1,
                    "batch": 0,
                    "ok": True,
                    "in_tokens": 1000,
                    "out_tokens": 1000,
                },
                {  # warm-up: measured tokens, but not part of the run's spend
                    "target": "local",
                    "prompt": "p",
                    "concurrency": 1,
                    "batch": -1,
                    "ok": True,
                    "in_tokens": 1000,
                    "out_tokens": 1000,
                },
                {  # failed: no tokens to bill
                    "target": "local",
                    "prompt": "p",
                    "concurrency": 4,
                    "batch": 0,
                    "ok": False,
                    "error": "timeout",
                },
            ],
            priced,
        )
        check(
            "spend counts measured successful requests only",
            abs((bill["total"] or 0) - 0.003) < 1e-12,
            str(bill["total"]),
        )
        down_only = core.spend(
            [
                {
                    "target": "down",
                    "prompt": "p",
                    "concurrency": 1,
                    "batch": 0,
                    "ok": False,
                    "error": "rate_limited: x",
                }
            ],
            {"targets": [{"name": "down", "price_in": 1.0, "price_out": 2.0}]},
        )
        check(
            "a priced target that never succeeded is unknown, not zero",
            down_only["total"] is None and down_only["unpriced"] == [],
            str(down_only),
        )
        unpriced = core.spend(
            [
                {
                    "target": "local",
                    "prompt": "p",
                    "concurrency": 1,
                    "batch": 0,
                    "ok": True,
                    "in_tokens": 10,
                    "out_tokens": 10,
                }
            ],
            {"targets": [{"name": "local"}]},
        )
        check(
            "a missing price means unknown spend, never zero",
            unpriced["total"] is None and unpriced["unpriced"] == ["local"],
            str(unpriced),
        )

        print("fingerprint (same weights vs different artifact):")
        fp_cfg = core.RunConfig(
            targets=[], prompts={"prose-256": dict(core.DEFAULT_PROMPTS["prose-256"])}
        )
        alpha = tgt("test-model", name="alpha", api_key="k")
        beta = tgt("other-model", name="beta", api_key="k")
        gamma = tgt("text-only-model", name="gamma", api_key="k")
        down = tgt("fp-fail", name="down", api_key="k")
        async with httpx.AsyncClient(http2=False, timeout=30.0) as fp_client:
            prints = []
            for t in (alpha, alpha, beta, beta, gamma, gamma):
                prints.append(
                    await core.fingerprint_once(fp_client, t, "probe", "prose-256", 120, fp_cfg, 0)
                )
            failed_prints = [
                await core.fingerprint_once(fp_client, down, "probe", "prose-256", 120, fp_cfg, rep)
                for rep in (0, 1)
            ]
        a0, a1, b0, b1, g0, g1 = prints
        check("streamed tokens parsed", a0.ok and a0.tokens[:3] == ["t0", "t1", "t2"])
        check("self-agreement is exact", a0.tokens == a1.tokens)
        frac_cross, first_cross = core.token_agreement(a0.tokens, b0.tokens, "tokens", "tokens")
        check("cross agreement 40/120", abs(frac_cross - 40 / 120) < 1e-9, str(frac_cross))
        check("first divergence at 40", first_cross == 40, str(first_cross))
        frac_short, first_short = core.token_agreement(
            a0.tokens[:10], a0.tokens, "tokens", "tokens"
        )
        check(
            "early stop is not a perfect match",
            frac_short < 0.1 and first_short == 10,
            f"{frac_short} {first_short}",
        )
        check(
            "logprobs-unsupported falls back to text mode",
            g0.ok and g0.mode == "text",
            f"{g0.mode} {g0.error}",
        )
        check("text mode self-agreement", core.compare_prints(g0, g1)[0] == 1.0)

        fp_report = core.fingerprint_reports(prints, ["alpha", "beta", "gamma"], ["prose-256"])
        check("two weight clusters", fp_report.count("**group") == 2, fp_report[:200])
        check(
            "alpha+gamma cluster together",
            "alpha, gamma" in fp_report or "gamma, alpha" in fp_report,
        )
        check("no reps -> noise floor reported as not measured", "Not measured" in fp_report)

        # a target that errored is absence of evidence, never "different weights"
        same = [
            core.Fingerprint("a", "p", rep, True, tokens=a0.tokens, text=a0.text, prompt_tokens=11)
            for rep in (0, 1)
        ] + [
            core.Fingerprint("b", "p", rep, True, tokens=a0.tokens, text=a0.text, prompt_tokens=11)
            for rep in (0, 1)
        ]
        for f in failed_prints:
            f.target, f.prompt = "down", "p"
        summary = core.fingerprint_summary(same + failed_prints, ["a", "b", "down"], ["p"])
        check(
            "failed target not clustered",
            summary["clusters"] == [["a", "b"]],
            str(summary["clusters"]),
        )
        check(
            "failed target listed as no data", "down" in summary["no_data"], str(summary["no_data"])
        )
        check(
            "verdict consistent despite failure",
            summary["verdict"] == "consistent",
            summary["verdict"],
        )
        none = core.fingerprint_summary(failed_prints, ["down"], ["p"])
        check("all failed -> inconclusive", none["verdict"] == "inconclusive", none["verdict"])
        one_rep = core.fingerprint_summary(
            [
                core.Fingerprint("a", "p", 0, True, tokens=a0.tokens),
                core.Fingerprint("b", "p", 0, True, tokens=b0.tokens),
            ],
            ["a", "b"],
            ["p"],
        )
        check(
            "no noise floor (--reps 1) -> inconclusive, never split",
            one_rep["verdict"] == "inconclusive" and not one_rep["noise_floor_measured"],
            one_rep["verdict"],
        )
        second_failed = same + [
            core.Fingerprint("c", "p", 0, True, tokens=b0.tokens, text=b0.text),
            core.Fingerprint("c", "p", 1, False, error="rate_limited: 429"),
        ]
        partial = core.fingerprint_summary(second_failed, ["a", "b", "c"], ["p"])
        check(
            "target without a floor is unmeasured and can't force a split",
            partial["verdict"] == "consistent" and partial["unmeasured"] == ["c"],
            f"{partial['verdict']} {partial['unmeasured']} {partial['clusters']}",
        )
        # x and y differ at 2 of 100 tokens (98%: split at a perfect floor); u
        # is 99% from each, so in one union-find it would bridge them
        ref = [f"t{i}" for i in range(100)]
        x_toks, y_toks, u_toks = ref, list(ref), list(ref)
        y_toks[10], y_toks[20], u_toks[10] = "y10", "y20", "y10"
        firm = [
            core.Fingerprint(name, "p", rep, True, tokens=toks)
            for name, toks in (("x", x_toks), ("y", y_toks))
            for rep in (0, 1)
        ]
        u0 = core.Fingerprint("u", "p", 0, True, tokens=u_toks)
        bridged = {
            "unmeasured": [u0, core.Fingerprint("u", "p", 1, False, error="rate_limited: 429")],
            "noisy": [u0, core.Fingerprint("u", "p", 1, True, tokens=u_toks[:50] + ref[:50])],
        }
        for kind, u_prints in bridged.items():
            bridge = core.fingerprint_summary(firm + u_prints, ["x", "y", "u"], ["p"])
            check(
                f"{kind} target can't bridge two groups into consistent",
                bridge["verdict"] == "split"
                and bridge["distinguishable_groups"] == 2
                and bridge["clusters"] == [["x"], ["y"], ["u"]]
                and bridge[kind] == ["u"],
                f"{bridge['verdict']} {bridge['clusters']}",
            )
        joined = core.fingerprint_summary(
            same + [core.Fingerprint("u", "p", 0, True, tokens=a0.tokens)],
            ["a", "b", "u"],
            ["p"],
        )
        check(
            "unmeasured target joins the one group it matches",
            joined["verdict"] == "consistent" and joined["clusters"] == [["a", "b", "u"]],
            f"{joined['verdict']} {joined['clusters']}",
        )

        print("fingerprint v2 groundwork:")
        omit_cfg = core.RunConfig(targets=[], prompts={}, temperature="omit")
        async with httpx.AsyncClient(http2=False, timeout=30.0) as fp_client:
            full = await core.fingerprint_once(
                fp_client, alpha, "probe", "prose-256", 120, fp_cfg, 0
            )
            full_body = dict(BODIES["test-model"])
            omitted = await core.fingerprint_once(
                fp_client, tgt("test-model", name="o"), "probe", "prose-256", 120, omit_cfg, 0
            )
            omit_body = dict(BODIES["test-model"])
            capped_lp = await core.fingerprint_once(
                fp_client, tgt("capped-logprobs", name="c"), "probe", "prose-256", 120, fp_cfg, 0
            )
            capped_body = dict(BODIES["capped-logprobs"])
            spent = await core.fingerprint_once(
                fp_client, tgt("reasoner-empty", name="r"), "probe", "prose-256", 120, fp_cfg, 0
            )
        check(
            "fingerprint asks for 20 top logprobs and stores the alternatives",
            full_body.get("top_logprobs") == core.TOP_LOGPROBS
            and full_body.get("temperature") == 0.0
            and full.top_logprobs
            and set(full.top_logprobs[0]) == {"t0", "alt0", "alt1"},
            str(full.top_logprobs[:1]),
        )
        check(
            'fingerprint honours temperature = "omit" (review #8)',
            "temperature" not in omit_body and omitted.ok,
            str(sorted(omit_body)),
        )
        check(
            "a provider that caps top_logprobs at 5 gets 5, and stays in token mode",
            capped_lp.ok and capped_lp.mode == "tokens" and capped_body.get("top_logprobs") == 5,
            f"{capped_lp.mode} {capped_body.get('top_logprobs')} {capped_lp.error}",
        )
        check(
            "reasoning that ate the budget is named, with the fix",
            core.error_class(spent.__dict__) == "reasoning_consumed_budget"
            and "--max-tokens" in (spent.error or "")
            and spent.reasoning_tokens == 160,
            str(spent.error),
        )
        spent_summary = core.fingerprint_summary([spent], ["r"], ["prose-256"])
        spent_report = core.fingerprint_reports([spent], ["r"], ["prose-256"])
        check(
            "prompt_tokens signature survives a failed completion (v1 said 'none reported')",
            spent_summary["prompt_tokens"] == {"r": 78}
            and "No provider reported" not in spent_report
            and spent_summary["reasoning_seen"] == ["r"],
            str(spent_summary["prompt_tokens"]),
        )

        sent: list[dict] = []

        def openrouter(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        router = core.Target(
            name="or", base_url="https://openrouter.ai/api/v1", model="m", api_key="k"
        )
        router_on = core.Target(
            name="or-on",
            base_url="https://openrouter.ai/api/v1",
            model="m",
            api_key="k",
            extra_body={"reasoning": {"enabled": True}},
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(openrouter)) as mocked:
            for t in (router, router_on, alpha):
                await core.fingerprint_once(mocked, t, "probe", "prose-256", 120, fp_cfg, 0)
        check(
            "fingerprint turns reasoning off on OpenRouter; extra_body can turn it back on",
            sent[0].get("reasoning") == {"enabled": False}
            and sent[1].get("reasoning") == {"enabled": True}
            and "reasoning" not in sent[2],
            str([s.get("reasoning") for s in sent]),
        )

        print("discover (offline fixture):")
        fixture = ROOT / "tests" / "fixtures" / "openrouter-endpoints.sample.json"
        out = run_dir / "targets.toml"
        core.discover("deepseek/deepseek-v3.2", out, None, fixture, None)
        loaded = core.load_config(out)
        check("generated toml parses", len(loaded.targets) > 0, str(len(loaded.targets)))
        check(
            "every target is pinned",
            all(t.extra_body.get("provider", {}).get("only") for t in loaded.targets),
        )
        tricky = [
            {
                "provider_name": 'Quote "Labs"',
                "tag": "quote",
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
        text, _count, _collapsed = core.targets_toml('vendor/model"x', tricky)
        tricky_path = run_dir / "tricky.toml"
        tricky_path.write_text(text, encoding="utf-8")
        reloaded = core.load_config(tricky_path).targets[0]
        check("names with quotes stay valid TOML", reloaded.name == 'Quote "Labs"', reloaded.name)
        check("free endpoints keep a 0 price", reloaded.price_out == 0.0, str(reloaded.price_out))

        print("cli contract (subprocess):")
        work = Path(tempfile.mkdtemp(prefix="tps-cli-"))
        (work / "local.toml").write_text(
            '[run]\nreps = 2\nconcurrency = [1, 4]\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block(
                "good", base, "test-model", "price = { in_per_1m = 0.1, out_per_1m = 0.2 }"
            )
            + target_block("limited", base, "rl-conc"),
            encoding="utf-8",
        )
        result = tps(["run", "-c", "local.toml", "-o", "out", "--json"], work)
        check("run exits 0", result.returncode == 0, result.stderr[-400:])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        check(
            "stdout is exactly one JSON object",
            payload.get("schema_version") == 1,
            result.stdout[:200],
        )
        check(
            "run JSON lists every model and preserves the legacy first-model field",
            payload.get("models") == ["test-model", "rl-conc"]
            and payload.get("model") == "test-model",
            str({k: payload.get(k) for k in ("model", "models")}),
        )
        rows = {r["target"]: r for r in payload.get("results", [])}
        limited = rows.get("limited", {})
        check(
            "ok/n counts every concurrency level",
            limited.get("n") == 2 * 1 + 2 * 4 and limited.get("ok", 99) < limited.get("n", 0),
            f"{limited.get('ok')}/{limited.get('n')}",
        )
        check(
            "by_concurrency shows where it failed",
            limited.get("by_concurrency", {}).get("4", {}).get("errors", {}).get("rate_limited", 0)
            > 0,
            str(limited.get("by_concurrency")),
        )
        check(
            "run JSON totals the run spend",
            (payload.get("cost_usd") or 0) > 0,
            str(payload.get("cost_usd")),
        )
        check(
            "an unpriced target is named, not billed as free",
            payload.get("cost_unpriced") == ["limited"],
            str(payload.get("cost_unpriced")),
        )
        check(
            "row costs add up to the run total",
            abs(
                sum(r.get("cost_usd") or 0 for r in payload.get("results", []))
                - (payload.get("cost_usd") or 0)
            )
            < 1e-6,
            str(payload.get("results")),
        )
        check(
            "only priced rows carry a cost",
            (rows.get("good", {}).get("cost_usd") or 0) > 0
            and rows.get("limited", {}).get("cost_usd") is None,
            str({k: v.get("cost_usd") for k, v in rows.items()}),
        )
        with open(work / "out" / "summary.csv", encoding="utf-8") as handle:
            csv_rows = [r for r in csv.DictReader(handle) if r["provider"] == "good"]
        by_c = {r["concurrency"]: r for r in csv_rows}
        check(
            "csv rows carry their own concurrency's counts",
            by_c.get("1", {}).get("n") == "2" and by_c.get("4", {}).get("n") == "8",
            str({c: r.get("n") for c, r in by_c.items()}),
        )
        out_report = (work / "out" / "report.md").read_text(encoding="utf-8")
        spend_lines = [
            line
            for line in out_report.splitlines()
            if line.startswith("Estimated spend for this run")
        ]
        check(
            "report states the run's spend and names unpriced targets",
            bool(spend_lines) and "$" in spend_lines[0] and "limited" in spend_lines[0],
            str(spend_lines),
        )
        load_rows = [
            line for line in out_report.splitlines() if line.startswith("| good | ") and "×" in line
        ]
        check("under-load table: one row per level", len(load_rows) == 2, str(load_rows))
        meta = json.loads((work / "out" / "meta.json").read_text())
        check("meta records completion", meta.get("complete") is True, str(meta.get("complete")))

        narrow = tps(["report", "out"], work, env_extra={"COLUMNS": "50"})
        widest = max(
            (len(line) for line in narrow.stdout.splitlines() if line[:1] in "│╭├╰"), default=0
        )
        check(
            "narrow terminal: tables fit by hiding optional columns",
            widest <= 50,
            f"widest line {widest}",
        )
        report_run = tps(["report", "out"], work)
        check(
            "report re-renders",
            report_run.returncode == 0 and "good" in report_run.stdout,
            report_run.stderr[-300:],
        )
        check(
            "mixed-model report command names the model count",
            "2 model ids" in report_run.stdout,
            report_run.stdout[:300],
        )
        markdown = tps(["report", "out", "--markdown"], work)
        check(
            "report --markdown prints markdown",
            markdown.stdout.startswith("# tps report"),
            markdown.stdout[:80],
        )
        check(
            "mixed-model markdown names the model count, not the first target",
            "- model: `2 model ids`" in markdown.stdout,
            markdown.stdout[:300],
        )

        bad_flag = tps(["run", "--json", "--reps", "abc"], work)
        check("bad flag exits 1 (not 2)", bad_flag.returncode == 1, str(bad_flag.returncode))
        check("bad flag still emits JSON", '"ok": false' in bad_flag.stdout, bad_flag.stdout[:120])
        check(
            "JSON error names the command",
            json.loads(bad_flag.stdout or "{}").get("command") == "run",
            bad_flag.stdout[:200],
        )
        version = tps(["--json", "--version"], work)
        check(
            "--json --version is a JSON object",
            json.loads(version.stdout or "{}").get("version") is not None,
            version.stdout[:80],
        )
        no_cmd = tps(["--json"], work)
        check(
            "--json with no command is an error object",
            no_cmd.returncode == 1 and '"ok": false' in no_cmd.stdout,
        )
        bad_prompt = tps(["run", "-c", "local.toml", "--prompts", "nope"], work)
        check(
            "unknown --prompts is a usage error",
            bad_prompt.returncode == 1 and "unknown prompt" in bad_prompt.stderr,
            bad_prompt.stderr[-200:],
        )
        zero = tps(["run", "-c", "local.toml", "--concurrency", "0"], work)
        check("--concurrency 0 rejected", zero.returncode == 1, zero.stderr[-200:])
        for flag, value in (("--max-tokens", "0"), ("--temperature", "-1"), ("--timeout", "0")):
            bad = tps(["run", "-c", "local.toml", flag, value], work)
            check(f"{flag} {value} rejected", bad.returncode == 1, bad.stderr[-200:])

        (work / "warm.toml").write_text(
            '[run]\nreps = 1\nwarmup = 2\nprompts = ["prose-256"]\n\n'
            + target_block("warm", base, "warm-model"),
            encoding="utf-8",
        )
        warm_payload = json.loads(
            tps(["run", "-c", "warm.toml", "-o", "warm", "--json"], work).stdout or "{}"
        )
        warm_caps = [cap for model, cap, _temp in SEEN if model == "warm-model"]
        check(
            "warm-ups are capped; measured requests keep the prompt's max_tokens",
            warm_caps == [core.WARMUP_MAX_TOKENS] * 2 + [256],
            str(warm_caps),
        )
        warm_raw = (work / "warm" / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        check(
            "raw rows say which phase they belong to",
            [json.loads(line).get("phase") for line in warm_raw] == ["warmup", "warmup", "measure"],
            str([json.loads(line).get("phase") for line in warm_raw]),
        )
        check(
            "run JSON labels every request count",
            warm_payload.get("total") == 1
            and warm_payload.get("warmup_requests") == 2
            and warm_payload.get("planned_requests") == 3
            and warm_payload.get("results", [{}])[0].get("n") == 1,
            str({k: warm_payload.get(k) for k in ("total", "warmup_requests", "planned_requests")}),
        )

        one_shot = tps(
            [
                "run",
                "--base-url",
                base,
                "--model",
                "test-model",
                "--name",
                "direct",
                "--price",
                "0.1,0.2",
                "--reps",
                "2",
                "-o",
                "oneshot",
                "--json",
            ],
            work,
        )
        one_payload = json.loads(one_shot.stdout or "{}")
        saved = work / "oneshot" / "targets.toml"
        one_row = (one_payload.get("results") or [{}])[0]
        check(
            "run --base-url: no config file, results as usual",
            one_shot.returncode == 0
            and one_row.get("target") == "direct"
            and one_row.get("ok") == 2
            and one_row.get("price_out_per_1m") == 0.2,
            one_shot.stderr[-300:] or str(one_row)[:200],
        )
        check(
            "run --base-url saves its config in the run dir, and it reloads",
            one_payload.get("config") == str(Path("oneshot") / "targets.toml")
            and saved.exists()
            and core.load_config(saved).targets[0].base_url == base,
            str(one_payload.get("config")),
        )
        mine = work / "handwritten"
        mine.mkdir()
        (mine / "targets.toml").write_text("# my hand-written config\n", encoding="utf-8")
        before_seen = len(SEEN)
        clobber = tps(
            ["run", "--base-url", base, "--model", "test-model", "-o", "handwritten"], work
        )
        check(
            "run --base-url never overwrites an existing config in -o (and sends nothing)",
            clobber.returncode == 1
            and (mine / "targets.toml").read_text(encoding="utf-8") == "# my hand-written config\n"
            and len(SEEN) == before_seen,
            clobber.stderr[-200:],
        )
        rerun = tps(
            [
                "run",
                "--base-url",
                base,
                "--model",
                "test-model",
                "--name",
                "direct",
                "--price",
                "0.1,0.2",
                "--reps",
                "2",
                "-o",
                "oneshot",
            ],
            work,
        )
        check(
            "...but re-running the same flags into its own run dir is fine",
            rerun.returncode == 0,
            rerun.stderr[-200:],
        )
        edited = saved.read_text(encoding="utf-8") + "# my edit\n"
        saved.write_text(edited, encoding="utf-8")
        before_seen = len(SEEN)
        reclobber = tps(
            [
                "run",
                "--base-url",
                base,
                "--model",
                "test-model",
                "--name",
                "direct",
                "--price",
                "0.1,0.2",
                "--reps",
                "2",
                "-o",
                "oneshot",
            ],
            work,
        )
        check(
            "...and a tps-written config someone edited is never replaced either",
            reclobber.returncode == 1
            and saved.read_text(encoding="utf-8") == edited
            and len(SEEN) == before_seen,
            reclobber.stderr[-200:],
        )
        (work / "init-ep.toml").unlink(missing_ok=True)
        init_ep = tps(
            [
                "init",
                "--endpoint",
                "https://api.example.invalid/v1",
                "--model",
                'vendor/model "x"',
                "--api-key-env",
                "TPS_SELFTEST_UNSET_KEY",
                "--out",
                "init-ep.toml",
            ],
            work,
        )
        init_target = (
            core.load_config(work / "init-ep.toml").targets[0]
            if (work / "init-ep.toml").exists()
            else None
        )
        check(
            "init --endpoint writes a loadable one-target config",
            init_ep.returncode == 0
            and init_target is not None
            and init_target.model == 'vendor/model "x"'
            and init_target.api_key_env == "TPS_SELFTEST_UNSET_KEY",
            init_ep.stderr[-200:],
        )
        for argv, why in (
            (["run", "--model", "m"], "--model without --base-url"),
            (["run", "--base-url", base], "--base-url without --model"),
            (["run", "--base-url", "ftp://x", "--model", "m"], "a non-http URL"),
            (["run", "-c", "local.toml", "--base-url", base, "--model", "m"], "-c and --base-url"),
            (["run", "--base-url", base, "--model", "m", "--price", "1"], "a one-number --price"),
            (["init", "--price", "1,2", "--out", "never.toml"], "init --price without --endpoint"),
        ):
            rejected = tps(argv, work)
            check(f"rejected: {why}", rejected.returncode == 1, rejected.stderr[-200:])
        unkeyed = tps(["run", "--base-url", "https://api.example.invalid/v1", "--model", "m"], work)
        check(
            "remote --base-url without a key exits 2 and says how to pass one",
            unkeyed.returncode == 2 and "--api-key-env" in unkeyed.stderr,
            unkeyed.stderr[-200:],
        )

        claude_env = {"TPS_SELFTEST_CLAUDE_KEY": "sk-ant-oat01-selftest-subscription"}
        claude_run = tps(
            [
                "run",
                "--api",
                "anthropic",
                "--base-url",
                base,
                "--model",
                "claude-sim",
                "--api-key-env",
                "TPS_SELFTEST_CLAUDE_KEY",
                "--reps",
                "2",
                "-o",
                "claude",
                "--json",
            ],
            work,
            env_extra=claude_env,
        )
        claude_payload = json.loads(claude_run.stdout or "{}")
        claude_row = (claude_payload.get("results") or [{}])[0]
        claude_md = (work / "claude" / "report.md").read_text(encoding="utf-8")
        check(
            "run --api anthropic: real streaming path end to end",
            claude_run.returncode == 0
            and claude_row.get("ok") == 2
            and claude_row.get("hidden_reasoning_tokens_median") == HIDDEN
            and any("thinking tokens" in n for n in claude_row.get("notes", [])),
            claude_run.stderr[-300:] or str(claude_row)[:300],
        )
        check(
            "run --api anthropic: saved config and report say what was sent",
            core.load_config(work / "claude" / "targets.toml").targets[0].api == "anthropic"
            and "temp not sent" in claude_md,
            claude_md[:300],
        )
        for argv, code, why in (
            (
                ["run", "--api", "anthropic", "--model", "m"],
                2,
                "Claude API default URL needs a key",
            ),
            (["run", "--api", "openai", "--model", "m"], 1, "--api openai without --base-url"),
            (["run", "-c", "local.toml", "--api", "anthropic"], 1, "-c with --api"),
            (["run", "--base-url", base, "--model", "m", "--api", "x"], 1, "an unknown --api"),
            (["run", "-c", "local.toml", "--temperature", "warm"], 1, "a non-numeric temperature"),
        ):
            got = tps(argv, work)
            check(
                f"exit {code}: {why}",
                got.returncode == code,
                f"{got.returncode} {got.stderr[-160:]}",
            )
        (work / "omit.toml").write_text(
            '[run]\nreps = 1\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("omit", base, "omit-model"),
            encoding="utf-8",
        )
        tps(["run", "-c", "omit.toml", "-o", "omit", "--temperature", "omit"], work)
        tps(["run", "-c", "omit.toml", "-o", "omit0"], work)
        omit_seen = [sent for model, _cap, sent in SEEN if model == "omit-model"]
        check(
            "--temperature omit leaves it out; the OpenAI default still sends 0",
            omit_seen == [False, True],
            str(omit_seen),
        )
        (work / "reserved.toml").write_text(
            target_block("r", base, "test-model", "extra_body = { max_tokens = 4096 }"),
            encoding="utf-8",
        )
        reserved = tps(["run", "-c", "reserved.toml", "--dry-run"], work)
        check(
            "extra_body can't override the token cap (it would defeat --dry-run and warm-ups)",
            reserved.returncode == 1 and "extra_body can't set max_tokens" in reserved.stderr,
            reserved.stderr[-200:],
        )
        (work / "reserved_run.toml").write_text(
            '[run]\nmax_tokens_field = "max_output_tokens"\n\n'
            + target_block("r", base, "test-model", "extra_body = { max_output_tokens = 4096 }"),
            encoding="utf-8",
        )
        reserved_run = tps(["run", "-c", "reserved_run.toml", "--dry-run"], work)
        check(
            "...nor through a run-level max_tokens_field",
            reserved_run.returncode == 1 and "max_output_tokens" in reserved_run.stderr,
            reserved_run.stderr[-200:],
        )
        cjk = "请用中文详细解释TCP拥塞控制的工作原理，包括慢启动和拥塞避免。" * 20
        check(
            "dry-run input bound holds for CJK text, where chars/4 undercounts ~4x",
            core.max_prompt_tokens(cjk) >= len(cjk) > 3 * core.estimate_tokens(cjk),
            f"{core.max_prompt_tokens(cjk)} vs {len(cjk)} chars, est {core.estimate_tokens(cjk)}",
        )
        (work / "fp_claude.toml").write_text(
            '[run]\nprompts = ["prose-256"]\n\n'
            + "".join(
                target_block(
                    name, base, model, 'api = "anthropic"\napi_key_env = "TPS_SELFTEST_CLAUDE_KEY"'
                )
                for name, model in (
                    ("opus", "claude-sim"),
                    ("sonnet", "claude-sim"),
                    ("tight", "claude-capped"),
                )
            ),
            encoding="utf-8",
        )
        fp_claude = tps(
            ["fingerprint", "-c", "fp_claude.toml", "-o", "fpc", "--prompt-count", "1", "--json"],
            work,
            env_extra=claude_env,
        )
        fpc = json.loads(fp_claude.stdout or "{}")
        check(
            "fingerprint on the Claude API: text mode; thinking that ate the cap is no_data",
            fpc.get("verdict") == "consistent"
            and fpc.get("no_data", {}).get("tight", "").startswith("reasoning_consumed_budget")
            and fpc.get("self_agreement", {}).get("opus") == 1.0,
            fp_claude.stdout[:300] or fp_claude.stderr[-300:],
        )

        print("dry run:")
        (work / "plan.toml").write_text(
            '[run]\nreps = 3\nconcurrency = [1, 4]\nwarmup = 1\nprompts = ["prose-256"]\n\n'
            + target_block(
                "priced",
                "https://api.example.invalid/v1",
                "m",
                'api_key_env = "TPS_SELFTEST_UNSET_KEY"\nprice = { in_per_1m = 1.0, out_per_1m = 2.0 }',
            )
            + target_block("free-local", base, "test-model"),
            encoding="utf-8",
        )
        before = len(SEEN)
        plan_run = tps(["run", "-c", "plan.toml", "-o", "planned", "--dry-run", "--json"], work)
        plan_payload = json.loads(plan_run.stdout or "{}")
        plan = plan_payload.get("plan", {})
        prompt_in = core.max_prompt_tokens(core.build_prompt(core.DEFAULT_PROMPTS["prose-256"]))
        # priced target: 15 measured × (in + 256 out) + 1 warm-up × (in + 16 out),
        # input at its byte bound so the cost really is an upper bound
        expected = (16 * prompt_in * 1.0 + (15 * 256 + core.WARMUP_MAX_TOKENS) * 2.0) / 1e6
        check(
            "dry run: sends nothing, writes nothing, exits 0 despite a missing key",
            plan_run.returncode == 0
            and len(SEEN) == before
            and not (work / "planned").exists()
            and plan_payload.get("dry_run") is True
            and plan_payload.get("missing_credentials") == ["priced"],
            f"{plan_run.returncode} {len(SEEN) - before} {plan_run.stderr[-200:]}",
        )
        check(
            "dry run: counts requests exactly as the run would",
            plan.get("requests") == 2 * (3 * 5 + 1)
            and plan.get("measured_requests") == 30
            and plan.get("warmup_requests") == 2,
            str({k: plan.get(k) for k in ("requests", "measured_requests", "warmup_requests")}),
        )
        check(
            "dry-run JSON lists every model and preserves the legacy first-model field",
            plan_payload.get("models") == ["m", "test-model"] and plan_payload.get("model") == "m",
            str({k: plan_payload.get(k) for k in ("model", "models")}),
        )
        check(
            "dry run: worst-case cost from max_tokens, unpriced targets listed, not guessed",
            plan.get("cost_max_usd") is not None
            and abs(plan["cost_max_usd"] - expected) < 1e-9
            and plan.get("unpriced") == ["free-local"],
            f"{plan.get('cost_max_usd')} vs {expected} {plan.get('unpriced')}",
        )
        check(
            "dry run: the time estimate states its assumption",
            plan.get("est_minutes", 0) > 0 and plan.get("assumed", {}).get("tok_s") > 0,
            str(plan.get("assumed")),
        )

        print("review fixes (CLI):")
        (work / "levels.toml").write_text(
            '[run]\nreps = 2\nconcurrency = [1, 2]\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("thinker", base, "thinker")
            + target_block("answers", base, "test-model"),
            encoding="utf-8",
        )
        levels = tps(["run", "-c", "levels.toml", "-o", "levels", "--json"], work)
        level_rows = {r["target"]: r for r in json.loads(levels.stdout or "{}").get("results", [])}
        thinker_row = level_rows.get("thinker", {})
        check(
            "reasoning_only counts every level like ok/n, so `reasoning_only == ok` holds",
            thinker_row.get("reasoning_only") == thinker_row.get("ok") == 2 * 1 + 2 * 2
            and thinker_row.get("by_concurrency", {}).get("2", {}).get("reasoning_only") == 4,
            str({k: thinker_row.get(k) for k in ("reasoning_only", "ok", "n")}),
        )
        fastest = next(
            (
                line
                for line in tps(["report", "levels"], work).stdout.splitlines()
                if "fastest" in line
            ),
            "",
        )
        check(
            "a row that timed thinking can't be 'fastest'",
            "answers" in fastest and "thinker" not in fastest,
            fastest,
        )
        from tps import cli as tps_cli

        starter = core.config_from_text(tps_cli.STARTER_CONFIG, "starter")
        example = core.load_config(ROOT / "targets.example.toml")
        check(
            "starter and example configs leave temperature to the per-API default",
            starter.temperature is None and example.temperature is None,
            f"{starter.temperature} {example.temperature}",
        )
        tps(
            [
                "run",
                "--base-url",
                base,
                "--model",
                "test-model",
                "--reps",
                "1",
                "--max-tokens",
                "64",
                "--no-warmup",
                "-o",
                "saved5",
            ],
            work,
        )
        saved5 = core.load_config(work / "saved5" / "targets.toml")
        check(
            "the saved config keeps the flags it ran with (reps, warmup, --max-tokens)",
            saved5.reps == 1
            and saved5.warmup == 0
            and saved5.prompts["prose-256"]["max_tokens"] == 64,
            f"{saved5.reps} {saved5.warmup} {saved5.prompts['prose-256'].get('max_tokens')}",
        )
        (work / "hot.toml").write_text(
            '[run]\nreps = 1\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("hot", base, "test-model", "extra_body = { temperature = 0.7 }"),
            encoding="utf-8",
        )
        tps(["run", "-c", "hot.toml", "-o", "hot"], work)
        hot_meta = json.loads((work / "hot" / "meta.json").read_text())
        check(
            "meta records the temperature actually sent (extra_body wins)",
            hot_meta["targets"][0]["temperature"] == 0.7
            and BODIES["test-model"].get("temperature") == 0.7,
            str(hot_meta["targets"][0].get("temperature")),
        )
        for argv, field in (
            (["run", "-c", "local.toml", "--temperature", "nan", "--json"], "temperature"),
            (
                [
                    "run",
                    "--base-url",
                    base,
                    "--model",
                    "m",
                    "--price",
                    "inf,1",
                    "--dry-run",
                    "--json",
                ],
                "price",
            ),
            (["run", "-c", "local.toml", "--timeout", "inf", "--json"], "timeout"),
        ):
            bad = tps(argv, work)
            try:
                bad_payload = json.loads(bad.stdout)
            except json.JSONDecodeError:
                bad_payload = {}
            message = (bad_payload.get("error") or {}).get("message", "")
            check(
                f"non-finite {field} rejected up front as a usage error, JSON stays valid",
                bad.returncode == 1 and bad_payload.get("ok") is False and field in message,
                bad.stdout[-200:],
            )
        rocket = tps(
            [
                "init",
                "--endpoint",
                "https://api.example.invalid/v1",
                "--model",
                "m\nx",
                "--name",
                "fast 🚀",
                "--out",
                "rocket.toml",
            ],
            work,
        )
        rocket_target = (
            core.load_config(work / "rocket.toml").targets[0]
            if (work / "rocket.toml").exists()
            else None
        )
        check(
            "init --endpoint writes valid TOML for emoji names and odd model ids",
            rocket.returncode == 0
            and rocket_target is not None
            and rocket_target.name == "fast 🚀"
            and rocket_target.model == "m\nx",
            rocket.stderr[-200:],
        )
        port = base.split(":")[2].split("/")[0]
        secret_url = f"http://user:s3cret@127.0.0.1:{port}/v1"
        creds = tps(
            [
                "run",
                "--base-url",
                secret_url,
                "--model",
                "test-model",
                "--reps",
                "1",
                "--no-warmup",
                "-o",
                "creds",
                "--json",
            ],
            work,
        )
        creds_meta = (work / "creds" / "meta.json").read_text(encoding="utf-8")
        check(
            "credentials in --base-url never reach the name, meta.json or JSON output",
            creds.returncode == 0
            and "s3cret" not in creds.stdout
            and "s3cret" not in creds_meta
            and json.loads(creds.stdout)["results"][0]["target"] == f"127.0.0.1:{port}",
            creds.stdout[:200],
        )

        (work / "think.toml").write_text(
            '[run]\nreps = 2\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("thinker", base, "thinker"),
            encoding="utf-8",
        )
        capped_run = tps(["run", "-c", "think.toml", "-o", "think", "--json"], work)
        capped_row = (json.loads(capped_run.stdout or "{}").get("results") or [{}])[0]
        check(
            "capped reasoning row explains its empty columns",
            capped_row.get("reasoning_only") == 2
            and capped_row.get("truncated") == 2
            and capped_row.get("itl_p50_ms") is None
            and capped_row.get("itl_n") == 0
            and capped_row.get("first_content_n") == 0
            and "measured thinking" in " ".join(capped_row.get("notes", [])),
            str({k: capped_row.get(k) for k in ("reasoning_only", "truncated", "itl_n", "notes")}),
        )
        (work / "bursty.toml").write_text(
            '[run]\nreps = 2\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("coalesced", base, "bursty")
            + target_block("steady", base, "test-model"),
            encoding="utf-8",
        )
        bursty_payload = json.loads(
            tps(["run", "-c", "bursty.toml", "-o", "bursty", "--json"], work).stdout or "{}"
        )
        by_target = {r["target"]: r for r in bursty_payload.get("results", [])}
        coalesced, steady = by_target.get("coalesced", {}), by_target.get("steady", {})
        check(
            "run JSON: bursty count, tokens/chunk and itl_n per row; steady stream unflagged",
            coalesced.get("bursty") == 2
            and coalesced.get("itl_n") == 2
            and coalesced.get("tokens_per_chunk_median") == 1.0
            and steady.get("bursty") == 0
            and not any("bursts" in n for n in steady.get("notes", [])),
            str({k: coalesced.get(k) for k in ("bursty", "itl_n", "tokens_per_chunk_median")}),
        )
        bursty_md = (work / "bursty" / "report.md").read_text(encoding="utf-8")
        coalesced_line = next(
            (line for line in bursty_md.splitlines() if line.startswith("| coalesced |")), ""
        )
        check(
            "report.md marks the bursty ITL and explains it",
            " †" in coalesced_line and "arrived in bursts" in bursty_md,
            coalesced_line,
        )
        check(
            "report.md and the terminal say so too",
            "measured thinking" in (work / "think" / "report.md").read_text(encoding="utf-8")
            and "measured thinking" in tps(["report", "think"], work).stdout,
        )
        raised_run = tps(
            ["run", "-c", "think.toml", "-o", "think-raised", "--max-tokens", "400", "--json"],
            work,
        )
        raised_row = (json.loads(raised_run.stdout or "{}").get("results") or [{}])[0]
        raised_meta = json.loads((work / "think-raised" / "meta.json").read_text())
        check(
            "--max-tokens reaches the request (the thinker answers above its cap)",
            raised_row.get("reasoning_only") == 0
            and raised_row.get("first_content_p50_s") is not None
            and not any("measured thinking" in n for n in raised_row.get("notes", []))
            and raised_meta.get("max_tokens") == {"prose-256": 400},
            f"{raised_row.get('reasoning_only')} {raised_meta.get('max_tokens')}",
        )
        # the thinker streams ~10 tokens of reasoning text but bills 300: the
        # unstreamed 290 are hidden, kept out of tok/s, and flagged as an estimate
        check(
            "partly streamed reasoning: the unstreamed rest is hidden and flagged ≈",
            raised_row.get("hidden_reasoning_tokens_median") == THINK - 10
            and "usage+chars/4" in raised_row.get("count_source", [])
            and any("thinking tokens" in n for n in raised_row.get("notes", [])),
            str({k: raised_row.get(k) for k in ("hidden_reasoning_tokens_median", "count_source")}),
        )

        (work / "dup.toml").write_text(
            target_block("same", base, "test-model") + target_block("same", base, "other-model"),
            encoding="utf-8",
        )
        (work / "typed.toml").write_text(
            '[run]\nprompts = "prose-256"\n\n'
            + target_block(
                "t", base, "test-model", 'price = { in_per_1m = "0.14", out_per_1m = 0.42 }'
            ),
            encoding="utf-8",
        )
        typed = core.load_config(work / "typed.toml")
        check(
            "quoted price and a string `prompts` are accepted",
            typed.targets[0].price_in == 0.14 and list(typed.prompts) == ["prose-256"],
            f"{typed.targets[0].price_in} {list(typed.prompts)}",
        )
        (work / "badprice.toml").write_text(
            target_block(
                "t", base, "test-model", 'price = { in_per_1m = "cheap", out_per_1m = 1 }'
            ),
            encoding="utf-8",
        )
        bad_price = tps(["run", "-c", "badprice.toml"], work)
        check(
            "non-numeric price rejected before any request",
            bad_price.returncode == 1 and "number" in bad_price.stderr,
            bad_price.stderr[-200:],
        )
        dup = tps(["run", "-c", "dup.toml"], work)
        check(
            "duplicate target names rejected",
            dup.returncode == 1 and "duplicate" in dup.stderr,
            dup.stderr[-200:],
        )

        mock_only = tps(["run", "-c", str(ROOT / "targets.mock.toml")], work)
        check(
            "mock-only config needs --mock",
            mock_only.returncode == 1 and "--mock" in mock_only.stderr,
        )
        mock_run = tps(
            [
                "run",
                "-c",
                str(ROOT / "targets.mock.toml"),
                "--mock",
                "--reps",
                "2",
                "-o",
                "m",
                "--json",
            ],
            work,
        )
        mock_payload = json.loads(mock_run.stdout or "{}")
        check(
            "--mock run flagged in JSON",
            mock_payload.get("mock") is True and mock_run.returncode == 0,
        )

        doctor_missing = tps(["doctor", "-c", "nope.toml"], work)
        check(
            "doctor: missing config exits 1",
            doctor_missing.returncode == 1,
            str(doctor_missing.returncode),
        )
        (work / "keyed.toml").write_text(
            target_block(
                "remote",
                "https://api.example.invalid/v1",
                "m",
                'api_key_env = "TPS_SELFTEST_UNSET_KEY"',
            ),
            encoding="utf-8",
        )
        doctor_keys = tps(["doctor", "-c", "keyed.toml"], work)
        check(
            "doctor: missing key exits 2", doctor_keys.returncode == 2, str(doctor_keys.returncode)
        )
        doctor_local = tps(["doctor", "-c", "local.toml"], work)
        check(
            "doctor: 127.0.0.1 needs no key",
            doctor_local.returncode == 0,
            doctor_local.stdout[-300:],
        )

        (work / "fp.toml").write_text(
            '[run]\nprompts = ["prose-256"]\n\n'
            + target_block("alpha", base, "test-model")
            + target_block("beta", base, "test-model")
            + target_block("down", base, "fp-fail"),
            encoding="utf-8",
        )
        fp = tps(["fingerprint", "-c", "fp.toml", "-o", "fp", "--json"], work)
        fp_payload = json.loads(fp.stdout or "{}")
        check(
            "fingerprint: failed target is no_data, verdict consistent",
            fp_payload.get("verdict") == "consistent" and "down" in fp_payload.get("no_data", {}),
            fp.stdout[:300],
        )
        (work / "fp_down.toml").write_text(target_block("down", base, "fp-fail"), encoding="utf-8")
        fp_down = tps(["fingerprint", "-c", "fp_down.toml", "-o", "fpd", "--json"], work)
        check(
            "fingerprint: every request failed exits 4",
            fp_down.returncode == 4,
            str(fp_down.returncode),
        )

        print("interrupt keeps partial data:")
        (work / "slow.toml").write_text(
            '[run]\nreps = 50\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("slow", base, "slow"),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "tps", "run", "-c", "slow.toml", "-o", "partial"],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        raw_path = work / "partial" / "raw.jsonl"
        deadline = time.time() + 30
        while time.time() < deadline and (not raw_path.exists() or raw_path.stat().st_size == 0):
            time.sleep(0.1)
        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        _out, err = proc.communicate(timeout=30)
        check("ctrl-c exits 130", proc.returncode == 130, f"{proc.returncode} {err[-200:]}")
        kept = (
            [line for line in raw_path.read_text().splitlines() if line.strip()]
            if raw_path.exists()
            else []
        )
        check("finished requests kept on disk", len(kept) >= 1, str(len(kept)))
        partial_meta = json.loads((work / "partial" / "meta.json").read_text())
        check("meta marks the run partial", partial_meta.get("complete") is False)
        partial_report = work / "partial" / "report.md"
        check(
            "partial report written and flagged",
            partial_report.exists() and "PARTIAL" in partial_report.read_text(),
        )
        (work / "fp_slow.toml").write_text(
            '[run]\nprompts = ["prose-256"]\n\n' + target_block("slow", base, "fp-slow"),
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tps",
                "fingerprint",
                "-c",
                "fp_slow.toml",
                "-o",
                "fpartial",
                "--reps",
                "30",
            ],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fp_path = work / "fpartial" / "fingerprints.jsonl"
        deadline = time.time() + 30
        while time.time() < deadline and (not fp_path.exists() or fp_path.stat().st_size == 0):
            time.sleep(0.1)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
        kept_fp = (
            [line for line in fp_path.read_text().splitlines() if line.strip()]
            if fp_path.exists()
            else []
        )
        fp_md = work / "fpartial" / "fingerprint.md"
        check(
            "fingerprint ctrl-c: exits 130, keeps data, partial report",
            proc.returncode == 130
            and len(kept_fp) >= 1
            and fp_md.exists()
            and "PARTIAL" in fp_md.read_text(),
            f"{proc.returncode} {len(kept_fp)}",
        )

        print("CLI targets (claude-code, fake client):")
        fake = work / "fake-claude"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        fake.chmod(0o755)
        fake_log = work / "fake-claude.log"
        cli_env = {"TPS_FAKE_LOG": str(fake_log), "XDG_CACHE_HOME": str(work / "cache")}
        saved_env = {k: os.environ.get(k) for k in (*cli_env, "ANTHROPIC_API_KEY")}
        os.environ.update(cli_env, ANTHROPIC_API_KEY="sk-ant-api-should-be-stripped")

        def cc(model: str, **kw) -> core.Target:
            return core.Target(
                name=model, base_url="", model=model, api="claude-code", cli=str(fake), **kw
            )

        cli_cfg = core.RunConfig(targets=[], prompts={}, reps=1, concurrency=[1], timeout_s=20)
        try:
            fast = await core.cli_once(cc("cc-fast", effort="high"), "hi", "p", 512, cli_cfg, 1, 0)
            check("cli: ok", fast.ok, str(fast.error))
            check(
                "cli: tokens from usage",
                fast.out_tokens == 25 and fast.count_source == "usage",
                f"{fast.out_tokens} {fast.count_source}",
            )
            check(
                "cli: hidden thinking kept out of tok/s",
                fast.hidden_reasoning_tokens == 5,
                str(fast.hidden_reasoning_tokens),
            )
            check(
                "cli: input counts cached prompt tokens", fast.in_tokens == 503, str(fast.in_tokens)
            )
            check(
                "cli: tok/s ~ fake pace (100/s)",
                fast.out_tps is not None and 50 <= fast.out_tps <= 130,
                str(fast.out_tps),
            )
            check(
                "cli: startup recorded, inside TTFT",
                fast.startup_s is not None
                and fast.ttft_s is not None
                and 0.04 <= fast.startup_s < fast.ttft_s,
                f"{fast.startup_s} {fast.ttft_s}",
            )
            check(
                "cli: plan window read after message_stop, outside the timing",
                fast.plan_utilization == 0.25
                and fast.plan_window == "five_hour"
                and fast.e2e_s is not None
                and fast.e2e_s < 1.0,
                f"{fast.plan_utilization} {fast.plan_window} {fast.e2e_s}",
            )
            check(
                "cli: served_by names the client",
                fast.served_by == "claude-code",
                str(fast.served_by),
            )
            logged = [json.loads(line) for line in fake_log.read_text().splitlines()]
            call = logged[-1]
            check("cli: prompt sent on stdin", call["prompt"] == "hi", repr(call["prompt"]))
            check(
                "cli: --effort passed",
                "--effort" in call["argv"]
                and call["argv"][call["argv"].index("--effort") + 1] == "high",
                str(call["argv"]),
            )
            check(
                "cli: tools and MCP stripped",
                "--tools" in call["argv"] and "--strict-mcp-config" in call["argv"],
                str(call["argv"]),
            )
            check(
                "cli: client retries disabled",
                call["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0",
                str(call["env"]),
            )
            check(
                "cli: output cap passed",
                call["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "512",
                str(call["env"]),
            )
            check(
                "cli: API key stripped so the plan is measured",
                call["env"]["ANTHROPIC_API_KEY"] is None,
                str(call["env"]),
            )
            check(
                "cli: runs in a stable empty workdir",
                call["cwd"] == str(work / "cache" / "tps" / "cli-workdir"),
                call["cwd"],
            )

            capped = await core.cli_once(cc("cc-capped"), "hi", "p", 20, cli_cfg, 1, 0)
            check(
                "cli: capped response is a truncated answer, not an error",
                capped.ok and capped.finish_reason == "length",
                f"{capped.ok} {capped.error}",
            )
            check(
                "cli: only the first API call is measured",
                capped.out_tokens == 25,
                str(capped.out_tokens),
            )
            check(
                "cli: capped response still reports plan usage",
                capped.plan_utilization == 0.5,
                str(capped.plan_utilization),
            )
            await asyncio.sleep(1.3)
            check(
                "cli: stopped before a continuation request",
                not Path(str(fake_log) + ".continued").exists(),
            )

            limited = await core.cli_once(cc("cc-ratelimit"), "hi", "p", 64, cli_cfg, 1, 0)
            check(
                "cli: 429 → rate_limited, no retry",
                not limited.ok
                and (limited.error or "").startswith("rate_limited")
                and limited.status == 429,
                str(limited.error),
            )
            crash = await core.cli_once(cc("cc-crash"), "hi", "p", 64, cli_cfg, 1, 0)
            check(
                "cli: crash → cli_exit_N with stderr",
                (crash.error or "").startswith("cli_exit_3") and "boom" in (crash.error or ""),
                str(crash.error),
            )
            gone = await core.cli_once(
                core.Target(
                    name="g",
                    base_url="",
                    model="m",
                    api="claude-code",
                    cli=str(work / "no-such-cli"),
                ),
                "hi",
                "p",
                64,
                cli_cfg,
                1,
                0,
            )
            check(
                "cli: missing binary is an error sample",
                (gone.error or "").startswith("cli_"),
                str(gone.error),
            )

            cc_toml = (
                '[run]\nreps = 2\nwarmup = 1\nprompts = ["prose-256"]\n\n'
                f'[[targets]]\nname = "plan"\napi = "claude-code"\nmodel = "cc-fast"\n'
                f'effort = "low"\ncli = "{fake}"\n'
            )
            (work / "cc.toml").write_text(cc_toml, encoding="utf-8")
            proc = tps(["run", "-c", "cc.toml", "-o", "cc-run", "--json"], work, env_extra=cli_env)
            out = json.loads(proc.stdout or "{}")
            row = (out.get("results") or [{}])[0]
            check(
                "cli run: exit 0, all ok",
                proc.returncode == 0 and row.get("ok") == 2,
                f"{proc.returncode} {proc.stderr[-300:]}",
            )
            check("cli run: startup_p50_s reported", row.get("startup_p50_s") is not None, str(row))
            check(
                "cli run: notes flag CLI startup in TTFT",
                any("CLI target" in n for n in row.get("notes", [])),
                str(row.get("notes")),
            )
            check(
                "cli run: unpriced, not $0",
                "plan" in out.get("cost_unpriced", []),
                str(out.get("cost_unpriced")),
            )
            meta = json.loads((work / "cc-run" / "meta.json").read_text())
            check(
                "cli run: meta records access and effort",
                meta["targets"][0].get("access") == "cli"
                and meta["targets"][0].get("effort") == "low",
                str(meta["targets"][0]),
            )

            proc = tps(
                [
                    "run",
                    "--api",
                    "claude-code",
                    "--model",
                    "cc-fast",
                    "--effort",
                    "max",
                    "--dry-run",
                    "--json",
                ],
                work,
                env_extra=cli_env,
            )
            plan = json.loads(proc.stdout or "{}")
            check(
                "cli dry-run: no URL or key needed",
                proc.returncode == 0
                and plan.get("missing_credentials") == []
                and plan.get("subscription_targets") == ["claude-code/cc-fast@max"],
                f"{proc.returncode} {proc.stdout[-300:]}",
            )

            (work / "cc-missing.toml").write_text(
                cc_toml.replace(str(fake), str(work / "no-such-cli")), encoding="utf-8"
            )
            proc = tps(["run", "-c", "cc-missing.toml", "--dry-run", "--json"], work)
            check(
                "cli dry-run: missing CLI listed",
                json.loads(proc.stdout or "{}").get("missing_clis") == ["plan"],
                proc.stdout,
            )
            proc = tps(["run", "-c", "cc-missing.toml", "--json"], work)
            check(
                "cli run: missing CLI → exit 1 + JSON error",
                proc.returncode == 1 and "CLI not installed" in proc.stdout,
                proc.stdout,
            )

            for label, extra in (
                ("api_key_env on a CLI target", 'api_key_env = "X"\n'),
                ("unknown effort", None),
            ):
                text = cc_toml + extra if extra else cc_toml.replace('"low"', '"turbo"')
                (work / "cc-bad.toml").write_text(text, encoding="utf-8")
                proc = tps(["run", "-c", "cc-bad.toml", "--dry-run", "--json"], work)
                check(f"cli config: {label} → exit 1", proc.returncode == 1, proc.stdout)
            (work / "effort-http.toml").write_text(
                "[run]\nreps = 1\n\n" + target_block("h", base, "test-model", 'effort = "low"'),
                encoding="utf-8",
            )
            proc = tps(["run", "-c", "effort-http.toml", "--dry-run", "--json"], work)
            check(
                "cli config: effort on an HTTP target is accepted (sent as reasoning_effort)",
                proc.returncode == 0,
                proc.stdout[-300:],
            )

            proc = tps(["doctor", "-c", "cc.toml", "--json"], work, env_extra=cli_env)
            doc = json.loads(proc.stdout or "{}")
            cli_row = next((c for c in doc.get("checks", []) if c["check"].startswith("cli ")), {})
            check(
                "doctor: CLI signed in + plan shown",
                cli_row.get("ok") and "max" in cli_row.get("detail", ""),
                str(cli_row),
            )
            check("doctor: account email never printed", "someone@example.com" not in proc.stdout)

            (work / "mixed.toml").write_text(
                '[run]\nreps = 2\nwarmup = 0\nprompts = ["prose-256"]\nbaseline = "direct"\n\n'
                + target_block("direct", base, "atlas", "price = { in_per_1m = 1, out_per_1m = 2 }")
                + target_block("gw", base, "atlas", "price = { in_per_1m = 1, out_per_1m = 3 }")
                + '[[targets]]\nname = "plan-low"\napi = "claude-code"\nmodel = "atlas"\n'
                'effort = "low"\n\n'
                + '[[targets]]\nname = "plan-high"\napi = "claude-code"\nmodel = "atlas"\n'
                'effort = "high"\n',
                encoding="utf-8",
            )
            proc = tps(
                ["run", "-c", "mixed.toml", "--mock", "-o", "mixed-run"],
                work,
                env_extra={"COLUMNS": "100"},
            )
            screen = proc.stdout + proc.stderr
            boxes = [line for line in screen.splitlines() if line.startswith("╭─ prose-256")]
            check(
                "mixed run: one box per access mode",
                len(boxes) == 2 and "· API" in boxes[0] and "plan" in boxes[1],
                str(boxes),
            )
            check(
                "mixed run: boxes share one width",
                len({len(b) for b in boxes}) == 1,
                str([len(b) for b in boxes]),
            )
            check("mixed run: PLAN badge", "PLAN" in screen.splitlines()[1], screen[:200])
            check(
                "mixed run: plan box labels rows by effort, cost column says plan",
                any(line.startswith("│ effort") for line in screen.splitlines())
                and any("plan │" in line or " plan " in line for line in screen.splitlines()),
                screen,
            )
            check(
                "mixed run: snappiest ranks API ttft, plan ttft as an aside",
                "snappiest" in screen and "on the plan:" in screen,
                screen,
            )
            check(
                "mixed run: spend line names plan usage",
                "4 requests on your plan" in screen and "on API" in screen,
                screen,
            )
            check(
                "mixed run: CLI startup said once (footer), not per row",
                "CLI startup" in screen and "CLI target:" not in screen,
                screen,
            )

            proc = tps(["fingerprint", "-c", "cc.toml", "--json"], work, env_extra=cli_env)
            check("fingerprint: CLI targets rejected (exit 1)", proc.returncode == 1, proc.stdout)
        finally:
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        print("effort and grids:")

        def body_for(api: str, url: str, effort: str | None) -> dict:
            t = core.Target(name="t", base_url=url, model="m", api=api, effort=effort)
            return core.request_body(t, cfg, "hi", 32, stream=True)

        check(
            "effort: Claude API → output_config.effort",
            body_for("anthropic", base, "high").get("output_config") == {"effort": "high"},
        )
        check(
            "effort: OpenAI-compatible → reasoning_effort",
            body_for("openai", base, "low").get("reasoning_effort") == "low",
        )
        check(
            "effort: OpenRouter → reasoning.effort",
            body_for("openai", "https://openrouter.ai/api/v1", "medium").get("reasoning")
            == {"effort": "medium"},
        )
        plain = body_for("openai", base, None)
        check(
            "effort: unset sends nothing (the model's default)",
            not ({"reasoning", "reasoning_effort", "output_config"} & set(plain)),
            str(plain),
        )
        grid_toml = (
            '[run]\nreps = 1\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            "[[grids]]\n"
            'name = "g"\n'
            f'base_url = "{base}"\n'
            'api_key = "good-key"\n'
            'models = ["test-model", "big-chunks"]\n'
            'effort = ["default", "high"]\n'
            "price = { in_per_1m = 1, out_per_1m = 2 }\n"
            'prices = { "big-chunks" = { in_per_1m = 3, out_per_1m = 4 } }\n'
        )
        (work / "grid.toml").write_text(grid_toml, encoding="utf-8")
        grid = core.load_config(work / "grid.toml")
        check(
            "grid: one target per model × effort",
            [t.name for t in grid.targets]
            == ["g/test-model", "g/test-model@high", "g/big-chunks", "g/big-chunks@high"],
            str([t.name for t in grid.targets]),
        )
        check(
            "grid: 'default' sends no effort, others do",
            [t.effort for t in grid.targets] == [None, "high", None, "high"],
        )
        check(
            "grid: per-model prices override the shared price",
            [t.price_out for t in grid.targets] == [2, 2, 4, 4],
            str([t.price_out for t in grid.targets]),
        )
        for label, broken in (
            ("grid without a name", grid_toml.replace('name = "g"\n', "")),
            ("grid with a repeated model", grid_toml.replace('"big-chunks"]', '"test-model"]')),
            (
                "grid price for a model it doesn't have",
                grid_toml.replace('{ "big-chunks"', '{ "x"'),
            ),
            (
                "effort and extra_body both setting it",
                grid_toml + 'extra_body = { reasoning_effort = "low" }\n',
            ),
            ("unknown effort level", grid_toml.replace('"high"]', '"turbo"]')),
        ):
            (work / "grid-bad.toml").write_text(broken, encoding="utf-8")
            proc = tps(["run", "-c", "grid-bad.toml", "--dry-run", "--json"], work)
            check(f"config: {label} → exit 1", proc.returncode == 1, proc.stdout[-300:])
        proc = tps(["run", "-c", "grid.toml", "-o", "grid-run", "--json"], work)
        sent = {BODIES.get(m, {}).get("reasoning_effort") for m in ("test-model", "big-chunks")}
        check(
            "grid run: every target measured, effort reached the server",
            proc.returncode == 0
            and len(json.loads(proc.stdout or "{}").get("results", [])) == 4
            and "high" in sent,
            f"{proc.returncode} {sent} {proc.stdout[-200:]}",
        )
        proc = tps(
            [
                "run",
                "--api",
                "claude-code",
                "--model",
                "a,b",
                "--effort",
                "low,high",
                "--dry-run",
                "--json",
            ],
            work,
        )
        plan = json.loads(proc.stdout or "{}").get("plan", {})
        check(
            "flags: --model a,b --effort low,high → a 4-target grid",
            [t["target"] for t in plan.get("targets", [])]
            == [
                "claude-code/a@low",
                "claude-code/a@high",
                "claude-code/b@low",
                "claude-code/b@high",
            ],
            proc.stdout[-300:],
        )
        proc = tps(["run", "--api", "claude-code", "--model", "a", "--effort", "turbo"], work)
        check("flags: unknown --effort → exit 1", proc.returncode == 1, proc.stdout[-200:])

        one = (
            '[run]\nreps = 2\nwarmup = 0\nprompts = ["prose-256"]\n\n[[grids]]\nname = "x"\n'
            f'base_url = "{base}"\napi_key = "k"\nmodels = ["atlas"]\n'
            'effort = ["low", "medium", "high"]\n'
            "mock = { ttft = [0.40, 0.42], itl = [0.0100, 0.0101] }\n"
        )
        (work / "effort-one.toml").write_text(one, encoding="utf-8")
        screen = tps(
            ["run", "-c", "effort-one.toml", "--mock", "-o", "e1"],
            work,
            env_extra={"COLUMNS": "100"},
        ).stdout
        check(
            "effort view: rows by effort + 'what effort costs' box",
            "│ effort" in screen and "atlas · what effort costs" in screen,
            screen,
        )
        check(
            "effort view: thinking said once (box), not per row",
            "TTFT includes ~" not in screen,
            screen,
        )
        check("effort view: takeaway line", "→ effort barely moves tok/s" in screen, screen)
        (work / "effort-grid.toml").write_text(
            one.replace('models = ["atlas"]', 'models = ["atlas", "comet"]'), encoding="utf-8"
        )
        screen = tps(
            ["run", "-c", "effort-grid.toml", "--mock", "-o", "e2"],
            work,
            env_extra={"COLUMNS": "100"},
        ).stdout
        check(
            "effort view: several models × levels → one matrix",
            "tok/s · ttft by effort" in screen and "│ model" in screen,
            screen,
        )

        def row(model, effort, tps_, ttft):
            return {
                "model": model,
                "effort": effort,
                "out_tps_p50": tps_,
                "ttft_p50_s": ttft,
                "concurrency": 1,
                "by_concurrency": {"1": {"reasoning_only": 0}},
            }

        from tps import cli as tps_cli

        flat = [row("a", "low", 100, 1.0), row("a", "high", 98, 3.0)]
        check(
            "takeaway: says it when it holds",
            tps_cli.effort_takeaway(flat)
            == "effort barely moves tok/s (≤2%) · it costs ttft: +2.0s from low to high",
            str(tps_cli.effort_takeaway(flat)),
        )
        mixed_up = [row("a", "low", 100, 3.0), row("a", "high", 70, 1.0)]
        check(
            "takeaway: silent when it doesn't hold for every row",
            tps_cli.effort_takeaway(mixed_up) is None,
            str(tps_cli.effort_takeaway(mixed_up)),
        )

        print("report provenance:")
        (work / "unknown_rate.toml").write_text(
            '[run]\nreps = 1\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("single", base, "single-chunk")
            + target_block("down", base, "test-model", 'api_key = "bad-key"'),
            encoding="utf-8",
        )
        tps(["run", "-c", "unknown_rate.toml", "-o", "ur"], work)
        ur_report = (work / "ur" / "report.md").read_text(encoding="utf-8")
        chart = [line for line in ur_report.splitlines() if line.startswith("single ")]
        check("unknown rate shows as —, not 0.0", bool(chart) and "— tok/s" in chart[0], str(chart))
        cost_section = ur_report.split("## Cost & provenance", 1)[-1]
        cost_row = next(
            (line for line in cost_section.splitlines() if line.startswith("| down |")), ""
        )
        check(
            "an all-failed target claims no token source",
            cost_row.rstrip(" |").endswith("—"),
            cost_row,
        )
    finally:
        server.shutdown()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
