# Metrics

Every number in the report, defined precisely. If a definition here disagrees
with your intuition, this file wins — and the code (`tps/core.py`) is the final
authority. `tests/selftest.py` checks each definition against a local server.

## TTFT — time to first token

```
t_send → t_first_generated_chunk
```

`t_send` is immediately before the HTTP request is issued. The clock stops at the
first streaming chunk that carries generated text — **content or reasoning**.
Role-only deltas (`{"delta":{"role":"assistant"}}`) are ignored; counting them
would let any provider fake a fast start by sending a preamble.

For reasoning models the report adds a **first answer** column: time to the first
*content* chunk, i.e. when a user would see the answer start. It only appears
when it differs from TTFT.

A reasoning model can spend its entire `max_tokens` thinking and never answer
(`finish_reason: "length"`, no content chunks). If the thinking streamed, its
TTFT and tok/s are real measurements *of the thinking*; if it was hidden (Claude),
nothing streamed at all. Either way first answer and ITL have nothing to time,
and the request still counts as `ok` — it is a result, not an empty stream.
`tps` counts these responses (`reasoning_only`, next to `truncated` for any
response stopped by the cap) and says so under the table — *measured thinking,
not answering* — instead of leaving a bare `—`. Raise `--max-tokens`.

TTFT includes your DNS, TLS, network RTT, queueing, and prefill. It is therefore a
measure of *your* experience, not a property of the model alone.

## ITL — inter-token latency

For each request, the median gap between consecutive content-bearing chunks:

```
itl_i = t_chunk_i − t_chunk_{i−1}
```

The table shows the median of those per-request medians. This is the metric that
decides whether streaming *feels* smooth. A provider can have a great TTFT and a
terrible ITL, or the reverse.

Caveat: many providers coalesce several tokens into one SSE chunk. A chunk is not
a token, so ITL here is really "chunk inter-arrival", which is what a user
perceives anyway. It is not comparable to a per-token ITL measured from raw
logits. **tok/chunk** (`tokens_per_chunk_median`) says how many tokens each chunk
carried, which is what reconciles the two: 3.7 tokens/chunk every 20 ms is
~180 tok/s.

**Bursts.** A proxy or gateway that buffers the stream can deliver chunks in
clumps — many arriving back to back, then a pause. The median gap then lands
inside a clump (≈1 ms) and describes the proxy's flushing, not the model: 1 ms
ITL next to 138 tok/s is not a contradiction you should resolve by believing
either. `tps` flags a request as **bursty** when its median gap is under 20% of
its typical gap (the mean with the longest 2% — at least two — left out, so a
single stall isn't called a burst) — a comparison of the stream with itself, so
it needs no token counts. Bursty rows are marked `†`, never win "best ITL", carry a note, and are
counted in `bursty` (with `itl_n`, the number of requests the ITL median came
from). No client-side measurement can recover the model's pacing from a
coalesced stream.

## tok/s — per-stream decode rate

```
(completion_tokens − tokens_in_first_chunk) / (t_last_chunk − t_first_chunk)
```

Decode phase only: prefill is excluded, so a slow start can't hide inside the
average. Tokens that arrived *in* the first chunk were generated before the
window opened, so they are not credited to it; their count is estimated from the
first chunk's share of the streamed text. Without that correction, a provider
that sends 32 tokens per chunk would look ~14% faster than one that streams
token by token at the same real speed.

- Token counts come from `usage.completion_tokens` when the provider reports it.
  Otherwise they are estimated as chars/4 (content + reasoning text) and flagged:
  `chars/4` in the token-source column, `≈` in front of the rate.
- A response that arrives as **one chunk** has no decode window, so its rate is
  *unknown* — it is left out of the tok/s statistics rather than reported as a
  near-infinite number. The request still counts as `ok`.

## aggregate tok/s — throughput under concurrency

```
Σ completion_tokens over a batch / wall clock of that batch
```

Measured per (prompt, target, concurrency) batch; the report shows the median
across batches, plus the ratio against the lowest concurrency. This is where rate
limits live: if c=1 gives 80 tok/s and c=16 gives 90 tok/s, the provider is
queueing, not scaling. The terminal labels the ratio `scales`, `partial` or
`queues` (≥ 60%, 35–60% or < 35% of linear).

## ok / n

`ok` counts requests that returned 2xx **and** a non-empty stream without an
in-band error. In the per-stream table, `n` is the number attempted at that
table's concurrency. In `--json` results, top-level `ok`/`n`/`errors` count
**every** request for the prompt × target, all concurrency levels included — that
is the reliability signal — and `by_concurrency` breaks it down per level.

Errors are classified (`rate_limited`, `timeout`, `http_<status>`, `transport`,
`stream_error`, `incomplete_stream`, `content_filter`, `refusal`, `no_content`,
`empty_stream`) and counted in the Failures table. `stream_error` means the
provider started streaming and then sent an error object mid-stream;
`incomplete_stream` is a Claude stream that ended without `message_stop` (cut
off — its counts can't be trusted); `content_filter` / `refusal` are declines,
never timed as answers; `no_content` is a billed response with nothing streamed
that wasn't reasoning hitting the cap.

There are no retries: a 429 at concurrency 16 is information.

## out tok (median completion tokens)

The sanity check nobody runs. A provider that truncates early looks fast. If one
row has a median of 90 tokens while the others have 240, its tok/s is measuring a
shorter job, not a faster one.

## Percentiles

Computed over all samples in the group with linear interpolation.

**p90 is always the slow tail.** For latencies (TTFT, ITL, E2E) it's the 90th
percentile. For rates (tok/s) — where higher is better — it's the 10th
percentile: the rate 90% of requests met or beat. So `tok/s p90` is never higher
than `tok/s p50`, and "p90" means "what a bad-but-not-rare request looks like"
for every metric.

With 7 reps, p50 is meaningful and p90 is indicative; p99 needs 100+ samples. Do
not quote a p99 from a 7-rep run.

## Cost columns

| Column | Formula |
|---|---|
| `$/1M in`, `$/1M out` | from `price` in the target config |
| `est. $/1k requests` | `(median_in × price_in + median_out × price_out) × 1000 / 1e6` |
| `cost of 1M generated tokens` | the same per-request cost scaled to 1M output tokens |
| `est. $ this run` | Σ over this run's successful requests of `(in_tokens × price_in + out_tokens × price_out) / 1e6` |

Medians are over the lowest-concurrency requests of every prompt. When a provider
omits `usage.prompt_tokens`, input tokens are estimated as chars/4 of the prompt
(and flagged), so input spend is never silently dropped.

`est. $ this run` is the one number that answers "what did this benchmark cost
me". It sums the run's own samples — warm-ups are excluded, failed requests are
not charged (they have no tokens to bill), and estimated token counts are
included as estimated. The report prints the total; `--json` exposes it as
`cost_usd` (with `cost_unpriced` naming targets whose successful requests had no
`price` block). A
missing price is *unknown* spend, never $0.

`cost of 1M generated tokens` is the honest cross-provider number, because it
includes the prompt tokens that produced those outputs. If your workload is
input-heavy (RAG, long context), the ranking can flip entirely — hence the
per-request column.

## CLI targets (`api = "muse-code"`)

A subscription client is not an HTTP stream, so the same field names carry
explicitly different clocks. Nothing is renamed to hide that; the differences
are stated here and in every row note.

- **TTFT** is process start to the first streamed answer text (`muse exec`
  output delta). It includes client startup, prefill of the client's own
  context, and hidden reasoning before the answer. `startup_p50_s` isolates the
  first part (process start to first CLI event). The client also reports a
  first-token time for its internal model stream; that value is recorded as
  `provider_ttft_p50_s`, labeled as client-reported, and never substituted for
  the measured TTFT. Never rank CLI TTFT against HTTP TTFT.
- **ITL** is the gap between output deltas. Deltas carry a few tokens each, so
  this is inter-delta latency; `tokens_per_chunk_median` reconciles it with
  tok/s exactly as it does for chunky HTTP providers. The CLI exposes no
  per-token timing, so none is reported or inferred.
- **tok/s** uses the same decode-window math as HTTP targets, over the first to
  last delta window, counting the streamed tokens the CLI reports in its usage
  (output minus hidden reasoning). Reasoning tokens are never streamed by the
  CLI and stay out of the window.
- **Answer length is uncapped**: the CLI has no output-token cap, so `out tok`
  is natural length, `truncated` stays 0, and rows with longer answers did more
  work. Read tok/s next to `out tok`, as always.
- **Plan use replaces cost.** `in_tokens` is what the model actually read
  (client context included). There is no per-token price; `plan_usage` reports
  the client's plan window utilization before and after the run.
- **Retries inside the client** are counted per sample (`cli_attempts`); a row
  note flags any response that needed more than one attempt. tps itself never
  retries.

## What is *not* measured

- **Quality.** Run an eval; speed and correctness are different questions.
- **Cold start** beyond the discarded warm-up requests.
- **Cancellation behaviour** — `tps` always drains the stream.
- **Prompt-cache effects** — each request uses the same prompt, so cache hits are
  included if the provider does them. That is realistic for repeated workloads but
  flattering for first-call latency.
- **Hidden reasoning** — when a provider bills reasoning it doesn't stream and
  reports how much (`completion_tokens_details.reasoning_tokens`, or the Claude
  API's `output_tokens_details.thinking_tokens`), those tokens are kept out of
  tok/s and recorded as `hidden_reasoning_tokens`; the row notes that TTFT
  includes them. A provider that hides reasoning *without* reporting it still
  inflates tok/s, and nothing in the stream can reveal it.
- **Tool calling, JSON mode, vision** — chat text only.
