# CLI reference

```
tps [--json] [--no-color] [-q] [-v] <command> [options]
```

Global flags may be given before or after the subcommand.

| Flag | Effect |
|---|---|
| `--json` | print exactly one JSON object on stdout; suppress human output |
| `--no-color` | disable ANSI colour (also honours `NO_COLOR`, `TERM=dumb`, non-TTY) |
| `-q`, `--quiet` | suppress everything except errors (progress included) |
| `-v`, `--verbose` | full traceback on unexpected errors |
| `--version` | print version (with `--json`: `{"version": "…"}`) |

`FORCE_COLOR=1` forces colour when stdout is not a TTY (`NO_COLOR` still wins).
Progress is drawn on stderr, only when stderr is a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage or configuration error — bad flag, bad config, unknown prompt, model not found, bad run directory |
| 2 | missing credentials |
| 3 | provider/API failure (OpenRouter unreachable or erroring during discovery) |
| 4 | the command ran but **every** request failed |
| 130 | interrupted (Ctrl-C) — whatever finished is kept on disk |
| 141 | stdout closed early (e.g. piped into `head`) |

Bad flags exit **1**, not argparse's usual 2, so they can never be mistaken for
"missing credentials"; with `--json` they still produce an error object.

Partial failures do **not** change the exit code: a run where 3 of 70 requests
failed exits 0 and reports `"failed": 3`. Treat `results[].ok/n` as the signal.

---

## `tps doctor`

```bash
tps doctor [-c FILE] [--network]
```

Checks Python ≥ 3.11, `httpx`, `h2`, config parse, and whether every target that
needs a key has one (targets on `localhost`/`127.0.0.1`/`::1` don't; a mock-only
config doesn't). `--network` additionally probes each `base_url` (5s timeout) —
any HTTP status counts as reachable, any connection error or timeout does not.

Exit 0 when every critical check passes, 2 when only credentials are missing,
1 for anything else (no config, invalid config, Python too old).

## `tps init`

```bash
tps init [--out targets.toml] [--force]
tps init --endpoint URL --model ID [--api-key-env VAR] [--name NAME] [--price IN,OUT]
tps init --api anthropic --model claude-opus-5-5 --api-key-env ANTHROPIC_API_KEY
tps init --model SLUG
```

With no flags: writes a commented starter config.
With `--endpoint`: writes a one-target config for any OpenAI-compatible
endpoint — no gateway, no OpenRouter key. `--price` is USD per 1M input,output
tokens and fills the cost columns; `--name` defaults to the URL's host. Add more
`[[targets]]` blocks to compare.
With `--model` alone: behaves like `discover --out`, generating one pinned
target per OpenRouter endpoint (needs network; uses `OPENROUTER_API_KEY` if set).

Refuses to overwrite without `--force`.

## `tps discover`

```bash
tps discover <author>/<slug> [--out FILE] [--api-key KEY]
             [--from-json FILE] [--json-out FILE]
```

Lists every endpoint serving the model with price, quantization, and context,
sorted by output price.

| Flag | Use |
|---|---|
| `--out FILE` | write a runnable config, one pinned target per endpoint tag |
| `--json-out FILE` | save the raw API payload for debugging |
| `--from-json FILE` | parse a recorded payload offline (used by tests) |
| `--api-key` | defaults to `OPENROUTER_API_KEY` |

An unknown model exits 1; OpenRouter being unreachable exits 3.

## `tps run`

```bash
tps run [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4,16]
        [--prompts name,other] [--max-tokens N] [--temperature T] [--timeout S]
        [--dry-run] [--mock] [--no-warmup]
tps run --base-url URL --model ID [--api-key-env VAR] [--name NAME] [--price IN,OUT] [...]
tps run --api anthropic --model ID --api-key-env VAR [...]      # native Claude API
```

| Flag | Default | Notes |
|---|---|---|
| `-c` | `targets.toml` → `targets.gateway.toml` | first one that exists |
| `-o` | `runs/<UTC stamp>` | created if missing |
| `--reps` | from config (7) | reps per prompt × concurrency × target, ≥ 1 |
| `--concurrency` | from config (`[1]`) | comma list of integers ≥ 1; each level adds a batch per rep |
| `--prompts` | from config | any built-in or `[prompts.*]` name |
| `--max-tokens` | from each prompt | overrides every selected prompt's cap; raise it for reasoning models |
| `--temperature` | from config (0; not sent to the Claude API) | a number ≥ 0, or `omit` to leave the field out |
| `--timeout` | from config (180) | per-request seconds |
| `--dry-run` | off | print the plan — requests, tokens, worst-case cost, rough time — and send nothing |
| `--mock` | off | synthesises samples; no network; flagged in every output |
| `--no-warmup` | off | skips discarded warm-up requests |

**No config file:** `--base-url URL --model ID` measures one OpenAI-compatible
endpoint directly (with `--api-key-env`, `--name`, `--price` as in `init
--endpoint`; it can't be combined with `-c`). The generated config is saved as
`<run_dir>/targets.toml` with the settings the run actually used (`--reps`,
`--max-tokens`, `--temperature`, …), so `-c` repeats it exactly; a different
`targets.toml` already there is never replaced (pick another `-o`). `--api
anthropic` speaks the native Claude Messages API instead (URL defaults to
`https://api.anthropic.com/v1`, with an API key) — see
[configuration](configuration.md#claude-api-anthropic). `--api claude-code --model
ID[,ID…] [--effort LEVEL[,LEVEL…]]` measures your **Claude subscription** through the signed-in
`claude` CLI — no URL, key or price — see
[configuration](configuration.md#claude-subscription-api-claude-code).

Cost model: requests = targets × prompts × reps × Σ(concurrency), plus
`warmup` per target. A curated 11-target run at 1 prompt, 7 reps, concurrency
1+4 is 385 measured requests + 22 warm-ups. Warm-ups are capped at 16 output
tokens; they open connections and warm the prompt cache, then are discarded.
`--dry-run` does this arithmetic for you, and its cost is a real upper bound:
output tokens at the `max_tokens` cap, input tokens at the prompt's UTF-8 byte
count + 32 for the chat template (no tokenizer produces more tokens than bytes;
chars/4, shown as the typical count, undercounts CJK text or dense code up to
4×). Targets without a price are listed rather than guessed, and minutes assume
40 tok/s and 1 s TTFT. A provider that injects its own system prompt can still
exceed the bound — nothing outside it can see that. It validates the config and
flags like a real run but does not require credentials — missing ones are listed
in `missing_credentials`.

`raw.jsonl` is written as batches finish and `meta.json` up front (with
`"complete": false` until the end). **Ctrl-C keeps everything measured so far**,
writes a report flagged `PARTIAL`, and exits 130.

## `tps report`

```bash
tps report <run_dir> [--markdown]
```

Re-reads `raw.jsonl` + `meta.json`, rewrites `report.md`/`summary.csv`, and prints
the same tables `run` prints. `--markdown` prints `report.md` instead. A missing
`raw.jsonl` exits 1.

Use it to analyse a run again after changing `tps`, to recover a deleted report,
or to render an interrupted run.

## `tps fingerprint`

```bash
tps fingerprint [-c FILE] [-o DIR] [--reps 2] [--prompt-count 2] [--max-tokens 160]
                [--timeout S] [--mock]
```

Sends the same prompt `--reps` times per target at temperature 0 (unless the
config says `temperature = "omit"`), non-streaming, with `logprobs` and 20
`top_logprobs` (5 where a provider caps it), and compares token sequences. On
OpenRouter targets reasoning is turned off (`reasoning = {enabled = false}`;
`extra_body.reasoning` overrides). Writes `fingerprints.jsonl` — each row keeps
the full `top_logprobs` per position and any `reasoning_tokens` — and
`fingerprint.md`. Exits 4 if no request succeeded. `fingerprints.jsonl` is
written as requests finish; Ctrl-C keeps them and writes a `PARTIAL` report.
See [fingerprinting](fingerprinting.md).

---

## JSON output

Every command prints one object:

```json
{"schema_version": 1, "command": "run", "ok": true, "...": "..."}
```

Errors go to **stdout** in JSON mode (so a pipe sees them) and to stderr in human
mode:

```json
{"schema_version": 1, "command": "run", "ok": false,
 "error": {"code": 2, "message": "missing credentials for: deepinfra — set OPENROUTER_API_KEY (or run with --mock)"}}
```

`schema_version` only changes on breaking changes; new keys may be added.
`--help` and `--version` are wrapped too (`"help"` / `"version"` keys), and
usage errors name the subcommand in `"command"`.

### `run`

```json
{
  "schema_version": 1, "command": "run", "ok": true, "mock": false,
  "run_dir": "runs/2026-10-27T12-00-00Z",
  "config": "targets.toml",
  "model": "deepseek/deepseek-v4.1-flash",
  "models": ["deepseek/deepseek-v4.1-flash"],
  "reps": 7, "concurrency": [1, 4],
  "results": [{
    "prompt": "prose-256", "target": "deepinfra", "model": "...",
    "base_url": "https://openrouter.ai/api/v1", "pin": {"only": ["deepinfra/fp8"]},
    "concurrency": 1,
    "ttft_p50_s": 0.42, "ttft_p90_s": 0.55, "first_content_p50_s": 0.42,
    "out_tps_p50": 78.3, "out_tps_p90": 70.1,
    "itl_p50_ms": 12.0, "itl_n": 7, "first_content_n": 7,
    "tokens_per_chunk_median": 1.0, "bursty": 0,
    "e2e_p50_s": 3.2, "out_tokens_median": 240,
    "truncated": 7, "reasoning_only": 0, "hidden_reasoning_tokens_median": null, "notes": [],
    "ok": 34, "n": 35, "errors": {"rate_limited": 1},
    "cost_usd": 0.0511,
    "aggregate_tps": {"1": 78.1, "4": 240.5},
    "by_concurrency": {
      "1": {"ttft_p50_s": 0.42, "out_tps_p50": 78.3, "aggregate_tps": 78.1, "ok": 7, "n": 7, "errors": {}},
      "4": {"ttft_p50_s": 0.51, "out_tps_p50": 61.0, "aggregate_tps": 240.5, "ok": 27, "n": 28, "errors": {"rate_limited": 1}}
    },
    "count_source": ["usage"], "served_by": ["DeepInfra"],
    "price_in_per_1m": 0.14, "price_out_per_1m": 0.42
  }],
  "cost_usd": 0.1943, "cost_unpriced": ["together"],
  "failed": 1, "total": 385, "warmup_requests": 22, "planned_requests": 407,
  "artifacts": {"report": "...", "csv": "...", "raw": "..."}
}
```

`model` remains the first target's model ID for compatibility. `models` is the
ordered, deduplicated list of every model ID in the run, so mixed-model configs
are unambiguous.

Three counts, three meanings: `total` is measured requests (warm-ups excluded);
`warmup_requests` were sent first and discarded; `planned_requests` is both
(`meta.json` has the same number). `raw.jsonl` holds every request, each row
tagged `"phase": "warmup"` or `"measure"`. `results[].n` is one (prompt,
target)'s measured requests across all concurrency levels.

`truncated` and `reasoning_only` count successful responses that stopped at
`max_tokens` (`finish_reason: "length"`) and that contained no answer text at all
— across **every** concurrency level, like `ok`/`n`, so `reasoning_only == ok`
means no response answered (per level: `by_concurrency`). The built-in prompts ask
for more than their cap, so `truncated` is normally everything; `reasoning_only`
is the one to watch. A response that was filtered (`content_filter`), refused
(`refusal`) or billed with nothing streamed for another reason (`no_content`) is
a failure, not a reasoning-only success. When `reasoning_only` is non-zero,
`notes` says what that does to the row — e.g. a reasoning model that spent its
whole cap thinking has a real TTFT and tok/s *for the thinking*, and no
first-answer time or ITL; raise `--max-tokens`.
`hidden_reasoning_tokens_median` is reasoning the provider billed but didn't
stream (Claude's thinking); it is excluded from tok/s, and `notes` says TTFT
includes it. When only a summary of the reasoning streamed, the unstreamed rest
counts as hidden too; that split rests on chars/4, so `count_source` reads
`usage+chars/4` and the rate is marked ≈.

`itl_n` / `first_content_n` are how many requests those medians came from
(reasoning-only and single-chunk responses contribute nothing). `bursty` counts
requests whose chunks arrived in clumps (median gap < 20% of the typical gap —
the mean with the longest 2% left out, so one stall isn't a burst): their ITL
measures a proxy's flushing, not the model — see
[metrics](metrics.md#itl-inter-token-latency). `tokens_per_chunk_median` is what
reconciles ITL with tok/s.

### `run --dry-run`

```json
{"schema_version":1,"command":"run","ok":true,"dry_run":true,"mock":false,
 "config":"targets.toml","model":"deepseek/deepseek-v4.1-flash",
 "models":["deepseek/deepseek-v4.1-flash"],
 "reps":7,"concurrency":[1,4],"prompts":["prose-256"],
 "plan":{"requests":407,"measured_requests":385,"warmup_requests":22,
   "in_tokens_est":24420,"in_tokens_max":110297,"out_tokens_max":98912,
   "cost_max_usd":0.1014,"unpriced":["openrouter auto"],
   "est_minutes":19.5,"assumed":{"tok_s":40.0,"ttft_s":1.0,"template_tokens":32},
   "targets":[{"target":"deepinfra (fp8)","requests":37,"cost_max_usd":0.0045}]},
 "missing_credentials":[]}
```

Latency and rate fields describe the lowest concurrency (`concurrency`);
`ok`/`n`/`errors` count every level; `by_concurrency` breaks both down.
`out_tps_p90` is the slow tail (see [metrics](metrics.md#percentiles)).

`cost_usd` (per row and in total) is the estimated spend on this run's successful
requests, at the configured `price`: warm-ups excluded, failures not charged,
chars/4 token estimates included as estimates. `cost_unpriced` lists targets whose
successful requests had no `price` — their spend is unknown, not zero. `report --json`
returns the same `cost_usd`/`cost_unpriced` alongside the markdown. This is what the
run *used*; `run --dry-run` estimates an upper bound *before* it starts.

### `discover`

```json
{"schema_version":1,"command":"discover","ok":true,
 "model":"deepseek/deepseek-v4.1-flash",
 "endpoints":[{"provider":"DeepInfra","slug":"deepinfra","tag":"deepinfra/fp8",
   "quantization":"fp8","service_tier":null,"context_length":1048576,
   "price_in_per_1m":0.14,"price_out_per_1m":0.42,
   "reported_p50_throughput":null,"reported_p50_latency_s":null}],
 "config":"targets.toml","targets":41}
```

### `fingerprint`

```json
{"schema_version":1,"command":"fingerprint","ok":true,"mock":false,
 "verdict":"split",
 "clusters":[["deepinfra","together"],["decart"]],
 "no_data":{"nebius":"http_401: ..."},
 "noisy":[],
 "unmeasured":[],
 "distinguishable_groups":2,
 "noise_floor_measured":true,
 "self_agreement":{"deepinfra":1.0,"together":0.99,"decart":1.0},
 "self_first_divergence":{"deepinfra":null,"together":142,"decart":null},
 "pairs":[{"a":"deepinfra","b":"together","agreement":0.997,"first_divergence":142}],
 "prompt_tokens":{"deepinfra":171,"together":171,"decart":171,"nebius":null},
 "prompt_used":"prose-256"}
```

`verdict` is `split` (two or more distinguishable groups among deterministic
targets), `consistent`, or `inconclusive` (fewer than two targets with a
measured, deterministic noise floor — always the case with `--reps 1`). Targets
that errored are in `no_data` and never in `clusters`; `noisy` and `unmeasured`
targets are clustered but never decide the verdict — each joins the one group
it matches, and never merges two.

### `doctor`

```json
{"schema_version":1,"command":"doctor","ok":false,"exit_code":2,
 "checks":[{"check":"credentials","ok":false,"detail":"missing for 1 target(s): deepinfra — set OPENROUTER_API_KEY","critical":true}]}
```

## Environment variables

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | `discover`, `init --model`, any target with that `api_key_env` |
| `NO_COLOR` | disables colour everywhere |
| `FORCE_COLOR` | forces colour when not on a TTY |
| `HTTPS_PROXY` | honoured by httpx for all requests |
| any `api_key_env` in your config | that target's `Authorization` header |
