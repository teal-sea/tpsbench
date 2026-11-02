#!/usr/bin/env python3
"""Render design mockups for views tps doesn't have yet.

    python scripts/render_mockups.py      # → docs/design/mockups/*.svg

Unlike render_assets.py (real CLI output only), these are proposals: each view
is composed with tps's own UI components — the same boxes, bars, badges and
colours `tps run` prints — and drawn by the same terminal renderer, so a mockup
looks exactly like the CLI would. Data, models and plans are fictional, and
every window is titled "mockup". Never use these in the README.
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from render_assets import prompt_line, terminal_svg  # noqa: E402

from tps.ui import UI, Bar, Col  # noqa: E402

OUT = ROOT / "docs" / "design" / "mockups"
COLS = 96
PLAN_BADGE = "\033[1;30;45m"  # bold black on magenta: subscription runs


def capture(draw: Callable[[UI], None]) -> str:
    ui = UI(color=True)
    ui.width = COLS
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        draw(ui)
    return buffer.getvalue()


def plan_badge(ui: UI, text: str = "PLAN") -> str:
    return ui.style(f" {text} ", PLAN_BADGE)


def header(ui: UI, command: str, detail: str, plan: bool = False) -> None:
    line = f"{ui.badge('tps')} {ui.bold(command)}"
    if plan:
        line += "  " + plan_badge(ui)
    print()
    print(line + "  " + ui.grey(detail))


def callouts(ui: UI, lines: list[tuple[str, str, str, str]]) -> None:
    width = max(len(name) for _i, _l, name, _d in lines)
    ui.say()
    for icon, label, name, detail in lines:
        main, _, aside = detail.partition("  · ")
        tail = main + (ui.grey("  · " + aside) if aside else "")
        ui.say(f"  {ui.cyan(icon)} {ui.grey(label.ljust(10))} {ui.bold(name.ljust(width))}  {tail}")


def secs(v: float) -> str:
    return f"{v:.2f}s"


# --------------------------------------------------------------------------
# 1. one model, measured over an API key and through a plan's CLI
# --------------------------------------------------------------------------


def mixed_run(ui: UI) -> None:
    header(ui, "run", "atlas-large · prose-256 · 5 reps · concurrency 1")
    ui.hint("  3 targets · 2 access modes · 18 requests")
    peak = 98.4

    footers = (
        "p50 · ttft first token · tok/s decode rate · itl gap between chunks",
        "ttft includes ~1.8s client startup — compare it within this box · tok/s is the model's",
    )
    wide = max(map(len, footers))
    name_w = 9

    def box(title, footer, first_col, rows, price_col):
        footer = footer.ljust(wide)
        best_tps = max(r[2] for r in rows)
        best_ttft = min(r[1] for r in rows)
        cols = [
            Col(first_col, "l"),
            Col("ttft"),
            Col("tok/s"),
            Col("", "l", flex=True),
            Col("itl"),
            Col("ok"),
            Col("$/1M out"),
            Col("Δ ttft"),
            Col("Δ tok/s"),
        ]
        base = rows[0]
        table = []
        for i, (name, ttft, tps, itl, price) in enumerate(rows):
            top = tps == best_tps
            cells = [
                (name.ljust(name_w), ui.bold if top else None),
                (secs(ttft), ui.best if ttft == best_ttft and len(rows) > 1 else None),
                (f"{tps:.1f}", ui.best if top and len(rows) > 1 else None),
                Bar(tps, peak, 22, ui.green if top else ui.cyan),
                f"{itl}ms",
                ("5/5", ui.green),
                (price, ui.grey) if price == "plan" else price,
            ]
            if i == 0:
                cells += [("base", ui.grey), ("base", ui.grey)]
            else:
                dt, ds = ttft - base[1], (tps - base[2]) / base[2] * 100
                cells += [
                    (f"{dt:+.2f}s", ui.red if dt > 0 else ui.green),
                    (f"{ds:+.0f}%", ui.red if ds < 0 else ui.green),
                ]
            table.append(cells)
        ui.say()
        ui.table(cols, table, title=title, footer=footer)

    box(
        "prose-256 · single stream · API key",
        footers[0],
        "provider",
        [("direct", 1.12, 98.4, 49, "20.00"), ("gateway-a", 1.31, 91.7, 52, "21.00")],
        "price",
    )
    box(
        "prose-256 · single stream · Acme Max plan " + "· via acme-code",
        footers[1],
        "effort",
        [("low", 2.31, 97.9, 50, "plan"), ("high", 4.07, 97.2, 50, "plan")],
        "plan",
    )
    callouts(
        ui,
        [
            ("▲", "fastest", "direct", "98.4 tok/s  · the plan decodes as fast (97.9)"),
            ("●", "snappiest", "direct", "1.12s  · on the plan: effort low, 2.31s"),
            ("$", "cheapest", "gateway-a", "$21.00 / 1M out  · plan rows draw on usage"),
        ],
    )
    ui.say()
    ui.say(
        "  "
        + ui.grey("est. spend this run: ")
        + "$0.0412"
        + ui.grey(" on API keys · Acme Max plan: ")
        + "12 requests"
        + ui.grey(", 5h window ")
        + "6% → 9%"
    )
    ui.say()
    ui.ok(f"wrote {ui.bold('runs/mixed/report.md')}  {ui.grey('· summary.csv · raw.jsonl')}")


# --------------------------------------------------------------------------
# 2. a plan, model × effort
# --------------------------------------------------------------------------

GRID = {  # tok/s, ttft (fictional)
    "atlas-large": [(97.9, 2.31), (97.5, 3.02), (97.2, 4.07), (96.8, 5.44)],
    "atlas-mid": [(141.6, 1.92), (141.0, 2.38), (140.4, 3.01), (139.9, 3.86)],
    "atlas-small": [(231.4, 1.58), (230.2, 1.81), (229.7, 2.12), None],
}
EFFORTS = ["low", "medium", "high", "max"]


def effort_grid(ui: UI) -> None:
    header(ui, "run", "Acme Max plan · via acme-code · prose-256 · 5 reps", plan=True)
    ui.hint("  3 models × 4 effort levels · 11 targets · 66 requests")
    best_tps = max(c[0] for row in GRID.values() for c in row if c)
    best_ttft = min(c[1] for row in GRID.values() for c in row if c)
    cols = [Col("model", "l")] + [Col(e) for e in EFFORTS]
    table = []
    for model, cells in GRID.items():
        row = [model]
        for c in cells:
            if c is None:
                row.append(("not offered", ui.grey))
                continue
            tps, ttft = c
            tps_s, ttft_s = f"{tps:.1f}", f"{ttft:.1f}s"
            # style each half on its own: best rate and best ttft are separate wins
            text = f"{tps_s} · {ttft_s}"
            if tps == best_tps or ttft == best_ttft:
                row.append((text, ui.best))
            else:
                row.append(text)
        table.append(row)
    ui.say()
    ui.table(
        cols,
        table,
        title="prose-256 · tok/s · ttft by effort",
        footer="p50 · ttft includes ~1.8s client startup · same plan, same client: rows compare",
    )

    # ttft vs effort, as bars: where effort actually costs you
    ui.say()
    peak = 5.44
    bar_cols = [Col("effort", "l"), Col("ttft"), Col("", "l", flex=True), Col("thinking")]
    thinking = [38, 210, 606, 1420]
    rows = [
        [e, secs(ttft), Bar(ttft, peak, 30, ui.magenta), f"~{t} tok"]
        for e, (_tps, ttft), t in zip(EFFORTS, GRID["atlas-large"], thinking, strict=True)
    ]
    ui.table(
        bar_cols,
        rows,
        title="atlas-large · what effort costs",
        footer="thinking tokens are generated before the first visible token",
    )
    ui.say()
    ui.say(
        "  "
        + ui.cyan("→")
        + " effort barely moves tok/s (−1%) · it costs ttft: "
        + ui.bold("+3.1s")
        + " from low to max on atlas-large"
    )
    ui.say(
        "  "
        + ui.grey("est. spend this run: ")
        + "66 requests"
        + ui.grey(" on your Acme Max plan · 5-hour window ")
        + "9% → 21%"
    )


# --------------------------------------------------------------------------
# 3. bare `tps` on a terminal: the guided start
# --------------------------------------------------------------------------


def guided_start(ui: UI) -> None:
    print()
    print(f"{ui.badge('tps')} {ui.grey('measure the models you pay for')}")
    print()
    found = [
        (True, "acme-code", "signed in · Max plan"),
        (True, "orbit", "signed in · Plus plan"),
        (True, "OPENROUTER_API_KEY", "set"),
        (False, "ACME_API_KEY", "not set"),
    ]
    for i, (ok, name, detail) in enumerate(found):
        mark = ui.green("✓") if ok else ui.grey("·")
        label = ui.grey("found".ljust(7)) if i == 0 else " " * 7
        print(f"  {label}{mark} {ui.bold(name) if ok else ui.grey(name)}  {ui.grey(detail)}")
    print()
    options = [
        ("Compare effort levels on my Acme plan", "atlas-large · low/high", "~4 min", True),
        ("Compare my plans: Acme vs Orbit", "best model each", "~6 min", False),
        ("Find the fastest provider of a model", "OpenRouter · asks for model", "", False),
        ("Measure one endpoint", "asks for URL + key", "", False),
    ]
    width = max(len(o[0]) for o in options)
    detail_w = max(len(o[1]) for o in options)
    for n, (label, detail, cost, selected) in enumerate(options, 1):
        pointer = ui.cyan("›") if selected else " "
        text = ui.bold(label.ljust(width)) if selected else label.ljust(width)
        tail = ui.grey(detail.ljust(detail_w)) + ("  " + cost if cost else "")
        print(f"  {pointer} {ui.grey(str(n))}  {text}  {tail}")
    print()
    print("  " + ui.grey("plan usage only · dry-run first · config saved to the run dir"))
    print()
    keys = [("↑↓", "pick"), ("enter", "run"), ("d", "dry-run"), ("e", "edit config"), ("q", "quit")]
    print("  " + ui.grey(" · ").join(f"{ui.bold(k)} {ui.grey(v)}" for k, v in keys))


VIEWS = {
    "run-mixed.svg": ("tps run -c mixed.toml", "tps run  ·  mockup: API key + plan", mixed_run),
    "run-effort.svg": (
        "tps run -c plan-grid.toml",
        "tps run  ·  mockup: model × effort on a plan",
        effort_grid,
    ),
    "start.svg": ("tps", "tps  ·  mockup: guided start", guided_start),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (command, title, draw) in VIEWS.items():
        svg = terminal_svg(prompt_line(command) + capture(draw), title, COLS)
        (OUT / name).write_text(svg, encoding="utf-8")
        print(f"wrote {(OUT / name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
