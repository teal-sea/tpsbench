# Configuration

A config is TOML with one `[run]` table and any number of `[[targets]]` tables.
`tps run` picks `targets.toml` then `targets.gateway.toml` if `-c` is omitted.

## `[run]`

| Key | Default | Meaning |
|---|---|---|
| `reps` | `7` | repetitions per prompt × concurrency × target |
| `concurrency` | `[1]` | list of parallel-request levels |
| `warmup` | `2` | discarded requests per target (TLS, cold start), capped at 16 output tokens |
| `temperature` | `0` (not sent to the Claude API) | a number, or `"omit"`; `--temperature` overrides |
| `timeout_s` | `180` | per-request read timeout; `--timeout` overrides |
| `prompts` | your `[prompts.*]`, else the first two built-ins | any built-in or `[prompts.*]` name |
| `baseline` | none | target name to compute Δ columns against (must exist) |
| `max_tokens_field` | `max_tokens` | use `max_completion_tokens` for GPT-5/o-series |
| `stream_options` | `true` | send `include_usage`; disable if a provider 400s |

## `[[targets]]`

| Key | Required | Notes |
|---|---|---|
| `name` | yes | shows up in reports; must be unique (every statistic is keyed by it) — keep it stable across runs |
| `base_url` | yes* | API root; `/chat/completions` is appended (`/messages` with `api = "anthropic"`) |
| `model` | yes | provider's model id, not your alias |
| `api` | no | `"openai"` (default, any OpenAI-compatible server), `"anthropic"` (the native Claude API, see below), or `"muse-code"` (the signed-in Muse CLI, see below) |
| `effort` | no | reasoning effort for CLI targets (for `muse-code`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`); unset means the model's own default |
| `cli` | no | CLI targets only: path or name of the client to spawn (default `muse`) |
| `api_key_env` | no* | env var holding the key |
| `api_key` | no* | literal key (avoid; use `api_key_env`) |
| `headers` | no | extra request headers |
| `extra_body` | no | merged into the JSON body — this is where pinning lives. It can't set `model`, `messages`, `stream` or the token cap: `tps` controls those, and an override would silently change what is measured and what `--dry-run` budgets |
| `price` | no | `{ in_per_1m, out_per_1m }` in USD for cost columns; must be numbers |
| `max_tokens_field` | no | per-target override |
| `stream_options` | no | per-target override |
| `mock` | no | synthetic profile, **only used with `--mock`** (see below) |

\* `base_url` is not used (and may be omitted) for CLI targets. One of the key fields is required for HTTP targets, unless the endpoint is on `localhost` / `127.0.0.1` / `::1`. CLI targets take neither: the client uses its own sign-in, and setting `api_key`, `api_key_env`, `headers`, `extra_body` or `max_tokens_field` on one is a config error.

`reps`, `concurrency` and `warmup` are validated up front: a config that would
measure nothing (0 reps, concurrency 0, unknown prompt, duplicate names) is
rejected before any request is sent.

## Claude (`api = "anthropic"`)

```toml
[[targets]]
name = "claude-opus-5-5"
base_url = "https://api.anthropic.com/v1"
model = "claude-opus-5-5"
api = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"
price = { in_per_1m = 4.0, out_per_1m = 20.0 }
```

Or with no file: `tps run --api anthropic --model claude-opus-5-5 --api-key-env
ANTHROPIC_API_KEY` (the URL defaults to Anthropic's).

- **Native, not the OpenAI-compatible layer.** Anthropic also serves
  `/v1/chat/completions`, but only for API keys; Claude subscription (OAuth)
  tokens are rejected there. The native API takes both: an `sk-ant-api…` key is
  sent as `x-api-key`, anything else as a bearer token, and an OAuth token
  (`sk-ant-oat…`) also gets the `anthropic-beta: oauth-2025-04-20` header it
  needs.
- **No `temperature`.** Claude 4.7 and later reject sampling parameters, so it
  isn't sent to `api = "anthropic"` targets unless you set a number explicitly.
  (On the OpenAI-compatible layer they reject `temperature: 0` too — use
  `--temperature omit` there.)
- **Hidden thinking.** Current Claude models think before answering and don't
  stream the thinking. `tps` reads `usage.output_tokens_details.thinking_tokens`,
  keeps those tokens out of tok/s, and notes that TTFT includes them. At the
  built-in prompts' 256-token cap Claude Opus 5.5 usually spends the whole cap
  thinking — `--max-tokens 2048`.
- **Fingerprinting** compares whole texts: there are no logprobs and no greedy
  decoding, so Claude normally shows up as `noisy` and can't decide a verdict.

`[run] temperature` takes a number or `"omit"`. Unset, OpenAI-compatible targets
get `0` and Claude API targets get nothing; `meta.json` records what each target
was actually sent.

## Muse subscription (`api = "muse-code"`)

```toml
[[targets]]
name = "muse-spark-1.3"
api = "muse-code"
model = "muse-spark-1.3"
# effort = "low"        # optional; unset uses the model's default
# cli = "~/.local/bin/muse"   # optional; default is `muse` on PATH
```

Or with no file: `tps run --api muse-code --model muse-spark-1.3`, or scaffold
one with `tps init --api muse-code --model muse-spark-1.3`. The shipped
`targets.muse.toml` lists all four current models.

- **The plan, through its own client.** Each request runs `muse exec --json`
  headless on the signed-in Muse subscription (run `muse login` once).
  tps never reads, stores or replays the plan credential; it only spawns the
  client and times what comes back. Contributor mode is forced off for every
  spawned run, so benchmark traffic is never contributed.
- **One model call per request.** The agent is stripped down (`empty` preset,
  no reminders, no web or user-input tools, one model step), the prompt goes
  in by file, and the client runs in an empty working directory so no project
  rules load. A reply that makes no model call (an unknown model id gets a
  canned greeting from the client) or reports a different model than asked
  for is recorded as a failure, never as a timed success.
- **What the numbers mean.** TTFT runs from process start to the first streamed
  answer text, so it includes client startup (`startup_p50_s` says how much),
  prefill of the client's own context (tens of thousands of tokens even with
  the stripped preset; `in_tokens` in `raw.jsonl` is what the model actually
  read), and hidden reasoning before the answer. ITL is the gap between CLI
  output deltas, which carry a few tokens each; `tokens_per_chunk_median`
  keeps that honest. The CLI also reports a first-token time for its internal
  stream; tps records it separately as `provider_ttft_p50_s` and never mixes
  it into the measured TTFT. True per-token timing is not exposed, so none is
  reported. Compare Muse TTFT only with other CLI rows.
- **No token cap.** `muse exec` has no output-token cap, so prompt
  `max_tokens` and `--max-tokens` do not bind these targets: answers come back
  at natural length and `out tok` says how long. Warm-ups cost a full answer
  each, which is why `targets.muse.toml` sets `warmup = 0`. No `temperature`
  is sent either (the CLI exposes no sampling parameters).
- **Retries.** tps adds none. The client can retry its model stream internally;
  the attempt count is recorded as `cli_attempts` in `raw.jsonl` and a row note
  flags any response that needed more than one.
- **Cost columns stay empty.** There is no per-token price on a plan. Instead,
  each sample records the plan window utilization the client reported after the
  request, and the report, terminal and `--json` (`plan_usage`) show the window
  before and after the run. `tps doctor` shows the signed-in plan and current
  window use (`muse usage`).
- **Fingerprinting refuses CLI targets**: the CLI exposes no logprobs and no
  sampling control, so there is nothing token-level to compare.

## Pinning an upstream

A gateway target is meaningless unless you pin it, otherwise you measure the
router's choice:

```toml
extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }
```

**Use the full tag slug for variants and tiers.** OpenRouter's routing rules say a
base slug matches every region of the standard endpoint but *never* matches a tier
endpoint:

```toml
# standard endpoint
extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }
# flex tier — pinning "deepinfra" here would silently measure the standard one
extra_body.provider = { only = ["deepinfra/fp8/flex"], allow_fallbacks = false }
# priority tier
extra_body.provider = { only = ["baseten/fast"], allow_fallbacks = false }
```

Other routing fields worth knowing: `sort = "throughput" | "latency" | "price"`,
`quantizations = ["fp8"]`, `ignore = [...]`, `data_collection = "deny"`,
`zdr = true`.

## Prompts

Built-ins: `prose-256` (long prose), `code-256` (code generation),
`long-context` (~7k-token prefill, 128-token output). They stay available by name
when you define your own, so `prompts = ["prose-256", "mine"]` works.

```toml
[prompts.summary]
text = "Summarise the document below in 150 words."
repeat = 40            # repeats the built-in ~170-token filler document
max_tokens = 128

[prompts.mine]
text = "Explain TCP congestion control."
max_tokens = 256
# document = "inline text"          # or
# document_file = "docs/big.txt"    # relative to this config file
```

`repeat` is how you build a prefill-heavy prompt without shipping a fixture file.
An explicit `document`/`document_file` is included once by default (`repeat`
multiplies it); the built-in filler is only used when `repeat` asks for it.

## Prices

`price` is only used for the cost columns and the run's estimated spend; it never
affects measurement. A target without both prices is left out of `cost_usd` and
named in `cost_unpriced` rather than counted as free.

```toml
price = { in_per_1m = 0.14, out_per_1m = 0.42 }
```

`tps discover --out` fills these in from the provider list. They drift — re-run
`discover` when they matter.

## Baseline deltas

Name a target and the report grows Δ TTFT / Δ tok/s columns:

```toml
[run]
baseline = "together-direct"
```

Use it for the gateway-hop question: same upstream direct vs through the gateway.

## Ready-made configs in this repo

| File | Targets | Use |
|---|---|---|
| `targets.mock.toml` | 3 fake | offline demo, no keys |
| `targets.v4.1-flash.toml` | 11 curated | DeepSeek-V4.1-Flash, ~20 min |
| `targets.v4.1-flash.full.toml` | 41 | everything OpenRouter lists |
| `targets.gateway.toml` | 3 + commented | hop overhead / routing choice |
| `targets.example.toml` | mixed | direct vendors, pinned and unpinned gateways |
| `targets.muse.toml` | 4 Muse models | Muse subscription via the `muse` CLI |

## Validating a config without spending money

```bash
tps run -c my.toml --mock --reps 1
```

The report is flagged `MOCK DATA`, and `meta.json` records `"mock": true`. If a
target's `extra_body` is malformed you'll see it here, not after 400 requests.

## Mock profiles

A `mock` table shapes the synthetic numbers for `--mock` runs. It is **ignored
without `--mock`** (with a warning), so a leftover profile can never leak fake
rows into a real measurement; a config where *every* target has one refuses to
run without `--mock`.

```toml
mock = { ttft = [0.21, 0.34], itl = [0.007, 0.011], scaling = 0.45 }
#        TTFT range (s)       chunk-gap range (s)   <1: degrades under load
mock = { family = 1, noisy = true }   # fingerprint demo: weight family 0–2, self-disagreement
```
