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
| `base_url` | yes | API root; `/chat/completions` is appended (`/messages` with `api = "anthropic"`) |
| `model` | yes | provider's model id, not your alias |
| `api` | no | `"openai"` (default, any OpenAI-compatible server), `"anthropic"` (the native Claude API) or `"claude-code"` (a Claude plan via the `claude` CLI) — see below |
| `effort` | no | reasoning effort, sent in each API's own format: `output_config.effort` (Claude API), `reasoning.effort` (OpenRouter), `reasoning_effort` (other OpenAI-compatible). `minimal`/`low`/`medium`/`high`/`xhigh`/`max`; which levels a model accepts is the provider's call (an unsupported one is its 400, recorded as data). Omit for the model's default. Can't be combined with an `extra_body` that sets the same field |
| `api_key_env` | no* | env var holding the key |
| `api_key` | no* | literal key (avoid; use `api_key_env`) |
| `headers` | no | extra request headers |
| `extra_body` | no | merged into the JSON body — this is where pinning lives. It can't set `model`, `messages`, `stream` or the token cap: `tps` controls those, and an override would silently change what is measured and what `--dry-run` budgets |
| `price` | no | `{ in_per_1m, out_per_1m }` in USD for cost columns; must be numbers |
| `max_tokens_field` | no | per-target override |
| `stream_options` | no | per-target override |
| `mock` | no | synthetic profile, **only used with `--mock`** (see below) |

\* One of them, unless the endpoint is on `localhost` / `127.0.0.1` / `::1`.

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
  `/v1/chat/completions`. An `sk-ant-api…` key is sent as `x-api-key`, anything
  else as a bearer token (for gateways that issue their own).
- **Not for subscription plans.** A Claude Pro/Max OAuth token sent straight to
  the API is refused (`429 rate_limit_error`, verified 2026-11-01): plan
  credentials are only honored by Anthropic's own client. Measure a plan with
  [`api = "claude-code"`](#claude-subscription-api-claude-code) instead.
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

## Claude subscription (`api = "claude-code"`)

```toml
[[targets]]
name = "opus-plan-high"
api = "claude-code"
model = "claude-opus-5-5"
effort = "high"            # low | medium | high | xhigh | max; omit = model default
# cli = "/path/to/claude"  # default: `claude` on PATH
```

Or with no file: `tps run --api claude-code --model claude-opus-5-5 --effort high`.

tps runs the signed-in `claude` CLI headless (`claude -p --output-format
stream-json`) — the route Anthropic documents as drawing on plan usage — and
parses the same Messages API events as `api = "anthropic"`. It never reads,
stores or replays your plan's OAuth token. `tps doctor` shows whether the CLI is
installed and signed in, and to which plan.

- **Stripped context.** No tools, MCP servers, skills, settings or CLAUDE.md, a
  one-line system prompt, in an empty working directory
  (`~/.cache/tps/cli-workdir`). A configured Claude Code otherwise sends ~245k
  tokens of setup per call, and you'd be timing that.
- **The plan, not a key.** `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are
  removed from the CLI's environment (`claude -p` would otherwise bill the key).
  A `claude setup-token` token in `CLAUDE_CODE_OAUTH_TOKEN` is kept.
- **No retries** (`CLAUDE_CODE_MAX_RETRIES=0`): a 429 or a rejected plan window
  is recorded as `rate_limited`, like any other target.
- **One API call per request.** When a response hits the cap, Claude Code
  silently sends continuation requests. tps stops the CLI at the first
  `message_stop`, so `max_tokens` is a real per-request cap, `finish_reason:
  length` counts as `truncated`, and no extra plan usage is spent.
- **TTFT includes client startup.** `startup_p50_s` is process start → first
  stream event (~1.5–2 s); the row's notes say so. Compare CLI TTFT with CLI TTFT
  only. tok/s, ITL and token counts are unaffected.
- **Unpriced.** Plan usage has no per-token price; rows land in
  `cost_unpriced`. `fingerprint` doesn't accept CLI targets (no logprobs or
  sampling control).
- `api_key`, `api_key_env`, `headers`, `extra_body` and `max_tokens_field` don't
  apply and are rejected (exit 1). `base_url` is ignored, and no `temperature` is
  sent (`meta.json` records `temperature: null`).

`[run] temperature` takes a number or `"omit"`. Unset, OpenAI-compatible targets
get `0` and Claude API targets get nothing; `meta.json` records what each target
was actually sent.

## Grids: models × effort (`[[grids]]`)

One block measures every combination. Each becomes an ordinary target — named
`<name>/<model>@<effort>`, interleaved like any other, keyed in every statistic:

```toml
[[grids]]
name = "claude-plan"
api = "claude-code"
models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
effort = ["low", "high"]          # "default" = the model's own level
```

Every other key (`api`, `base_url`, `api_key_env`, `price`, `cli`, `extra_body`,
`mock`, …) is shared by the grid's targets; `prices = { "<model>" = { in_per_1m, out_per_1m } }`
overrides `price` per model. A config can mix `[[targets]]` and `[[grids]]`.

From flags, a comma list does the same: `tps run --api claude-code --model
claude-opus-5-5,claude-sonnet-5-5 --effort low,high` (and `init` writes the
`[[grids]]` block).

The terminal groups the results: one model across levels gets a "what effort
costs" box (TTFT and hidden thinking per level); several models × levels get a
`tok/s · ttft by effort` matrix. A one-line takeaway appears only when it holds
for every model measured — tok/s flat across levels, and/or TTFT rising with
each level.

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
