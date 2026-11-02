# AGENTS.md

Instructions for coding agents using this repository. Human docs are in
[`docs/`](docs/index.md); this file is the dense, contract-oriented version.

## What this is

`tps` benchmarks **one model across many providers/gateways**. It is a CLI, not a
library API — drive it as a subprocess and parse `--json`.

## Invariants you must not break

1. **Never add automatic retries** for 429/5xx. Failures are data. (The one
   resend is a compatibility fallback: a 400/422 that names `stream_options`
   is resent once without it, and that target stops sending it.)
2. **Never batch targets sequentially by provider.** The run loop rotates target
   order per rep; that interleaving is the point.
3. **`--json` must print exactly one JSON object on stdout.** Human text goes to
   stderr or is suppressed. Exit codes must match the table below.
4. **Token counts come from `usage`**, with a chars/4 fallback that must stay
   flagged in the output (`count_source`). Don't silently estimate.
5. Provider pins use the **full tag slug** (`baseten/fast`). A base slug never
   matches a tier endpoint — see `docs/configuration.md`.
6. Tests must not require network or API keys. `tests/selftest.py` runs against a
   local SSE server and a recorded fixture.
7. **Mock data never leaks.** `mock = {…}` profiles apply only under `--mock`;
   every mock output is flagged (`"mock": true`, `MOCK DATA`). README images come
   from `scripts/render_assets.py` with fictional providers only.
8. **A failed request is not evidence.** Fingerprint targets with no data are
   reported in `no_data`, never clustered as "different weights"; targets
   without a measured, deterministic noise floor never decide the verdict.

## Commands

```bash
tps doctor      [-c FILE] [--network]              # environment + credentials
tps init        [--model SLUG] [--out FILE] [--force]
tps init        --endpoint URL --model ID [--api-key-env VAR] [--name N] [--price IN,OUT]
tps discover    MODEL [--out FILE] [--from-json F] [--json-out F]
tps run         [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4] [--prompts a,b]
                [--max-tokens N] [--temperature T] [--timeout S] [--dry-run] [--mock]
tps run         --base-url URL --model ID [--api-key-env VAR] [--price IN,OUT] [...]
tps report      RUN_DIR [--markdown]
tps fingerprint [-c FILE] [--reps N] [--prompt-count N] [--max-tokens N] [--timeout S] [--mock]
```

`--base-url` measures one OpenAI-compatible endpoint with no config file (not
with `-c`); the generated config is saved as `<run_dir>/targets.toml` with the
effective settings (overrides included), and never replaces a different
`targets.toml` already there (exit 1).
`--api anthropic` (or `api = "anthropic"` on a target) uses the native Claude
Messages API with an API key; it sends no `temperature` (Claude 4.7+ 400s on it)
and keeps hidden thinking out of tok/s. A Claude **subscription** is measured with
`--api claude-code --model ID [--effort low|medium|high|xhigh|max]` (or
`api = "claude-code"`): tps runs the signed-in `claude` CLI headless — no URL,
key or price. Never extract or replay plan OAuth tokens over HTTP: Anthropic
answers them with `429 rate_limit_error`, and impersonating its client is not
something tps does. CLI rows add `startup_p50_s` (client startup inside TTFT —
don't rank against HTTP TTFT); only the first API call is timed. `discover` and `init --model` are OpenRouter-only;
everything else is not.

Global: `--json`, `--no-color`, `-q`, `-v`, `--version`. May appear before or
after the subcommand. Config defaults to `targets.toml`, then
`targets.gateway.toml`.

## Exit codes

| Code | Meaning | Agent action |
|---|---|---|
| 0 | success | continue |
| 1 | usage/config error | fix the invocation; do not retry |
| 2 | missing credentials | ask the human for the env var |
| 3 | provider/API failure | safe to retry later, not immediately |
| 4 | every request failed | check network/model id before retrying |
| 130 | interrupted | partial results are in `run_dir`; `tps report` renders them |
| 141 | stdout closed early | — |

Bad flags exit **1** (not argparse's 2) and still emit a JSON error object.

## JSON contract (schema_version 1)

Every command emits:

```json
{ "schema_version": 1, "command": "run", "ok": true, "...": "command-specific" }
```

Errors emit `{"schema_version":1,"command":"...","ok":false,"error":{"code":2,"message":"..."}}`
on **stdout** and exit non-zero.

`run` adds:

```json
{
  "run_dir": "runs/2026-10-27T12-00-00Z",
  "results": [{
    "target": "deepinfra", "prompt": "prose-256", "model": "deepseek/deepseek-v4.1-flash",
    "concurrency": 1,
    "ttft_p50_s": 0.42, "ttft_p90_s": 0.55, "first_content_p50_s": 0.42,
    "out_tps_p50": 78.3, "out_tps_p90": 70.1,
    "itl_p50_ms": 12.0, "itl_n": 7, "first_content_n": 7,
    "tokens_per_chunk_median": 1.0, "bursty": 0,
    "e2e_p50_s": 3.2, "out_tokens_median": 240,
    "truncated": 7, "reasoning_only": 0, "hidden_reasoning_tokens_median": null, "notes": [],
    "ok": 35, "n": 35, "errors": {},
    "cost_usd": 0.0043,
    "aggregate_tps": {"1": 78.1, "4": 240.5},
    "by_concurrency": {"1": {"ok": 7, "n": 7, "...": "..."}, "4": {"ok": 28, "n": 28, "...": "..."}},
    "count_source": ["usage"], "served_by": ["DeepInfra"],
    "price_in_per_1m": 0.14, "price_out_per_1m": 0.42
  }],
  "cost_usd": 0.0511, "cost_unpriced": [],
  "failed": 0, "total": 385, "warmup_requests": 22, "planned_requests": 407, "mock": false,
  "artifacts": {"report": "...", "csv": "...", "raw": "..."}
}
```

Counts: `total` = measured requests; `warmup_requests` were sent first and
discarded; `planned_requests` = both. `raw.jsonl` rows carry
`"phase": "warmup"|"measure"`.

`reasoning_only` > 0 means responses had no answer text — usually a reasoning
model that hit `max_tokens` while thinking (`truncated` counts
`finish_reason: "length"`, which the built-in prompts reach by design). Then
`first_content_p50_s`/`itl_p50_ms` are null and TTFT/tok/s time the *thinking*;
`notes` says so. Re-run with a larger `--max-tokens`; don't report those rates as
answer speed. Like `ok`/`n`, `truncated` and `reasoning_only` count every
concurrency level; `by_concurrency` has them per level. `count_source`
`usage+chars/4` means part of the reasoning was hidden and estimated.

Error classes beyond HTTP/transport: `stream_error`, `incomplete_stream` (Claude
stream with no `message_stop`), `content_filter`, `refusal`, `no_content`,
`empty_stream` — none is timed as an answer.

`bursty` > 0 means chunks arrived in clumps (median gap < 20% of the typical
gap — the mean without its longest 2%), so
`itl_p50_ms` measures a proxy's flushing, not the model — don't rank providers
on it. `itl_n` / `first_content_n` give the sample count behind those medians;
`tokens_per_chunk_median` reconciles ITL with tok/s.

`run --dry-run` sends nothing and adds `dry_run: true`, `plan{requests,
measured_requests, warmup_requests, in_tokens_est, in_tokens_max, out_tokens_max,
cost_max_usd, unpriced[], est_minutes, assumed{tok_s, ttft_s, template_tokens},
targets[]}` and `missing_credentials[]` (exit 0 even when credentials are
missing). `cost_max_usd` is an upper bound: every response at `max_tokens`, input
at `in_tokens_max` (UTF-8 bytes + template allowance — chars/4 `in_tokens_est`
undercounts CJK/code); `null` when no target has a price. `est_minutes` assumes
the stated rate.

`discover` adds `model`, `endpoints[]`, `config`, `targets`.
`fingerprint` adds `verdict` (`split`|`consistent`|`inconclusive`), `clusters[][]`,
`no_data{target: error}`, `noisy[]`, `unmeasured[]`, `distinguishable_groups`,
`noise_floor_measured`, `self_agreement{}`,
`pairs[]`, `prompt_tokens{}`, `reasoning_seen[]`. It requests 20
`top_logprobs` (5 if capped; all kept in `fingerprints.jsonl`) and turns
reasoning off on OpenRouter; a target that spent the budget reasoning anyway is
`no_data` with `reasoning_consumed_budget`.
`doctor` adds `checks[]` with `{check, ok, detail, critical}`; CLI targets add a
`cli <binary>` check (installed, signed in, plan — never the account email).
Rows also carry `api`, `access` (`http`|`cli`), `effort` and `plan` (the
signed-in plan, e.g. `"Max"`, when the CLI reports it); samples in `raw.jsonl`
carry `plan_utilization`/`plan_window` from the CLI's rate-limit events. The
terminal shows one box per access mode (CLI TTFT is never ranked against HTTP
TTFT) and a `PLAN` badge. `run --dry-run` also returns `missing_clis[]` (exit 1 on a real run) and
`subscription_targets[]` (billed to plan usage, so unpriced). CLI error classes:
`cli_error`, `cli_exit_N`, `cli_missing`, `cli_io`; `fingerprint` rejects CLI
targets (exit 1).

Treat `results[].ok/n` as the reliability signal — they count every concurrency
level; `by_concurrency` shows where failures happened. `errors` is a map of
class → count. Latency/rate fields describe the lowest concurrency. **p90 is
always the slow tail**: `out_tps_p90 ≤ out_tps_p50`.

## Typical agent sequences

**"Is provider X actually better?"**
```bash
tps discover <model> --out /tmp/t.toml --json | jq '.endpoints | sort_by(.price_out_per_1m)[:5]'
tps run -c /tmp/t.toml --reps 7 --dry-run --json | jq '.plan | {requests, cost_max_usd, est_minutes}'
tps run -c /tmp/t.toml --reps 7 --json | jq '.results | sort_by(-.out_tps_p50) | .[:3] | .[] | {target, out_tps_p50, ttft_p50_s}'
```

**"How fast is this endpoint?"** (not on OpenRouter)
```bash
tps run --base-url https://api.example.com/v1 --model <id> --api-key-env VAR --json \
  | jq '.results[] | {ttft_p50_s, out_tps_p50, itl_p50_ms, reasoning_only, notes}'
```

**"Is the cheap one the same model?"**
```bash
tps fingerprint -c /tmp/t.toml --json | jq '.verdict, .clusters, .no_data'
```

## Cost and time

Don't estimate by hand: `tps run … --dry-run --json` returns the request count,
worst-case cost and rough minutes for exactly the flags you pass. For scale: 11
targets, 1 prompt, 7 reps, concurrency 1 and 4 = 385 measured + 22 warm-up
requests ≈ 20 min, ≤ $0.10 worst case (≈ $0.05 in practice). `fingerprint` ≈ 44
requests ≈ 1 min. Always dry-run a 41-target config first.

## Failure modes you will hit

- `missing credentials` (2) — the env var in `api_key_env` (`--api-key-env`) isn't set.
- A row with `reasoning_only == ok` — the model never answered inside
  `max_tokens`; see above. Claude Opus 5.5 does this at the built-in 256-token
  cap: use `--max-tokens 2048`.
- `http_400 … temperature` on Claude — the target is on the OpenAI-compatible
  layer; use `--api anthropic`, or `--temperature omit`.
- `extra_body can't set …` (1) — token caps come from prompts / `--max-tokens`.
- `HTTP 404` on a pin — the provider slug is wrong or the model moved; re-run `discover`.
- Everything timing out — check egress/proxy; `tps doctor --network`.
- Mock data — `"mock": true` in `meta.json`; never report it as a measurement.
- `only defines mock targets` (1) — the demo configs need `--mock`.
- `duplicate target name(s)` (1) — names key every statistic; make them unique.
