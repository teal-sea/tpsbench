# Contributing

Thanks for helping. `tps` is small on purpose: one runtime dependency, no
retries, numbers you can defend. Changes that keep it that way are very welcome.

## Setup

```bash
git clone https://github.com/teal-sea/tpsbench && cd tpsbench
python -m venv .venv && . .venv/bin/activate
pip install -e ".[http2,dev]"
```

## Before you open a PR

```bash
python tests/selftest.py          # offline: local SSE server, recorded fixture, CLI contract
ruff check . && ruff format --check .
```

Both run in CI on Python 3.11–3.13, plus a strict docs build.

- **Tests never touch the network or need keys.** New measurement logic gets a
  check in `tests/selftest.py` — usually a new misbehaving model in its local
  server.
- **Docs live in `docs/` only.** The mkdocs site and the GitHub wiki are both
  generated from it; never edit the wiki directly.
- **Changed what the terminal looks like?** Regenerate the README images from
  the real CLI: `pip install -e ".[assets]" && python scripts/render_assets.py`
  (needs the JetBrains Mono and Inter fonts, and Playwright for the PNG).

## Invariants

These are contracts other people's scripts depend on. A PR that changes one
needs a very good reason and a docs update:

1. No automatic retries for 429/5xx — failures are data.
2. Target order rotates every rep; never batch by provider.
3. `--json` prints exactly one JSON object on stdout; exit codes match
   [docs/cli.md](docs/cli.md#exit-codes).
4. Token counts come from `usage`; estimates are always flagged.
5. Mock data is always flagged and never mixes into a real run.
6. A failed request is never evidence that a provider serves different weights.
7. Demo data and screenshots use fictional providers only.

[AGENTS.md](AGENTS.md) has the same rules in a denser form.

## Adding a subscription CLI

A plan (Claude, ChatGPT, …) is measured by running the vendor's own client
headless. Everything vendor-specific is one `CliBackend` subclass in
`tps/clis.py`; registering it in `BACKENDS` gives you `api = "<api>"`, the
`tps <command>` shortcut and its `--help`, doctor's sign-in row, plan names in
titles and spend, grids, and the reports — the timing loop (`core.cli_once`) is
shared and must not grow vendor branches.

A backend provides:

| Member | What it is |
|---|---|
| `api`, `command`, `binary`, `vendor` | config value, shortcut name, executable, word in "<vendor> Max plan" |
| `efforts`, `default_models`, `model_help`, `example_model`, `examples` | what `--effort` accepts and what `tps <command>` and its help show |
| `continues_after_cap` | `True` if the client silently sends more requests after a capped response (the loop then stops it at once) |
| `argv(path, model, effort)` | headless invocation; the prompt arrives on **stdin** |
| `env(max_tokens)` | the child environment: output cap, **retries off**, and API-key variables **removed** so the plan is what gets measured |
| `parse(obj)` | one JSON line → `CliEvent` (text, streamed reasoning, Messages-API-shaped usage, finish, end of the first response, served model, plan-window reading, final error) or `None` |
| `auth(path)` | sign-in, plan and whether it is a subscription; unknown is `signed_in=None` with a reason, never "not signed in" |

Before adding one, check the vendor's **current** terms for headless use of
that plan, and keep the rules at the top of `tps/clis.py`: never read, store or
replay plan tokens or impersonate the client; strip the user's setup (tools,
MCP, project files); measure only the first API call. Test it offline: a fake
executable that replays the client's recorded JSON (see `FAKE_CLAUDE` and the
backend-contract check in `tests/selftest.py`), covering a normal answer, hidden
reasoning, a capped answer, a rate limit, a crash and the sign-in status.

## Reporting results about real providers

Please don't open issues or PRs that accuse a named provider of serving the
wrong model based on one fingerprint run. The tool can fail to disprove
identical weights; it cannot prove a difference is intentional. Share the
`fingerprint.md`, the config and the date, and say what you'd like to change in
the tool.
