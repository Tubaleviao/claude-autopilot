#!/usr/bin/env python3
"""Summarize autopilot telemetry (runs.jsonl, usage-log.jsonl, state.json) as JSON.

  analyze.py [--days 7] [--out FILE]
"""
import argparse, json, re, statistics, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

AP = Path(__file__).resolve().parent
MARKERS = {"PR_OPENED", "NO_PENDING_STEPS", "INCOMPLETE", "BLOCKED", "MERGED", "FIXED", "NO_NEW_ITEMS", "REVIEW_DONE"}


def bucket(s):
    """resets_at has sub-second (sometimes +-1 s) jitter: round to 10 minutes, as autopilot.learn_k does."""
    if not s or s == "sim":
        return s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s
    return datetime.fromtimestamp(round(dt.timestamp() / 600) * 600, dt.tzinfo).isoformat()[:16]


def norm_marker(m):
    """'STATUS: PR_OPENED', '**MERGED**', 'FIXED (pending CI)' -> bare token, like autopilot.parse_marker."""
    t = (m or "").strip().strip("*_`#>").strip()
    if t.upper().startswith("STATUS:"):
        t = t.split(":", 1)[1].strip()
    for v in sorted(MARKERS, key=len, reverse=True):
        if t.upper() == v or t.upper().startswith((v + " ", v + ":", v + "(")):
            return v
    return m


def load(p):
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    for r in rows:
        for k in ("ra5", "ra7"):
            if r.get(k):
                r[k] = bucket(r[k])
        if "marker" in r:
            r["marker"] = norm_marker(r["marker"])
    return rows


def q(xs, f):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(f * len(xs)))], 2) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def summarize(days=7):
    now = time.time()
    since = now - days * 86400
    runs = [r for r in load(AP / "runs.jsonl") if r["ts"] >= since]
    rows = [r for r in load(AP / "usage-log.jsonl") if r["ts"] >= since and r.get("ra5") != "sim"]
    out = {"days": days, "generated": time.strftime("%Y-%m-%d %H:%M"), "runs_total": len(runs)}

    # ---- runs ----
    by_mode = defaultdict(list)
    for r in runs:
        by_mode[r["mode"]].append(r)
    out["runs_by_mode"] = {}
    for m, rs in by_mode.items():
        d = [r["duration_s"] for r in rs]
        used = [r["duration_s"] / (r["budget_min"] * 60) for r in rs if r.get("budget_min")]
        du5 = [r["u5_after"] - r["u5_before"] for r in rs
               if r.get("u5_after") is not None and r.get("u5_before") is not None
               and r["u5_after"] >= r["u5_before"]]
        out["runs_by_mode"][m] = dict(
            n=len(rs), models=dict(Counter(r.get("model") for r in rs)),
            duration_s=dict(mean=mean(d), p50=q(d, .5), p90=q(d, .9), max=max(d)),
            cost_usd=dict(total=round(sum(r.get("cost") or 0 for r in rs), 2), mean=mean([r.get("cost") for r in rs])),
            turns_mean=mean([r.get("turns") for r in rs]),
            markers=dict(Counter(r["marker"][:30] for r in rs)),
            interrupted=dict(Counter(r["interrupted"] for r in rs if r.get("interrupted"))),
            resumed_runs=sum(1 for r in rs if r.get("resumed")),
            budget_used_frac_mean=mean(used), runs_over_90pct_budget=sum(1 for x in used if x > .9),
            window_pct_per_run_mean=mean(du5))
    out["runs_by_project"] = dict(Counter(r["project"] for r in runs))

    # ---- PR flow (review runs until merge) ----
    flow = {}
    for proj in {r["project"] for r in runs}:
        seq = [r["marker"] for r in runs if r["project"] == proj and r["mode"] == "review" and not r.get("resumed")]
        iters, cur = [], 0
        for mk in seq:
            cur += 1
            if mk == "MERGED":
                iters.append(cur); cur = 0
        flow[proj] = dict(review_runs=len(seq), merges=len(iters), iterations_per_merged_pr=iters,
                          blocked=seq.count("BLOCKED"))
    out["pr_flow"] = flow

    # ---- ticks / decisions ----
    dec = Counter()
    for r in rows:
        if "go" in r:
            why = re.sub(r"\(.*", "", (r.get("reason") or "").split(";")[0])  # drop per-tick numbers
            why = re.sub(r"\d+(\.\d+)?%?m?", "N", why).strip()[:40]
            dec[f"{'GO' if r['go'] else 'skip'}:{r.get('mode')}:{why}"] += 1
    out["tick_decisions"] = dict(dec.most_common(15))
    out["ticks"] = len(rows)

    # ---- windows (5h) ----
    wins = defaultdict(list)
    for r in rows:
        if r.get("ra5"):
            wins[r["ra5"]].append(r)
    wl = []
    for ra, rs in sorted(wins.items()):
        rs.sort(key=lambda r: r["ts"])
        ended = rs[-1]["r5"] is not None and (rs[-1]["ts"] + rs[-1]["r5"] * 60) < now
        peak = max(r["u5"] for r in rs)
        arun = [x for x in runs if rs[0]["ts"] - 600 <= x["ts"] <= rs[-1]["ts"] + 300]
        wl.append(dict(resets_at=ra, ended=ended, peak_u5=peak, ticks=len(rs), autopilot_runs=len(arun),
                       autopilot_minutes=round(sum(x["duration_s"] for x in arun) / 60, 1)))
    done = [w for w in wl if w["ended"]]
    out["windows"] = dict(
        n=len(wl), ended=len(done), mean_peak_u5_ended=mean([w["peak_u5"] for w in done]),
        ended_below_80pct=sum(1 for w in done if w["peak_u5"] < 80),
        wasted_headroom_pct_total=round(sum(100 - w["peak_u5"] for w in done), 1),
        recent=wl[-12:])

    # ---- week ----
    wk = defaultdict(list)
    for r in rows:
        if r.get("ra7"):
            wk[r["ra7"]].append(r)
    out["weeks"] = [dict(resets_at=ra, u7_first=min(rs, key=lambda r: r["ts"])["u7"],
                         u7_last=max(rs, key=lambda r: r["ts"])["u7"], ticks=len(rs))
                    for ra, rs in sorted(wk.items())]

    # ---- k (weekly % per window %), pooled like autopilot.learn_k: u7 is an integer, so per-pair
    # ratios are mostly 0 and their median is meaningless ----
    d5s = d7s = pairs = 0
    for a, b in zip(rows, rows[1:]):
        if a.get("ra5") and a.get("ra5") == b.get("ra5") and a.get("ra7") == b.get("ra7"):
            d5, d7 = b["u5"] - a["u5"], b["u7"] - a["u7"]
            if d5 > 0 and d7 >= 0:
                d5s, d7s, pairs = d5s + d5, d7s + d7, pairs + 1
    out["k_estimate"] = dict(pairs=pairs, sum_d5=d5s, sum_d7=d7s,
                             pooled=round(d7s / d5s, 3) if d5s else None)

    st = {}
    sp = AP / "state.json"
    if sp.exists():
        st = json.loads(sp.read_text())
    out["state"] = {p: {k: (v if k != "prs" else v) for k, v in d.items() if k in ("pending", "prs", "impl_exhausted", "roadmap_exhausted")}
                    for p, d in st.items() if isinstance(d, dict) and not p.startswith("_")}
    out["config"] = json.loads((AP / "config.json").read_text())
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out")
    a = ap.parse_args()
    res = json.dumps(summarize(a.days), indent=2, default=str)
    if a.out:
        Path(a.out).write_text(res)
    else:
        print(res)
