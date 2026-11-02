"""tps — command line interface.

Exit codes are part of the contract (scripts and agents depend on them):

    0    success
    1    usage or configuration error (including bad flags)
    2    missing credentials
    3    upstream/provider failure
    4    run completed but every request failed
    130  interrupted (partial results are kept)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, clis, core
from .ui import UI, Bar, Col, Progress

SCHEMA_VERSION = 1
CONFIG_CANDIDATES = ("targets.toml", "targets.gateway.toml")
CLAUDE_API_URL = "https://api.anthropic.com/v1"

STARTER_CONFIG = """\
# tps config — one [[targets]] block per upstream you want to measure.
# Generate it instead of typing it:
#   tps init --endpoint https://api.example.com/v1 --model ID --api-key-env VAR
#   tps discover <author>/<slug> --out targets.toml      (every OpenRouter endpoint)

[run]
reps = 7
concurrency = [1, 4]
warmup = 2
# temperature = 0.0    # the default; never sent to api = "anthropic" targets
prompts = ["prose-256"]
# baseline = "my-direct-endpoint"   # adds Δ columns to the report

[[targets]]
name = "my-direct-endpoint"
base_url = "https://api.example.com/v1"
model = "some-model-id"
api_key_env = "EXAMPLE_API_KEY"
price = { in_per_1m = 0.30, out_per_1m = 1.20 }

[[targets]]
name = "same-model-via-gateway"
base_url = "https://openrouter.ai/api/v1"
model = "vendor/model-name"
api_key_env = "OPENROUTER_API_KEY"
extra_body.provider = { only = ["example-provider"], allow_fallbacks = false }
"""


class Interrupted(core.TpsError):
    """Ctrl-C during a run; whatever finished is already on disk."""

    exit_code = 130


class Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad usage, which would read as "missing credentials".
    Raise a UsageError instead so bad flags exit 1 and honour --json."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise core.UsageError(f"{message} — see `{self.prog} --help`")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def resolve_config(path: Path | None) -> Path:
    if path:
        if not path.exists():
            raise core.UsageError(f"config not found: {path}")
        return path
    for candidate in CONFIG_CANDIDATES:
        if Path(candidate).exists():
            return Path(candidate)
    raise core.UsageError("no config found — run `tps init` to scaffold one, or pass -c <file>")


def require_keys(cfg: core.RunConfig) -> None:
    missing = [t for t in cfg.targets if t.needs_key()]
    if missing:
        envs = sorted({t.api_key_env for t in missing if t.api_key_env})
        raise core.MissingKeyError(
            f"missing credentials for: {', '.join(t.name for t in missing)}"
            + (
                f" — set {', '.join(envs)}"
                if envs
                else " — name the env var that holds the key (api_key_env, or --api-key-env)"
            )
            + " (or run with --mock)"
        )
    missing_cli = missing_clis(cfg)
    if missing_cli:
        raise core.UsageError(
            "CLI not installed for: "
            + ", ".join(f"{t.name} (`{t.cli_binary()}`)" for t in missing_cli)
            + " — install it and sign in, or set `cli` to its path"
        )


def missing_clis(cfg: core.RunConfig) -> list[core.Target]:
    return [t for t in cfg.targets if t.is_cli and not t.cli_path()]


def cli_checks(cfg: core.RunConfig) -> list[tuple[str, str, bool]]:
    """One doctor row per distinct client: installed, version, signed in, plan.
    The backend reads the sign-in; the account email is never shown."""
    rows = []
    seen: set[str] = set()
    for target in cfg.targets:
        if not target.is_cli or target.cli_binary() in seen:
            continue
        binary = target.cli_binary()
        seen.add(binary)
        path = target.cli_path()
        if not path:
            rows.append((binary, "not found on PATH — install it, or set `cli` to its path", False))
            continue
        try:
            version = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=20
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            rows.append((binary, f"{path} · can't query it: {type(exc).__name__}", False))
            continue
        auth = target.backend.auth(path)
        where = version or path
        if auth.signed_in is None:
            rows.append((binary, f"{where} · sign-in unknown — {auth.why}", False))
        elif not auth.signed_in:
            rows.append((binary, f"{where} · not signed in — sign in with `{binary}` first", False))
        else:
            how = ", ".join(x for x in (auth.method, (auth.plan or "").lower() or None) if x)
            detail = f"{where} · signed in" + (f" ({how})" if how else "")
            if not auth.subscription:
                detail += " — not a subscription sign-in; usage is billed to that account"
            rows.append((binary, detail, True))
    return rows


def label_plans(cfg: core.RunConfig) -> None:
    """Name the plan each CLI target is signed in to ("Max"), for headers and
    reports. An unreadable status just leaves it unnamed ("Claude plan")."""
    cache: dict[str, str | None] = {}
    for target in cfg.targets:
        path = target.cli_path() if target.is_cli else None
        if not path:
            continue
        if path not in cache:
            auth = target.backend.auth(path)
            cache[path] = auth.plan if auth.signed_in and auth.subscription else None
        target.plan = cache[path]


def plan_title(rows: list[dict[str, Any]]) -> str:
    """ "Claude Max plan" — or just "plan" when the client didn't say which."""
    plans = sorted({r.get("plan") for r in rows} - {None})
    apis = {r.get("api") for r in rows}
    vendor = clis.backend(next(iter(apis))).vendor + " " if len(apis) == 1 else ""
    return f"{vendor}{plans[0]} plan" if len(plans) == 1 else f"{vendor}plan".strip()


def _price_pair(text: str | None) -> tuple[float, float] | None:
    if text is None:
        return None
    try:
        price_in, price_out = (float(p) for p in text.split(","))
    except ValueError:
        raise core.UsageError(
            f"--price takes IN,OUT in USD per 1M tokens, e.g. 0.14,0.42 — got {text!r}"
        ) from None
    if not all(math.isfinite(p) and p >= 0 for p in (price_in, price_out)):
        raise core.UsageError(f"--price takes two finite, non-negative numbers — got {text!r}")
    return price_in, price_out


def endpoint_config(
    url: str | None,
    args: argparse.Namespace,
    flag: str,
    run_block: str = core.DEFAULT_RUN_TOML,
) -> str | None:
    """A one-target config from flags, for endpoints no gateway lists.

    Returns None when `flag` wasn't given, after rejecting the flags that
    only make sense with it.
    """
    extras = {"--api-key-env": args.api_key_env, "--name": args.name, "--price": args.price}
    if flag == "--base-url":
        extras["--model"] = args.model
    effort = _effort_list(getattr(args, "effort", None), args.api)
    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    if args.api in core.CLI_APIS:
        # a CLI target has no URL, key or price: the vendor's client signs in
        stray = [n for n, v in extras.items() if v is not None and n not in ("--name", "--model")]
        if url is not None:
            stray.insert(0, flag)
        if stray:
            raise core.UsageError(f"{', '.join(stray)} can't apply to --api {args.api}")
        if not args.model:
            raise core.UsageError(
                f"--api {args.api} needs --model, e.g. {clis.backend(args.api).example_model}"
            )
        return core.cli_target_toml(
            args.api,
            models,
            args.name,
            effort,
            source=f"tps {args.command} --api {args.api}",
            run_block=run_block,
        )
    if effort and url is None and args.api is None:
        raise core.UsageError(f"--effort needs {flag} or --api (or set `effort` in a config)")
    if url is None and args.api == "anthropic":
        url = CLAUDE_API_URL  # `--api anthropic` alone means Anthropic's own endpoint
    elif url is None and args.api is not None:
        extras["--api"] = args.api
    if url is None:
        stray = [name for name, value in extras.items() if value is not None]
        if stray:
            verb = "needs" if len(stray) == 1 else "need"
            raise core.UsageError(f"{', '.join(stray)} {verb} {flag} (or set it in a config)")
        return None
    if not args.model:
        raise core.UsageError(f"{flag} needs --model — the model id that endpoint serves")
    return core.endpoint_toml(
        url,
        models,
        args.name,
        args.api_key_env,
        _price_pair(args.price),
        source=f"tps {args.command} {flag}",
        api=args.api or "openai",
        run_block=run_block,
        effort=effort,
    )


def _effort_list(text: str | None, api: str | None) -> list[str] | None:
    """`--effort low,high` → ["low", "high"]; "default" means the model's own."""
    if text is None:
        return None
    levels = [e.strip().lower() for e in text.split(",") if e.strip()]
    allowed = clis.backend(api).efforts if api in core.CLI_APIS else core.EFFORTS_HTTP
    bad = [e for e in levels if e not in (*allowed, "default")]
    if not levels or bad:
        raise core.UsageError(
            f"--effort takes a comma list of {', '.join(allowed)} or default — got {text!r}"
        )
    return levels


def check_mock(cfg: core.RunConfig, path: Path, mock: bool, ui: UI) -> None:
    """`mock = {…}` profiles only ever apply under --mock."""
    if mock:
        return
    profiled = [t.name for t in cfg.targets if t.mock is not None]
    if profiled and len(profiled) == len(cfg.targets):
        raise core.UsageError(
            f"{path} only defines mock targets — add --mock for synthetic data, "
            "or point -c at a real config"
        )
    if profiled:
        ui.warn(f"ignoring mock profiles on {', '.join(profiled)} (they only apply with --mock)")


def _int_list(text: str, flag: str) -> list[int]:
    try:
        return [int(c) for c in text.split(",") if c.strip()]
    except ValueError:
        raise core.UsageError(f"{flag} takes comma-separated integers, got {text!r}") from None


def apply_overrides(cfg: core.RunConfig, args: argparse.Namespace) -> None:
    if getattr(args, "reps", None) is not None:
        cfg.reps = args.reps
    if getattr(args, "concurrency", None) is not None:
        cfg.concurrency = _int_list(args.concurrency, "--concurrency")
    if getattr(args, "prompts", None) is not None:
        wanted = [p.strip() for p in args.prompts.split(",") if p.strip()]
        unknown = [p for p in wanted if p not in cfg.available_prompts]
        if unknown:
            raise core.UsageError(
                f"unknown prompt(s) {unknown}; have {sorted(cfg.available_prompts)}"
            )
        cfg.prompts = {p: cfg.available_prompts[p] for p in wanted}
    if getattr(args, "max_tokens", None) is not None:
        cfg.prompts = {
            name: {**spec, "max_tokens": args.max_tokens} for name, spec in cfg.prompts.items()
        }
    if getattr(args, "temperature", None) is not None:
        cfg.temperature = args.temperature
    if getattr(args, "timeout", None) is not None:
        cfg.timeout_s = args.timeout
    if getattr(args, "no_warmup", False):
        cfg.warmup = 0
    core.validate(cfg, "flags")


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def summarise(samples: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Structured result used by --json and the terminal tables alike.

    Latency/rate fields describe the lowest concurrency (`concurrency`); `ok`,
    `n` and `errors` count every request at every level — the reliability
    signal; `by_concurrency` breaks both down per level.
    """
    stats = core.summarize(samples)
    buckets, per_stream, totals = stats["buckets"], stats["per_stream"], stats["totals"]
    prices = {t["name"]: t for t in meta.get("targets", [])}
    order = {name: i for i, name in enumerate(prices)}
    rows: list[dict[str, Any]] = []
    for prompt, target in sorted(per_stream, key=lambda k: (k[0], order.get(k[1], 1e9), k[1])):
        info = prices.get(target, {})
        st = core.bucket_stats(per_stream[(prompt, target)])
        total = totals[(prompt, target)]
        by_conc = {}
        for (p, t, conc), bucket in sorted(buckets.items()):
            if p != prompt or t != target:
                continue
            level = core.bucket_stats(bucket)
            by_conc[str(conc)] = {
                "ttft_p50_s": _r(level["ttft_p50_s"]),
                "out_tps_p50": _r(level["out_tps_p50"], 2),
                "aggregate_tps": _r(level["aggregate_tps"], 2),
                "ok": level["ok"],
                "n": level["n"],
                "truncated": level["truncated"],
                "reasoning_only": level["reasoning_only"],
                "errors": level["errors"],
            }
        rows.append(
            {
                "prompt": prompt,
                "target": target,
                "model": info.get("model"),
                "base_url": info.get("base_url"),
                "pin": info.get("pin"),
                "concurrency": stats["min_conc"][prompt],
                "ttft_p50_s": _r(st["ttft_p50_s"]),
                "ttft_p90_s": _r(st["ttft_p90_s"]),
                "startup_p50_s": _r(st["startup_p50_s"]),
                "first_content_p50_s": _r(st["first_content_p50_s"]),
                "out_tps_p50": _r(st["out_tps_p50"], 2),
                "out_tps_p90": _r(st["out_tps_p90"], 2),
                "itl_p50_ms": _r(st["itl_p50_ms"], 2),
                "itl_n": st["itl_n"],
                "first_content_n": st["first_content_n"],
                "tokens_per_chunk_median": _r(st["tokens_per_chunk_median"], 2),
                "bursty": st["bursty"],
                "e2e_p50_s": _r(st["e2e_p50_s"]),
                "out_tokens_median": st["out_tokens_median"],
                # every concurrency level, like ok/n (per level: by_concurrency)
                "truncated": total["truncated"],
                "reasoning_only": total["reasoning_only"],
                "hidden_reasoning_tokens_median": st["hidden_reasoning_tokens_median"],
                "notes": core.row_notes(st),
                "ok": total["ok"],
                "n": total["n"],
                "errors": total["errors"],
                "aggregate_tps": {
                    c: v["aggregate_tps"] for c, v in by_conc.items() if v["aggregate_tps"]
                },
                "by_concurrency": by_conc,
                "count_source": st["count_source"],
                "served_by": st["served_by"],
                "api": info.get("api", "openai"),
                "access": info.get("access", "http"),
                "effort": info.get("effort"),
                "plan": info.get("plan"),
                "price_in_per_1m": info.get("price_in"),
                "price_out_per_1m": info.get("price_out"),
            }
        )
    served: dict[str, set[str]] = {}
    for sample in samples:
        if sample.get("served_model") and sample.get("ok"):
            served.setdefault(sample["target"], set()).add(sample["served_model"])
    for row in rows:
        row["served_models"] = sorted(served.get(row["target"], ()))
    return {"rows": rows, "baseline": meta.get("baseline")}


# --------------------------------------------------------------------------
# terminal rendering
# --------------------------------------------------------------------------


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}s"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}ms"


def _thinking(row: dict[str, Any]) -> bool:
    """Some responses at the row's own (lowest) level were reasoning only."""
    return bool(row["by_concurrency"][str(row["concurrency"])].get("reasoning_only"))


def _best(rows: list[dict[str, Any]], key, lowest: bool) -> float | None:
    values = [key(r) for r in rows if key(r) is not None]
    if len(values) < 2:
        return None
    return min(values) if lowest else max(values)


def print_run_table(ui: UI, summary: dict[str, Any], prompt: str) -> None:
    rows = [r for r in summary["rows"] if r["prompt"] == prompt and r["n"]]
    if not rows or ui.silent:
        return
    conc = rows[0]["concurrency"]
    # one box per access mode: CLI TTFT includes client startup, so it is never
    # ranked against HTTP TTFT; tok/s is comparable, so bars share one scale
    peak = max((r["out_tps_p50"] or 0 for r in rows), default=0)
    groups = [g for g in (_by_access(rows, "http"), _by_access(rows, "cli")) if g]
    # a shared first-column width lines the columns up from box to box
    label_w = max(len(x) for g in groups for x in [_row_labels(g)[0], *_row_labels(g)[1]])
    bar_w, width = 24, 0
    if len(groups) > 1 and not any(_is_matrix(g) for g in groups):
        # boxes shown together share one width and one bar width, so their
        # columns line up: size the bars so the widest box needs no shrinking
        args = (summary["baseline"], prompt, conc, peak, True, label_w, bar_w)
        real, ui.width = ui.width, 10**4
        natural = max(_run_box(ui, g, *args, measure=True) for g in groups)
        ui.width = real
        bar_w = max(8, bar_w - max(0, natural - (ui.width - 4)))
        width = natural - (24 - bar_w)
    args = (summary["baseline"], prompt, conc, peak, len(groups) > 1, label_w, bar_w)
    for group in groups:
        if _is_matrix(group):
            inner = _effort_matrix(ui, group, prompt)
        else:
            inner = _run_box(ui, group, *args, min_inner=width)
        if len({_series(r) for r in group}) == 1 and len(_efforts(group)) > 1:
            _effort_cost(ui, group, min_inner=inner)
        if line := effort_takeaway(group):
            ui.say()
            ui.say(f"  {ui.cyan('→')} {line}")


def _effort_rank(effort: str | None) -> int:
    """`default` (None) first, then low → max."""
    return -1 if effort is None else core.EFFORTS_HTTP.index(effort)


def _efforts(rows: list[dict[str, Any]]) -> list[str | None]:
    return sorted({r.get("effort") for r in rows}, key=_effort_rank)


def _provider(row: dict[str, Any]) -> str:
    """The target name without a grid's `/<model>@<effort>` suffix, so rows from
    one grid share a provider while `baseten/fast` and `baseten/turbo` don't."""
    name, model, effort = str(row.get("target") or ""), str(row.get("model")), row.get("effort")
    for suffix in (f"/{model}@{effort}", f"/{model}"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _series(row: dict[str, Any]) -> tuple[str, str]:
    """What an effort comparison holds fixed: where requests go (the client for
    CLI rows; base URL and routing pin for HTTP rows) and the model. Names are
    labels, not identity: `opus-low` and `opus-high` on one endpoint are one series.
    """
    if row.get("access") == "cli":
        where = f"cli:{row.get('api')}"
    else:
        where = f"{row.get('base_url')}|{json.dumps(row.get('pin'), sort_keys=True)}"
    return where, str(row.get("model"))


def _series_names(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Model names, prefixed by provider only when several providers are present."""
    label = {}
    for r in rows:
        label.setdefault(_series(r), (_provider(r), str(r.get("model"))))
    several = len({where for where, _m in label}) > 1
    return {k: f"{p} · {m}" if several else m for k, (p, m) in label.items()}


def _is_matrix(rows: list[dict[str, Any]]) -> bool:
    """Several models (or providers) × several effort levels: a matrix — unless
    two rows would share a cell (the same thing measured twice): then rows."""
    cells = {(_series(r), r.get("effort")) for r in rows}
    return (
        len({_series(r) for r in rows}) > 1 and len(_efforts(rows)) > 1 and len(cells) == len(rows)
    )


def _effort_matrix(ui: UI, rows: list[dict[str, Any]], prompt: str) -> int:
    efforts = _efforts(rows)
    names = _series_names(rows)
    cell = {(_series(r), r.get("effort")): r for r in rows}
    rated = [r["out_tps_p50"] for r in rows if r["out_tps_p50"] and not _thinking(r)]
    best = max(rated) if len(rated) > 1 else None
    table = []
    for key, label in names.items():
        line: list[Any] = [label]
        for effort in efforts:
            r = cell.get((key, effort))
            if r is None:
                line.append(("—", ui.grey))
            elif not r["ok"]:
                line.append(("failed", ui.red))
            elif r["out_tps_p50"] is None or r["ttft_p50_s"] is None:
                line.append(("no answer", ui.yellow))
            else:
                text = f"{r['out_tps_p50']:.1f} · {r['ttft_p50_s']:.1f}s"
                if r["ok"] < r["n"]:
                    line.append((text + " !", ui.yellow))
                else:
                    line.append((text, ui.best if r["out_tps_p50"] == best else None))
        table.append(line)
    cli = rows[0].get("access") == "cli"
    title = f"{prompt} · tok/s · ttft by effort" + (f" · {plan_title(rows)}" if cli else "")
    footer = "p50 · green = fastest tok/s"
    footer += _alias_note(rows)
    if any(0 < r["ok"] < r["n"] for r in rows):
        footer += " · ! = some requests failed"
    if cli:
        startup = core.median([r.get("startup_p50_s") for r in rows])
        footer += " · ttft incl. " + (f"~{startup:.1f}s " if startup else "") + "CLI startup"
    ui.say()
    return ui.table(
        [Col("provider · model" if " · " in next(iter(names.values())) else "model", "l")]
        + [Col(e or "default") for e in efforts],
        table,
        title=title,
        footer=footer,
    )


def _alias_note(rows: list[dict[str, Any]]) -> str:
    """ " · opus = claude-opus-5-5" for models asked for by alias."""
    resolved = {}
    for r in rows:
        ids = r.get("served_models") or []
        if len(ids) == 1 and ids[0] != r.get("model"):
            resolved[r.get("model")] = ids[0]
    return "".join(f" · {alias} = {full}" for alias, full in resolved.items())


def _effort_cost(ui: UI, rows: list[dict[str, Any]], min_inner: int = 0) -> None:
    """One model across effort levels: how TTFT and thinking grow."""
    ordered = sorted(rows, key=lambda r: _effort_rank(r.get("effort")))
    timed = [r for r in ordered if r["ttft_p50_s"] is not None]
    if len(timed) < 2:
        return
    peak = max(r["ttft_p50_s"] for r in timed)
    table = []
    for r in timed:
        thinking = r.get("hidden_reasoning_tokens_median")
        table.append(
            [
                r.get("effort") or "default",
                _secs(r["ttft_p50_s"]),
                Bar(r["ttft_p50_s"], peak, 30, ui.magenta),
                f"~{thinking:.0f} tok" if thinking else "—",
            ]
        )
    ui.say()
    ui.table(
        [Col("effort", "l"), Col("ttft"), Col("", "l", flex=True), Col("thinking")],
        table,
        title=f"{rows[0].get('model')} · what effort costs",
        footer="thinking tokens are generated, unseen, before the first visible token",
        min_inner=min_inner,
    )


def effort_takeaway(rows: list[dict[str, Any]]) -> str | None:
    """One sentence about effort, only when it holds for every model measured:
    tok/s stays flat, and/or TTFT rises with each level."""
    names = _series_names(rows)
    by_model: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["out_tps_p50"] is not None and r["ttft_p50_s"] is not None and not _thinking(r):
            by_model.setdefault(_series(r), []).append(r)
    series = [
        sorted(rs, key=lambda r: _effort_rank(r.get("effort")))
        for rs in by_model.values()
        if len({r.get("effort") for r in rs}) > 1
    ]
    if not series:
        return None
    clauses = []
    spreads = [
        (max(r["out_tps_p50"] for r in s) - min(r["out_tps_p50"] for r in s))
        / max(r["out_tps_p50"] for r in s)
        for s in series
    ]
    if max(spreads) < 0.05:
        clauses.append(f"effort barely moves tok/s (≤{max(spreads):.0%})")
    rising = all(
        all(b["ttft_p50_s"] >= a["ttft_p50_s"] for a, b in zip(s, s[1:], strict=False))
        for s in series
    )
    if rising:
        worst = max(series, key=lambda s: s[-1]["ttft_p50_s"] - s[0]["ttft_p50_s"])
        delta = worst[-1]["ttft_p50_s"] - worst[0]["ttft_p50_s"]
        low, high = (worst[0].get("effort") or "default"), (worst[-1].get("effort") or "default")
        where = f" on {names[_series(worst[0])]}" if len(series) > 1 else ""
        clauses.append(f"it costs ttft: +{delta:.1f}s from {low} to {high}{where}")
    return " · ".join(clauses) or None


def _by_access(rows: list[dict[str, Any]], access: str) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("access", "http") == access]


def _row_labels(rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """The first column: provider names for HTTP rows; for CLI rows, whichever of
    model and effort varies (shared values are in the box title instead)."""
    if rows[0].get("access", "http") != "cli":
        if len({r.get("model") for r in rows}) == 1 and len({r.get("effort") for r in rows}) == 1:
            return "provider", [r["target"] for r in rows]
        # a grid: name rows by what varies, as for CLI rows
        parts = {
            "provider": _provider,
            "model": lambda r: r.get("model"),
            "effort": lambda r: r.get("effort") or "default",
        }
        vary = [k for k, get in parts.items() if len({get(r) for r in rows}) > 1]
        return " · ".join(vary), [" · ".join(str(parts[k](r)) for k in vary) for r in rows]
    vary = [k for k in ("model", "effort") if len({r.get(k) for r in rows}) > 1]
    if not vary:
        if len({r.get("api") for r in rows}) == len(rows):
            return "via", [str(r.get("api")) for r in rows]
        return "target", [r["target"] for r in rows]
    return " · ".join(vary), [" · ".join(str(r.get(k) or "default") for k in vary) for r in rows]


def _run_box(
    ui: UI,
    rows: list[dict[str, Any]],
    baseline: str | None,
    prompt: str,
    conc: int,
    peak: float,
    mixed: bool,
    label_w: int = 0,
    bar_w: int = 24,
    min_inner: int = 0,
    measure: bool = False,
) -> int:
    level = str(conc)
    cli = rows[0].get("access") == "cli"
    base = next((r for r in rows if r["target"] == baseline), None)
    show_delta = bool(base and base["ttft_p50_s"] is not None and base["out_tps_p50"])

    # a rate that timed thinking, not an answer, can't be "fastest"
    best_tps = _best(rows, lambda r: None if _thinking(r) else r["out_tps_p50"], lowest=False)
    best_ttft = _best(rows, lambda r: r["ttft_p50_s"], lowest=True)
    # a bursty ITL measures the proxy's flushing; it can't win "lowest ITL"
    best_itl = _best(rows, lambda r: None if r["bursty"] else r["itl_p50_ms"], lowest=True)
    cheapest = _best(rows, lambda r: r["price_out_per_1m"], lowest=True)

    def mark(value, best):
        return ui.best if best is not None and value == best else None

    first_col, labels = _row_labels(rows)
    labels = [label.ljust(label_w) for label in labels]
    cols = [
        Col(first_col.ljust(label_w), "l"),
        Col("ttft"),
        Col("tok/s"),
        Col("", "l", flex=True),
        Col("itl", drop=1),
        Col("tok/chunk", drop=5),
        Col("ok"),
        Col("$/1M out", drop=2),
    ]
    if show_delta:
        cols += [Col("Δ ttft", drop=3), Col("Δ tok/s", drop=4)]

    estimated = False
    table = []
    for r, label in zip(rows, labels, strict=True):
        est = core.is_estimated(r["count_source"])
        estimated |= est
        ok, n = r["by_concurrency"][level]["ok"], r["by_concurrency"][level]["n"]
        ok_style = ui.green if ok == n else ui.red if ok == 0 else ui.yellow
        is_best = best_tps is not None and r["out_tps_p50"] == best_tps
        price = (
            ("plan", ui.grey)
            if cli and r["price_out_per_1m"] is None
            else (core.fmt(r["price_out_per_1m"], 2), mark(r["price_out_per_1m"], cheapest))
        )
        cells: list[Any] = [
            (label, ui.bold if is_best else None),
            (_secs(r["ttft_p50_s"]), mark(r["ttft_p50_s"], best_ttft)),
            (("≈" if est else "") + core.fmt(r["out_tps_p50"]), mark(r["out_tps_p50"], best_tps)),
            Bar(r["out_tps_p50"], peak, bar_w, ui.green if is_best else ui.cyan),
            (
                (_ms(r["itl_p50_ms"]) + "†", ui.yellow)
                if r["bursty"]
                else (_ms(r["itl_p50_ms"]), mark(r["itl_p50_ms"], best_itl))
            ),
            core.fmt(r["tokens_per_chunk_median"]),
            (f"{ok}/{n}", ok_style),
            price,
        ]
        if show_delta:
            if r is base:
                cells += [("base", ui.grey), ("base", ui.grey)]
            else:
                dt = r["ttft_p50_s"] - base["ttft_p50_s"] if r["ttft_p50_s"] is not None else None
                ds = (
                    (r["out_tps_p50"] - base["out_tps_p50"]) / base["out_tps_p50"] * 100
                    if r["out_tps_p50"] is not None
                    else None
                )
                cells += [
                    ("—", None) if dt is None else (f"{dt:+.2f}s", ui.red if dt > 0 else ui.green),
                    ("—", None) if ds is None else (f"{ds:+.0f}%", ui.red if ds < 0 else ui.green),
                ]
        table.append(cells)

    title = f"{prompt} · single stream" if conc == 1 else f"{prompt} · c={conc}"
    legend = "p50 · ttft first token · tok/s decode rate · itl gap between chunks"
    if cli:
        title += " · " + plan_title(rows)
        shared = [_resolved(rows[0])] if _shared(rows, "model") else []
        if _shared(rows, "effort"):
            shared.append(f"effort {rows[0]['effort']}")
        if shared and _row_labels(rows)[0] != "target":
            title += " · " + " · ".join(shared)
        startup = core.median([r.get("startup_p50_s") for r in rows])
        legend = "p50 · ttft incl. " + (f"~{startup:.1f}s " if startup else "")
        legend += "CLI startup — compare within this box · tok/s decode rate"
    else:
        title += " · API" if mixed else ""
        if _row_labels(rows)[0] not in ("provider", "target"):
            if _shared(rows, "model"):
                title += f" · {rows[0]['model']}"
            if _shared(rows, "effort"):
                title += f" · effort {rows[0]['effort']}"
    if estimated:
        legend += " · ≈ chars/4 estimate"
    if any(r["bursty"] for r in rows):
        legend += " · † chunks arrived in bursts"
    if measure:
        return ui.table(cols, table, title=title, footer=legend, measure=True)
    ui.say()
    inner = ui.table(cols, table, title=title, footer=legend, min_inner=min_inner)
    for r in rows:
        for note in r["notes"]:
            if cli and note.startswith("CLI target:"):
                continue  # the box's footer says it once
            if note.startswith("TTFT includes ~") and len(_efforts(rows)) > 1:
                continue  # the "what effort costs" box shows thinking per level
            first, *rest = _wrap(f"{r['target']}: {note}", ui.width - 6)
            ui.warn(first)
            for line in rest:
                ui.say("    " + line)
    return inner


def _resolved(row: dict[str, Any]) -> str:
    """The model as asked for, plus what it resolved to when that differs."""
    ids = row.get("served_models") or []
    asked = str(row.get("model"))
    return f"{asked} ({ids[0]})" if len(ids) == 1 and ids[0] != asked else asked


def _shared(rows: list[dict[str, Any]], key: str) -> bool:
    values = {r.get(key) for r in rows}
    return len(values) == 1 and None not in values


def print_load_table(ui: UI, summary: dict[str, Any], prompt: str) -> None:
    rows = [r for r in summary["rows"] if r["prompt"] == prompt and r["n"]]
    levels = sorted({int(c) for r in rows for c in r["by_concurrency"]})
    if ui.silent or len(levels) < 2:
        return
    low, high = levels[0], levels[-1]
    cols = (
        [Col("provider", "l")] + [Col(f"c={c}") for c in levels] + [Col("scaling", "l"), Col("ok")]
    )
    peak = max(
        (r["by_concurrency"].get(str(high), {}).get("aggregate_tps") or 0 for r in rows), default=0
    )
    table = []
    for r in rows:
        by = r["by_concurrency"]
        cells: list[Any] = [r["target"]]
        for c in levels:
            agg = by.get(str(c), {}).get("aggregate_tps")
            style = ui.best if c == high and agg and agg == peak and len(rows) > 1 else None
            cells.append((core.fmt(agg), style))
        lo_agg = by.get(str(low), {}).get("aggregate_tps")
        hi_agg = by.get(str(high), {}).get("aggregate_tps")
        if lo_agg and hi_agg:
            ratio = hi_agg / lo_agg
            efficiency = ratio / (high / low)
            if efficiency >= 0.6:
                cells.append((f"{ratio:.1f}× scales", ui.green))
            elif efficiency < 0.35:
                cells.append((f"{ratio:.1f}× queues", ui.yellow))
            else:
                cells.append((f"{ratio:.1f}× partial", None))
        else:
            cells.append(("—", None))
        ok = sum(by[str(c)]["ok"] for c in levels[1:] if str(c) in by)
        n = sum(by[str(c)]["n"] for c in levels[1:] if str(c) in by)
        cells.append((f"{ok}/{n}", ui.green if ok == n else ui.red if ok == 0 else ui.yellow))
        table.append(cells)
    ui.say()
    ui.table(
        cols,
        table,
        title=f"{prompt} · under load · aggregate tok/s",
        footer=f"all tokens in a batch ÷ its wall clock · scaling = c={high} vs c={low}",
    )


def _display_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    """What callouts call each row: the box's own label ("plan · low" for CLI
    rows), so a name in a callout matches a row in a box."""
    names = {r["target"]: r["target"] for r in rows}
    for access in ("http", "cli"):
        group = _by_access(rows, access)
        if not group:
            continue
        header, labels = _row_labels(group)
        if header in ("target", "provider"):
            continue
        prefix = "plan · " if access == "cli" else ""
        if header == "effort":
            labels = [f"effort {lab}" for lab in labels]
        names.update({r["target"]: prefix + lab for r, lab in zip(group, labels, strict=True)})
    return names


def print_rankings(ui: UI, summary: dict[str, Any], prompt: str) -> None:
    rows = [r for r in summary["rows"] if r["prompt"] == prompt and r["ok"]]
    if ui.silent or len(rows) < 2:
        return
    name = _display_names(rows)
    lines = []
    rated = [r for r in rows if r["out_tps_p50"] is not None and not _thinking(r)]
    if rated:
        fastest = max(rated, key=lambda r: r["out_tps_p50"])
        lines.append(
            ("▲", "fastest", name[fastest["target"]], f"{fastest['out_tps_p50']:.1f} tok/s")
        )
    # CLI TTFT includes client startup: it only competes with other CLI rows
    timed = [r for r in rows if r["ttft_p50_s"] is not None]
    http = [r for r in timed if r.get("access", "http") == "http"]
    cli = [r for r in timed if r.get("access") == "cli"]
    if timed:
        pool = http or cli
        snappiest = min(pool, key=lambda r: r["ttft_p50_s"])
        detail = f"{snappiest['ttft_p50_s']:.2f}s to first token"
        if http and cli:
            on_plan = min(cli, key=lambda r: r["ttft_p50_s"])
            detail += ui.grey(
                f"  · on the plan: {name[on_plan['target']].removeprefix('plan · ')} "
                f"{on_plan['ttft_p50_s']:.2f}s"
            )
        lines.append(("●", "snappiest", name[snappiest["target"]], detail))
    priced = [r for r in rows if r["price_out_per_1m"] is not None]
    if len({r["price_out_per_1m"] for r in priced}) > 1:
        cheapest = min(priced, key=lambda r: r["price_out_per_1m"])
        lines.append(
            (
                "$",
                "cheapest",
                name[cheapest["target"]],
                f"${cheapest['price_out_per_1m']:.2f} / 1M out",
            )
        )
    width = max(len(name) for _i, _l, name, _d in lines)
    ui.say()
    for icon, label, name, detail in lines:
        ui.say(
            f"  {ui.cyan(icon)} {ui.grey(label.ljust(10))} {ui.bold(name.ljust(width))}  {detail}"
        )


def render_run(ui: UI, summary: dict[str, Any], prompts: list[str]) -> None:
    for prompt in prompts:
        print_run_table(ui, summary, prompt)
        print_load_table(ui, summary, prompt)
        print_rankings(ui, summary, prompt)


def attach_cost(summary: dict[str, Any], cost: dict[str, Any]) -> None:
    """Put each prompt × target's share of the run spend on its JSON row.

    8 decimals, not 6: a single cheap request can cost less than $1e-6, and a row
    rounded to 0.0 while the total is non-zero reads like a bug to a consumer.
    """
    for row in summary["rows"]:
        row["cost_usd"] = _r(cost["by_prompt"].get((row["prompt"], row["target"])), 8)


def _plan_spend(cost: dict[str, Any]) -> str | None:
    """ "Claude Max plan: 12 requests, 5-hour window 6% → 9%" for CLI targets."""
    plan = cost.get("plan")
    if not plan:
        return None
    name = f"{plan['plans'][0]} plan" if len(plan["plans"]) == 1 else "plan"
    text = f"{_n(plan['requests'], 'request')} on your {name}"
    first, last = plan["utilization_first"], plan["utilization_last"]
    if first is not None and last is not None:
        window = {"five_hour": "5-hour", "seven_day": "7-day"}.get(plan["window"], "usage")
        text += f", {window} window {first:.0%} → {last:.0%}"
    return text


def print_spend(ui: UI, cost: dict[str, Any]) -> None:
    if ui.silent:
        return
    plan = _plan_spend(cost)
    if cost["total"] is None and plan:
        unpriced = [t for t in cost["unpriced"] if t not in cost.get("plan_targets", [])]
        detail = plan + (f" · no price, not counted: {', '.join(unpriced)}" if unpriced else "")
        ui.say()
        ui.hint(f"  est. spend this run: {detail}")
        return
    if cost["total"] is None:
        detail = "unknown — no successful request on a target with both prices"
        if cost["unpriced"]:
            detail += f" · no price, not counted: {', '.join(cost['unpriced'])}"
        ui.say()
        ui.hint(f"  est. spend: {detail}")
        return
    detail = f"${cost['total']:.4f} on measured requests · configured prices · warm-ups excluded"
    unpriced = [t for t in cost["unpriced"] if t not in cost.get("plan_targets", [])]
    if plan:
        detail = f"${cost['total']:.4f} on API · {plan}"
    if unpriced:
        detail += f" · no price, not counted: {', '.join(unpriced)}"
    ui.say()
    ui.hint(f"  est. spend this run: {detail}")


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, max(30, width), break_on_hyphens=False) or [""]


def print_fingerprint(
    ui: UI, summary: dict[str, Any], prints: list[core.Fingerprint], targets: list[str]
) -> None:
    if ui.silent:
        return
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    prompt = summary["prompt_used"]
    present = [t for t in targets if t not in summary["no_data"]]
    group_of = {t: i for i, g in enumerate(summary["clusters"]) for t in g}

    if 2 <= len(present) <= 12:
        letters = "ABCDEFGHIJKL"
        name_w = min(26, max(len(t) for t in present))
        label_w = name_w + 4
        grid = label_w + 5 * len(present) + 1
        title = f" agreement · {prompt} · % identical tokens "
        plain = "■ ≥99  ■ 90–99  ■ 50–90  ■ <50  ■ self"
        # a two-target grid is narrower than its own title and legend
        width = max(grid, len(title) + 1, len(plain) + 2)
        ui.say()
        ui.say(ui.grey("╭─") + ui.bold(title) + ui.grey("─" * max(0, width - len(title) - 1) + "╮"))
        head = " " * label_w + "".join(f"{letters[i]:>4} " for i in range(len(present)))
        ui.say(ui.grey("│") + ui.dim(head.ljust(width)) + ui.grey("│"))
        for i, a_name in enumerate(present):
            label = f" {letters[i]}  {a_name[:name_w]:<{name_w}}"
            cells = []
            for b_name in present:
                if a_name == b_name:
                    cells.append(ui.heat(summary["self_agreement"].get(a_name), diagonal=True))
                    continue
                frac, _ = core.compare_prints(
                    by_key[(a_name, prompt, 0)], by_key[(b_name, prompt, 0)]
                )
                cells.append(ui.heat(frac))
            colour = ui.group(group_of.get(a_name, 0))
            pad = " " * (1 + width - grid)
            ui.say(ui.grey("│") + colour(label) + "".join(cells) + pad + ui.grey("│"))
        swatch = lambda code, text: ui.style("■", code) + " " + ui.grey(text)  # noqa: E731
        legend = "  ".join(
            [
                swatch("\033[38;5;28m", "≥99"),
                swatch("\033[38;5;178m", "90–99"),
                swatch("\033[38;5;130m", "50–90"),
                swatch("\033[38;5;88m", "<50"),
                swatch("\033[38;5;238m", "self"),
            ]
        )
        ui.say(ui.grey("├" + "─" * width + "┤"))
        ui.say(ui.grey("│") + " " + legend + " " * max(0, width - len(plain) - 1) + ui.grey("│"))
        ui.say(ui.grey("╰" + "─" * width + "╯"))

    ui.say()
    tokens = {}
    for target, count in summary["prompt_tokens"].items():
        if count is not None:  # the input's count, valid even if the reply failed
            tokens.setdefault(count, []).append(target)
    if len(tokens) > 1:
        ui.warn(f"tokenizer: {len(tokens)} different prompt-token counts for the same input")
        for count, names in sorted(tokens.items()):
            for i, line in enumerate(_wrap(", ".join(names), ui.width - 20)):
                ui.say(f"      {ui.grey(f'{count:>5} tok' if i == 0 else ' ' * 9)}  {line}")

    noisy = set(summary["noisy"])
    unweighed = noisy | set(summary["unmeasured"])
    digits = len(str(max((len(g) for g in summary["clusters"]), default=1)))
    for index, group in enumerate(summary["clusters"]):
        colour = ui.group(index)
        tag = (
            " (no deterministic noise floor — can't be placed)"
            if all(t in unweighed for t in group)
            else ""
        )
        lines = _wrap(", ".join(group) + tag, ui.width - 22)
        label = f"group {index + 1}"
        count = ui.grey(f"({len(group):>{digits}})")
        ui.say(f"  {colour('●')} {ui.bold(label)}  {count}  {lines[0]}")
        for line in lines[1:]:
            ui.say(" " * (17 + digits) + line)
    for target, error in summary["no_data"].items():
        ui.say(
            f"  {ui.grey('○')} {ui.grey('no data')}  {target} {ui.grey('— ' + error.split(':')[0])}"
        )
    for target in summary["noisy"]:
        value = summary["self_agreement"].get(target, 0)
        ui.say(
            f"  {ui.yellow('~')} {ui.grey('noisy')}    {target} {ui.grey(f'— self-agreement {value * 100:.0f}%')}"
        )
    if summary["noise_floor_measured"]:
        for target in summary["unmeasured"]:
            ui.say(
                f"  {ui.yellow('?')} {ui.grey('no floor')} {target} {ui.grey('— second rep failed; not weighed')}"
            )
    else:
        ui.warn("noise floor not measured — needs two successful reps per provider (--reps 2)")

    ui.say()
    verdict = summary["verdict"]
    if verdict == "split":
        firm = summary["distinguishable_groups"]
        ui.say(
            f"  {ui.badge('SPLIT', 'warn')} {firm} distinguishable groups — not the same artifact."
        )
        ui.hint("          Run a task eval before treating them as equivalent.")
    elif verdict == "consistent":
        ui.say(
            f"  {ui.badge('CONSISTENT', 'ok')} indistinguishable at this resolution — "
            "consistent with the same weights."
        )
    else:
        ui.say(
            f"  {ui.badge('INCONCLUSIVE', 'muted')} fewer than two providers with a measured, "
            "deterministic noise floor."
        )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0] + " (need 3.11+)")
    try:
        import httpx

        add("httpx", True, httpx.__version__)
    except ImportError as exc:  # pragma: no cover
        add("httpx", False, str(exc))
        httpx = None  # type: ignore

    add(
        "http2",
        core.HTTP2,
        "h2 installed" if core.HTTP2 else 'optional — pip install "httpx[http2]"',
        critical=False,
    )

    config_path = None
    cfg = None
    try:
        config_path = resolve_config(args.config)
        cfg = core.load_config(config_path)
        add("config", True, f"{config_path} · {len(cfg.targets)} targets")
    except core.TpsError as exc:
        add("config", False, str(exc))

    mock_only = bool(cfg and all(t.mock is not None for t in cfg.targets))
    if cfg:
        missing = [t for t in cfg.targets if t.needs_key()]
        if mock_only:
            detail = "not needed — mock-only config (use --mock)"
        elif missing:
            envs = sorted({t.api_key_env for t in missing if t.api_key_env})
            detail = (
                f"missing for {len(missing)} target(s): {', '.join(t.name for t in missing[:4])}"
            )
            detail += f"{' …' if len(missing) > 4 else ''}" + (
                f" — set {', '.join(envs)}" if envs else ""
            )
        else:
            detail = "all set"
        add("credentials", mock_only or not missing, detail)
        for binary, detail, ok in cli_checks(cfg):
            add(f"cli {binary}", ok, detail)

        if args.network and httpx is not None:

            async def probe() -> list[tuple[str, str]]:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(5.0), follow_redirects=True
                ) as client:

                    async def one(target: core.Target) -> tuple[str, str]:
                        try:
                            response = await client.get(target.base_url)
                            return target.name, f"HTTP {response.status_code}"
                        except Exception as exc:  # noqa: BLE001 - report, never crash
                            return target.name, type(exc).__name__

                    return await asyncio.gather(*(one(t) for t in cfg.targets if not t.is_cli))

            for name, detail in asyncio.run(probe()):
                # any HTTP status means the host answered; exceptions mean it didn't
                add(f"reach {name}", detail.startswith("HTTP"), detail, critical=False)

    failed = [c for c in checks if c["critical"] and not c["ok"]]
    if not failed:
        exit_code = 0
    elif all(c["check"] == "credentials" for c in failed):
        exit_code = 2
    else:
        exit_code = 1

    ui.header("doctor", "environment · config · credentials")
    ui.say()
    width = max(len(c["check"]) for c in checks) + 3
    for item in checks:
        line = ui.kv(item["check"], item["detail"], width)
        if item["ok"]:
            ui.ok(line)
        elif item["critical"]:
            ui.fail(line)
        else:
            ui.warn(line)
    ui.say()
    if not failed:
        run = f"tps run -c {config_path}" + (" --mock" if mock_only else "")
        ui.say(f"  {ui.green(ui.bold('Ready.'))} {ui.grey('next →')} {run}")
    else:
        ui.say(ui.grey("  Fix the ✗ rows above, then re-run `tps doctor`."))
    return {"command": "doctor", "ok": not failed, "checks": checks, "exit_code": exit_code}


def cmd_init(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    out = args.out
    if out.exists() and not args.force:
        raise core.UsageError(f"{out} exists — pass --force to overwrite")

    text = endpoint_config(args.endpoint_url, args, "--endpoint")
    ui.header("init")
    ui.say()
    if text is not None:
        expanded = core.config_from_text(text, "--endpoint").targets
        made = expanded[0]
        url = None if made.is_cli else core.redact_url(made.base_url)
        out.write_text(text, encoding="utf-8")
        where = f"via `{made.cli_binary()}` (your plan)" if made.is_cli else f"at {url}"
        what = (
            f"1 target: {made.model}"
            if len(expanded) == 1
            else f"{_n(len(expanded), 'target')} (models × effort levels)"
        )
        ui.ok(f"wrote {ui.bold(str(out))} — {what} {where}")
        export = f"export {args.api_key_env}=… && " if args.api_key_env else ""
        ui.hint(f"  next → {export}tps run -c {out} --dry-run   (then without --dry-run)")
        return {
            "command": "init",
            "ok": True,
            "config": str(out),
            "model": args.model,
            "targets": len(expanded),
            "endpoint": url,
        }
    if args.model:
        canonical, endpoints, _payload = core.fetch_endpoints(
            args.model, os.environ.get("OPENROUTER_API_KEY")
        )
        text, count, collapsed = core.targets_toml(canonical, endpoints)
        out.write_text(text, encoding="utf-8")
        note = f" ({collapsed} duplicate endpoint(s) collapsed)" if collapsed else ""
        ui.ok(f"wrote {ui.bold(str(out))} — {count} pinned targets for {canonical}{note}")
        ui.hint(f"  next → export OPENROUTER_API_KEY=… && tps run -c {out}")
        return {
            "command": "init",
            "ok": True,
            "config": str(out),
            "model": canonical,
            "targets": count,
        }

    out.write_text(STARTER_CONFIG, encoding="utf-8")
    ui.ok(f"wrote {ui.bold(str(out))} (starter template)")
    ui.hint(f"  next → edit the targets, then: tps doctor && tps run -c {out}")
    ui.hint("  or generate one: tps init --endpoint URL --model ID --api-key-env VAR --force")
    ui.hint(
        "               or: tps init --model deepseek/deepseek-v4.1-flash --force  (OpenRouter)"
    )
    return {"command": "init", "ok": True, "config": str(out), "model": None, "targets": 0}


QUANT_STYLE = {"fp4": "magenta", "fp6": "magenta", "fp8": "cyan", "bf16": "green", "fp16": "green"}


def cmd_discover(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    canonical, endpoints, payload = core.fetch_endpoints(
        args.model, args.api_key or os.environ.get("OPENROUTER_API_KEY"), args.from_json
    )
    rows = core.normalize_endpoints(endpoints)
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    ui.header("discover", f"{canonical} · {len(endpoints)} endpoints")
    shown = sorted(rows, key=lambda r: (r["price_out_per_1m"] is None, r["price_out_per_1m"] or 0))
    peak = max((r["price_out_per_1m"] or 0 for r in rows), default=0)
    cheapest = _best(rows, lambda r: r["price_out_per_1m"], lowest=True)
    table = []
    for row in shown:
        quant = row["quantization"] or "—"
        style = getattr(ui, QUANT_STYLE.get(str(quant).lower(), "grey"))
        price_out = row["price_out_per_1m"]
        table.append(
            [
                row["provider"] or "—",
                (row["tag"] or row["slug"], ui.grey),
                (quant, style),
                core.fmt(row["price_in_per_1m"], 3),
                (
                    core.fmt(price_out, 3),
                    ui.best if cheapest is not None and price_out == cheapest else None,
                ),
                Bar(price_out, peak, 16, ui.cyan),
                f"{row['context_length'] // 1000}k" if row["context_length"] else "—",
            ]
        )
    ui.say()
    ui.table(
        [
            Col("provider", "l"),
            Col("pin", "l"),
            Col("quant", "l"),
            Col("$/1M in"),
            Col("$/1M out"),
            Col("", "l", flex=True),
            Col("ctx", drop=1),
        ],
        table,
        title=f"{canonical} · sorted by output price",
        footer="prices per 1M tokens from OpenRouter · quantization as reported by the provider",
    )

    written = None
    count = 0
    if args.out:
        text, count, collapsed = core.targets_toml(canonical, endpoints)
        args.out.write_text(text, encoding="utf-8")
        written = str(args.out)
        ui.say()
        ui.ok(f"wrote {ui.bold(written)} — {count} pinned targets")
        if collapsed:
            ui.hint(f"  {collapsed} duplicate endpoint(s) collapsed into the same pin")
        ui.hint(f"  next → tps run -c {written}")
    return {
        "command": "discover",
        "ok": True,
        "model": canonical,
        "endpoints": rows,
        "config": written,
        "targets": count,
    }


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _models_label(cfg: core.RunConfig) -> str:
    return core.model_label(core.model_ids([t.model for t in cfg.targets]))


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def run_plan(
    ui: UI, cfg: core.RunConfig, path: Path | None, args: argparse.Namespace
) -> dict[str, Any]:
    """`tps run --dry-run`: the bill before the run. Nothing is sent."""
    plan = core.plan_run(cfg, mock=args.mock)
    missing = [] if args.mock else [t.name for t in cfg.targets if t.needs_key()]
    no_cli = [] if args.mock else [t.name for t in missing_clis(cfg)]
    plan_billed = [t.name for t in cfg.targets if t.is_cli]
    ui.header(
        getattr(args, "label", "run"),
        f"{_models_label(cfg)} · dry run",
        mock=args.mock,
        plan=any(t.is_cli for t in cfg.targets),
    )
    ui.hint(
        f"  {_n(len(cfg.targets), 'target')} · {_n(len(cfg.prompts), 'prompt')} · "
        f"{_n(cfg.reps, 'rep')} · concurrency {','.join(map(str, cfg.concurrency))}"
    )
    ui.say()
    width = 12
    ui.say(
        "  "
        + ui.kv(
            "requests",
            f"{plan['requests']} ({plan['measured_requests']} measured + "
            f"{plan['warmup_requests']} warm-up)",
            width,
        )
    )
    ui.say(
        "  "
        + ui.kv(
            "tokens",
            f"≈{_k(plan['in_tokens_est'])} in (≤{_k(plan['in_tokens_max'])} by bytes) · "
            f"≤{_k(plan['out_tokens_max'])} out (max_tokens cap)",
            width,
        )
    )
    if args.mock:
        cost = "$0 — --mock sends nothing"
    elif plan["cost_max_usd"] is None:
        cost = "unknown — no target has a price (price = { … } or --price)"
    else:
        spend = plan["cost_max_usd"]
        cost = f"≤ ${spend:.4f}" if spend < 1 else f"≤ ${spend:,.2f}"
        if plan["unpriced"]:
            cost += f" + unpriced: {', '.join(plan['unpriced'])}"
    if plan_billed and not args.mock:
        cost += f" · subscription usage: {', '.join(plan_billed)}"
    ui.say("  " + ui.kv("cost", cost, width))
    assumed = plan["assumed"]
    ui.say(
        "  "
        + ui.kv(
            "time",
            f"≈{plan['est_minutes']:g} min at an assumed {assumed['tok_s']:g} tok/s and "
            f"{assumed['ttft_s']:g}s TTFT",
            width,
        )
    )
    if missing:
        names = ", ".join(missing[:4]) + (f" +{len(missing) - 4} more" if len(missing) > 4 else "")
        ui.say()
        ui.warn(f"missing credentials for {names} — the real run would exit 2")
    if no_cli:
        ui.say()
        ui.warn(f"CLI not installed for {', '.join(no_cli)} — the real run would exit 1")
    ui.say()
    ui.hint("  nothing was sent · drop --dry-run to run it")
    return {
        "command": "run",
        "ok": True,
        "dry_run": True,
        "mock": args.mock,
        "config": str(path) if path else None,
        "model": cfg.targets[0].model,
        "models": core.model_ids([t.model for t in cfg.targets]),
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "prompts": list(cfg.prompts),
        "plan": plan,
        "missing_credentials": missing,
        "missing_clis": no_cli,
        "subscription_targets": plan_billed,
    }


def cmd_run(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    if args.config and (args.endpoint_url or args.api):
        raise core.UsageError("pass either -c FILE or --base-url/--api, not both")
    text = endpoint_config(args.endpoint_url, args, "--base-url")
    if text is None:
        path: Path | None = resolve_config(args.config)
        cfg = core.load_config(path)
    else:
        path = None
        cfg = core.config_from_text(text, "--base-url")
    apply_overrides(cfg, args)
    check_mock(cfg, path or Path("--base-url"), args.mock, ui)
    if args.dry_run:
        return run_plan(ui, cfg, path, args)
    if not args.mock:
        require_keys(cfg)

    outdir = args.out or Path("runs") / stamp()
    if text is not None:
        # Keep the run as a config next to its results — with the settings it
        # actually ran with (--reps, --max-tokens, …), so `-c` repeats it.
        text = endpoint_config(args.endpoint_url, args, "--base-url", core.run_toml(cfg))
        path = outdir / "targets.toml"
        if path.exists() and path.read_text(encoding="utf-8") != text:
            # `-o .` next to a hand-written (or hand-edited) config must never
            # replace it
            raise core.UsageError(
                f"{path} already exists and isn't this run's config — pick another -o"
            )
        outdir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    total = core.planned_requests(cfg)
    if not args.mock:
        label_plans(cfg)
    ui.header(
        getattr(args, "label", "run"),
        _models_label(cfg),
        mock=args.mock,
        plan=any(t.is_cli for t in cfg.targets),
    )
    ui.hint(
        f"  {_n(len(cfg.targets), 'target')} · {_n(len(cfg.prompts), 'prompt')} · {_n(cfg.reps, 'rep')} · "
        f"concurrency {','.join(map(str, cfg.concurrency))} · {total} requests"
    )
    if cfg.reps < 7:
        ui.warn(f"{cfg.reps} reps — differences under ~15% are noise at this sample size")

    with Progress(ui, total, "warming up") as progress:

        def on_batch(**kw: Any) -> None:
            if kw["phase"] == "warmup":
                label = f"warm-up · {kw['target']}"
            else:
                label = f"{kw['target']} · c={kw['concurrency']} · rep {kw['rep'] + 1}/{cfg.reps}"
            progress.update(kw["done"], label)

        try:
            samples = asyncio.run(core.execute(cfg, outdir, args.mock, on_batch=on_batch))
        except KeyboardInterrupt:
            progress.close()
            kept = 0
            try:
                partial, meta = core.load_samples(outdir)
                kept = sum(1 for s in partial if not core.is_warmup(s))
                if kept:
                    core.write_report(outdir, partial, meta)
            except core.TpsError:
                pass
            raise Interrupted(
                f"interrupted — {kept} measured request(s) kept in {outdir}"
                + (f"; partial report at {outdir}/report.md" if kept else "")
            ) from None

    meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
    rows = [s.as_dict() for s in samples]
    core.write_report(outdir, rows, meta)
    summary = summarise(rows, meta)
    cost = core.spend(rows, meta)
    attach_cost(summary, cost)
    render_run(ui, summary, list(cfg.prompts))
    print_spend(ui, cost)

    measured = [s for s in samples if s.phase != "warmup"]
    failed = sum(1 for s in measured if not s.ok)
    ui.say()
    if failed:
        ui.warn(f"{failed}/{len(measured)} requests failed — see Failures in the report")
    ui.ok(f"wrote {ui.bold(f'{outdir}/report.md')}  {ui.grey('· summary.csv · raw.jsonl')}")
    ui.hint(
        f"  next → tps report {outdir}   ·   tps fingerprint -c {path}"
        + (" --mock" if args.mock else "")
    )

    return {
        "command": "run",
        "ok": failed < len(measured),
        "mock": args.mock,
        "run_dir": str(outdir),
        "config": str(path),
        "model": next((t["model"] for t in meta["targets"]), None),
        "models": core.model_ids([t["model"] for t in meta["targets"]]),
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "results": summary["rows"],
        "cost_usd": _r(cost["total"], 8),
        "cost_unpriced": cost["unpriced"],
        "failed": failed,
        # `total` is measured requests only; raw.jsonl also holds the warm-ups
        "total": len(measured),
        "warmup_requests": len(samples) - len(measured),
        "planned_requests": core.planned_requests(cfg),
        # partial failure is data, not a usage error: it shows up in `failed`
        "exit_code": 4 if measured and failed == len(measured) else 0,
        "artifacts": {
            "report": str(outdir / "report.md"),
            "csv": str(outdir / "summary.csv"),
            "raw": str(outdir / "raw.jsonl"),
        },
    }


def cmd_plan(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    """`tps claude [models…]` (one command per CLI backend): benchmark the
    models in your plan. A shortcut for `tps run --api <backend> …` with
    plan-friendly defaults; it runs the very same code, so output, JSON and
    exit codes match `run`.
    """
    backend = clis.backend(args.backend_api)
    models = [m for arg in (args.models or backend.default_models) for m in arg.split(",") if m]
    efforts = None
    if args.effort:
        efforts = ",".join(backend.efforts) if args.effort.strip() == "all" else args.effort
    run_argv = ["run", "--api", backend.api, "--model", ",".join(models)]
    run_argv += ["--max-tokens", str(args.max_tokens), "--reps", str(args.reps)]
    run_argv += ["--prompts", args.prompt]
    if efforts:
        run_argv += ["--effort", efforts]
    if args.out:
        run_argv += ["-o", str(args.out)]
    if args.mock:
        run_argv.append("--mock")
    run_args = build_parser().parse_args(run_argv)
    for flag in ("json", "no_color", "quiet", "verbose"):
        setattr(run_args, flag, getattr(args, flag, False))
    run_args.label = backend.command

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not ui.silent
    if args.dry_run or (interactive and not args.yes and not args.mock):
        run_args.dry_run = True
        plan = cmd_run(run_args, ui)
        if args.dry_run:
            return plan
        try:
            answer = input("  run it on your plan? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            ui.hint("  cancelled — nothing was sent")
            return {"command": "run", "ok": True, "cancelled": True}
        run_args.dry_run = False
    return cmd_run(run_args, ui)


def cmd_report(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    samples, meta = core.load_samples(args.run_dir)
    text = core.write_report(args.run_dir, samples, meta)
    if args.markdown:
        if not args.json:
            print(text)
        return {"command": "report", "ok": True, "run_dir": str(args.run_dir), "report": text}

    flags = " · partial" if meta.get("complete") is False else ""
    model = core.model_label(core.model_ids([t.get("model") for t in meta.get("targets", [])]))
    ui.header(
        "report",
        f"{args.run_dir} · {model} · {meta.get('created', '?')}{flags}",
        mock=bool(meta.get("mock")),
        plan=any(t.get("access") == "cli" for t in meta.get("targets", [])),
    )
    summary = summarise(samples, meta)
    prompts = list(dict.fromkeys(r["prompt"] for r in summary["rows"]))
    cost = core.spend(samples, meta)
    attach_cost(summary, cost)
    render_run(ui, summary, prompts)
    print_spend(ui, cost)
    ui.say()
    ui.ok(f"rewrote {ui.bold(f'{args.run_dir}/report.md')}  {ui.grey('· summary.csv')}")
    return {
        "command": "report",
        "ok": True,
        "run_dir": str(args.run_dir),
        "cost_usd": _r(cost["total"], 8),
        "cost_unpriced": cost["unpriced"],
        "report": text,
    }


def cmd_fingerprint(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    path = resolve_config(args.config)
    cfg = core.load_config(path)
    cli_targets = [t.name for t in cfg.targets if t.is_cli]
    if cli_targets:
        raise core.UsageError(
            f"fingerprint can't use CLI targets ({', '.join(cli_targets)}): a CLI exposes no "
            "logprobs or sampling control — remove them from this config"
        )
    for flag, value in (
        ("--reps", args.reps),
        ("--prompt-count", args.prompt_count),
        ("--max-tokens", args.max_tokens),
    ):
        if value < 1:
            raise core.UsageError(f"{flag} must be at least 1 (got {value})")
    if args.timeout is not None:
        cfg.timeout_s = args.timeout
        core.validate(cfg, "flags")
    check_mock(cfg, path, args.mock, ui)
    if not args.mock:
        require_keys(cfg)
    outdir = args.out or Path("runs") / f"{stamp()}-fingerprint"
    prompt_count = min(args.prompt_count, len(cfg.prompts))
    total = prompt_count * args.reps * len(cfg.targets)
    ui.header("fingerprint", _models_label(cfg), mock=args.mock)
    claude = [t.name for t in cfg.targets if t.api == "anthropic"]
    decoding = "greedy" if not claude else "greedy where allowed (Claude API: sampled)"
    ui.hint(
        f"  {_n(len(cfg.targets), 'target')} · {_n(prompt_count, 'prompt')} · {_n(args.reps, 'rep')} · "
        f"{decoding}, {args.max_tokens} tokens · {total} requests"
    )

    with Progress(ui, total) as progress:
        try:
            report, prints, summary = core.run_fingerprint(
                cfg,
                outdir,
                args.max_tokens,
                args.reps,
                prompt_count,
                args.mock,
                on_progress=lambda **kw: progress.update(
                    kw["done"], f"{kw['target']} · {kw['prompt']} · rep {kw['rep'] + 1}"
                ),
            )
        except KeyboardInterrupt:
            progress.close()
            path = outdir / "fingerprints.jsonl"
            kept = len(path.read_text().splitlines()) if path.exists() else 0
            raise Interrupted(
                f"interrupted — {kept} fingerprint(s) kept in {outdir}"
                + (f"; partial report at {outdir}/fingerprint.md" if kept else "")
            ) from None

    targets = [t.name for t in cfg.targets]
    print_fingerprint(ui, summary, prints, targets)
    ui.say()
    ui.ok(f"wrote {ui.bold(f'{outdir}/fingerprint.md')}  {ui.grey('· fingerprints.jsonl')}")

    any_ok = any(p.ok for p in prints)
    return {
        "command": "fingerprint",
        "ok": any_ok,
        "mock": args.mock,
        "run_dir": str(outdir),
        "report": report,
        **summary,
        "exit_code": 0 if any_ok else 4,
    }


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="tps",
        description="Measure one model across many providers and gateways.",
        epilog=(
            "examples:\n"
            "  tps claude                                 # the models in your Claude plan\n"
            "  tps run -c targets.mock.toml --mock        # 2-second offline demo\n"
            "  tps init --model deepseek/deepseek-v4.1-flash\n"
            "  tps doctor\n"
            "  tps run -c targets.toml --reps 7 --concurrency 1,4\n"
            "  tps fingerprint -c targets.toml\n"
            "\ndocs: https://github.com/teal-sea/tpsbench/tree/main/docs"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"tps {__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("-q", "--quiet", action="store_true", help="only errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="tracebacks on failure")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def common(p: argparse.ArgumentParser) -> None:
        # mirrored on subcommands so `tps run --json` works as well as `tps --json run`
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="one JSON object on stdout",
        )
        p.add_argument(
            "--no-color", action="store_true", default=argparse.SUPPRESS, help="no ANSI colour"
        )
        p.add_argument(
            "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="only errors"
        )
        p.add_argument(
            "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="tracebacks"
        )

    def endpoint_flags(p: argparse.ArgumentParser, flag: str) -> None:
        p.add_argument(
            flag,
            dest="endpoint_url",
            default=None,
            metavar="URL",
            help="any OpenAI-compatible base URL, e.g. https://api.example.com/v1 — no config file",
        )
        p.add_argument(
            "--model",
            default=None,
            help=(
                "model id at that endpoint (a comma list compares several)"
                if flag == "--base-url"
                else f"with {flag}: the model id there; alone: OpenRouter slug to discover"
            ),
        )
        p.add_argument(
            "--api",
            choices=core.APIS,
            default=None,
            help=f"wire format (default openai); anthropic = native Claude API, {CLAUDE_API_URL} "
            "unless a URL is given; "
            + "; ".join(
                f"{b.api} = your {b.vendor} plan via the signed-in `{b.binary}` CLI"
                for b in clis.BACKENDS.values()
            )
            + " (no URL or key)",
        )
        p.add_argument(
            "--effort",
            default=None,
            metavar="LEVELS",
            help="reasoning effort, e.g. high or low,high (a grid: one target per "
            "model × level); default = the model's own",
        )
        p.add_argument("--api-key-env", default=None, help="env var holding the key")
        p.add_argument("--name", default=None, help="target name (default: the host)")
        p.add_argument("--price", default=None, metavar="IN,OUT", help="USD per 1M tokens")

    p_doctor = sub.add_parser("doctor", help="check environment, config and credentials")
    p_doctor.add_argument("-c", "--config", type=Path, default=None, help="config file")
    p_doctor.add_argument("--network", action="store_true", help="also probe base URLs")
    common(p_doctor)

    p_init = sub.add_parser("init", help="scaffold a config")
    endpoint_flags(p_init, "--endpoint")
    p_init.add_argument("--out", type=Path, default=Path("targets.toml"), help="config to write")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing file")
    common(p_init)

    p_disc = sub.add_parser("discover", help="list providers serving a model, with prices")
    p_disc.add_argument("model", help="e.g. deepseek/deepseek-v4.1-flash")
    p_disc.add_argument("--out", type=Path, default=None, help="write a config")
    p_disc.add_argument("--api-key", default=None, help="defaults to OPENROUTER_API_KEY")
    p_disc.add_argument("--from-json", type=Path, default=None, help="offline: recorded response")
    p_disc.add_argument("--json-out", type=Path, default=None, help="save the raw API payload")
    common(p_disc)

    p_run = sub.add_parser("run", help="measure throughput and latency")
    p_run.add_argument("-c", "--config", type=Path, default=None, help="config file")
    p_run.add_argument("-o", "--out", type=Path, default=None, help="run directory to create")
    p_run.add_argument(
        "--dry-run", action="store_true", help="print requests, tokens, cost and time; send nothing"
    )
    endpoint_flags(p_run, "--base-url")
    p_run.add_argument("--reps", type=int, default=None, help="reps per target × prompt × level")
    p_run.add_argument("--concurrency", default=None, help="comma separated levels, e.g. 1,4,16")
    p_run.add_argument("--prompts", default=None, help="comma separated prompt names")
    p_run.add_argument(
        "--max-tokens", type=int, default=None, help="override every prompt's max_tokens"
    )
    p_run.add_argument(
        "--temperature",
        default=None,
        help='a number, or "omit" to not send it (default: 0; not sent to the Claude API)',
    )
    p_run.add_argument("--timeout", type=float, default=None, help="per-request timeout, seconds")
    p_run.add_argument("--mock", action="store_true", help="synthetic numbers, no network")
    p_run.add_argument("--no-warmup", action="store_true", help="skip discarded warm-up requests")
    common(p_run)

    for backend in clis.BACKENDS.values():
        plan_shortcut(sub, backend, common)

    p_rep = sub.add_parser("report", help="re-render a previous run")
    p_rep.add_argument("run_dir", type=Path, help="folder printed by `tps run`")
    p_rep.add_argument("--markdown", action="store_true", help="print report.md instead of tables")
    common(p_rep)

    p_fp = sub.add_parser("fingerprint", help="test whether targets serve the same weights")
    p_fp.add_argument("-c", "--config", type=Path, default=None, help="config file")
    p_fp.add_argument("-o", "--out", type=Path, default=None, help="run directory to create")
    p_fp.add_argument("--max-tokens", type=int, default=160, help="probe length in tokens")
    p_fp.add_argument("--reps", type=int, default=2, help="runs per target (2 = noise floor)")
    p_fp.add_argument("--prompt-count", type=int, default=2, help="how many prompts to probe")
    p_fp.add_argument("--timeout", type=float, default=None, help="per-request timeout, seconds")
    p_fp.add_argument("--mock", action="store_true", help="synthetic fingerprints, no network")
    common(p_fp)

    return parser


def plan_shortcut(sub, backend: clis.CliBackend, common) -> None:
    """`tps <vendor>`: the models in that plan, from the backend's own words."""
    width = max(len(a) for a, _ in backend.examples) + len(backend.command) + 7
    examples = "".join(
        f"  {f'tps {backend.command} {a}'.rstrip():<{width}}{what}\n"
        for a, what in backend.examples
    )
    p = sub.add_parser(
        backend.command,
        help=f"benchmark the models in your {backend.vendor} plan",
        description=(
            f"Benchmark the models in your {backend.vendor} subscription through the signed-in\n"
            f"`{backend.binary}` CLI. Runs on plan usage; nothing is billed per token."
        ),
        epilog=(
            f"models: {backend.model_help}\n"
            f"effort: {', '.join(backend.efforts)}, default, or all\n\n"
            f"examples:\n{examples}\n"
            f"the long form, for configs and scripts: tps run --api {backend.api} --model …"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(backend_api=backend.api)
    p.add_argument(
        "models", nargs="*", metavar="MODEL", help=f"default: {' '.join(backend.default_models)}"
    )
    p.add_argument("-e", "--effort", default=None, metavar="LEVELS", help="e.g. low,high or all")
    p.add_argument(
        "-n", "--reps", type=int, default=5, metavar="N", help="runs per model × level (5)"
    )
    p.add_argument(
        "--prompt",
        default="prose-256",
        metavar="NAME",
        help="prose-256 (default), code-256 or long-context",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        metavar="N",
        help="answer cap (2048: room to think, then answer)",
    )
    p.add_argument(
        "-o", "--out", type=Path, default=None, metavar="DIR", help="run directory to create"
    )
    p.add_argument("--dry-run", action="store_true", help="show the plan; send nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before running")
    p.add_argument("--mock", action="store_true", help="synthetic numbers, no network")
    common(p)


COMMANDS = {
    "doctor": cmd_doctor,
    "init": cmd_init,
    "discover": cmd_discover,
    "run": cmd_run,
    **{command: cmd_plan for command in clis.by_command()},
    "report": cmd_report,
    "fingerprint": cmd_fingerprint,
}


def _safe_streams() -> None:
    """Never crash on a legacy console encoding: degrade glyphs instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _error_payload(command: str | None, code: int, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:
        # stdout closed early (`tps … | head`): stop quietly, like other CLIs
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 141


def _main(argv: list[str] | None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    _safe_streams()
    parser = build_parser()
    want_json = "--json" in argv
    command = next((a for a in argv if a in COMMANDS), None)
    captured = io.StringIO()
    try:
        # in --json mode, --help/--version text is wrapped in the one JSON object
        with contextlib.redirect_stdout(captured) if want_json else contextlib.nullcontext():
            args = parser.parse_args(argv)
    except core.UsageError as exc:
        ui = UI(json_mode=want_json, color=False if "--no-color" in argv else None)
        if want_json:
            ui.emit(_error_payload(command, exc.exit_code, str(exc)))
        else:
            ui.error(str(exc))
        return exc.exit_code
    except SystemExit as exc:  # --help / --version
        if want_json:
            payload: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "command": command,
                "ok": True,
            }
            if "--version" in argv:
                payload["version"] = __version__
            else:
                payload["help"] = captured.getvalue()
            UI(json_mode=True).emit(payload)
        return int(exc.code or 0)

    if not args.command:
        if args.json:
            UI(json_mode=True).emit(_error_payload(None, 1, "no command given — see `tps --help`"))
            return 1
        parser.print_help()
        return 0

    ui = UI(
        json_mode=args.json,
        color=False if args.no_color else None,
        quiet=args.quiet,
    )
    handler = COMMANDS[args.command]

    try:
        payload = handler(args, ui)
        if args.json:
            ui.emit({"schema_version": SCHEMA_VERSION, **payload})
        return int(payload.get("exit_code", 0))
    except core.TpsError as exc:
        if args.json:
            ui.emit(_error_payload(args.command, exc.exit_code, str(exc)))
        else:
            ui.error(str(exc))
        return exc.exit_code
    except BrokenPipeError:
        raise
    except KeyboardInterrupt:
        if args.json:
            ui.emit(_error_payload(args.command, 130, "interrupted"))
        else:
            print("", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, keep it readable
        if args.json:
            ui.emit(_error_payload(args.command, 1, f"{type(exc).__name__}: {exc}"))
        elif args.verbose:
            raise
        else:
            ui.error(f"{type(exc).__name__}: {exc}", hint="re-run with -v for a traceback")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
