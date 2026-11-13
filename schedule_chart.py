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
    active, u7s, u5s, windows = [0] * WEEK, [], [], []
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
        u5s.append(u5)
    return active, u7s, u5s, windows


# dark palette: the SVG paints its own surface, so it reads the same on GitHub light and dark
SURF, INK, INK2, MUTED, GRID = "#1a1a19", "#f0efec", "#c3c2b7", "#8f8e88", "#33322f"
TRACK, WIN_FILL, WIN_LINE = "#2a2927", "#262f3b", "#4a6a93"
RUN, PACE = "#3987e5", "#8f8e88"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(active, u7s, u5s, windows, k, rate):
    W, H = 960, 760
    o = []
    t = lambda x, y, s, size=12, fill=INK2, anchor="start", weight=400: o.append(
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{esc(s)}</text>')
    rect = lambda x, y, w, h, fill, rx=3, extra="": o.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" {extra}/>')
    o.append(f'<rect width="{W}" height="{H}" rx="12" fill="{SURF}"/>')
    t(32, 40, "Autopilot over one week when you don't use Claude yourself", 18, INK, weight=600)
    t(32, 62, f"Simulated with the real decide() policy · k = {k:.2f} weekly % per 1% of a 5h window "
              f"· {rate:.2f}% of the window per run-minute", 12, MUTED)

    # ---- timeline: one row per day of the weekly limit period, 5h windows as boxes, runs as bars
    x0, y0, cw, rh, rg = 96, 132, 26, 34, 12
    t(32, 96, "When autopilot runs, inside the 5h windows it opens", 13, INK, weight=600)
    for h in range(0, 25, 3):
        xx = x0 + h * cw
        t(xx, y0 - 10, f"+{h}h", 10, MUTED, "middle")
        o.append(f'<line x1="{xx}" x2="{xx}" y1="{y0 - 4}" y2="{y0 + 7 * (rh + rg) - rg}" stroke="{GRID}" stroke-width="1"/>')
    sx = x0 + 24 * cw + 22
    t(sx, y0 - 10, "work", 10, MUTED)
    t(sx + 58, y0 - 10, "weekly +", 10, MUTED)
    t(sx + 122, y0 - 10, "weekly total", 10, MUTED)
    X = lambda m, d: x0 + (m - d * DAY) / 60 * cw
    for d in range(7):
        y = y0 + d * (rh + rg)
        t(32, y + rh / 2 + 4, f"Day {d + 1}", 12, INK2)
        rect(x0, y + rh / 2 - 1, 24 * cw, 2, TRACK, 1)
        for w in windows:
            we = w + int(a.WINDOW_MIN)
            s_, e_ = max(w, d * DAY), min(we, (d + 1) * DAY)
            if s_ >= e_:
                continue
            xa, xb = X(s_, d), X(e_, d)
            rect(xa + 1, y, xb - xa - 2, rh, WIN_FILL, 5, f'stroke="{WIN_LINE}" stroke-width="1"')
            peak = max(u5s[w:min(we, WEEK)])
            # label once, on the part of a split window that is wide enough
            if xb - xa >= 64 and (s_ == w or min(we, (d + 1) * DAY) - max(w, d * DAY) > (we - w) / 2):
                t(xa + 6, y + 11, f"5h window · {peak:.0f}%", 9, INK2)
        m = d * DAY
        while m < (d + 1) * DAY:  # run segments
            if active[m]:
                e = m
                while e < (d + 1) * DAY and active[e]:
                    e += 1
                rect(X(m, d), y + 16, max(2, X(e, d) - X(m, d)), 12, RUN, 2)
                m = e
            else:
                m += 1
        hrs = sum(active[d * DAY:(d + 1) * DAY]) / 60
        prev = u7s[d * DAY - 1] if d else 0
        cur = min(100, u7s[(d + 1) * DAY - 1])
        t(sx, y + rh / 2 + 4, f"{hrs:.1f} h", 12, INK, weight=600)
        t(sx + 58, y + rh / 2 + 4, f"+{cur - min(100, prev):.0f}%", 12, INK2)
        t(sx + 122, y + rh / 2 + 4, f"{cur:.0f}%", 12, INK2)

    ly = y0 + 7 * (rh + rg) + 4
    rect(x0, ly, 34, 16, WIN_FILL, 4, f'stroke="{WIN_LINE}" stroke-width="1"')
    t(x0 + 42, ly + 12, "5h usage window (label: how full it got)", 11, INK2)
    rect(x0 + 300, ly + 3, 34, 10, RUN, 2)
    t(x0 + 342, ly + 12, "autopilot running", 11, INK2)

    # ---- weekly usage vs linear pace
    py0, ph, px0, pw = ly + 80, 150, x0, 24 * cw
    t(32, py0 - 22, "Weekly usage across the period", 13, INK, weight=600)
    for v in (0, 50, 100):
        yy = py0 + ph - v / 100 * ph
        o.append(f'<line x1="{px0}" x2="{px0 + pw}" y1="{yy}" y2="{yy}" stroke="{GRID}" stroke-width="1"/>')
        t(px0 - 8, yy + 4, f"{v}%", 10, MUTED, "end")
    for d in range(8):
        t(px0 + d / 7 * pw, py0 + ph + 16, f"Day {d + 1}" if d < 7 else "reset", 10, MUTED, "middle")
    o.append(f'<line x1="{px0}" y1="{py0 + ph}" x2="{px0 + pw}" y2="{py0}" stroke="{PACE}" '
             f'stroke-width="2" stroke-dasharray="5 4"/>')
    pts = " ".join(f"{px0 + i / WEEK * pw:.1f},{py0 + ph - min(100, u7s[i]) / 100 * ph:.1f}"
                   for i in range(0, WEEK, 30))
    o.append(f'<polyline points="{pts}" fill="none" stroke="{RUN}" stroke-width="2" stroke-linejoin="round"/>')
    lx = px0 + 260
    o.append(f'<line x1="{lx}" x2="{lx + 24}" y1="{py0 - 26}" y2="{py0 - 26}" stroke="{RUN}" stroke-width="2"/>')
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
    active, u7s, u5s, windows = simulate(k, args.rate)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(active, u7s, u5s, windows, k, args.rate))
    print(f"{out}: {sum(active) / 60:.1f} h active, {len(windows)} windows, weekly {u7s[-1]:.0f}%")


if __name__ == "__main__":
    main()
