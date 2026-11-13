#!/usr/bin/env python3
"""Simulate one weekly limit period with no manual usage and draw when autopilot runs.

Drives the real `decide()` policy (config.json / config.example.json) minute by minute:
a 10-minute cron tick, back-to-back runs while it says GO, a 5h window opened by the
first run, u7 growing by k per 1% of u5. Writes docs/weekly-schedule.svg.

    python3 schedule_chart.py [--k 0.10] [--rate 0.55] [--out docs/weekly-schedule.svg]
"""
import argparse
from pathlib import Path

import autopilot as a

WEEK = 168 * 60
DAY = 24 * 60


def simulate(k, rate):
    u5 = u7 = 0.0
    win_end = None
    run_left, nxt, mode = 0, 0, None
    active, u7s, windows = [0] * WEEK, [], []
    for t in range(WEEK):
        if win_end is not None and t >= win_end:
            win_end, u5 = None, 0.0
        if run_left <= 0 and t >= nxt:
            r5 = None if win_end is None else win_end - t
            go, mode, _, run = a.decide(dict(u5=u5, r5=r5, u7=u7, r7=(WEEK - t) / 60), k)
            if go:
                if win_end is None:
                    win_end = t + a.WINDOW_MIN
                    windows.append(t)
                run_left = int(run)
            else:
                nxt = t + 10
        if run_left > 0:
            line = 100 * (a.WINDOW_MIN - (win_end - t)) / a.WINDOW_MIN + a.CFG["u5_pace_margin"]
            if u5 >= a.CFG["max_u5"] or (mode != "saturate" and u5 >= line):
                run_left, nxt = 0, t
            else:
                active[t] = 1
                run_left -= 1
                u5 += rate
                u7 += k * rate
                if run_left <= 0:
                    nxt = t
        u7s.append(u7)
    return active, u7s, windows


# light-only palette: the SVG paints its own surface, so it reads the same on GitHub light and dark
SURF, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
EMPTY = "#efeeea"
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
PACE = "#8a8984"


def cell_color(minutes):
    if minutes == 0:
        return EMPTY
    return RAMP[min(len(RAMP) - 1, int(minutes / 60 * len(RAMP)))]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(active, u7s, windows, k, rate):
    W, H = 920, 670
    o = []
    t = lambda x, y, s, size=12, fill=INK2, anchor="start", weight=400: o.append(
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{esc(s)}</text>')
    o.append(f'<rect width="{W}" height="{H}" rx="12" fill="{SURF}"/>')
    t(32, 40, "Autopilot over one week when you don't use Claude yourself", 18, INK, weight=600)
    t(32, 62, f"Simulated with the real decide() policy · learned k = {k:.2f} weekly % per 1% of the 5h "
              f"window · {rate:.2f}% of the window per run-minute", 12, MUTED)

    # ---- heatmap: day of the limit period x hour since the weekly reset
    x0, y0, cw, ch, gap = 96, 128, 26, 26, 2
    t(32, 96, "Minutes of autopilot work per hour", 13, INK, weight=600)
    for h in range(0, 24, 3):
        t(x0 + h * cw + cw / 2, y0 - 6, f"+{h}h", 10, MUTED, "middle")
    sx = x0 + 24 * cw + 18
    t(sx, y0 - 6, "work", 10, MUTED)
    t(sx + 64, y0 - 6, "weekly used", 10, MUTED)
    for d in range(7):
        y = y0 + d * (ch + 8)
        t(32, y + ch / 2 + 4, f"Day {d + 1}", 12, INK2)
        for h in range(24):
            m = sum(active[d * DAY + h * 60:d * DAY + (h + 1) * 60])
            o.append(f'<rect x="{x0 + h * cw + gap / 2}" y="{y}" width="{cw - gap}" height="{ch}" rx="3" '
                     f'fill="{cell_color(m)}"><title>Day {d + 1}, +{h}h: {m} min</title></rect>')
        # 5h windows opened by autopilot, as a thin bracket under the row
        for w in windows:
            s, e = max(w, d * DAY), min(w + a.WINDOW_MIN, (d + 1) * DAY)
            if s < e:
                xa, xb = x0 + (s - d * DAY) / 60 * cw, x0 + (e - d * DAY) / 60 * cw
                o.append(f'<rect x="{xa + 1}" y="{y + ch + 2}" width="{xb - xa - 2}" height="3" rx="1.5" fill="{MUTED}"/>')
        hrs = sum(active[d * DAY:(d + 1) * DAY]) / 60
        t(sx, y + ch / 2 + 4, f"{hrs:.1f} h", 12, INK, weight=600)
        t(sx + 64, y + ch / 2 + 4, f"{min(100, u7s[(d + 1) * DAY - 1]):.0f}%", 12, INK2)

    # legend
    ly = y0 + 7 * (ch + 8) + 14
    t(x0, ly + 11, "0 min", 10, MUTED, "end")
    o.append(f'<rect x="{x0 + 6}" y="{ly}" width="14" height="14" rx="3" fill="{EMPTY}"/>')
    for i, c in enumerate(RAMP):
        o.append(f'<rect x="{x0 + 24 + i * 18}" y="{ly}" width="16" height="14" rx="3" fill="{c}"/>')
    t(x0 + 24 + len(RAMP) * 18 + 4, ly + 11, "60 min", 10, MUTED)
    o.append(f'<rect x="{x0 + 200}" y="{ly + 5}" width="30" height="3" rx="1.5" fill="{MUTED}"/>')
    t(x0 + 236, ly + 11, "5h window opened by autopilot", 10, MUTED)

    # ---- weekly usage vs linear pace
    py0, ph, px0, pw = ly + 70, 150, x0, 24 * cw
    t(32, py0 - 22, "Weekly usage across the period", 13, INK, weight=600)
    for v in (0, 50, 100):
        yy = py0 + ph - v / 100 * ph
        o.append(f'<line x1="{px0}" x2="{px0 + pw}" y1="{yy}" y2="{yy}" stroke="{GRID}" stroke-width="1"/>')
        t(px0 - 8, yy + 4, f"{v}%", 10, MUTED, "end")
    for d in range(8):
        xx = px0 + d / 7 * pw
        t(xx, py0 + ph + 16, f"Day {d + 1}" if d < 7 else "reset", 10, MUTED, "middle")
    o.append(f'<line x1="{px0}" y1="{py0 + ph}" x2="{px0 + pw}" y2="{py0}" stroke="{PACE}" '
             f'stroke-width="2" stroke-dasharray="5 4"/>')
    pts = " ".join(f"{px0 + i / WEEK * pw:.1f},{py0 + ph - min(100, u7s[i]) / 100 * ph:.1f}"
                   for i in range(0, WEEK, 30))
    o.append(f'<polyline points="{pts}" fill="none" stroke="{RAMP[4]}" stroke-width="2" stroke-linejoin="round"/>')
    lx = px0 + 260
    o.append(f'<line x1="{lx}" x2="{lx + 24}" y1="{py0 - 26}" y2="{py0 - 26}" stroke="{RAMP[4]}" stroke-width="2"/>')
    t(lx + 30, py0 - 22, "weekly % used by autopilot", 11, INK2)
    o.append(f'<line x1="{lx + 210}" x2="{lx + 234}" y1="{py0 - 26}" y2="{py0 - 26}" '
             f'stroke="{PACE}" stroke-width="2" stroke-dasharray="5 4"/>')
    t(lx + 240, py0 - 22, "linear pace (weekly cap = pace + pace_margin)", 11, INK2)
    t(px0 + pw + 8, py0 + 4, f"{min(100, u7s[-1]):.0f}%", 12, INK, weight=600)

    total = sum(active) / 60
    t(32, H - 20, f"≈ {total:.0f} h of work in {len(windows)} windows ({total / 7:.1f} h/day). Your own usage "
                  "opens windows and raises weekly %, so autopilot then keeps to each window's tail.", 11, MUTED)
    body = "\n".join(o)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">\n{body}\n</svg>\n')


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--k", type=float, default=None, help="weekly %% per 1%% of u5 (default: learned)")
    p.add_argument("--rate", type=float, default=0.55, help="u5 %% burned per run-minute")
    p.add_argument("--out", default=str(Path(__file__).parent / "docs" / "weekly-schedule.svg"))
    args = p.parse_args()
    k = args.k if args.k is not None else a.learn_k()
    active, u7s, windows = simulate(k, args.rate)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(active, u7s, windows, k, args.rate))
    print(f"{out}: {sum(active) / 60:.1f} h active, {len(windows)} windows, weekly {u7s[-1]:.0f}%")


if __name__ == "__main__":
    main()
