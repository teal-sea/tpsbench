# Muse subscription backend

Branch `muse/muse-backend`. Not a real-plan benchmark. The Power window was
already exhausted (105% used, reset 2026-11-23T00:48:07Z) when this branch
was committed, so no live `muse exec` measurement was run.

## What landed

`api = "muse-code"` drives `muse exec --json` as a subprocess. tps does not
read `~/.config/muse/auth.json`. The spawned command forces
`--contributor-mode off`, `--preset empty`, one model step, and no web,
reminder, or user-input tools. The prompt goes in by file. The client runs
in an empty cache directory so project rules do not load.

Honesty limits, stated in `tps/core.py` and the docs, not papered over:

- TTFT is process start to first answer text. It includes client startup,
  the client's own context prefill, and hidden reasoning. It is not HTTP TTFT.
- ITL is the gap between CLI output deltas, not between tokens. The CLI does
  not expose per-token timing, and none is fabricated.
- `muse exec` has no output-token cap, so `max_tokens` does not bind.
- Cost columns stay empty. Plan-window percent comes from the CLI usage
  report, before and after the run.
- Fingerprint refuses CLI targets: no logprobs, no sampling control.

Shipped config: `targets.muse.toml` (four model ids, warmup 0, reps 3).

## Checks

`ruff check .` and `ruff format --check .` passed on this worktree
(27 files already formatted).

`tests/selftest.py` drives a fake `muse` binary. On this branch, under load,
211 checks passed and 3 failed, all tok/s timing tolerances:

```text
[FAIL] out tok/s within 20% of truth — 73.70836399214775 vs 100.0
[FAIL] claude: tok/s counts only streamed tokens (hidden thinking not credited) — 27.439490656351406 vs 100.0
[FAIL] OpenAI-format hidden reasoning: not credited to tok/s either — 40 31.97450670471392
```

The same script on clean `main`, same machine, same hour, failed those
checks worse, plus one more:

```text
[FAIL] out tok/s within 20% of truth — 19.4963259044985 vs 100.0
[FAIL] itl ~ token delay — 69.97565599158406
[FAIL] claude: tok/s counts only streamed tokens — 39.293319635361485 vs 100.0
[FAIL] OpenAI-format hidden reasoning — 40 31.23953954153484
```

Load at the time was above 2 on 4 vCPUs, with a Lean replay in the other
worktree. The failures are the shared host inflating inter-token gaps, not
a regression in the Muse path. Every Muse-specific check in that run passed,
including fake-client token counts, startup-vs-TTFT split, plan window,
model mismatch, missing binary, and the shipped config parse.

## Not done

No live benchmark of `muse-wonder-full-1.0` against `muse-spark-1.3`. That
needs a window that is not exhausted. `tps run -c targets.muse.toml` is the
command, after `muse usage` shows room.
