"""Engine for tps: config, transport, metrics, reporting, discovery, fingerprinting."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import math
import os
import shutil
import statistics
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__, clis

try:  # HTTP/2 is a nice-to-have; HTTP/1.1 is fine and universally supported
    import h2  # noqa: F401

    HTTP2 = True
except ImportError:  # pragma: no cover
    HTTP2 = False


# --------------------------------------------------------------------------
# errors (stable exit codes for scripts and agents)
# --------------------------------------------------------------------------


class TpsError(Exception):
    """Base error carrying the exit code the CLI should use."""

    exit_code = 1

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UsageError(TpsError):
    """Bad config, bad flags, model not found."""

    exit_code = 1


class MissingKeyError(TpsError):
    """Credential for a target is not set."""

    exit_code = 2


class ProviderError(TpsError):
    """Upstream/API failure while talking to a provider."""

    exit_code = 3


# identify benchmark traffic honestly to the providers being measured
USER_AGENT = f"tps/{__version__} (+https://github.com/teal-sea/tpsbench)"

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
# CLI targets ("claude-code", …) spawn a vendor's signed-in client headless: the
# supported way to measure a subscription plan, whose tokens only the vendor's
# own client may use. Each client is an adapter in clis.py.
CLI_APIS = clis.CLI_APIS
APIS = ("openai", "anthropic", *CLI_APIS)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# HTTP APIs: what each provider accepts varies by model (OpenAI also has
# "minimal"); an unsupported level is the provider's 400, recorded as data
EFFORTS_HTTP = ("minimal", *EFFORTS)
# where effort goes in each API's request body; extra_body may not also set it
EFFORT_FIELDS = {
    "anthropic": "output_config",
    "openrouter": "reasoning",
    "openai": "reasoning_effort",
}
# what the CLI spends before the first byte of the request (process start,
# config load); dry-run budgets it per request
PLAN_CLI_STARTUP_S = 2.0
# Claude API stop reasons in OpenAI's vocabulary, so one report covers both
ANTHROPIC_STOP = {
    "max_tokens": "length",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}
# request fields tps sets itself; overriding them in extra_body would silently
# change what is measured (and what --dry-run budgets for)
RESERVED_BODY = ("model", "messages", "stream", "max_tokens", "max_completion_tokens")


SECRET_HEADER_HINTS = ("key", "token", "auth", "secret", "password")


def redact(text: str, secrets: str | list[str]) -> str:
    """Providers sometimes echo the submitted key in error bodies; never store it.

    Redact the *full* text before truncating it, so a key that straddles the
    cut can't leave its prefix behind."""
    for secret in [secrets] if isinstance(secrets, str) else secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

# ~170 tokens of filler used to build a prefill-heavy prompt without shipping
# a big fixture file. Repeat it `repeat` times to reach the target context size.
PAD_DOC = (
    "Distributed systems fail in ways that are rarely captured by a single "
    "component's unit tests. A request crosses a load balancer, an "
    "authentication service, a queue, two stateless workers and a shard of a "
    "relational database before a byte reaches the client. Each hop has its "
    "own timeout budget, its own retry policy and its own notion of what "
    "idempotency means. When one hop slows down, the others do not simply "
    "wait: they retry, they queue, they amplify. The classic failure mode is "
    "metastable collapse, where a service that is technically healthy keeps "
    "failing because its queues never drain. Operator intuition tends to "
    "underestimate this because the system looks fine at every individual "
    "dashboard while the aggregate is on fire."
)

DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "prose-256": {
        "text": (
            "Write a continuous technical explanation of how TCP congestion "
            "control works, covering slow start, congestion avoidance, fast "
            "retransmit and CUBIC. Do not use bullet points, headers or lists. "
            "Write flowing paragraphs of at least 500 words."
        ),
        "max_tokens": 256,
    },
    "code-256": {
        "text": (
            "Write a complete, runnable Python implementation of a red-black "
            "tree with insert, delete and search, including type hints and "
            "docstrings. Output only code, at least 120 lines."
        ),
        "max_tokens": 256,
    },
    "long-context": {
        "text": "Summarise the document below in about 150 words.",
        "repeat": 40,
        "max_tokens": 128,
    },
}


def build_prompt(spec: dict[str, Any]) -> str:
    """Turn a prompt spec into the string actually sent.

    An explicit `document`/`document_file` is included once by default; the
    built-in filler document is only used when `repeat` asks for it.
    """
    text = spec.get("text", "")
    source = spec.get("document_file")
    if source:
        try:
            doc = Path(source).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(
                f"prompt document_file not readable: {source} ({exc.strerror})"
            ) from exc
        default_repeat = 1
    elif "document" in spec:
        doc = str(spec["document"])
        default_repeat = 1
    else:
        doc = PAD_DOC
        default_repeat = 0

    repeat = int(spec.get("repeat", default_repeat) or 0)
    parts = [text] if text else []
    if repeat > 0:
        parts.append("\n\n".join([doc] * repeat))
    if not parts:
        raise UsageError("prompt spec needs `text`, `document` or `document_file`")
    return "\n\n---\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


@dataclass
class Target:
    name: str
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    price_in: float | None = None  # USD per 1M input tokens
    price_out: float | None = None  # USD per 1M output tokens
    max_tokens_field: str | None = None
    send_stream_options: bool = True
    mock: dict[str, Any] | None = None
    # "openai" = /chat/completions; "anthropic" = the native Claude Messages
    # API, the only one that accepts Claude subscription (OAuth) tokens
    api: str = "openai"
    # reasoning effort; None = the model's default (sent as nothing at all)
    effort: str | None = None
    # CLI targets: the executable to spawn (default: the backend's, e.g. `claude`)
    cli: str | None = None
    # CLI targets: the plan the CLI is signed in to ("max"), when it says
    plan: str | None = None

    @property
    def is_cli(self) -> bool:
        return self.api in CLI_APIS

    @property
    def backend(self) -> clis.CliBackend:
        return clis.backend(self.api)

    def cli_binary(self) -> str:
        return self.cli or (self.backend.binary if self.is_cli else "")

    def cli_path(self) -> str | None:
        """Absolute path of the CLI to spawn, or None when it isn't installed."""
        binary = os.path.expanduser(self.cli_binary())
        if os.sep in binary:
            return binary if os.access(binary, os.X_OK) else None
        return shutil.which(binary)

    def effort_field(self) -> str | None:
        """The request field that carries `effort`; None for CLI targets."""
        if self.is_cli:
            return None
        if self.api == "anthropic":
            return EFFORT_FIELDS["anthropic"]
        host = urlparse(self.base_url).hostname or ""
        return EFFORT_FIELDS["openrouter" if host.endswith("openrouter.ai") else "openai"]

    def effort_body(self) -> dict[str, Any]:
        """`effort` in this API's own shape, or {} for the model's default."""
        field_name = self.effort_field()
        if not self.effort or not field_name:
            return {}
        if field_name == "output_config":
            return {"output_config": {"effort": self.effort}}
        if field_name == "reasoning":
            return {"reasoning": {"effort": self.effort}}
        return {"reasoning_effort": self.effort}

    def url(self) -> str:
        base = self.base_url.rstrip("/")
        path = "/messages" if self.api == "anthropic" else "/chat/completions"
        return base if base.endswith(path) else base + path

    def auth_headers(self) -> dict[str, str]:
        key = self.key()
        if self.api == "anthropic":
            # API keys go in x-api-key; OAuth / session tokens are bearer tokens,
            # and a Claude subscription OAuth token also needs the oauth beta
            auth = {"x-api-key": key} if key.startswith("sk-ant-api") else {}
            if key and not auth:
                auth = {"Authorization": f"Bearer {key}"}
            if key.startswith("sk-ant-oat"):
                auth["anthropic-beta"] = ANTHROPIC_OAUTH_BETA
            return {"anthropic-version": ANTHROPIC_VERSION, **auth}
        return {"Authorization": f"Bearer {key}"} if key else {}

    def key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        return ""

    def secrets(self) -> list[str]:
        """The API key, credential-looking header values (x-api-key, …) and any
        password embedded in the base URL."""
        values = [self.key()]
        for name, value in self.headers.items():
            if isinstance(value, str) and any(h in name.lower() for h in SECRET_HEADER_HINTS):
                values.append(value)
                values.append(value.removeprefix("Bearer ").strip())
        try:
            values.append(urlparse(self.base_url).password or "")
        except ValueError:
            pass
        return [v for v in values if v]

    def needs_key(self) -> bool:
        """True when a real request would go out unauthenticated to a remote host."""
        if self.is_cli:
            return False  # the CLI brings its own sign-in; doctor checks it
        return not self.key() and not is_local(self.base_url)

    def note(self) -> str:
        pin = self.extra_body.get("provider")
        if isinstance(pin, dict):
            only = pin.get("only") or pin.get("order")
            if only:
                return f"pinned:{','.join(only)}"
        return ""


@dataclass
class RunConfig:
    targets: list[Target]
    prompts: dict[str, dict[str, Any]]
    reps: int = 7
    concurrency: list[int] = field(default_factory=lambda: [1])
    warmup: int = 2
    timeout_s: float = 180.0
    # None = each API's default (0 for OpenAI-compatible, omitted for the Claude
    # API, which rejects it); "omit" = never send it; a number = always send it
    temperature: float | str | None = None
    max_tokens_field: str = "max_tokens"
    stream_options: bool = True
    baseline: str | None = None
    # every prompt that may be selected by name (built-ins + [prompts.*])
    available_prompts: dict[str, dict[str, Any]] = field(default_factory=dict)


def validate(cfg: RunConfig, where: str = "config") -> None:
    """Reject runs that would measure nothing or crash halfway through."""
    if cfg.reps < 1:
        raise UsageError(f"{where}: reps must be at least 1 (got {cfg.reps})")
    if not cfg.concurrency or any(c < 1 for c in cfg.concurrency):
        raise UsageError(
            f"{where}: concurrency levels must be integers ≥ 1 (got {cfg.concurrency})"
        )
    if cfg.warmup < 0:
        raise UsageError(f"{where}: warmup cannot be negative")
    if not math.isfinite(cfg.timeout_s) or cfg.timeout_s <= 0:
        raise UsageError(f"{where}: timeout_s must be a positive number of seconds")
    cfg.temperature = parse_temperature(cfg.temperature, where)
    if not cfg.prompts:
        raise UsageError(f"{where}: no prompts selected")
    for name, spec in cfg.prompts.items():
        try:
            capped = int(spec.get("max_tokens", 256)) >= 1
        except (TypeError, ValueError):
            capped = False
        if not capped:
            raise UsageError(f"{where}: prompt {name!r}: max_tokens must be an integer ≥ 1")
    names = [t.name for t in cfg.targets]
    if cfg.baseline and cfg.baseline not in names:
        raise UsageError(
            f"{where}: baseline {cfg.baseline!r} is not a target name; have {', '.join(names)}"
        )


def parse_temperature(value: Any, where: str) -> float | str | None:
    if value is None or value == "omit":
        return value
    if isinstance(value, str) and value.strip().lower() in ("omit", "none"):
        return "omit"
    try:
        number = float(value) if not isinstance(value, bool) else -1.0
    except (TypeError, ValueError):
        number = -1.0
    if not math.isfinite(number) or number < 0:
        raise UsageError(f'{where}: temperature must be a number ≥ 0, or "omit" (got {value!r})')
    return number


def request_temperature(cfg: RunConfig, target: Target) -> float | None:
    """The temperature to send this target, or None to leave the field out.

    Claude 4.7+ rejects any sampling parameter, so the Claude API default is
    to omit it; OpenAI-compatible targets default to 0 for comparability.
    """
    if cfg.temperature == "omit":
        return None
    if target.is_cli:
        return None  # the CLI exposes no sampling parameters
    if cfg.temperature is None:
        return None if target.api == "anthropic" else 0.0
    return float(cfg.temperature)


def sent_temperature(cfg: RunConfig, target: Target) -> Any:
    """What a request to this target actually carries: extra_body is applied
    last and may set (or null) temperature itself."""
    if "temperature" in target.extra_body:
        return target.extra_body["temperature"]
    return request_temperature(cfg, target)


def _table(entry: dict[str, Any], key: str, label: str, path: Path) -> dict[str, Any]:
    value = entry.get(key) or {}
    if not isinstance(value, dict):
        raise UsageError(f"{path}: {label}: `{key}` must be a table, e.g. {key} = {{ … }}")
    return value


def _price(value: Any, key: str, label: str, path: Path) -> float | None:
    """USD per 1M tokens; a quoted number is accepted, anything else is an error
    now rather than a crash after every request has been sent."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise UsageError(f"{path}: {label}: `{key}` must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(f"{path}: {label}: `{key}` must be a number, got {value!r}") from None
    if not math.isfinite(number):  # TOML allows inf/nan; JSON output doesn't
        raise UsageError(f"{path}: {label}: `{key}` must be a finite number, got {value!r}")
    return number


GRID_ONLY = ("models", "effort", "prices")


def expand_grids(grids: list[dict[str, Any]], path: Path) -> list[dict[str, Any]]:
    """`[[grids]]` → one target per model × effort, interleaved like any other.

    Every other key (api, base_url, api_key_env, price, cli, extra_body, …) is
    shared by the grid's targets; `prices` overrides `price` per model.
    `effort = ["default", "high"]` includes the model's own default.
    """
    out: list[dict[str, Any]] = []
    for index, grid in enumerate(grids, start=1):
        if not isinstance(grid, dict):
            raise UsageError(f"{path}: [[grids]] #{index} must be a table")
        name = grid.get("name")
        if not name:
            raise UsageError(f"{path}: [[grids]] #{index} is missing `name` (the targets' prefix)")
        models = grid.get("models", grid.get("model"))
        models = [models] if isinstance(models, str) else list(models or [])
        efforts = grid.get("effort")
        efforts = [efforts] if efforts is None or isinstance(efforts, str) else list(efforts)
        efforts = [e.lower() if isinstance(e, str) else e for e in efforts]
        if not models or not efforts or not all(isinstance(m, str) and m for m in models):
            raise UsageError(
                f"{path}: grid {name!r}: `models` and `effort` need at least one value"
            )
        if len(set(models)) < len(models) or len(set(map(str, efforts))) < len(efforts):
            raise UsageError(f"{path}: grid {name!r}: `models` and `effort` must not repeat")
        prices = grid.get("prices") or {}
        if not isinstance(prices, dict) or any(m not in models for m in prices):
            raise UsageError(f"{path}: grid {name!r}: `prices` keys must be models in the grid")
        shared = {k: v for k, v in grid.items() if k not in (*GRID_ONLY, "name", "model")}
        for model in models:
            for effort in efforts:
                level = None if effort in (None, "default") else str(effort)
                entry = dict(shared, model=model, effort=level)
                entry["name"] = f"{name}/{model}" + (f"@{level}" if level else "")
                if model in prices:
                    entry["price"] = prices[model]
                out.append(entry)
    return out


def load_config(path: Path) -> RunConfig:
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: invalid TOML — {exc}") from exc
    return config_from_dict(raw, path)


def config_from_text(text: str, label: str) -> RunConfig:
    """A config built in memory (`--base-url` mode), validated like a file."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{label}: can't build a valid config from these flags — {exc}") from exc
    return config_from_dict(raw, Path(label))


def config_from_dict(raw: dict[str, Any], path: Path) -> RunConfig:
    run = raw.get("run", {}) or {}
    target_dicts = list(raw.get("targets") or []) + expand_grids(raw.get("grids") or [], path)
    if not target_dicts:
        raise UsageError(f"{path}: no [[targets]] or [[grids]] defined")

    targets: list[Target] = []
    for index, entry in enumerate(target_dicts, start=1):
        label = entry.get("name") or f"target #{index}"
        api = str(entry.get("api", "openai"))
        if api not in APIS:
            raise UsageError(f"{path}: {label}: `api` must be one of {', '.join(APIS)}")
        # a CLI target has no URL: the vendor's client decides where requests go
        for key in ("model",) if api in CLI_APIS else ("base_url", "model"):
            if not entry.get(key):
                raise UsageError(f"{path}: {label} is missing `{key}`")
        price = _table(entry, "price", label, path)
        effort = entry.get("effort")
        if effort is not None:
            effort = str(effort).lower()
            allowed = clis.backend(api).efforts if api in CLI_APIS else EFFORTS_HTTP
            if effort not in allowed:
                raise UsageError(f"{path}: {label}: `effort` must be one of {', '.join(allowed)}")
        if api in CLI_APIS:
            unsupported = sorted(
                k
                for k in ("api_key", "api_key_env", "headers", "extra_body", "max_tokens_field")
                if entry.get(k)
            )
            if unsupported:
                raise UsageError(
                    f"{path}: {label}: {', '.join(unsupported)} can't apply to api = {api!r} — "
                    "the CLI uses its own sign-in and request format"
                )
        elif entry.get("cli"):
            raise UsageError(
                f"{path}: {label}: `cli` only applies to {', '.join(CLI_APIS)} targets"
            )
        extra_body = _table(entry, "extra_body", label, path)
        # the token-cap field can be renamed per target or for the whole run
        cap_fields = {entry.get("max_tokens_field"), run.get("max_tokens_field")}
        reserved = sorted(k for k in extra_body if k in RESERVED_BODY or k in cap_fields)
        if reserved:
            raise UsageError(
                f"{path}: {label}: extra_body can't set {', '.join(reserved)} — tps sends "
                "those itself (token caps: a prompt's max_tokens, --max-tokens, or "
                "max_tokens_field for the field name)"
            )
        clash = sorted(k for k in extra_body if k in EFFORT_FIELDS.values())
        if effort and clash:
            raise UsageError(
                f"{path}: {label}: `effort` and extra_body.{clash[0]} both set reasoning "
                "effort — keep one (`effort` is sent in each API's own format)"
            )
        targets.append(
            Target(
                name=str(entry.get("name") or entry["model"]),
                base_url=str(entry.get("base_url") or ""),
                model=entry["model"],
                api_key_env=entry.get("api_key_env"),
                api_key=entry.get("api_key"),
                headers=_table(entry, "headers", label, path),
                extra_body=extra_body,
                price_in=_price(price.get("in_per_1m"), "price.in_per_1m", label, path),
                price_out=_price(price.get("out_per_1m"), "price.out_per_1m", label, path),
                max_tokens_field=entry.get("max_tokens_field"),
                send_stream_options=bool(entry.get("stream_options", True)),
                mock=_table(entry, "mock", label, path) if "mock" in entry else None,
                api=api,
                effort=effort,
                cli=str(entry["cli"]) if entry.get("cli") else None,
            )
        )

    names = [t.name for t in targets]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise UsageError(
            f"{path}: duplicate target name(s): {', '.join(dupes)} — every statistic is "
            "keyed by name, so give each target a unique `name`"
        )

    available = {name: dict(spec) for name, spec in DEFAULT_PROMPTS.items()}
    prompt_table = raw.get("prompts", {}) or {}
    for name, spec in prompt_table.items():
        merged = dict(available.get(name, {}))
        merged.update(spec)
        merged.setdefault("max_tokens", 256)
        source = merged.get("document_file")
        if source and not Path(source).expanduser().is_absolute():
            # relative to the config file, so `-c elsewhere/targets.toml` works
            merged["document_file"] = str((Path(path).parent / source).resolve())
        available[name] = merged

    selected = run.get("prompts") or list(prompt_table) or list(DEFAULT_PROMPTS)[:2]
    if isinstance(selected, str):
        selected = [selected]
    missing = [n for n in selected if n not in available]
    if missing:
        raise UsageError(f"{path}: unknown prompt(s) {missing}; have {sorted(available)}")

    try:
        conc = run.get("concurrency", [1])
        if isinstance(conc, int):
            conc = [conc]
        cfg = RunConfig(
            targets=targets,
            prompts={n: available[n] for n in selected},
            reps=int(run.get("reps", 7)),
            concurrency=[int(c) for c in conc],
            warmup=int(run.get("warmup", 2)),
            timeout_s=float(run.get("timeout_s", 180)),
            temperature=parse_temperature(run.get("temperature"), str(path)),
            max_tokens_field=run.get("max_tokens_field", "max_tokens"),
            stream_options=bool(run.get("stream_options", True)),
            baseline=run.get("baseline"),
            available_prompts=available,
        )
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{path}: [run] has a value of the wrong type — {exc}") from exc
    validate(cfg, str(path))
    return cfg


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


@dataclass
class Sample:
    target: str
    prompt: str
    concurrency: int
    batch: int
    ok: bool
    status: int | None = None
    error: str | None = None
    ttft_s: float | None = None
    first_content_s: float | None = None
    e2e_s: float | None = None
    decode_s: float | None = None
    out_tokens: int | None = None
    in_tokens: int | None = None
    out_tps: float | None = None
    itl_p50_ms: float | None = None
    itl_p90_ms: float | None = None
    # A median gap far below the mean means chunks arrived in bursts (a proxy
    # flushing several at once): the ITL then measures delivery, not the model.
    itl_mean_ms: float | None = None
    itl_pace_ms: float | None = None  # mean gap without the longest few (stalls)
    tokens_per_chunk: float | None = None
    chunks: int = 0
    count_source: str = "usage"
    served_by: str | None = None
    batch_wall_s: float | None = None
    wall_clock: str = ""
    phase: str = "measure"  # "warmup" rows are discarded by every statistic
    finish_reason: str | None = None  # "length" = stopped by max_tokens
    reasoning_tokens: int | None = None  # from usage: OpenAI or Claude token details
    hidden_reasoning_tokens: int = 0  # billed and generated, but never streamed
    # CLI targets: process start → first stream event (client startup + the
    # request reaching the API). TTFT for CLI targets includes this.
    startup_s: float | None = None
    # CLI targets: share of the plan's usage window used, as the client reports
    # it after this request (0–1), and which window ("five_hour")
    plan_utilization: float | None = None
    plan_window: str | None = None
    # the model id the API says served the request ("opus" → "claude-opus-5-5")
    served_model: str | None = None
    # CLI targets: model calls the client made for this request (Codex can make
    # a hidden one before the call that streams the answer)
    api_calls: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def percentile(values: list[float | None], q: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def p50(values: list[float | None]) -> float | None:
    return percentile(values, 0.5)


def p90(values: list[float | None], higher_is_better: bool = False) -> float | None:
    """The tail. For latencies that's the 90th percentile; for rates (where
    higher is better) it's the 10th — the rate 90% of requests met or beat."""
    return percentile(values, 0.1 if higher_is_better else 0.9)


def median(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return statistics.median(xs) if xs else None


def _first_present(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


async def stream_once(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
    send_stream_options: bool,
) -> Sample:
    wants_usage = send_stream_options and target.api == "openai"
    extra = {"stream_options": {"include_usage": True}} if wants_usage else None
    body = request_body(target, cfg, prompt_text, max_tokens, stream=True, extra=extra)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    headers.update(target.auth_headers())
    headers.update(target.headers)

    sample = Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=False,
        wall_clock=now_iso(),
    )

    t0 = time.perf_counter()
    first = last = None
    gaps: list[float] = []
    chars = 0  # content + reasoning: the chars/4 fallback and first-chunk share
    reasoning_chars = 0
    first_chars = 0
    usage: dict[str, Any] = {}
    served: str | None = None
    stream_error: str | None = None
    stopped = False  # Claude ends every complete stream with message_stop

    try:
        async with client.stream("POST", target.url(), json=body, headers=headers) as response:
            sample.status = response.status_code
            if response.status_code >= 400:
                raw = await response.aread()
                snippet = redact(raw.decode("utf-8", "replace"), target.secrets())[:600].strip()
                # Some OpenAI-compatible servers reject stream_options outright.
                # That is a compatibility issue, not a performance result: resend
                # without it, and remember so later requests skip the doomed one.
                if (
                    send_stream_options
                    and response.status_code in (400, 422)
                    and "stream_options" in snippet
                ):
                    target.send_stream_options = False
                    return await stream_once(
                        client,
                        target,
                        prompt_text,
                        prompt_name,
                        max_tokens,
                        cfg,
                        concurrency,
                        batch,
                        False,
                    )
                sample.error = f"http_{response.status_code}: {snippet}"
                if response.status_code == 429:
                    sample.error = "rate_limited: " + snippet
                sample.e2e_s = time.perf_counter() - t0
                return sample

            served = response.headers.get("x-openrouter-provider") or response.headers.get(
                "x-provider"
            )

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    # gateways report mid-stream upstream failures in-band
                    err = obj["error"]
                    message = err.get("message") if isinstance(err, dict) else err
                    stream_error = redact(f"stream_error: {message}", target.secrets())[:300]
                    break
                event = parse_event(obj, target.api)
                stopped = stopped or event["stop"]
                usage.update(event["usage"])
                if event["provider"] and not served:
                    served = event["provider"]
                if event["finish"]:
                    sample.finish_reason = event["finish"]
                piece, reasoning = event["piece"], event["reasoning"]
                if not piece and not reasoning:
                    continue
                now = time.perf_counter()
                size = len(piece) + len(reasoning)
                if first is None:
                    first = now
                    first_chars = size
                    sample.ttft_s = now - t0
                elif piece and last is not None:
                    gaps.append(now - last)
                if piece and sample.first_content_s is None:
                    sample.first_content_s = now - t0
                last = now
                sample.chunks += 1
                chars += size
                reasoning_chars += len(reasoning)

    except httpx.TimeoutException:
        sample.error = "timeout"
        sample.e2e_s = time.perf_counter() - t0
        return sample
    except (httpx.HTTPError, OSError) as exc:
        sample.error = redact(f"transport: {type(exc).__name__}: {exc}", target.secrets())
        sample.e2e_s = time.perf_counter() - t0
        return sample

    sample.e2e_s = time.perf_counter() - t0
    sample.served_by = served
    if not stream_error and target.api == "anthropic" and not stopped:
        # cut off mid-stream (a proxy closing early): its counts can't be trusted
        stream_error = "incomplete_stream: ended without message_stop"
    if stream_error:
        sample.error = stream_error
        return sample
    return finish_sample(
        sample, usage, prompt_text, first, last, gaps, chars, reasoning_chars, first_chars
    )


def finish_sample(
    sample: Sample,
    usage: dict[str, Any],
    prompt_text: str,
    first: float | None,
    last: float | None,
    gaps: list[float],
    chars: int,
    reasoning_chars: int,
    first_chars: int,
) -> Sample:
    """Token counts, rates and the ok/error verdict for one complete stream.

    Shared by HTTP and CLI targets so both are counted and timed identically:
    `first`/`last` are perf_counter times of the first and last streamed text,
    `gaps` the seconds between answer chunks.
    """
    estimated = False
    if usage:
        sample.out_tokens = _first_present(
            usage.get("completion_tokens"), usage.get("output_tokens")
        )
        sample.in_tokens = _first_present(usage.get("prompt_tokens"), usage.get("input_tokens"))
        for key, name in (
            ("completion_tokens_details", "reasoning_tokens"),  # OpenAI-compatible
            ("output_tokens_details", "thinking_tokens"),  # Claude API
        ):
            details = usage.get(key)
            if isinstance(details, dict) and details.get(name) is not None:
                sample.reasoning_tokens = details[name]
    out_from_usage = sample.out_tokens is not None
    if sample.out_tokens is None and chars:
        sample.out_tokens = max(1, round(chars / 4))
        estimated = True
    if sample.in_tokens is None:
        # without it the cost columns would silently drop input spend
        sample.in_tokens = estimate_tokens(prompt_text)
        estimated = True
    sample.count_source = "chars/4" if estimated else "usage"

    # Reasoning that was counted in usage but never streamed (Claude's omitted
    # thinking, o-series reasoning) happened before the window opened: crediting
    # it to the window would inflate tok/s by the thinking share. When only a
    # summary of it streamed, the unstreamed remainder is hidden too — that
    # split rests on chars/4, so the rate is flagged as estimated.
    hidden = 0
    if out_from_usage and sample.reasoning_tokens:
        shown = round(reasoning_chars / 4)
        if not reasoning_chars:
            hidden = sample.reasoning_tokens
        elif sample.reasoning_tokens > 2 * shown:
            hidden = sample.reasoning_tokens - shown
            sample.count_source = "usage+chars/4"
    sample.hidden_reasoning_tokens = hidden
    streamed = (sample.out_tokens or 0) - hidden
    if first is not None and last is not None and last > first and streamed > 0:
        sample.decode_s = last - first
        # Tokens in the first chunk were generated before the window opened.
        # Estimate them by that chunk's share of the text, so providers that
        # send big chunks aren't credited with tokens they made "for free".
        first_tokens = max(1.0, streamed * first_chars / chars) if chars else 1.0
        if streamed > first_tokens:
            sample.out_tps = (streamed - first_tokens) / sample.decode_s
        if gaps:
            sample.itl_p50_ms = percentile(gaps, 0.5) * 1000
            sample.itl_p90_ms = percentile(gaps, 0.9) * 1000
            sample.itl_mean_ms = sum(gaps) / len(gaps) * 1000
            pace = itl_pace(gaps)
            sample.itl_pace_ms = None if pace is None else pace * 1000
    if sample.chunks and streamed > 0:
        sample.tokens_per_chunk = streamed / sample.chunks
    # A single chunk has no decode window: the rate is unknown, not infinite.

    finish = sample.finish_reason
    if finish in ("content_filter", "refusal"):
        # the provider declined; whatever streamed is not an answer to time
        sample.error = f"{finish}: finish_reason={finish}"
    elif sample.chunks:
        sample.ok = bool(sample.out_tokens)
    elif finish == "length" and sample.reasoning_tokens:
        # Nothing streamed, but the cap was spent on billed reasoning: a model
        # that only thought (Claude's hidden thinking) — a result, flagged as such.
        sample.ok = True
    elif finish or sample.out_tokens:
        sample.error = f"no_content: nothing streamed (finish_reason={finish})"
    if not sample.ok and not sample.error:
        sample.error = "empty_stream"
    return sample


def request_body(
    target: Target,
    cfg: RunConfig,
    prompt_text: str,
    max_tokens: int,
    stream: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON body for one request. `extra` holds the fields tps adds for
    the command (stream_options, logprobs); extra_body still wins over them."""
    body: dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": stream,
    }
    temperature = request_temperature(cfg, target)
    if temperature is not None:
        body["temperature"] = temperature
    if target.api == "anthropic":
        body["max_tokens"] = max_tokens
    else:
        body[target.max_tokens_field or cfg.max_tokens_field] = max_tokens
    # effort before `extra`: fingerprint turns reasoning off on top of it
    body.update(target.effort_body())
    body.update(extra or {})
    # extra_body last: it is how provider pinning / vendor flags get applied
    body.update(target.extra_body)
    return body


def parse_event(obj: dict[str, Any], api: str) -> dict[str, Any]:
    """One streamed JSON event, reduced to what the timing loop needs."""
    event: dict[str, Any] = {
        "usage": {},
        "provider": None,
        "finish": None,
        "piece": "",
        "reasoning": "",
        "stop": False,
    }
    if api == "anthropic":
        kind = obj.get("type")
        if kind == "message_start":
            usage = (obj.get("message") or {}).get("usage") or {}
            # output_tokens here is a placeholder (≈1); only message_delta's
            # count is final, so never let a cut-off stream keep it
            event["usage"] = {k: v for k, v in usage.items() if k != "output_tokens"}
        elif kind == "message_stop":
            event["stop"] = True
        elif kind == "message_delta":
            event["usage"] = obj.get("usage") or {}
            stop = (obj.get("delta") or {}).get("stop_reason")
            event["finish"] = ANTHROPIC_STOP.get(stop, stop)
        elif kind == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                event["piece"] = delta.get("text") or ""
            elif delta.get("type") == "thinking_delta":
                event["reasoning"] = delta.get("thinking") or ""  # "" when omitted
    else:
        if isinstance(obj.get("usage"), dict):
            event["usage"] = obj["usage"]
        if obj.get("provider"):
            event["provider"] = str(obj["provider"])
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason"):
                event["finish"] = str(choices[0]["finish_reason"])
            delta = choices[0].get("delta") or {}
            event["piece"] = delta.get("content") or ""
            event["reasoning"] = delta.get("reasoning_content") or delta.get("reasoning") or ""
    for key in ("piece", "reasoning"):
        if not isinstance(event[key], str):
            event[key] = ""
    return event


# --------------------------------------------------------------------------
# CLI targets: a vendor's own client, run headless
# --------------------------------------------------------------------------


def cli_workdir() -> str:
    """An empty directory to run clients in, so no project files (CLAUDE.md,
    AGENTS.md, .claude/, …) load. Stable across runs: clients key per-project
    state on the working directory, and a fresh temp dir would add an entry
    every run."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "tps", "cli-workdir")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def cli_once(
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
) -> Sample:
    """One request through a CLI target, timed like an HTTP stream.

    The backend (clis.py) turns each line of the client's output into a
    CliEvent; this loop times them exactly as HTTP streams are timed and counts
    them with the same finish_sample(). TTFT runs from process start, so it
    includes client startup; `startup_s` says how much of it that was.

    Only the first API call is measured: the client is stopped at the end of
    the first response (immediately when it would continue a capped one), so
    the cap is a real per-request cap and no extra plan usage is spent.
    """
    backend = target.backend
    session = backend.session(prompt_text, target.model, target.effort)
    sample = Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=False,
        wall_clock=now_iso(),
    )
    first = last = None
    gaps: list[float] = []
    chars = reasoning_chars = first_chars = 0
    usage: dict[str, Any] = {}
    failure: clis.CliEvent | None = None
    stopped = False
    plan_rejected = False

    t0 = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *backend.argv(target.cli_path() or target.cli_binary(), target.model, target.effort),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=backend.env(max_tokens),
            cwd=cli_workdir(),
            limit=1 << 24,  # one JSON line can carry a whole message
        )
    except OSError as exc:
        sample.error = f"cli_missing: {target.cli_binary()}: {exc.strerror or exc}"
        sample.e2e_s = time.perf_counter() - t0
        return sample
    assert proc.stdin and proc.stdout and proc.stderr
    stdin, stdout = proc.stdin, proc.stdout
    stderr_task = asyncio.create_task(proc.stderr.read())

    def read(raw: bytes) -> clis.CliEvent | None:
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        return session.parse(obj) if isinstance(obj, dict) else None

    async def write(lines: list[str] | list[dict[str, Any]]) -> None:
        for line in lines:
            text = line if isinstance(line, str) else json.dumps(line) + "\n"
            stdin.write(text.encode("utf-8"))
        await stdin.drain()

    def note_plan(event: clis.CliEvent) -> None:
        nonlocal plan_rejected
        plan_rejected = plan_rejected or event.plan_rejected
        if event.plan_utilization is not None:
            sample.plan_utilization, sample.plan_window = event.plan_utilization, event.plan_window

    async def consume() -> None:
        nonlocal first, last, chars, reasoning_chars, first_chars, failure, stopped
        await write(session.opening())
        if session.close_stdin:
            stdin.close()
        async for raw in stdout:
            event = read(raw)
            if event is None:
                continue
            note_plan(event)
            if event.send and not stdin.is_closing():
                await write(event.send)
            if event.model:
                sample.served_model = event.model
            if event.api_call:
                sample.api_calls = (sample.api_calls or 0) + 1
            if event.error:
                failure = event
                if event.final:
                    return  # the client reported a failure: nothing more to time
            if not event.stream:
                continue
            if stopped:
                return  # a second API call (a continuation): never measured
            now = time.perf_counter()
            if sample.startup_s is None:
                sample.startup_s = now - t0
            usage.update(event.usage)
            if event.finish:
                sample.finish_reason = event.finish
            if event.stop:
                # the response is complete: the measurement ends here
                stopped = True
                sample.e2e_s = now - t0
                if event.final:
                    return  # the client won't exit on its own: stop it now
                if sample.finish_reason == "length" and backend.continues_after_cap:
                    # a continuation request follows: take a plan reading if it
                    # is the very next line, then stop the client
                    with contextlib.suppress(TimeoutError):
                        follow = read(await asyncio.wait_for(stdout.readline(), 0.25))
                        if follow is not None and not follow.stream:
                            note_plan(follow)
                    return
                continue  # read on, untimed, for the plan's usage reading
            piece, reasoning = event.piece, event.reasoning
            if not piece and not reasoning:
                continue
            size = len(piece) + len(reasoning)
            if first is None:
                first = now
                first_chars = size
                sample.ttft_s = now - t0
            elif piece and last is not None:
                gaps.append(now - last)
            if piece and sample.first_content_s is None:
                sample.first_content_s = now - t0
            last = now
            sample.chunks += 1
            chars += size
            reasoning_chars += len(reasoning)

    try:
        try:
            await asyncio.wait_for(consume(), cfg.timeout_s)
        except TimeoutError:
            # a slow exit after a complete answer isn't the model's failure:
            # only an unfinished first response is a timeout
            if not stopped:
                sample.error = "timeout"
        except (OSError, ValueError) as exc:  # broken pipe, oversized line
            sample.error = f"cli_io: {type(exc).__name__}: {exc}"[:300]
        if sample.e2e_s is None or sample.error:
            sample.e2e_s = time.perf_counter() - t0
        if stopped or sample.error or failure is not None or not session.close_stdin:
            # before any continuation request goes out — and a client that
            # reported a failure, or that never exits on its own, is stopped too
            await _stop(proc)
        code = await proc.wait()
        stderr = (await stderr_task).decode("utf-8", "replace").strip()
    finally:
        # Ctrl-C / cancellation: never leave a headless client running on the plan
        if proc.returncode is None:
            await asyncio.shield(_stop(proc))
        if not stderr_task.done():
            stderr_task.cancel()
    if sample.error:
        return sample
    sample.served_by = target.api

    if not stopped:
        if failure is not None:
            message = failure.error or "error"
            sample.status = failure.status
            limited = failure.status == 429 or plan_rejected or "rate limit" in message.lower()
            sample.error = ("rate_limited: " if limited else "cli_error: ") + message[:300]
        elif plan_rejected:
            sample.error = "rate_limited: plan limit reached (the client's rate-limit event)"
        elif code != 0:
            sample.error = f"cli_exit_{code}: {stderr[-300:] or 'no output'}"
        else:
            sample.error = "incomplete_stream: the response never finished"
        return sample
    return finish_sample(
        sample, usage, prompt_text, first, last, gaps, chars, reasoning_chars, first_chars
    )


async def run_target_batch(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
    mock: bool = False,
) -> list[Sample]:
    if mock:
        samples = [
            mock_sample(target, prompt_name, prompt_text, concurrency, batch, max_tokens, i)
            for i in range(concurrency)
        ]
        wall = max(s.e2e_s or 0.0 for s in samples)
        for sample in samples:
            sample.batch_wall_s = wall
        return samples

    async def one() -> Sample:
        if target.is_cli:
            return await cli_once(
                target, prompt_text, prompt_name, max_tokens, cfg, concurrency, batch
            )
        return await stream_once(
            client,
            target,
            prompt_text,
            prompt_name,
            max_tokens,
            cfg,
            concurrency,
            batch,
            target.send_stream_options and cfg.stream_options,
        )

    started = time.perf_counter()
    samples = await asyncio.gather(*(one() for _ in range(concurrency)))
    wall = time.perf_counter() - started
    for sample in samples:
        sample.batch_wall_s = wall
    return list(samples)


def mock_sample(
    target: Target,
    prompt_name: str,
    prompt_text: str,
    concurrency: int,
    batch: int,
    max_tokens: int,
    index: int = 0,
) -> Sample:
    """Synthesise a plausible sample. No network, no sleeping."""
    import random

    prof = target.mock or {}
    rng = random.Random(f"{target.name}:{prompt_name}:{concurrency}:{batch}:{index}")
    ttft = rng.uniform(*prof.get("ttft", (0.35, 0.8)))
    itl = rng.uniform(*prof.get("itl", (0.012, 0.028)))
    tokens = rng.randint(int(max_tokens * 0.85), max_tokens)
    # concurrency degrades per-stream ITL and queues the first token, as real
    # serving stacks do; a lower `scaling` makes a provider that saturates early
    scaling = float(prof.get("scaling", 0.94))
    itl *= 1 + (1 - scaling) * (concurrency - 1)
    ttft *= 1 + 0.04 * (concurrency - 1)
    # higher effort thinks longer before the first visible token (hidden)
    level = EFFORTS_HTTP.index(target.effort) if target.effort in EFFORTS_HTTP else None
    thinking = round(30 * 2.2**level * rng.uniform(0.85, 1.15)) if level is not None else 0
    ttft += thinking * itl * 0.9
    decode = itl * (tokens - 1)
    sample = Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=True,
        status=200,
        ttft_s=ttft,
        first_content_s=ttft,
        e2e_s=ttft + decode,
        decode_s=decode,
        out_tokens=tokens,
        in_tokens=estimate_tokens(prompt_text),
        out_tps=(tokens - 1) / decode,
        itl_p50_ms=itl * 1000,
        itl_p90_ms=itl * 1000 * 1.4,
        itl_mean_ms=itl * 1000 * 1.1,  # a steady stream: one token per chunk
        itl_pace_ms=itl * 1000 * 1.05,
        tokens_per_chunk=1.0,
        chunks=tokens,
        count_source="mock",
        served_by=target.note() or "mock",
        batch_wall_s=ttft + decode,
        wall_clock=now_iso(),
        finish_reason="length" if tokens == max_tokens else "stop",
    )
    if thinking:
        sample.reasoning_tokens = sample.hidden_reasoning_tokens = thinking
    if target.is_cli:
        # a CLI's TTFT includes its own startup, as real CLI rows report
        sample.startup_s = min(ttft * 0.8, rng.uniform(*prof.get("startup", (1.5, 2.1))))
        sample.served_by = target.api
    return sample


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def rotate(items: list[Any], n: int) -> list[Any]:
    if not items:
        return items
    n %= len(items)
    return items[n:] + items[:n]


# Warm-ups only open connections and warm the provider's prompt cache, and are
# discarded; generating a full-length answer for them would just cost money.
WARMUP_MAX_TOKENS = 16
WARMUP_UNCAPPED_PROMPT = "Reply with one word: ready"


def planned_requests(cfg: RunConfig) -> int:
    """targets × prompts × reps × Σ(concurrency), plus warm-ups."""
    measured = len(cfg.targets) * len(cfg.prompts) * cfg.reps * sum(cfg.concurrency)
    return measured + len(cfg.targets) * cfg.warmup


# `--dry-run` turns requests into minutes at an assumed per-stream rate. Real
# rates differ 10x between providers: the result is a budget, not a forecast.
PLAN_TOK_S = 40.0
PLAN_TTFT_S = 1.0
# chat-template tokens a provider wraps around one user message; a provider
# that injects its own system prompt can exceed this, and nothing can see that
PLAN_TEMPLATE_TOKENS = 32


def max_prompt_tokens(text: str) -> int:
    """An upper bound on a prompt's input tokens, whatever the tokenizer.

    Every BPE/SentencePiece token covers at least one UTF-8 byte, so a prompt
    can't tokenize to more tokens than it has bytes. chars/4 is the typical
    count for English but undercounts CJK text or dense code up to 4x.
    """
    return len(text.encode("utf-8")) + PLAN_TEMPLATE_TOKENS


def plan_run(cfg: RunConfig, mock: bool = False) -> dict[str, Any]:
    """What `execute` would send — counted the same way, nothing sent.

    `cost_max_usd` is a true upper bound: output at the max_tokens cap, input
    at `max_prompt_tokens`. `in_tokens_est` (chars/4) is the typical count.
    """
    warm_spec = next(iter(cfg.prompts.values()))
    warm_out = min(int(warm_spec.get("max_tokens", 256)), WARMUP_MAX_TOKENS)
    warm_text = build_prompt(warm_spec)
    measured_per_target = cfg.reps * sum(cfg.concurrency) * len(cfg.prompts)
    in_tokens = cfg.warmup * estimate_tokens(warm_text)
    in_tokens_max = cfg.warmup * max_prompt_tokens(warm_text)
    out_tokens = cfg.warmup * warm_out
    seconds = cfg.warmup * (PLAN_TTFT_S + warm_out / PLAN_TOK_S)
    for spec in cfg.prompts.values():
        cap = int(spec.get("max_tokens", 256))
        requests = cfg.reps * sum(cfg.concurrency)
        text = build_prompt(spec)
        in_tokens += requests * estimate_tokens(text)
        in_tokens_max += requests * max_prompt_tokens(text)
        out_tokens += requests * cap
        # batches run one after another; a concurrent batch lasts ~one request
        seconds += cfg.reps * len(cfg.concurrency) * (PLAN_TTFT_S + cap / PLAN_TOK_S)

    targets = []
    for target in cfg.targets:
        priced = target.price_in is not None and target.price_out is not None
        cost = (
            (in_tokens_max * target.price_in + out_tokens * target.price_out) / 1e6
            if priced
            else None
        )
        targets.append(
            {
                "target": target.name,
                "requests": measured_per_target + cfg.warmup,
                "cost_max_usd": 0.0 if mock else cost,
            }
        )
    costs = [t["cost_max_usd"] for t in targets if t["cost_max_usd"] is not None]
    # each CLI request (or concurrent batch) also pays for a process start
    batches = cfg.warmup + cfg.reps * len(cfg.concurrency) * len(cfg.prompts)
    cli_seconds = sum(1 for t in cfg.targets if t.is_cli) * batches * PLAN_CLI_STARTUP_S
    return {
        "requests": planned_requests(cfg),
        "measured_requests": measured_per_target * len(cfg.targets),
        "warmup_requests": cfg.warmup * len(cfg.targets),
        "in_tokens_est": in_tokens * len(cfg.targets),
        "in_tokens_max": in_tokens_max * len(cfg.targets),
        "out_tokens_max": out_tokens * len(cfg.targets),
        "cost_max_usd": sum(costs) if costs else None,
        "unpriced": [t["target"] for t in targets if t["cost_max_usd"] is None],
        "est_minutes": round((seconds * len(cfg.targets) + cli_seconds) / 60, 1),
        "assumed": {
            "tok_s": PLAN_TOK_S,
            "ttft_s": PLAN_TTFT_S,
            "template_tokens": PLAN_TEMPLATE_TOKENS,
            "cli_startup_s": PLAN_CLI_STARTUP_S,
        },
        "targets": targets,
    }


def run_meta(cfg: RunConfig, mock: bool) -> dict[str, Any]:
    return {
        "created": now_iso(),
        "tps_version": __version__,
        "mock": mock,
        "complete": False,
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "warmup": cfg.warmup,
        "temperature": cfg.temperature,
        "max_tokens": {
            name: int(spec.get("max_tokens", 256)) for name, spec in cfg.prompts.items()
        },
        "timeout_s": cfg.timeout_s,
        "targets": [
            {
                "name": t.name,
                "model": t.model,
                "base_url": redact_url(t.base_url),  # meta, JSON and report are shared
                "pin": t.extra_body.get("provider"),
                "price_in": t.price_in,
                "price_out": t.price_out,
                "api": t.api,
                "access": "cli" if t.is_cli else "http",
                "effort": t.effort,  # null = the model's default
                "plan": t.plan,  # CLI targets: the signed-in plan, when known
                "temperature": sent_temperature(cfg, t),  # null = not sent
            }
            for t in cfg.targets
        ],
        "prompts": list(cfg.prompts),
        "baseline": cfg.baseline,
        "http2": HTTP2,
        "planned_requests": planned_requests(cfg),  # includes the warm-ups
        "planned_warmup": len(cfg.targets) * cfg.warmup,
    }


async def execute(
    cfg: RunConfig, outdir: Path, mock: bool, on_batch: Callable[..., None] | None = None
) -> list[Sample]:
    """Run every batch, interleaved, streaming results to disk as they land.

    raw.jsonl is appended batch by batch and meta.json is written up front, so
    an interrupted run keeps everything measured so far (`tps report` renders
    it, flagged PARTIAL).
    """
    outdir.mkdir(parents=True, exist_ok=True)
    meta = run_meta(cfg, mock)
    meta_path = outdir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    samples: list[Sample] = []
    total = planned_requests(cfg)
    done = 0
    limits = httpx.Limits(max_connections=256, max_keepalive_connections=64)
    async with httpx.AsyncClient(
        http2=HTTP2,
        timeout=httpx.Timeout(cfg.timeout_s, connect=15.0),
        limits=limits,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        with open(outdir / "raw.jsonl", "w", encoding="utf-8") as raw:

            def record(batch_samples: list[Sample], **info: Any) -> None:
                nonlocal done
                for sample in batch_samples:
                    sample.phase = info["phase"]
                    raw.write(json.dumps(sample.as_dict()) + "\n")
                raw.flush()
                samples.extend(batch_samples)
                done += len(batch_samples)
                if on_batch is not None:
                    rates = [s.out_tps for s in batch_samples if s.out_tps]
                    on_batch(
                        done=done,
                        total=total,
                        ok=sum(1 for s in batch_samples if s.ok),
                        n=len(batch_samples),
                        tps=statistics.median(rates) if rates else None,
                        **info,
                    )

            # warm-up: establish TLS/HTTP2, let the provider's cache settle
            warm_name, warm_spec = next(iter(cfg.prompts.items()))
            warm_text = build_prompt(warm_spec)
            for target in cfg.targets:
                # a client that can't cap output would write a whole answer just
                # to warm up: give it a one-word prompt instead
                uncapped = target.is_cli and not target.backend.caps_output
                for _ in range(cfg.warmup):
                    record(
                        await run_target_batch(
                            client,
                            target,
                            WARMUP_UNCAPPED_PROMPT if uncapped else warm_text,
                            warm_name,
                            min(int(warm_spec.get("max_tokens", 256)), WARMUP_MAX_TOKENS),
                            cfg,
                            1,
                            -1,
                            mock,
                        ),
                        phase="warmup",
                        target=target.name,
                        prompt=warm_name,
                        concurrency=1,
                        rep=-1,
                    )

            for prompt_name, spec in cfg.prompts.items():
                prompt_text = build_prompt(spec)
                max_tokens = int(spec.get("max_tokens", 256))
                for concurrency in cfg.concurrency:
                    for rep in range(cfg.reps):
                        # rotate the order every rep so nobody always runs "first"
                        for target in rotate(cfg.targets, rep):
                            record(
                                await run_target_batch(
                                    client,
                                    target,
                                    prompt_text,
                                    prompt_name,
                                    max_tokens,
                                    cfg,
                                    concurrency,
                                    rep,
                                    mock,
                                ),
                                phase="measure",
                                target=target.name,
                                prompt=prompt_name,
                                concurrency=concurrency,
                                rep=rep,
                            )

    meta["complete"] = True
    meta["finished"] = now_iso()
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return samples


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def load_samples(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = run_dir / "raw.jsonl"
    if not raw.exists():
        raise UsageError(
            f"{run_dir} is not a tps run directory (no raw.jsonl) — "
            "pass the folder printed by `tps run`"
        )
    samples = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a run killed mid-write can leave one torn line
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if not meta.get("targets"):
        names = list(dict.fromkeys(s["target"] for s in samples))
        meta["targets"] = [{"name": n} for n in names]
    return samples, meta


def error_class(sample: dict[str, Any]) -> str:
    return (sample.get("error") or "unknown").split(":")[0]


def is_estimated(count_sources: list[str]) -> bool:
    """True when any token count behind a row came from chars/4."""
    return any("chars/4" in source for source in count_sources)


def is_warmup(sample: dict[str, Any]) -> bool:
    # runs recorded before `phase` existed mark warm-ups with batch -1 only
    return sample.get("phase") == "warmup" or sample.get("batch") == -1


def _bucket() -> dict[str, Any]:
    return {
        "ttft": [],
        "first_content": [],
        "e2e": [],
        "tps": [],
        "itl": [],
        "tpc": [],
        "in": [],
        "out": [],
        "startup": [],
        "calls": [],
        "batches": [],
        "ok": 0,
        "n": 0,
        "truncated": 0,
        "reasoning_only": 0,
        "bursty": 0,
        "hidden": [],
        "errors": {},
        "served": set(),
        "count_source": set(),
    }


_FIELDS = (
    ("ttft", "ttft_s"),
    ("first_content", "first_content_s"),
    ("e2e", "e2e_s"),
    ("tps", "out_tps"),
    ("itl", "itl_p50_ms"),
    ("tpc", "tokens_per_chunk"),
    ("in", "in_tokens"),
    ("out", "out_tokens"),
    ("startup", "startup_s"),
    ("calls", "api_calls"),
)

# A request whose median chunk gap is under this share of its typical gap got
# its chunks in bursts. Comparing the stream with itself needs no token counts.
BURST_RATIO = 0.2


def itl_pace(gaps: list[float]) -> float | None:
    """The mean chunk gap with the longest few left out.

    One long stall in a steady stream drags the plain mean up and would make
    an honest median look like a burst; clumped delivery has a long gap per
    clump, so dropping just the top 2% (at least two) keeps it visible.
    """
    drop = max(2, math.ceil(len(gaps) * 0.02))
    if len(gaps) <= drop + 2:
        return None  # too few gaps to tell a burst from a pause
    kept = sorted(gaps)[:-drop]
    return sum(kept) / len(kept)


def is_bursty(sample: dict[str, Any]) -> bool:
    p50_gap = sample.get("itl_p50_ms")
    # rows recorded before itl_pace_ms existed fall back to the plain mean
    pace = sample["itl_pace_ms"] if "itl_pace_ms" in sample else sample.get("itl_mean_ms")
    return bool(p50_gap is not None and pace and p50_gap < BURST_RATIO * pace)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Group measured samples by (prompt, target, concurrency).

    `per_stream` is the bucket at the *lowest* concurrency configured for each
    prompt (normally 1), because mixing c=1 and c=4 samples into one percentile
    is how people accidentally invent a 30% regression that never happened.
    `totals` counts every request for a (prompt, target), all concurrency
    levels included — that is the reliability signal.
    """
    buckets: dict[tuple[str, str, int], dict[str, Any]] = {}
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    batch_tokens: dict[tuple, float] = {}
    batch_wall: dict[tuple, float] = {}

    for s in samples:
        if is_warmup(s):
            continue
        key = (s["prompt"], s["target"], s["concurrency"])
        bucket = buckets.setdefault(key, _bucket())
        total = totals.setdefault(
            (s["prompt"], s["target"]),
            {"ok": 0, "n": 0, "truncated": 0, "reasoning_only": 0, "errors": {}},
        )
        bucket["n"] += 1
        total["n"] += 1
        bkey = (*key, s["batch"])
        batch_wall[bkey] = max(batch_wall.get(bkey, 0.0), s.get("batch_wall_s") or 0.0)
        if s.get("ok"):
            bucket["ok"] += 1
            total["ok"] += 1
            bucket["served"].add(s.get("served_by"))
            bucket["count_source"].add(s.get("count_source"))
            truncated = s.get("finish_reason") == "length"
            # every chunk was reasoning: there is no answer to time
            reasoning_only = s.get("first_content_s") is None
            bucket["truncated"] += truncated
            bucket["reasoning_only"] += reasoning_only
            # counted at every level, like ok/n, so `reasoning_only == ok` holds
            total["truncated"] += truncated
            total["reasoning_only"] += reasoning_only
            bucket["hidden"].append(s.get("hidden_reasoning_tokens") or 0)
            bucket["bursty"] += is_bursty(s)
            for name, source in _FIELDS:
                bucket[name].append(s.get(source))
            batch_tokens[bkey] = batch_tokens.get(bkey, 0) + (s.get("out_tokens") or 0)
        else:
            cls = error_class(s)
            bucket["errors"][cls] = bucket["errors"].get(cls, 0) + 1
            total["errors"][cls] = total["errors"].get(cls, 0) + 1

    # aggregate throughput per batch: sum of tokens / wall clock of the batch
    for bkey, tokens in batch_tokens.items():
        wall = batch_wall.get(bkey) or 0.0
        if wall > 0:
            buckets[bkey[:3]]["batches"].append(tokens / wall)

    min_conc: dict[str, int] = {}
    for prompt, _target, conc in buckets:
        min_conc[prompt] = min(min_conc.get(prompt, conc), conc)
    per_stream = {(p, t): b for (p, t, c), b in buckets.items() if c == min_conc[p]}
    return {"buckets": buckets, "per_stream": per_stream, "totals": totals, "min_conc": min_conc}


def bucket_stats(bucket: dict[str, Any]) -> dict[str, Any]:
    """The numbers every output (terminal, report, CSV, JSON) is built from."""
    return {
        "ttft_p50_s": p50(bucket["ttft"]),
        "ttft_p90_s": p90(bucket["ttft"]),
        "first_content_p50_s": p50(bucket["first_content"]),
        "out_tps_p50": p50(bucket["tps"]),
        "out_tps_p90": p90(bucket["tps"], higher_is_better=True),
        "itl_p50_ms": p50(bucket["itl"]),
        # how many samples each median actually came from: reasoning-only and
        # single-chunk responses have no first answer / no ITL to contribute
        "itl_n": sum(v is not None for v in bucket["itl"]),
        "first_content_n": sum(v is not None for v in bucket["first_content"]),
        "tokens_per_chunk_median": median(bucket["tpc"]),
        "bursty": bucket["bursty"],
        "e2e_p50_s": p50(bucket["e2e"]),
        "out_tokens_median": median(bucket["out"]),
        "in_tokens_median": median(bucket["in"]),
        # CLI targets only: client startup inside TTFT (None for HTTP targets)
        "startup_p50_s": p50(bucket["startup"]),
        # CLI targets: model calls per request (>1: the client made hidden ones)
        "api_calls_median": median(bucket["calls"]),
        "aggregate_tps": median(bucket["batches"]),
        "ok": bucket["ok"],
        "n": bucket["n"],
        "truncated": bucket["truncated"],
        "reasoning_only": bucket["reasoning_only"],
        "hidden_reasoning_tokens_median": median(bucket["hidden"]) or None,
        "errors": dict(bucket["errors"]),
        "count_source": sorted(x for x in bucket["count_source"] if x),
        "served_by": sorted(x for x in bucket["served"] if x),
    }


def reasoning_note(st: dict[str, Any]) -> str | None:
    """Why first-answer and ITL are empty: the model never got to an answer.

    Its TTFT and tok/s are still real, but they time the thinking — say so
    instead of printing a bare "—" that looks like a broken tool.
    """
    ok, only = st["ok"], st["reasoning_only"]
    if not only:
        return None
    if only < ok:
        return (
            f"{only}/{ok} responses were reasoning only (no answer text); first answer "
            f"is from {st['first_content_n']} and ITL from {st['itl_n']} of {ok}"
        )
    if st["truncated"] == ok:
        return (
            f"hit the token cap while reasoning ({ok}/{ok} finish_reason=length, no answer "
            "text) — any TTFT or tok/s here measured thinking, not answering; "
            "raise --max-tokens"
        )
    return (
        f"no answer text in {ok}/{ok} responses (reasoning only) — any TTFT or tok/s "
        "here measured thinking, not answering"
    )


def row_notes(st: dict[str, Any]) -> list[str]:
    """Everything a reader needs to interpret a row's numbers correctly."""
    notes = [note] if (note := reasoning_note(st)) else []
    hidden = st["hidden_reasoning_tokens_median"]
    if hidden and st["reasoning_only"] < st["ok"]:
        notes.append(
            f"TTFT includes ~{hidden:.0f} thinking tokens (median) that the API doesn't "
            "stream; tok/s counts only the streamed answer"
        )
    startup = st.get("startup_p50_s")
    if startup is not None:
        notes.append(
            f"CLI target: TTFT includes ~{startup:.1f}s of client startup (median, process "
            "start → first stream event) — don't rank it against HTTP TTFT; tok/s is unaffected"
        )
    calls = st.get("api_calls_median")
    if calls and calls > 1:
        notes.append(
            f"the client made {calls:g} model calls per request (median) before the answer "
            "finished — TTFT includes the earlier ones; tok/s is the answering call's"
        )
    if st["bursty"]:
        tpc = st["tokens_per_chunk_median"]
        notes.append(
            f"chunks arrived in bursts in {st['bursty']}/{st['itl_n']} responses (median gap "
            f"under {BURST_RATIO:.0%} of the typical gap) — ITL measures how the stream was "
            "flushed, not the model's pacing" + (f" ({tpc:.1f} tokens/chunk)" if tpc else "")
        )
    return notes


def request_cost(
    in_tokens: float | None,
    out_tokens: float | None,
    price_in: float | None,
    price_out: float | None,
) -> tuple[float | None, float | None]:
    """(USD per 1k requests, USD per 1M generated tokens incl. their prompts)."""
    if price_in is None or price_out is None or not out_tokens:
        return None, None
    per_request = ((in_tokens or 0) * price_in + out_tokens * price_out) / 1e6
    return per_request * 1000, per_request * (1e6 / out_tokens)


def spend(samples: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Estimated USD spent on the measured requests, from the configured prices.

    Failed requests aren't charged (there is no token count to bill) and warm-ups
    are excluded — `batch` is `-1` for those. `total` is None when no measured
    target carried both prices: that is *unknown*, not free.
    """
    prices = {t.get("name"): t for t in meta.get("targets", [])}
    by_target: dict[str, float] = {}
    by_prompt: dict[tuple[str, str], float] = {}
    by_level: dict[tuple[str, str, int], float] = {}
    unpriced: set[str] = set()
    for sample in samples:
        if sample.get("batch") == -1 or not sample.get("ok"):
            continue
        name = sample["target"]
        info = prices.get(name) or {}
        price_in, price_out = info.get("price_in"), info.get("price_out")
        if price_in is None or price_out is None:
            unpriced.add(name)
            continue
        amount = (
            (sample.get("in_tokens") or 0) * price_in + (sample.get("out_tokens") or 0) * price_out
        ) / 1e6
        by_target[name] = by_target.get(name, 0.0) + amount
        key = (sample["prompt"], name)
        by_prompt[key] = by_prompt.get(key, 0.0) + amount
        level = (*key, sample.get("concurrency"))
        by_level[level] = by_level.get(level, 0.0) + amount
    return {
        "total": sum(by_target.values()) if by_target else None,
        "plan": plan_usage(samples, meta),
        # plan-billed: unpriced by nature, not a missing price
        "plan_targets": sorted(
            t.get("name") for t in meta.get("targets", []) if t.get("access") == "cli"
        ),
        "by_target": by_target,
        "by_prompt": by_prompt,
        "by_level": by_level,
        "unpriced": sorted(unpriced),
    }


def plan_usage(samples: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any] | None:
    """What CLI targets drew from subscription plans: measured requests, and the
    utilization of one usage window as the client reported it during the run
    (lowest → highest reading; warm-ups count, since they draw on it too).

    Readings are never compared across windows: the window is the one reported
    most recently, and only its readings are used. None without CLI targets.
    """
    cli = {t.get("name") for t in meta.get("targets", []) if t.get("access") == "cli"}
    if not cli:
        return None
    mine = [s for s in samples if s.get("target") in cli]
    readings = [
        (s.get("wall_clock") or "", s["plan_window"], s["plan_utilization"])
        for s in mine
        if s.get("plan_utilization") is not None and s.get("plan_window")
    ]
    window = max(readings)[1] if readings else None
    levels = [u for _t, w, u in readings if w == window]
    plans = sorted({t.get("plan") for t in meta["targets"] if t.get("name") in cli} - {None})
    return {
        "requests": sum(1 for s in mine if not is_warmup(s)),
        "plans": plans,
        "window": window,
        "utilization_first": min(levels) if levels else None,
        "utilization_last": max(levels) if levels else None,
    }


def fmt(value: float | None, digits: int = 1, scale: float = 1.0) -> str:
    if value is None:
        return "—"
    return f"{value * scale:.{digits}f}"


def bar(value: float, peak: float, width: int = 28) -> str:
    if not peak or not value:
        return ""
    filled = max(1, round(width * value / peak))
    return "█" * filled


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def model_ids(models: list[str | None]) -> list[str]:
    return list(dict.fromkeys(model for model in models if model))


def model_label(models: list[str]) -> str:
    if len(models) == 1:
        return models[0]
    return f"{len(models)} model ids" if models else "?"


def _ordered_targets(meta: dict[str, Any], keys) -> list[str]:
    names = [t["name"] for t in meta.get("targets", [])]
    for key in keys:
        if key not in names:
            names.append(key)
    return names


def write_report(run_dir: Path, samples: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    stats = summarize(samples)
    buckets = stats["buckets"]
    per_stream = stats["per_stream"]
    min_conc = stats["min_conc"]
    targets = _ordered_targets(meta, [t for (_p, t) in per_stream])
    prompts = [p for p in dict.fromkeys([*(meta.get("prompts") or []), *min_conc]) if p in min_conc]
    price_by_name = {t["name"]: t for t in meta.get("targets", [])}

    flags = []
    if meta.get("mock"):
        flags.append("MOCK DATA")
    if meta.get("complete") is False:
        flags.append("PARTIAL")
    tag = f" ({', '.join(flags)})" if flags else ""

    lines: list[str] = [f"# tps report — {run_dir.name}{tag}", ""]
    if meta.get("mock"):
        lines += [
            "> **MOCK DATA** — synthetic numbers from `--mock`. Not a measurement; never publish them.",
            "",
        ]
    if meta.get("complete") is False:
        lines += [
            "> **PARTIAL** — the run was interrupted; tables cover only the requests that finished.",
            "",
        ]
    models = model_ids([t.get("model") for t in meta.get("targets", [])])
    model = model_label(models)
    caps = sorted(set((meta.get("max_tokens") or {}).values()))
    cap = f" · max_tokens {'/'.join(map(str, caps))}" if caps else ""
    targets_meta = meta.get("targets", [])
    if targets_meta and all("temperature" in t for t in targets_meta):
        temps = {t["temperature"] for t in targets_meta}
        temp = "/".join(sorted("not sent" if v is None else f"{v:g}" for v in temps))
    else:  # runs recorded before per-target temperatures
        temp = str(meta.get("temperature"))
    lines.append(
        f"- model: `{model}` · reps {meta.get('reps')} · "
        f"concurrency {meta.get('concurrency')} · temp {temp}{cap} · "
        f"http2 {meta.get('http2')}"
    )
    version = f" · tps {meta['tps_version']}" if meta.get("tps_version") else ""
    lines.append(f"- captured: {meta.get('created')}{version}")
    lines.append("")
    lines.append(
        "TTFT = time to the first generated token (content or reasoning). "
        "tok/s = per-stream decode rate across the streaming window. ITL = median gap "
        "between content chunks; tok/chunk shows how many tokens each chunk carried, and "
        "† marks streams that arrived in bursts, where ITL reflects flushing rather than "
        "the model. **p90 is always the slow tail** (for tok/s, the rate 90% "
        "of requests met or beat). Aggregate = all tokens of a concurrent batch ÷ its wall "
        "clock. Definitions: `docs/metrics.md`."
    )
    lines.append("")

    estimated_any = False
    for prompt in prompts:
        conc = min_conc[prompt]
        label = "single stream" if conc == 1 else f"lowest concurrency (c={conc})"
        lines.append(f"## `{prompt}` — {label}")
        lines.append("")
        rows = [(t, per_stream[(prompt, t)]) for t in targets if (prompt, t) in per_stream]

        # Optional delta columns against a named baseline (usually the no-gateway
        # direct endpoint): this turns "which is faster" into "what did the
        # gateway hop cost me".
        base_name = meta.get("baseline")
        base = (
            bucket_stats(per_stream[(prompt, base_name)])
            if (prompt, base_name) in per_stream
            else None
        )
        show_delta = bool(base and base["ttft_p50_s"] is not None and base["out_tps_p50"])
        show_first = any(
            fc is not None and tt is not None and fc - tt > 1e-6
            for _t, b in rows
            for fc, tt in zip(b["first_content"], b["ttft"], strict=False)
        )

        header = ["provider", "TTFT p50", "TTFT p90"]
        if show_first:
            header.append("first answer p50")
        header += ["tok/s p50", "tok/s p90", "ITL p50 ms", "tok/chunk", "E2E p50", "out tok", "ok"]
        if show_delta:
            header += [f"Δ TTFT vs {base_name}", f"Δ tok/s vs {base_name}"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))

        for target, bucket in rows:
            st = bucket_stats(bucket)
            estimated = is_estimated(st["count_source"])
            estimated_any |= estimated
            mark = " ≈" if estimated else ""
            cells = [target, fmt(st["ttft_p50_s"], 2), fmt(st["ttft_p90_s"], 2)]
            if show_first:
                cells.append(fmt(st["first_content_p50_s"], 2))
            cells += [
                fmt(st["out_tps_p50"]) + mark,
                fmt(st["out_tps_p90"]) + mark,
                fmt(st["itl_p50_ms"]) + (" †" if st["bursty"] else ""),
                fmt(st["tokens_per_chunk_median"]),
                fmt(st["e2e_p50_s"], 2),
                fmt(st["out_tokens_median"], 0),
                f"{st['ok']}/{st['n']}",
            ]
            if show_delta:
                if target == base_name:
                    cells += ["—", "—"]
                else:
                    d_ttft = (
                        st["ttft_p50_s"] - base["ttft_p50_s"]
                        if st["ttft_p50_s"] is not None
                        else None
                    )
                    d_tps = (
                        (st["out_tps_p50"] - base["out_tps_p50"]) / base["out_tps_p50"] * 100
                        if st["out_tps_p50"] is not None
                        else None
                    )
                    cells += [
                        "—" if d_ttft is None else f"{d_ttft:+.2f}s",
                        "—" if d_tps is None else f"{d_tps:+.0f}%",
                    ]
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        lines.append("")
        notes = [(t, note) for t, b in rows for note in row_notes(bucket_stats(b))]
        for target, note in notes:
            lines.append(f"> ⚠ **{target}**: {note}.")
        if notes:
            lines.append("")

        peak = max((p50(b["tps"]) or 0 for _t, b in rows), default=0.0)
        lines.append("```")
        width = max((len(t) for t, _b in rows), default=10)
        for target, bucket in rows:
            rate = p50(bucket["tps"])
            extra = ""
            if bucket["errors"]:
                extra = "  " + " ".join(f"[{k}×{v}]" for k, v in bucket["errors"].items())
            shown = "      — tok/s" if rate is None else f"{rate:7.1f} tok/s"
            lines.append(f"{target:<{width}}  {bar(rate or 0, peak):<28} {shown}{extra}")
        lines.append("```")
        lines.append("")

        concs = sorted({c for (p, _t, c) in buckets if p == prompt})
        if len(concs) > 1 or concs[0] != 1:
            lines.append(f"## `{prompt}` — under load")
            lines.append("")
            lines.append(
                "| provider | c | aggregate tok/s | vs c="
                + str(concs[0])
                + " | per-stream tok/s p50 "
                "| TTFT p50 | ok | errors |"
            )
            lines.append("|---|---|---|---|---|---|---|---|")
            for target in targets:
                first_agg = None
                for c in concs:
                    bucket = buckets.get((prompt, target, c))
                    if not bucket:
                        continue
                    st = bucket_stats(bucket)
                    agg = st["aggregate_tps"]
                    if c == concs[0]:
                        first_agg = agg
                    scale = f"{agg / first_agg:.1f}×" if agg and first_agg else "—"
                    errors = ", ".join(f"{k} × {v}" for k, v in sorted(st["errors"].items()))
                    lines.append(
                        f"| {target} | {c} | {fmt(agg)} | {scale} | {fmt(st['out_tps_p50'])} | "
                        f"{fmt(st['ttft_p50_s'], 2)} | {st['ok']}/{st['n']} | {errors or '—'} |"
                    )
            lines.append("")
            lines.append(
                "Aggregate rising roughly with `c` means the provider scales; flat aggregate "
                "with collapsing per-stream tok/s means it queues."
            )
            lines.append("")

    if estimated_any:
        lines.append(
            "> ≈ token counts were estimated as chars/4 — the provider sent no `usage` "
            "block, or (`usage+chars/4`) only a summary of its billed reasoning streamed "
            "and the streamed/hidden split was estimated. Treat those rates as approximate."
        )
        lines.append("")

    # cost + provenance
    lines.append("## Cost & provenance")
    lines.append("")
    cost = spend(samples, meta)
    lines.append(
        "| provider | $/1M in | $/1M out | est. $/1k requests | cost of 1M generated tokens "
        "| served by | token source | est. $ this run |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for target in targets:
        info = price_by_name.get(target, {})
        pin = info.get("pin") or {}
        pooled = [b for (_p, t), b in per_stream.items() if t == target]
        served = sorted({x for b in pooled for x in b["served"] if x})
        source = sorted({x for b in pooled for x in b["count_source"] if x})
        median_in = median([v for b in pooled for v in b["in"]])
        median_out = median([v for b in pooled for v in b["out"]])
        per_1k, per_mtok = request_cost(
            median_in, median_out, info.get("price_in"), info.get("price_out")
        )
        pinned = ",".join(pin.get("only", [])) if isinstance(pin, dict) else ""
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                target,
                fmt(info.get("price_in"), 3),
                fmt(info.get("price_out"), 3),
                fmt(per_1k, 3),
                fmt(per_mtok, 2),
                ", ".join(served) or (f"pin:{pinned}" if pinned else "—"),
                ", ".join(source) or "—",
                fmt(cost["by_target"].get(target), 4),
            )
        )
    lines.append("")
    if cost["total"] is not None:
        line = (
            f"Estimated spend for this run: **${cost['total']:.4f}** — successful requests "
            "only, warm-ups excluded, at the configured prices."
        )
        if cost["unpriced"]:
            line += f" No price configured, so not counted: {', '.join(cost['unpriced'])}."
        lines.append(line)
    else:
        line = (
            "Run spend is unknown: no successful request ran on a target with both "
            "`price.in_per_1m` and `price.out_per_1m`."
        )
        if cost["unpriced"]:
            line += (
                f" No price configured, so not counted: {', '.join(cost['unpriced'])}"
                " — a missing price is not free."
            )
        lines.append(line)
    lines.append("")
    lines.append(
        "> Cost uses the median request across all prompts. Provider attribution comes from "
        "the response (`provider` field or `x-openrouter-provider` header) when the gateway "
        "exposes it; if the column is empty you were routed by a gateway, not pinned."
    )

    # failures — counted across every concurrency level, not just the lowest
    totals: dict[str, int] = {}
    errors: dict[str, dict[str, int]] = {}
    for s in samples:
        if is_warmup(s):
            continue
        name = s["target"]
        totals[name] = totals.get(name, 0) + 1
        if not s.get("ok"):
            code = error_class(s)
            errors.setdefault(name, {})
            errors[name][code] = errors[name].get(code, 0) + 1
    if errors:
        lines.append("")
        lines.append("## Failures")
        lines.append("")
        lines.append("| provider | failed / total | error classes |")
        lines.append("|---|---|---|")
        for name in sorted(errors):
            failed = sum(errors[name].values())
            detail = ", ".join(f"{k} × {v}" for k, v in sorted(errors[name].items()))
            lines.append(f"| {name} | {failed} / {totals.get(name, 0)} | {detail} |")
        lines.append("")
        lines.append(
            "Retries are intentionally off: a provider that 429s at concurrency 16 "
            "is telling you something a retry would erase."
        )

    report = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(report, encoding="utf-8")

    # csv: one row per prompt × provider × concurrency, each with its own stats
    with open(run_dir / "summary.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "prompt",
                "provider",
                "concurrency",
                "ttft_p50_s",
                "ttft_p90_s",
                "out_tps_p50",
                "out_tps_p90",
                "itl_p50_ms",
                "e2e_p50_s",
                "aggregate_tps",
                "out_tokens_median",
                "ok",
                "n",
                "errors",
                "first_content_p50_s",
                "count_source",
                "served_by",
                "truncated",
                "reasoning_only",
                "itl_n",
                "tokens_per_chunk_median",
                "bursty",
                "cost_usd",
            ]
        )
        for (prompt, target, conc), bucket in sorted(buckets.items()):
            st = bucket_stats(bucket)
            writer.writerow(
                [
                    prompt,
                    target,
                    conc,
                    fmt(st["ttft_p50_s"], 3),
                    fmt(st["ttft_p90_s"], 3),
                    fmt(st["out_tps_p50"]),
                    fmt(st["out_tps_p90"]),
                    fmt(st["itl_p50_ms"]),
                    fmt(st["e2e_p50_s"], 3),
                    fmt(st["aggregate_tps"]),
                    fmt(st["out_tokens_median"], 0),
                    st["ok"],
                    st["n"],
                    "; ".join(f"{k}×{v}" for k, v in st["errors"].items()),
                    fmt(st["first_content_p50_s"], 3),
                    ";".join(st["count_source"]),
                    ";".join(st["served_by"]),
                    st["truncated"],
                    st["reasoning_only"],
                    st["itl_n"],
                    fmt(st["tokens_per_chunk_median"], 2),
                    st["bursty"],
                    fmt(cost["by_level"].get((prompt, target, conc)), 5),
                ]
            )

    return report


# --------------------------------------------------------------------------
# discover (OpenRouter)
# --------------------------------------------------------------------------


def fetch_endpoints(
    model_slug: str,
    api_key: str | None = None,
    from_json: Path | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Return (canonical_slug, endpoints, raw_payload) for one model."""
    if from_json:
        try:
            payload = json.loads(from_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise UsageError(f"cannot read {from_json}: {exc.strerror}") from exc
        except json.JSONDecodeError as exc:
            raise UsageError(f"{from_json} is not JSON: {exc}") from exc
    else:
        url = f"https://openrouter.ai/api/v1/models/{model_slug.strip('/')}/endpoints"
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not reach OpenRouter: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            raise UsageError(
                f"model not found on OpenRouter: {model_slug} — use the author/slug "
                "shown on openrouter.ai/models"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"GET {url} -> {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"GET {url} returned non-JSON") from exc

    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        data = data[0] if data else {}
    endpoints = data.get("endpoints") or []
    if not endpoints:
        raise ProviderError("no endpoints in response — dump it with --json-out and inspect")
    canonical = data.get("id") or data.get("slug") or model_slug
    return canonical, endpoints, payload


def normalize_endpoints(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat, JSON-safe rows — the same shape the CLI emits and renders."""
    rows = []
    for endpoint in endpoints:
        pricing = endpoint.get("pricing") or {}
        rows.append(
            {
                "provider": endpoint.get("provider_name") or endpoint.get("name"),
                "slug": provider_slug(endpoint),
                "tag": endpoint.get("tag"),
                "quantization": endpoint.get("quantization"),
                "service_tier": endpoint.get("service_tier"),
                "context_length": endpoint.get("context_length"),
                "price_in_per_1m": _per_million(pricing.get("prompt")),
                "price_out_per_1m": _per_million(pricing.get("completion")),
                "reported_p50_throughput": _stat(endpoint.get("throughput_last_30m"), "p50"),
                "reported_p50_latency_s": _stat(endpoint.get("latency_last_30m"), "p50"),
            }
        )
    return rows


def render_endpoint_table(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'provider':<20} {'slug':<20} {'quant':<8} {'$/1M in':>8} "
        f"{'$/1M out':>9} {'p50 t/s*':>9} {'p50 ttft*':>10}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{_text(row['provider'], 20):<20} "
            f"{_text(row['slug'], 20):<20} "
            f"{_text(row['quantization'], 8):<8} "
            f"{fmt(row['price_in_per_1m'], 3):>8} "
            f"{fmt(row['price_out_per_1m'], 3):>9} "
            f"{fmt(row['reported_p50_throughput'], 1):>9} "
            f"{fmt(row['reported_p50_latency_s'], 2):>10}"
        )
    return "\n".join(lines)


def targets_toml(canonical: str, endpoints: list[dict[str, Any]]) -> tuple[str, int, int]:
    """Build a config with one pinned target per endpoint tag.

    Returns (toml_text, target_count, collapsed_count).
    """
    blocks = [
        f"# generated by: tps discover {canonical}",
        "# each target pins ONE upstream: allow_fallbacks = false means a",
        "# failure shows up as an error row instead of silently rerouting.",
        "",
        "[run]",
        "reps = 7",
        "concurrency = [1, 4]",
        'prompts = ["prose-256", "code-256"]',
        "",
    ]
    # Pin the *full* tag slug (e.g. `baseten/fast`, `sail-research/fp4/us`).
    # A base slug like `baseten` matches every endpoint of that provider but
    # explicitly does NOT match tier endpoints, so pinning `baseten` while
    # labelling the row "priority" would silently measure the wrong thing.
    groups: dict[str, list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        groups.setdefault(str(endpoint.get("tag") or provider_slug(endpoint)), []).append(endpoint)

    def price_key(e: dict[str, Any]) -> float:
        pricing = e.get("pricing") or {}
        price_in = _per_million(pricing.get("prompt"))
        price_out = _per_million(pricing.get("completion"))
        return (1e9 if price_in is None else price_in) + (1e9 if price_out is None else price_out)

    collapsed = 0
    labels: dict[str, int] = {}
    for tag, group in groups.items():
        group.sort(key=price_key)
        endpoint = group[0]
        collapsed += len(group) - 1

        quant = endpoint.get("quantization")
        if quant in (None, "unknown"):
            quant = None
        label = _text(endpoint.get("provider_name"), 40) if endpoint.get("provider_name") else tag
        if quant:
            label = f"{label} ({quant})"
        tier = endpoint.get("service_tier")
        if tier:
            label = f"{label} [{tier}]"
        labels[label] = labels.get(label, 0) + 1
        if labels[label] > 1:
            label = f"{label} #{labels[label]}"

        pricing = endpoint.get("pricing") or {}
        price_in = _per_million(pricing.get("prompt"))
        price_out = _per_million(pricing.get("completion"))

        blocks.append("[[targets]]")
        blocks.append(f"name = {_toml_inline(label)}")
        if len(group) > 1:
            blocks.append(f"# {len(group)} endpoints share this pin; cheapest kept")
        blocks.append('base_url = "https://openrouter.ai/api/v1"')
        blocks.append(f"model = {_toml_inline(canonical)}")
        blocks.append('api_key_env = "OPENROUTER_API_KEY"')
        pin: dict[str, Any] = {"only": [tag], "allow_fallbacks": False}
        if quant:
            pin["quantizations"] = [quant]
        blocks.append("extra_body.provider = " + _toml_inline(pin))
        price = {}
        if price_in is not None:
            price["in_per_1m"] = round(price_in, 4)
        if price_out is not None:
            price["out_per_1m"] = round(price_out, 4)
        if price:
            blocks.append("price = " + _toml_inline(price))
        blocks.append("")
    return "\n".join(blocks), len(groups), collapsed


def redact_url(url: str) -> str:
    """The URL without its user:password@ part, for anything that gets shared."""
    try:
        parsed = urlparse(url)
        if not (parsed.username or parsed.password):
            return url
        host = parsed.hostname or ""
        host = f"[{host}]" if ":" in host else host
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return url
    return parsed._replace(netloc=f"***@{host}{port}").geturl()


def _comment(text: str) -> str:
    # a newline in a TOML comment would end it and turn the rest into config
    return "".join(c if c.isprintable() else "?" for c in text)


def run_toml(cfg: RunConfig) -> str:
    """A [run] section (plus any overridden built-in prompt caps) that
    reproduces cfg's effective settings, flags included."""
    lines = [
        "[run]",
        f"reps = {cfg.reps}",
        f"concurrency = {_toml_inline(cfg.concurrency)}",
        f"warmup = {cfg.warmup}",
        f"timeout_s = {_toml_inline(cfg.timeout_s)}",
        f"prompts = {_toml_inline(list(cfg.prompts))}",
    ]
    if cfg.temperature is not None:
        lines.append(f"temperature = {_toml_inline(cfg.temperature)}")
    if cfg.max_tokens_field != "max_tokens":
        lines.append(f"max_tokens_field = {_toml_inline(cfg.max_tokens_field)}")
    for name, spec in cfg.prompts.items():
        cap = int(spec.get("max_tokens", 256))
        if cap != int(DEFAULT_PROMPTS.get(name, {}).get("max_tokens", -1)):
            lines += ["", f"[prompts.{_toml_key(name)}]", f"max_tokens = {cap}"]
    return "\n".join(lines)


DEFAULT_RUN_TOML = '[run]\nreps = 7\nconcurrency = [1]\nprompts = ["prose-256"]'


def endpoint_toml(
    base_url: str,
    model: str | list[str],
    name: str | None = None,
    api_key_env: str | None = None,
    price: tuple[float, float] | None = None,
    source: str = "tps init --endpoint",
    api: str = "openai",
    run_block: str = DEFAULT_RUN_TOML,
    effort: str | list[str] | None = None,
) -> str:
    """A one-target config for any OpenAI-compatible endpoint, no gateway needed.

    `tps init --endpoint` writes it; `tps run --base-url` builds it in memory
    and saves a copy, with the run's effective settings, in the run directory
    so the run can be repeated.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UsageError(f"not an http(s) URL: {base_url!r} — e.g. https://api.example.com/v1")
    # never the netloc: a user:password@ in the URL must not become the name
    default_name = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    models = _as_list(model)
    fields: list[tuple[str, Any]] = [("base_url", base_url)]
    if api != "openai":
        fields.append(("api", api))
    if api_key_env:
        fields.append(("api_key_env", api_key_env))
    if price:
        fields.append(("price", {"in_per_1m": price[0], "out_per_1m": price[1]}))
    lines = [
        _comment(f"# generated by: {source} {redact_url(base_url)} --model {','.join(models)}"),
        "# add more blocks to compare providers side by side",
        "",
        run_block,
        "",
        *target_block(name or default_name, fields, models, _as_list(effort)),
    ]
    return "\n".join(lines) + "\n"


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def target_block(
    name: str, fields: list[tuple[str, Any]], models: list[str], efforts: list[str]
) -> list[str]:
    """One [[targets]] block — or a [[grids]] block when several models or
    effort levels are asked for (each combination becomes its own target)."""
    if len(models) == 1 and len(efforts) <= 1:
        lines = ["[[targets]]", f"name = {_toml_inline(name)}"]
        lines += [f"{k} = {_toml_inline(v)}" for k, v in fields]
        lines.append(f"model = {_toml_inline(models[0])}")
        if efforts and efforts[0] != "default":
            lines.append(f"effort = {_toml_inline(efforts[0])}")
        return lines
    lines = ["[[grids]]  # one target per model × effort level", f"name = {_toml_inline(name)}"]
    lines += [f"{k} = {_toml_inline(v)}" for k, v in fields]
    lines.append(f"models = {_toml_inline(models)}")
    lines.append(f"effort = {_toml_inline(efforts or ['default'])}")
    return lines


def cli_target_toml(
    api: str,
    model: str | list[str],
    name: str | None = None,
    effort: str | list[str] | None = None,
    source: str = "tps init --api claude-code",
    run_block: str = DEFAULT_RUN_TOML,
) -> str:
    """A config for a CLI target (a subscription plan via its client): one
    target, or a grid when several models or effort levels are given."""
    models, efforts = _as_list(model), _as_list(effort)
    single = len(models) == 1 and len(efforts) <= 1
    level = efforts[0] if efforts and efforts[0] != "default" else None
    default = f"{api}/{models[0]}" + (f"@{level}" if level else "") if single else api
    lines = [
        _comment(f"# generated by: {source} --model {','.join(models)}"),
        "# add more blocks (other models, effort levels) to compare them",
        "",
        run_block,
        "",
        *target_block(name or default, [("api", api)], models, efforts),
    ]
    return "\n".join(lines) + "\n"


def discover(
    model_slug: str,
    out_path: Path | None,
    api_key: str | None,
    from_json: Path | None,
    json_out: Path | None,
) -> None:
    """Library convenience wrapper: print the table, optionally write a config."""
    canonical, endpoints, payload = fetch_endpoints(model_slug, api_key, from_json)
    if json_out:
        json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    rows = normalize_endpoints(endpoints)
    print(f"{canonical}: {len(endpoints)} endpoint(s)\n", file=sys.stderr)
    print(render_endpoint_table(rows))
    print(
        "\n* provider-reported rolling stats (30m), not your measurement; "
        "often null without an API key.",
        file=sys.stderr,
    )
    if out_path:
        text, count, collapsed = targets_toml(canonical, endpoints)
        out_path.write_text(text, encoding="utf-8")
        note = f", collapsed {collapsed} indistinguishable endpoint(s)" if collapsed else ""
        print(f"\nwrote {out_path} ({count} pinned targets{note})", file=sys.stderr)


def provider_slug(endpoint: dict[str, Any]) -> str:
    """`tag` looks like `deepinfra/fp4/flex`; the router's pin key is the first part."""
    tag = endpoint.get("tag")
    if isinstance(tag, str) and tag:
        return tag.split("/")[0]
    name = endpoint.get("provider_name") or endpoint.get("name") or "unknown"
    return str(name).lower().replace(" ", "-")


def _text(value: Any, width: int) -> str:
    text = "—" if value is None else str(value)
    return text[:width]


def _stat(value: Any, key: str) -> float | None:
    if isinstance(value, dict):
        for candidate in (key, "p50", "mean", "value"):
            if value.get(candidate) is not None:
                try:
                    return float(value[candidate])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def _toml_inline(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_inline(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # JSON escapes are valid TOML basic-string escapes, but ensure_ascii's
        # surrogate pairs (🚀) are not: keep non-ASCII literal, and
        # escape DEL, the one control character JSON leaves alone
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    return str(value)


def _toml_key(key: Any) -> str:
    key = str(key)
    bare = key and all(c.isascii() and (c.isalnum() or c in "-_") for c in key)
    return key if bare else _toml_inline(key)


# --------------------------------------------------------------------------
# fingerprint: are they even serving the same weights?
# --------------------------------------------------------------------------
#
# Same model slug does not mean same artifact. A cheaper endpoint may serve a
# different quantization (fp4 vs fp8), a distilled sibling, an older revision,
# or a different model entirely. Price and speed cannot tell you which.
#
# Signal used here: greedy decoding at temperature 0 is a near-deterministic
# function of the weights. Identical weights + identical kernels reproduce the
# same tokens; fp4 vs fp8 diverges within a few dozen tokens; a distilled
# model diverges almost immediately. Three confounders are handled explicitly:
#
#   1. batching/numerics make *any* endpoint non-deterministic, so each target
#      is sampled twice and its self-agreement is the noise floor. A target
#      below the floor can't be placed, so it never decides the verdict.
#   2. tokenizer/template differences change `usage.prompt_tokens` for the same
#      input string, which is cheap to read and impossible to explain away.
#   3. a target that errored has no data. It is listed separately, never
#      counted as "different weights".
#
# This is evidence, not proof. Treat a split as "worth investigating".

NOISE_FLOOR = 0.98


@dataclass
class Fingerprint:
    target: str
    prompt: str
    rep: int
    ok: bool
    error: str | None = None
    text: str = ""
    tokens: list[str] = field(default_factory=list)
    logprobs: list[float] = field(default_factory=list)
    # per position, the provider's top alternatives {token: logprob} — the raw
    # material for distribution tests, not just the sampled token's logprob
    top_logprobs: list[dict[str, float]] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None  # nonzero = reasoning stayed on
    mode: str = "tokens"  # "tokens" | "text" (no logprobs support)


TOP_LOGPROBS = 20  # OpenAI's maximum; providers that cap lower get FALLBACK_TOP_LOGPROBS
FALLBACK_TOP_LOGPROBS = 5
REASONING_CONSUMED = (
    "reasoning_consumed_budget: the model spent max_tokens reasoning and never answered "
    "— raise --max-tokens, or turn reasoning off for this target"
)


def is_openrouter(target: Target) -> bool:
    return (urlparse(target.base_url).hostname or "").endswith("openrouter.ai")


async def fingerprint_once(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    rep: int,
    use_logprobs: bool = True,
    top_k: int = TOP_LOGPROBS,
) -> Fingerprint:
    # Greedy where the API allows it, and "omit" is honoured for models that
    # reject temperature. The Claude API rejects sampling settings and has no
    # logprobs, so Claude is compared on text and its self-agreement decides
    # whether it can be placed at all.
    greedy = replace(cfg, temperature="omit" if cfg.temperature == "omit" else None)
    use_logprobs = use_logprobs and target.api == "openai"
    extra: dict[str, Any] = {}
    if use_logprobs:
        extra.update(logprobs=True, top_logprobs=top_k)
    if is_openrouter(target):
        # reasoning makes greedy traces longer and noisier and can eat the whole
        # budget; extra_body can still turn it back on
        extra["reasoning"] = {"enabled": False}
    body = request_body(target, greedy, prompt_text, max_tokens, False, extra=extra)

    headers = {"Content-Type": "application/json"}
    headers.update(target.auth_headers())
    headers.update(target.headers)

    result = Fingerprint(target=target.name, prompt=prompt_name, rep=rep, ok=False)
    try:
        response = await client.post(target.url(), json=body, headers=headers)
        if response.status_code >= 400:
            text = redact(response.text, target.secrets())[:600]
            compat = use_logprobs and response.status_code in (400, 422)
            if compat and "top_logprobs" in text.lower() and top_k > FALLBACK_TOP_LOGPROBS:
                # some providers cap top_logprobs (often at 5): ask for fewer
                return await fingerprint_once(
                    client,
                    target,
                    prompt_text,
                    prompt_name,
                    max_tokens,
                    cfg,
                    rep,
                    True,
                    FALLBACK_TOP_LOGPROBS,
                )
            if compat and "logprob" in text.lower():
                # provider doesn't implement logprobs; exact-text mode still
                # detects "different model" but not "same model, different quant"
                return await fingerprint_once(
                    client, target, prompt_text, prompt_name, max_tokens, cfg, rep, False
                )
            result.error = f"http_{response.status_code}: {text}"
            return result
        payload = response.json()
    except httpx.TimeoutException:
        result.error = "timeout"
        return result
    except (httpx.HTTPError, OSError) as exc:
        result.error = redact(f"transport: {type(exc).__name__}: {exc}", target.secrets())
        return result
    except ValueError:
        result.error = "non_json_response"
        return result

    if not isinstance(payload, dict):
        result.error = "non_json_response"
        return result
    usage = payload.get("usage") or {}
    result.prompt_tokens = _first_present(usage.get("prompt_tokens"), usage.get("input_tokens"))
    result.completion_tokens = _first_present(
        usage.get("completion_tokens"), usage.get("output_tokens")
    )
    for key, name in (
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "thinking_tokens"),
    ):
        details = usage.get(key)
        if isinstance(details, dict) and details.get(name) is not None:
            result.reasoning_tokens = details[name]

    if target.api == "anthropic":
        blocks = payload.get("content") or []
        result.text = "".join(
            b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        result.mode = "text"
        result.ok = bool(result.text)
        if not result.ok:
            capped = payload.get("stop_reason") == "max_tokens"
            result.error = REASONING_CONSUMED if capped else "empty_response"
        return result

    choices = payload.get("choices") or []
    if not choices:
        result.error = "no_choices"
        return result
    choice = choices[0]
    message = choice.get("message") or {}
    result.text = message.get("content") or ""
    logprob_block = choice.get("logprobs") or {}
    items = logprob_block.get("content") or []
    for item in items:
        token = item.get("token")
        if token is None:
            continue
        result.tokens.append(token)
        result.logprobs.append(item.get("logprob"))
        alternatives = item.get("top_logprobs") or []
        result.top_logprobs.append(
            {
                alt["token"]: alt.get("logprob")
                for alt in alternatives
                if isinstance(alt, dict) and alt.get("token") is not None
            }
        )
    if not result.tokens:
        result.mode = "text"
    result.ok = bool(result.text or result.tokens)
    if not result.ok:
        thought = (
            message.get("reasoning") or message.get("reasoning_content") or result.reasoning_tokens
        )
        result.error = REASONING_CONSUMED if thought else "empty_response"
    return result


def token_agreement(
    a: list[str], b: list[str], mode_a: str, mode_b: str
) -> tuple[float, int | None]:
    """Fraction of token positions that match, and the first divergence.

    The denominator is the *longer* sequence: a completion that stops early is
    a different completion, not a perfect match on its prefix.
    """
    if mode_a == "text" or mode_b == "text":
        # coarse: whole-string equality when token ids aren't available
        if not a or not b:
            return 0.0, None
        return (1.0 if a == b else 0.0), (None if a == b else 0)
    longest = max(len(a), len(b))
    if longest == 0:
        return 0.0, None
    same = 0
    first = None
    for i in range(min(len(a), len(b))):
        if a[i] == b[i]:
            same += 1
        elif first is None:
            first = i
    if first is None and len(a) != len(b):
        first = min(len(a), len(b))
    return same / longest, first


def compare_prints(a: Fingerprint, b: Fingerprint) -> tuple[float, int | None]:
    """Agreement between two fingerprints, degrading to text equality when a
    provider couldn't return token-level logprobs."""
    if a.mode == "text" or b.mode == "text":
        if not a.text or not b.text:
            return 0.0, None
        same = a.text == b.text
        return (1.0 if same else 0.0), (None if same else 0)
    return token_agreement(a.tokens, b.tokens, a.mode, b.mode)


def self_agreement_map(
    prints: list[Fingerprint], targets: list[str], prompts: list[str]
) -> dict[str, float]:
    """Worst-case agreement of each target with itself across reps."""
    return {t: v for t, (v, _first) in _self_agreement(prints, targets, prompts).items()}


def _self_agreement(
    prints: list[Fingerprint], targets: list[str], prompts: list[str]
) -> dict[str, tuple[float, int | None]]:
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    result: dict[str, tuple[float, int | None]] = {}
    for target in targets:
        for prompt in prompts:
            a = by_key.get((target, prompt, 0))
            b = by_key.get((target, prompt, 1))
            if not a or not b or not a.ok or not b.ok:
                continue
            frac, first = compare_prints(a, b)
            if target not in result or frac < result[target][0]:
                result[target] = (frac, first)
    return result


def cluster_targets(
    prints: list[Fingerprint],
    targets: list[str],
    prompt: str,
    self_agree: dict[str, float],
) -> list[list[str]]:
    """Union targets whose agreement clears both noise floors.

    Only targets that returned data for `prompt` are clustered; a failed
    request is absence of evidence, not a different artifact.
    """
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    present = [t for t in targets if (f := by_key.get((t, prompt, 0))) is not None and f.ok]
    parent: dict[str, str] = {t: t for t in present}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a_name in enumerate(present):
        for b_name in present[i + 1 :]:
            if _same_artifact(by_key, a_name, b_name, prompt, self_agree):
                union(a_name, b_name)

    groups: dict[str, list[str]] = {}
    for target in present:
        groups.setdefault(find(target), []).append(target)
    return sorted(groups.values(), key=len, reverse=True)


def _same_artifact(
    by_key: dict[tuple[str, str, int], Fingerprint],
    a_name: str,
    b_name: str,
    prompt: str,
    self_agree: dict[str, float],
) -> bool:
    frac, _ = compare_prints(by_key[(a_name, prompt, 0)], by_key[(b_name, prompt, 0)])
    floor = min(self_agree.get(a_name, 1.0), self_agree.get(b_name, 1.0))
    return frac >= max(NOISE_FLOOR, floor - 0.01)


def place_unweighed(
    prints: list[Fingerprint],
    targets: list[str],
    firm_groups: list[list[str]],
    unweighed: list[str],
    prompt: str,
    self_agree: dict[str, float],
) -> list[list[str]]:
    """Display clusters: the verdict's groups plus the targets it can't weigh.

    An unweighed target joins the one firm group it matches. One that matches
    several would bridge them, so it is grouped only with the other leftovers
    and never merges two groups the verdict told apart.
    """
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    order = {t: i for i, t in enumerate(targets)}
    groups = [list(g) for g in firm_groups]
    loose = []
    for target in unweighed:
        hits = [
            i
            for i, firm in enumerate(firm_groups)
            if any(_same_artifact(by_key, target, m, prompt, self_agree) for m in firm)
        ]
        if len(hits) == 1:
            groups[hits[0]].append(target)
        else:
            loose.append(target)
    placed = [sorted(g, key=order.__getitem__) for g in groups]
    return sorted(placed, key=len, reverse=True) + cluster_targets(
        prints, loose, prompt, self_agree
    )


def fingerprint_summary(
    prints: list[Fingerprint], targets: list[str], prompts: list[str]
) -> dict[str, Any]:
    """Everything the report, terminal and --json output say about a fingerprint."""
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    prompt = prompts[0]
    selfs = _self_agreement(prints, targets, prompts)
    self_agree = {t: v for t, (v, _f) in selfs.items()}
    present = [t for t in targets if (f := by_key.get((t, prompt, 0))) is not None and f.ok]
    no_data = {}
    for target in targets:
        if target in present:
            continue
        attempts = [f for f in prints if f.target == target and f.prompt == prompt]
        no_data[target] = next((f.error for f in attempts if f.error), "no response")
    # Only a target whose noise floor was *measured* and is high can weigh in.
    # No floor (one rep, or its second rep failed) is unknown, not deterministic.
    noisy = [t for t in present if t in self_agree and self_agree[t] < NOISE_FLOOR]
    unmeasured = [t for t in present if t not in self_agree]
    deterministic = [t for t in present if t in self_agree and self_agree[t] >= NOISE_FLOOR]
    # The verdict clusters deterministic targets alone: in one union-find, an
    # unweighed target close to two of them (a~u, u~b, a≁b) would bridge
    # their groups and turn a split into "consistent".
    firm_groups = cluster_targets(prints, deterministic, prompt, self_agree)
    unweighed = [t for t in present if t not in deterministic]
    clusters = place_unweighed(prints, targets, firm_groups, unweighed, prompt, self_agree)
    if len(deterministic) < 2:
        verdict = "inconclusive"
    elif len(firm_groups) > 1:
        verdict = "split"
    else:
        verdict = "consistent"

    pairs = []
    for i, a_name in enumerate(present):
        for b_name in present[i + 1 :]:
            frac, first = compare_prints(by_key[(a_name, prompt, 0)], by_key[(b_name, prompt, 0)])
            pairs.append(
                {"a": a_name, "b": b_name, "agreement": round(frac, 4), "first_divergence": first}
            )
    # The tokenizer signature is the prompt's usage.prompt_tokens, which is
    # valid even when the completion failed (reasoning ate the budget): take it
    # from any rep of the probe prompt, not only from rep 0 that succeeded.
    prompt_tokens = {
        t: next(
            (
                f.prompt_tokens
                for f in prints
                if f.target == t and f.prompt == prompt and f.prompt_tokens is not None
            ),
            None,
        )
        for t in targets
    }
    reasoning_seen = sorted(
        {f.target for f in prints if f.prompt == prompt and (f.reasoning_tokens or 0) > 0},
        key=targets.index,
    )
    return {
        "verdict": verdict,
        "clusters": clusters,
        "no_data": no_data,
        "noisy": noisy,
        "unmeasured": unmeasured,
        "distinguishable_groups": len(firm_groups),
        "noise_floor_measured": bool(self_agree),
        "self_agreement": {k: round(v, 4) for k, v in self_agree.items()},
        "self_first_divergence": {k: f for k, (_v, f) in selfs.items()},
        "pairs": pairs,
        "prompt_tokens": prompt_tokens,
        # targets that reasoned anyway (reasoning off was asked for, or isn't possible)
        "reasoning_seen": reasoning_seen,
        "prompt_used": prompt,
    }


def fingerprint_reports(prints: list[Fingerprint], targets: list[str], prompts: list[str]) -> str:
    by_key = {(item.target, item.prompt, item.rep): item for item in prints}
    summary = fingerprint_summary(prints, targets, prompts)
    self_agree = summary["self_agreement"]
    prompt = summary["prompt_used"]

    lines: list[str] = ["# Fingerprint: same weights or just the same name?", ""]
    lines.append(
        "Greedy decode (temp 0) where the API allows it, reasoning off where it can be "
        "turned off. `self` = the provider's agreement with itself across reps — its "
        "noise floor. Agreement below that floor is not evidence of different weights."
    )
    lines.append("")
    if summary["reasoning_seen"]:
        lines.append(
            f"⚠ Reasoned anyway: {', '.join(summary['reasoning_seen'])} — reasoning tokens "
            "were billed despite the request (or can't be disabled). Their traces include "
            "reasoning, which makes them longer and noisier."
        )
        lines.append("")

    # 1. tokenizer / template signature
    lines.append("## Tokenizer & chat-template signature")
    lines.append("")
    lines.append("| provider | prompt_tokens (same input) | completion_tokens | logprobs |")
    lines.append("|---|---|---|---|")
    signatures: dict[int, set[str]] = {}
    for target in targets:
        item = next(
            (by_key[(target, p, 0)] for p in prompts if (target, p, 0) in by_key),
            None,
        )
        # usage.prompt_tokens describes the input, so it counts even when the
        # completion itself failed
        value = summary["prompt_tokens"].get(target)
        if value is not None:
            signatures.setdefault(value, set()).add(target)
        completion = item.completion_tokens if item and item.ok else None
        mode = item.mode if item and item.ok else "—"
        lines.append(
            f"| {target} | {'—' if value is None else value} | "
            f"{'—' if completion is None else completion} | {mode} |"
        )
    lines.append("")
    if len(signatures) > 1:
        groups = "; ".join(
            f"{count} tokens → {', '.join(sorted(names))}"
            for count, names in sorted(signatures.items())
        )
        lines.append(
            f"⚠ {len(signatures)} distinct prompt-token counts for identical input — "
            f"different tokenizer or chat template: {groups}."
        )
    elif signatures:
        lines.append(
            "All providers with data tokenized the probe identically — consistent with one "
            "tokenizer/template, which is necessary but not sufficient."
        )
    else:
        lines.append("No provider reported `usage.prompt_tokens`.")
    lines.append("")

    # 2. self-agreement noise floor
    lines.append("## Self-agreement (noise floor)")
    lines.append("")
    if not summary["noise_floor_measured"]:
        lines.append(
            "Not measured — it needs two successful reps per provider (`--reps 2`, the "
            "default). Without a noise floor, cross-provider agreement can't be interpreted."
        )
    else:
        lines.append("| provider | token agreement rep0 vs rep1 | first divergence |")
        lines.append("|---|---|---|")
        for target in targets:
            value = self_agree.get(target)
            first_div = summary["self_first_divergence"].get(target)
            lines.append(
                f"| {target} | {'—' if value is None else f'{value * 100:.1f}%'} | "
                f"{'—' if first_div is None else first_div} |"
            )
        lines.append("")
        if summary["noisy"]:
            lines.append(
                f"⚠ Non-deterministic at temp 0: {', '.join(summary['noisy'])}. Their "
                "cross-provider agreement can't be interpreted — the endpoint itself varies "
                "run to run — so they never decide the verdict."
            )
        else:
            lines.append("Every provider with two successful reps reproduced itself near-exactly.")
    lines.append("")

    # 3. pairwise agreement, first prompt
    lines.append(f"## Pairwise agreement — `{prompt}`")
    lines.append("")
    if len(targets) <= 12:
        lines.append("| | " + " | ".join(f"`{t[:14]}`" for t in targets) + " |")
        lines.append("|---" * (len(targets) + 1) + "|")
        for a_name in targets:
            cells = []
            for b_name in targets:
                if a_name == b_name:
                    value = self_agree.get(a_name)
                    cells.append("—" if value is None else f"**{value * 100:.0f}**")
                    continue
                a = by_key.get((a_name, prompt, 0))
                b = by_key.get((b_name, prompt, 0))
                if not a or not b or not a.ok or not b.ok:
                    cells.append("·")
                    continue
                frac, _ = compare_prints(a, b)
                cells.append(f"{frac * 100:.0f}")
            lines.append(f"| **{a_name}** | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("Diagonal = self-agreement. Values are % of token positions identical.")
        lines.append("")
    else:
        lines.append("Too many targets for a matrix; showing each provider's nearest neighbours.")
        lines.append("")
        lines.append("| provider | closest peers (≥99%) | nearest other |")
        lines.append("|---|---|---|")
        for a_name in targets:
            a = by_key.get((a_name, prompt, 0))
            scored = []
            if a and a.ok:
                for b_name in targets:
                    b = by_key.get((b_name, prompt, 0))
                    if b_name == a_name or not b or not b.ok:
                        continue
                    frac, _ = compare_prints(a, b)
                    scored.append((frac, b_name))
            scored.sort(reverse=True)
            peers = [n for f, n in scored if f >= 0.99]
            nearest = f"{scored[0][1]} ({scored[0][0] * 100:.0f}%)" if scored else "—"
            lines.append(f"| {a_name} | {', '.join(peers) or '—'} | {nearest} |")
        lines.append("")

    # 4. clusters + verdict
    lines.append("## Clusters")
    lines.append("")
    unweighed = set(summary["noisy"]) | set(summary["unmeasured"])
    for index, members in enumerate(summary["clusters"], start=1):
        note = (
            " — no deterministic noise floor, can't be placed"
            if all(m in unweighed for m in members)
            else ""
        )
        lines.append(f"- **group {index}** ({len(members)}): {', '.join(members)}{note}")
    for target, error in summary["no_data"].items():
        lines.append(
            f"- *no data*: {target} — `{error.split(':')[0]}` (excluded; not evidence of anything)"
        )
    if summary["noise_floor_measured"]:
        for target in summary["unmeasured"]:
            lines.append(f"- *no noise floor*: {target} — its second rep failed; not weighed")
    lines.append("")
    verdict = summary["verdict"]
    if verdict == "split":
        lines.append(
            f"**Verdict: split.** {summary['distinguishable_groups']} distinguishable groups. "
            "Members of different groups are *not* serving the same "
            "artifact: expect different quantization, revision, or model. Confirm with a task "
            "eval before assuming the cheaper one is equivalent."
        )
    elif verdict == "consistent":
        lines.append(
            "**Verdict: consistent.** Indistinguishable at this resolution — consistent with "
            "the same weights, but token-level agreement can't see small quality differences; "
            "a distilled model sometimes survives 150 tokens."
        )
    else:
        lines.append(
            "**Verdict: inconclusive.** Fewer than two providers have a measured, "
            "deterministic noise floor (two successful reps each, ≥98% self-agreement), so "
            "differences between them can't be told apart from noise."
        )
    lines.append("")
    lines.append(
        "> Limits: a provider without logprobs support is compared on whole-text "
        "equality only (coarse). Two runs never prove identical weights — they "
        "only fail to disprove it."
    )
    return "\n".join(lines) + "\n"


def mock_fingerprint(target: Target, prompt: str, prompt_text: str, rep: int) -> Fingerprint:
    """Three synthetic 'weight families' so the clustering demo has structure.

    Family 0 is the reference; family 1 shares a long prefix (same base, other
    quantization); family 2 diverges early and tokenizes differently (another
    model). About one target in eleven is non-deterministic. A target's mock
    profile can pin both: `mock = { family = 1, noisy = true }`.
    """
    import hashlib

    prof = target.mock or {}
    digest = int(hashlib.sha256(target.name.encode()).hexdigest(), 16)
    family = int(prof.get("family", digest % 3)) % 3
    tokens = [f"t{i}" for i in range(150)]
    diverge = {0: None, 1: 96, 2: 14}[family]
    if diverge is not None:
        tokens[diverge:] = [f"f{family}_{i}" for i in range(150 - diverge)]
    if rep == 1 and prof.get("noisy", digest % 11 == 0):
        tokens[70:] = [f"alt{i}" for i in range(80)]
    return Fingerprint(
        target=target.name,
        prompt=prompt,
        rep=rep,
        ok=True,
        text=" ".join(tokens),
        tokens=tokens,
        logprobs=[-0.1] * len(tokens),
        prompt_tokens=estimate_tokens(prompt_text) + (9 if family == 2 else 0),
        completion_tokens=len(tokens),
        mode="tokens",
    )


def run_fingerprint(
    cfg: RunConfig,
    outdir: Path,
    max_tokens: int,
    reps: int,
    prompt_count: int,
    mock: bool,
    on_progress: Callable[..., None] | None = None,
) -> tuple[str, list[Fingerprint], dict[str, Any]]:
    prompt_names = list(cfg.prompts)[:prompt_count]
    prints: list[Fingerprint] = []
    total = len(prompt_names) * reps * len(cfg.targets)

    async def go(flush: Callable[[], None]) -> None:
        async with httpx.AsyncClient(
            http2=HTTP2,
            timeout=httpx.Timeout(cfg.timeout_s, connect=15.0),
            headers={"User-Agent": USER_AGENT},
        ) as client:
            for prompt_name in prompt_names:
                text = build_prompt(cfg.prompts[prompt_name])
                for rep in range(reps):
                    for target in cfg.targets:
                        if on_progress is not None:
                            on_progress(
                                done=len(prints),
                                total=total,
                                target=target.name,
                                prompt=prompt_name,
                                rep=rep,
                            )
                        if mock:
                            prints.append(mock_fingerprint(target, prompt_name, text, rep))
                        else:
                            prints.append(
                                await fingerprint_once(
                                    client, target, text, prompt_name, max_tokens, cfg, rep
                                )
                            )
                        flush()

    names = [t.name for t in cfg.targets]
    outdir.mkdir(parents=True, exist_ok=True)

    def render(partial: bool) -> str:
        report = fingerprint_reports(prints, names, prompt_names)
        if partial:
            report = (
                "> **PARTIAL** — interrupted; only the requests that finished are compared.\n\n"
                + report
            )
        if mock:
            report = (
                "> **MOCK** — synthetic data for pipeline testing, not measurements.\n\n" + report
            )
        (outdir / "fingerprint.md").write_text(report, encoding="utf-8")
        return report

    with open(outdir / "fingerprints.jsonl", "w", encoding="utf-8") as handle:
        written = 0

        def flush() -> None:
            nonlocal written
            for item in prints[written:]:
                handle.write(json.dumps(item.__dict__) + "\n")
            written = len(prints)
            handle.flush()

        try:
            asyncio.run(go(flush))
        except KeyboardInterrupt:
            flush()
            if prints:
                render(partial=True)
            raise
        flush()

    report = render(partial=False)
    return report, prints, fingerprint_summary(prints, names, prompt_names)
