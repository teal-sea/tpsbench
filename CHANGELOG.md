# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.1.0] — unreleased

First public release.

### Added

- `tps run`: streaming TTFT, first-answer time, ITL, per-stream tok/s, aggregate
  throughput under concurrency, failures and cost, interleaved across targets.
- `tps fingerprint`: greedy-decode agreement matrix, noise floors, tokenizer
  signature, clusters and a `split` / `consistent` / `inconclusive` verdict.
- `tps discover` / `tps init --model`: one pinned target per OpenRouter endpoint.
- `tps doctor`, `tps report` (terminal tables or `--markdown`).
- Run spend: `est. $ this run` per target in the report, `cost_usd` per row and
  in total in `--json`, with `cost_unpriced` naming targets that have no `price`
  (missing price is unknown spend, never $0).
- `--json` on every command with stable exit codes; partial runs survive Ctrl-C.
- Redesigned terminal output: boxed tables, best-value highlights, smooth bars,
  heatmap, live progress with ETA. `NO_COLOR` / `FORCE_COLOR` honoured.
- Docs site (mkdocs-material), wiki generated from `docs/`, README images
  rendered from real CLI output.
- `tps run --base-url URL --model ID` and `tps init --endpoint URL --model ID`:
  measure any OpenAI-compatible endpoint without OpenRouter or a hand-written
  config; `--price IN,OUT` fills the cost columns.
- `tps run --dry-run`: requests, token budget, worst-case cost and rough time,
  with nothing sent.
- `tps run --max-tokens / --temperature / --timeout` (and `fingerprint
  --timeout`) override the config.
- `finish_reason` and reasoning-token counts are recorded; results carry
  `truncated`, `reasoning_only` and `notes`.
- Native Claude API targets (`api = "anthropic"`, `--api anthropic`) for API keys.
  `--temperature omit` / `temperature = "omit"`.
- Claude subscription targets (`api = "claude-code"`, `--api claude-code`, `effort`
  / `--effort`): measured through the signed-in `claude` CLI, context stripped,
  client retries off, only the first API call timed; `startup_p50_s` and a note
  separate CLI startup from TTFT; `doctor` checks the CLI's sign-in and plan.
- `effort` on every target, in each API's own request shape; `[[grids]]` and
  comma lists in `--model`/`--effort` measure every model × effort level. Grids
  render as a `tok/s · ttft by effort` matrix, or rows by effort plus a "what
  effort costs" box, with a takeaway only when it holds for every model.
- Runs that mix access modes render one box per mode, aligned to a shared width,
  with a `PLAN` badge, `plan` in the cost column, like-for-like "snappiest", and
  plan usage (requests, 5-hour window before → after) in the spend line.
  Provider-only output is unchanged.
- Fingerprint groundwork for richer comparisons: 20 `top_logprobs` per token (5
  where a provider caps it), every alternative kept in `fingerprints.jsonl`;
  reasoning turned off on OpenRouter; `reasoning_seen` lists targets that
  reasoned anyway, and one that spent the budget reasoning is `no_data`
  (`reasoning_consumed_budget`) with a hint. The tokenizer signature reads
  `prompt_tokens` from any rep that reported it, failed completions included.

### Fixed before release

- A fingerprint target whose request failed was reported as a different artifact.
- A noisy or unmeasured fingerprint target could bridge two distinguishable
  groups and turn a `split` verdict into `consistent`.
- A reasoning model that spent its whole `max_tokens` thinking produced a row
  of unexplained `—`; it is now flagged as *measured thinking, not answering*.
- Warm-ups generated a full-length answer that was thrown away; they are now
  capped at 16 tokens.
- `raw.jsonl` didn't say which rows were warm-ups (only `batch: -1`); every row
  now has `phase`, and run JSON labels `total` / `warmup_requests` /
  `planned_requests`.
- Found by benchmarking Claude Opus 5.5: every request sent `temperature: 0`,
  which Claude 4.7+ rejects; reasoning that was billed but never streamed was
  credited to tok/s (up to 40% too fast), and a response whose hidden thinking
  used the whole cap was counted as a failed `empty_stream`.
- `extra_body` could override the token cap, making `--dry-run` understate cost
  and defeating the warm-up cap; tps-controlled fields are now rejected there,
  including a field renamed by a run-level `max_tokens_field`.
- `--dry-run` called its cost an upper bound while counting input as chars/4,
  which undercounts CJK text and code up to 4×; input is now bounded by UTF-8
  bytes (`in_tokens_max`).
- `tps run --base-url -o DIR` overwrote an existing `DIR/targets.toml` (e.g. a
  hand-written config with `-o .`); it now refuses before sending anything.
- The fingerprint agreement box overflowed with only two targets.
- From a code review of the above:
    - `truncated` / `reasoning_only` counted only the lowest concurrency; they
      count every level, with `by_concurrency` per level.
    - A response with nothing streamed counted as `ok` when it was a
      `content_filter` / `refusal` decline or a billed empty answer
      (`no_content`); a Claude stream cut off before `message_stop` counted as
      `ok` with a placeholder token count (`incomplete_stream`).
    - A reasoning-only row could win "best tok/s" and "fastest".
    - Claude OAuth tokens (`sk-ant-oat…`) were sent without the
      `anthropic-beta: oauth-2025-04-20` header they need.
    - The starter config set `temperature = 0`, which Claude API targets reject.
    - `stream_options` and fingerprint `logprobs` overwrote the same keys in
      `extra_body`; `extra_body` now wins.
    - `fingerprint` sent `temperature: 0` even under `--temperature omit`, and
      `meta.json` recorded the config's temperature rather than what each
      target was sent.
    - The config saved by `run --base-url` dropped `--reps`, `--max-tokens` and
      the other overrides, so `-c` didn't repeat the run.
    - `nan` / `inf` were accepted as a temperature, price or timeout.
    - A name or model with quotes or control characters produced an invalid
      generated TOML; a `user:password@` in a `--base-url` leaked into the
      default target name, `meta.json` and the report.
    - One long stall made a steady stream look bursty; the burst test now
      compares against the mean without its longest 2% of gaps.
    - Reasoning that was only partly streamed (a summary) credited the hidden
      remainder to tok/s; it is now estimated and flagged `usage+chars/4`.
- A stream coalesced by a proxy showed a 1 ms ITL next to 138 tok/s with nothing
  saying why, and won "best ITL". Bursty delivery is now detected (median gap <
  20% of the typical gap), marked `†`, noted, and excluded from "best"; rows gain
  `tokens_per_chunk_median`, `bursty`, `itl_n` and `first_content_n`.
- The selftest's no-buffering check used a wall-clock budget that a cold first
  connection could exceed; it now compares against the same request's end. A
  missing dependency prints setup steps instead of a traceback.
- `summary.csv` repeated the lowest-concurrency latency on every concurrency row.
- `results[].ok/n` hid failures at higher concurrency; now counts every level.
- A `mock` profile in a real config produced synthetic rows without `--mock`.
- Single-chunk responses reported near-infinite tok/s; mid-stream SSE errors
  counted as successes; first-chunk tokens inflated tok/s for chunky providers.
- `out_tps_p90` was the fast end; p90 is now the slow tail for every metric.
- Bad flags exited 2 ("missing credentials"); they exit 1 with a JSON error.
- Input cost was dropped when a provider omitted `usage`.
- Duplicate target names silently pooled their samples.
- The fingerprint verdict counted targets with no measured noise floor
  (`--reps 1`, or a failed second rep) as deterministic, so it could say
  `split` without evidence.
- API keys could survive in stored errors when sent via custom headers or cut
  mid-key by truncation; both are now redacted.
- A quoted price crashed the run after every request had been sent.

[0.1.0]: https://github.com/teal-sea/tpsbench
