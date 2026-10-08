#!/usr/bin/env python3
"""Claude autopilot: spend leftover Claude Code budget on roadmap work.

Run from cron every 10 min. Cheap tick (one HTTP call); launches `claude -p`
only when the pacing policy says go. See README.md.

  autopilot.py                      normal tick
  --dry-run                         decide, print, do not launch claude
  --simulate "u5=60 r5=20 u7=10 r7=96"   fake usage (r5 minutes, r7 hours)
  --force --project my-project [--mode implement|review|roadmap]   skip pacing
"""
import argparse, functools, hashlib, json, shutil, statistics, subprocess, time, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
AP = Path(__file__).resolve().parent
if not (AP / "config.json").exists():
    raise SystemExit("config.json missing: copy config.example.json to config.json and edit it")
CFG = json.loads((AP / "config.json").read_text())
PROJECTS_DIR = Path(CFG["projects_dir"]).expanduser()
STATE_F = AP / "state.json"
LOG_F = AP / "usage-log.jsonl"
RUNS_F = AP / "runs.jsonl"
SELF = "_autopilot"  # pseudo-project key for the weekly self-review
WEEK_H = 168.0
WINDOW_H = 5.0


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


# ---------- usage ----------
CRED_F = HOME / ".claude" / ".credentials.json"


def refresh_token():
    """A tiny claude call makes Claude Code refresh the OAuth token in .credentials.json."""
    log("OAuth token expired/expiring: refreshing via a tiny claude call")
    subprocess.run(["claude", "-p", "Reply OK", "--model", "haiku", "--output-format", "json"],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=120)


def read_cred():
    return json.loads(CRED_F.read_text())["claudeAiOauth"]


def fetch_usage():
    cred = read_cred()
    if cred.get("expiresAt", 1e18) / 1000 - time.time() < 600:  # <10 min left: refresh first
        refresh_token()
        cred = read_cred()
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {cred['accessToken']}",
                     "anthropic-beta": "oauth-2025-04-20"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=20))
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 1:
                refresh_token()
                cred = read_cred()
                continue
            raise

    def part(k):
        x = d.get(k) or {}
        ra = x.get("resets_at")
        r = None
        if ra:
            r = (datetime.fromisoformat(ra) - datetime.now(timezone.utc)).total_seconds() / 60
        return x.get("utilization"), r, ra
    u5, r5, ra5 = part("five_hour")
    u7, r7, ra7 = part("seven_day")
    return dict(u5=u5 or 0.0, r5=r5, ra5=ra5, u7=u7 or 0.0,
                r7=(r7 / 60 if r7 is not None else WEEK_H), ra7=ra7)


def fetch_usage_retry():
    """fetch_usage, backing off once on HTTP 429 (back-to-back runs trip the rate limit)."""
    try:
        return fetch_usage()
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        log("usage fetch 429; backing off 60s")
        time.sleep(60)
        return fetch_usage()


def simulated(s):
    kv = dict(p.split("=") for p in s.split())
    return dict(u5=float(kv.get("u5", 0)), r5=float(kv["r5"]) if "r5" in kv else None, ra5="sim",
                u7=float(kv.get("u7", 0)), r7=float(kv.get("r7", WEEK_H)), ra7="sim")


def append_usage(u):
    with LOG_F.open("a") as f:
        f.write(json.dumps(dict(ts=int(time.time()), **u)) + "\n")


def learn_k():
    """Pooled weekly-% per 1% of 5h window (sum d7 / sum d5) over consecutive rows in the same windows.

    Per-pair ratios are useless: u7 is an integer, so most pairs show d7=0 and the median collapses to 0.
    """
    if not LOG_F.exists():
        return CFG["default_k"]
    rows = [json.loads(l) for l in LOG_F.read_text().splitlines()[-2000:] if l.strip()]

    def key(s):  # resets_at has sub-second jitter: bucket to 10 minutes
        return round(datetime.fromisoformat(s).timestamp() / 600) if s and s != "sim" else None

    d5s = d7s = 0
    for a, b in zip(rows, rows[1:]):
        if key(a.get("ra5")) is None or key(a.get("ra5")) != key(b.get("ra5")) \
                or key(a.get("ra7")) != key(b.get("ra7")):
            continue
        d5, d7 = b["u5"] - a["u5"], b["u7"] - a["u7"]
        if d5 > 0 and d7 >= 0:
            d5s += d5
            d7s += d7
    if d5s < 40:  # need ~40 pts of u5 movement
        return CFG["default_k"]
    return min(0.5, max(0.02, d7s / d5s))


# ---------- policy ----------
WINDOW_MIN = WINDOW_H * 60


def decide(u, k):
    """Return (go, mode, reason, run_minutes).

    Ramp: fill = share of every remaining 5h window we must burn to still reach
    100% weekly. The allowed "tail" grows with fill, so work starts early in a
    week left untouched; fill*gain >= 1 means saturate (any time, open windows).
    Intra-window pace line: inside a live window the autopilot may only be at
    `100 * elapsed_share + u5_pace_margin` percent, so it consumes leftover
    budget gradually across the window instead of draining it in one burst and
    leaving no headroom for the user. SATURATE is exempt (weekly far behind:
    spending is the goal).
    """
    u5, r5, u7, r7 = u["u5"], u["r5"], u["u7"], u["r7"]
    if u5 >= CFG["max_u5"]:
        return False, None, f"5h window full ({u5:.0f}%)", 0
    if u7 >= CFG["max_u7"]:
        return False, None, f"weekly full ({u7:.0f}%)", 0
    windows_left = max(r7 / WINDOW_H, 0.2)
    need = 100 - u7
    fill = need / windows_left / (k * 100)
    tail = min(WINDOW_MIN, max(CFG["tail_minutes"], fill * CFG["ramp_gain"] * WINDOW_MIN))
    info = f"k={k:.3f} windows_left={windows_left:.1f} need={need:.0f}% fill={fill:.2f} tail={tail:.0f}m"
    # Hard weekly cap: autopilot may never push usage past the linear pace line
    # (+ small margin), so it can't front-load the week and starve you later.
    pace = 100 * (1 - r7 / WEEK_H)
    cap = min(CFG["max_u7"], pace + CFG["pace_margin"])
    if u7 >= cap:
        return False, "cap", f"weekly cap reached ({u7:.0f}% >= pace {pace:.0f}% + {CFG['pace_margin']}); {info}", 0
    info += f" cap={cap:.0f}%"
    if tail >= WINDOW_MIN:
        run = CFG["max_run_minutes"] if r5 is None else min(r5 - 2, CFG["max_run_minutes"])
        if run < CFG["min_run_minutes"]:
            return False, "saturate", f"window ends in {r5:.0f}m, too short; {info}", 0
        return True, "saturate", f"SATURATE {info}", run
    # Intra-window pace line: hold u5 on the elapsed-share line (+ margin) so a
    # window drains across its whole life and the rest stays free for the user.
    elapsed = 0.0 if r5 is None else min(1.0, max(0.0, (WINDOW_MIN - r5) / WINDOW_MIN))
    u5_line = 100 * elapsed + CFG["u5_pace_margin"]
    info += f" u5line={u5_line:.0f}%"
    if u5 >= u5_line:
        return False, "u5pace", (f"u5 pace line reached (u5={u5:.0f}% >= {u5_line:.0f}% "
                                 f"at {elapsed*100:.0f}% of window); {info}"), 0
    if r5 is None:
        if fill >= CFG["open_window_fill"] and u7 < pace:  # clearly behind the line: open a window
            return True, "open", f"OPEN window (behind pace {u7:.0f}% < {pace:.0f}%); {info}", CFG["max_run_minutes"]
        return False, "tail", f"no active window; waiting for user to open one; {info}", 0
    if r5 > tail:
        return False, "tail", f"window tail not reached ({r5:.0f}m left > {tail:.0f}m); {info}", 0
    if 100 - u5 < CFG["min_window_headroom_pct"]:
        return False, "tail", f"headroom {100-u5:.0f}% too small; {info}", 0
    run = min(r5 - 2, CFG["max_run_minutes"])
    if run < CFG["min_run_minutes"]:
        return False, "tail", f"only {r5:.0f}m left, too short; {info}", 0
    return True, "tail", f"TAIL {r5:.0f}m left, u5={u5:.0f}% u7={u7:.0f}% pace={pace:.0f}%; {info}", run


# ---------- state / repos ----------
def load_state():
    return json.loads(STATE_F.read_text()) if STATE_F.exists() else {}


def save_state(s):
    STATE_F.write_text(json.dumps(s, indent=2))


def sh(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


@functools.cache
def repo_info(proj):
    repo = PROJECTS_DIR / proj
    url = sh(["git", "remote", "get-url", "origin"], repo)
    slug = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    r = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo,
                       capture_output=True, text=True)
    default = r.stdout.strip().split("/")[-1] if r.returncode == 0 else "main"
    return repo, slug, default


def ensure_worktree(proj, repo, default):
    wt = AP / "worktrees" / proj
    sh(["git", "fetch", "origin", "--prune"], repo)
    if not wt.exists():
        sh(["git", "worktree", "add", "--detach", str(wt), f"origin/{default}"], repo)
    else:
        sh(["git", "reset", "--hard", "-q"], wt)
        sh(["git", "clean", "-fdq"], wt)
    return wt


def roadmap_hash(proj, default):
    repo = PROJECTS_DIR / proj
    r = subprocess.run(["git", "show", f"origin/{default}:ROADMAP.md"], cwd=repo, capture_output=True)
    return hashlib.sha1(r.stdout).hexdigest()


_ME = []


def gh_login():
    if not _ME:
        _ME.append(sh(["gh", "api", "user", "--jq", ".login"]))
    return _ME[0]


def open_prs(slug):
    out = sh(["gh", "pr", "list", "-R", slug, "--state", "open", "--limit", "50",
              "--json", "number,headRefName,labels,isCrossRepository,author"])
    # Only our own same-repo PRs: on a public repo anyone can open a fork PR from a
    # branch named autopilot/*, and review mode would merge it if it looks clean.
    me = gh_login()
    prs = [p for p in json.loads(out)
           if not p.get("isCrossRepository") and (p.get("author") or {}).get("login") == me]
    if CFG["pr_scope"] == "autopilot":
        prs = [p for p in prs if p["headRefName"].startswith("autopilot/")]
    return [p for p in prs if "autopilot-stuck" not in [l["name"] for l in p["labels"]]]


DIGEST_F = AP / "digest.jsonl"


def notify(msg, urgent=False):
    """Urgent messages go out now; routine ones (PR opened/merged, resumes) are batched by flush_digest()."""
    if not CFG.get("notify_telegram"):
        return
    if not urgent and CFG.get("digest_minutes", 60) > 0:
        with DIGEST_F.open("a") as f:
            f.write(json.dumps(dict(ts=int(time.time()), msg=msg)) + "\n")
        return
    send_telegram(msg)


def flush_digest(force=False):
    """Send queued routine messages as one Telegram message once the oldest is digest_minutes old."""
    if not DIGEST_F.exists():
        return
    rows = [json.loads(l) for l in DIGEST_F.read_text().splitlines() if l.strip()]
    if not rows:
        return
    if not force and time.time() - rows[0]["ts"] < CFG.get("digest_minutes", 60) * 60:
        return
    lines = [f"{time.strftime('%H:%M', time.localtime(r['ts']))} {r['msg']}" for r in rows]
    if len(lines) > 40:
        lines = lines[:15] + [f"... {len(lines) - 30} more ..."] + lines[-15:]
    if send_telegram(f"digest ({len(rows)} events)\n" + "\n".join(lines)):
        DIGEST_F.unlink()


def send_telegram(msg):
    try:
        env = (HOME / ".claude/channels/telegram/.env").read_text()
        tok = next(l.split("=", 1)[1].strip() for l in env.splitlines() if l.startswith("TELEGRAM_BOT_TOKEN="))
        chat = json.loads((HOME / ".claude/channels/telegram/access.json").read_text())["allowFrom"][0]
        data = json.dumps({"chat_id": chat, "text": f"[autopilot] {msg}"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data,
            {"Content-Type": "application/json"}), timeout=15)
        return True
    except Exception as e:  # best effort
        log(f"notify failed: {e}")
        return False


# ---------- run claude ----------
VALID = {"implement": {"PR_OPENED", "NO_PENDING_STEPS", "INCOMPLETE", "BLOCKED"},
         "review": {"MERGED", "FIXED", "BLOCKED"},
         "roadmap": {"PR_OPENED", "NO_NEW_ITEMS", "BLOCKED"},
         "selfreview": {"REVIEW_DONE", "BLOCKED"}}

# INCOMPLETE = "the step is not finished, keep this session and resume it". It is
# accepted from the model but never offered back to a resumed run (otherwise a
# resume could stall forever by re-declaring itself incomplete).
RESUME_MARKERS = {m: " | ".join(sorted(v - {"INCOMPLETE"})) for m, v in VALID.items()}


def parse_marker(text, mode):
    """Status marker from the tail of the answer.

    Models write it bare ("PR_OPENED") or decorated ("STATUS: PR_OPENED",
    "**MERGED**", "FIXED (pending CI on abc123)"). Scan the last few lines for one
    that starts with a valid token and return the bare token; otherwise return the
    literal last line so the caller can report what the run actually produced.
    """
    lines = [l for l in text.strip().splitlines() if l.strip()]
    for raw in reversed(lines[-5:]):
        line = raw.strip().strip("*_`#>").strip()
        if line.upper().startswith("STATUS:"):
            line = line.split(":", 1)[1].strip()
        up = line.upper()
        for v in sorted(VALID[mode], key=len, reverse=True):
            if up == v or up.startswith((v + " ", v + ":", v + "(")):
                return v
    return (lines or [""])[-1].strip()

# config keys the weekly self-review may change, with allowed bounds
BOUNDS = {"tail_minutes": (30, 150), "pace_margin": (2, 8), "default_k": (0.05, 0.3),
          "max_run_minutes": (20, 60), "min_run_minutes": (10, 25), "ramp_gain": (1.0, 2.0),
          "min_window_headroom_pct": (1, 10), "max_review_iterations": (4, 12),
          "exhausted_hours": (6, 72), "open_window_fill": (0.3, 0.9),
          "max_runs_per_tick": (3, 30), "max_resumes": (1, 5), "u5_pace_margin": (5, 30)}

RESUME_PROMPT = """Your previous autopilot run on this task was interrupted ({reason}) before finishing.
RESUME it now: first check the real state (`git status`, `git log --oneline -5`, current branch, open PR),
then continue exactly where you left off, with the same task and the same hard rules.
You have roughly {minutes} more minutes. Finish with one status marker as the LAST line: {markers}."""


def model_for(mode):
    return CFG.get("model_by_mode", {}).get(mode, CFG["model"])


def run_claude(proj, mode, minutes, extra, resume=None):
    """Return (marker, interrupted_reason|None, session_id, meta)."""
    selfrev = mode == "selfreview"
    if selfrev:
        slug = default = None
    else:
        repo, slug, default = repo_info(proj)
    if resume:
        wt = AP if selfrev else AP / "worktrees" / proj  # keep uncommitted work: no reset
        sid = resume["session_id"]
        prompt = RESUME_PROMPT.format(reason=resume["reason"], minutes=int(minutes),
                                      markers=RESUME_MARKERS[mode])
        sess = ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        sess = ["--session-id", sid]
        if selfrev:
            wt = AP
            bounds = "\n".join(f"   - {k}: {lo} .. {hi}" for k, (lo, hi) in BOUNDS.items())
            prompt = (AP / "prompts/selfreview.md").read_text().format(
                ap_dir=str(AP), minutes=int(minutes), today=time.strftime("%Y-%m-%d"),
                bounds=bounds, **extra)
        else:
            wt = ensure_worktree(proj, repo, default)
            ctx = dict(project=proj, worktree=str(wt), slug=slug, default_branch=default,
                       branch_prefix=f"autopilot/{time.strftime('%m%d')}-{sid[:6]}",
                       minutes=int(minutes), **extra)
            prompt = ((AP / "prompts/common.md").read_text() + "\n"
                      + (AP / f"prompts/{mode}.md").read_text()).format(**ctx)
    model = model_for(mode)
    logf = AP / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{proj}-{mode}{'-resume' if resume else ''}.log"
    log(f"launch {proj}/{mode}{' (RESUME)' if resume else ''} model={model} timeout={int(minutes)}m "
        f"session={sid[:8]} log={logf.name}")
    t0 = time.time()
    with logf.open("w") as f, logf.with_suffix(".err").open("w") as e:
        try:
            r = subprocess.run(
                ["claude", "-p", prompt, "--model", model, "--dangerously-skip-permissions",
                 "--output-format", "json", *sess],
                cwd=wt, stdin=subprocess.DEVNULL, stdout=f, stderr=e, timeout=minutes * 60)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    text = logf.read_text()
    is_err, cost, turns = False, None, None
    try:
        d = json.loads(text)
        text, is_err = d.get("result", text) or "", bool(d.get("is_error"))
        sid = d.get("session_id") or sid  # a resume may continue under a new id
        cost, turns = d.get("total_cost_usd"), d.get("num_turns")
    except Exception:
        pass
    marker = parse_marker(text, mode)
    reason = None
    if marker == "INCOMPLETE":  # ran out of budget with the step unfinished: resume it
        reason = "step not finished (INCOMPLETE)"
    elif marker not in VALID[mode]:
        low = (text + logf.with_suffix(".err").read_text()).lower()
        if rc == "timeout":
            reason = "killed at time limit"
        elif "limit reached" in low or "usage limit" in low or "rate limit" in low:
            reason = "usage limit hit"
        else:
            reason = "ended without status marker" + (" (error)" if is_err or rc else "")
    log(f"done {proj}/{mode} rc={rc} marker={marker[:60]!r} interrupted={reason}")
    meta = dict(model=model, duration_s=round(time.time() - t0), cost=cost, turns=turns,
                budget_min=int(minutes), resumed=bool(resume), log=logf.name)
    return marker, reason, sid, meta


def record_run(proj, mode, marker, reason, sid, meta, u_before):
    """Append the run row; return the post-run usage ({} if the fetch failed) for reuse by the tick loop."""
    u_after = {}
    try:
        u_after = fetch_usage_retry()
    except Exception:
        pass
    row = dict(ts=int(time.time()), project=proj, mode=mode, marker=marker[:60], interrupted=reason,
               session=sid[:8], u5_before=u_before.get("u5"), u7_before=u_before.get("u7"),
               u5_after=u_after.get("u5"), u7_after=u_after.get("u7"), **meta)
    with RUNS_F.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return u_after


# ---------- weekly self-review ----------
def selfreview_due(state):
    last = state.get(SELF, {}).get("last_selfreview", 0)
    if time.time() - last < CFG["selfreview_days"] * 86400:
        return False
    if not RUNS_F.exists():
        return False
    n = sum(1 for l in RUNS_F.read_text().splitlines() if l.strip() and json.loads(l)["ts"] > last)
    return n >= CFG["selfreview_min_runs"]


def prepare_selfreview():
    """Snapshot config/code/prompts (restored/validated afterwards) and write stats."""
    day = time.strftime("%Y-%m-%d")
    rep = AP / "reports"
    rep.mkdir(exist_ok=True)
    bk = rep / f"backup-{day}-{time.strftime('%H%M%S')}"
    bk.mkdir()
    for f in ("config.json", "autopilot.py", "analyze.py"):
        shutil.copy(AP / f, bk / f)
    shutil.copytree(AP / "prompts", bk / "prompts")
    stats = rep / f"stats-{day}.json"
    sh(["python3", str(AP / "analyze.py"), "--days", "14", "--out", str(stats)])
    return dict(stats_file=str(stats), report_file=str(rep / f"{day}-selfreview.md"), backup=str(bk))


def finalize_selfreview(extra):
    """Enforce guardrails: restore code/prompts, keep only in-bounds config changes."""
    bk = Path(extra["backup"])
    for old in [bk / "autopilot.py", bk / "analyze.py", *sorted((bk / "prompts").iterdir())]:
        cur = AP / old.relative_to(bk)
        if not cur.exists() or cur.read_bytes() != old.read_bytes():
            shutil.copy(old, cur)
            log(f"selfreview modified {cur.name}: restored (proposals belong in the report)")
    old = json.loads((bk / "config.json").read_text())
    new = json.loads((AP / "config.json").read_text())
    final, applied = dict(old), {}
    for k, (lo, hi) in BOUNDS.items():
        v = new.get(k)
        if v != old.get(k) and isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            if len(applied) < 3:
                final[k], applied[k] = v, (old.get(k), v)
    (AP / "config.json").write_text(json.dumps(final, indent=2))
    rejected = [k for k in new if new.get(k) != old.get(k) and k not in applied]
    if rejected:
        log(f"selfreview config changes rejected: {rejected}")
    return applied


def prune(dry=False):
    """Delete per-run logs, tick.log lines, old telemetry rows and report backups past retention."""
    now = time.time()
    lcut = now - CFG["log_keep_days"] * 86400
    dcut = now - CFG["data_keep_days"] * 86400
    freed, files = 0, 0

    def rm(p):
        nonlocal freed, files
        freed += p.stat().st_size if p.is_file() else sum(x.stat().st_size for x in p.rglob("*") if x.is_file())
        files += 1
        if not dry:
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    for f in (AP / "logs").glob("*"):  # per-run .log/.err files (tick.log is trimmed by line below)
        if f.name != "tick.log" and f.is_file() and f.stat().st_mtime < lcut:
            rm(f)
    rep = AP / "reports"
    if rep.exists():
        for f in rep.glob("backup-*"):
            if f.stat().st_mtime < dcut:
                rm(f)
        for f in rep.glob("stats-*.json"):
            if f.stat().st_mtime < dcut:
                rm(f)

    def rewrite(path, keep):  # in place: cron holds tick.log open in append mode
        nonlocal freed
        if not path.exists():
            return
        lines = path.read_text().splitlines(keepends=True)
        kept = keep(lines)
        freed += sum(len(l) for l in lines) - sum(len(l) for l in kept)
        if not dry and len(kept) != len(lines):
            with path.open("r+") as fh:
                fh.seek(0)
                fh.writelines(kept)
                fh.truncate()

    def keep_tick(lines):
        out, ok = [], True
        for l in lines:
            try:
                ok = datetime.fromisoformat(l[:19]).timestamp() >= lcut
            except ValueError:
                pass  # continuation line: follows its parent
            if ok:
                out.append(l)
        return out

    def keep_jsonl(lines):
        out = []
        for l in lines:
            try:
                if json.loads(l)["ts"] >= dcut:
                    out.append(l)
            except Exception:
                out.append(l)
        return out

    rewrite(AP / "logs" / "tick.log", keep_tick)
    rewrite(LOG_F, keep_jsonl)
    rewrite(RUNS_F, keep_jsonl)
    msg = f"{'would prune' if dry else 'pruned'} {files} files, {freed/1e6:.1f} MB (logs>{CFG['log_keep_days']}d, data>{CFG['data_keep_days']}d)"
    log(msg)
    return msg


def exhausted(state, proj, kind, default):
    e = state.get(proj, {}).get(kind)
    if not e or e.get("hash") != roadmap_hash(proj, default):
        return False
    if e.get("forever"):  # parked pending a human; only a roadmap edit releases it
        return True
    return e.get("until", 0) > time.time()


def mark_exhausted(state, proj, kind, default, forever=False):
    """forever=True parks the project until ROADMAP.md changes (BLOCKED: needs a human).

    A BLOCKED run would otherwise be re-attempted on every tick, burning a full
    budget and re-notifying each time, because nothing else records the block.
    The roadmap hash is the release valve: a new/edited step makes it eligible
    again; `--unblock <project>` clears it by hand.
    """
    state.setdefault(proj, {})[kind] = dict(
        until=4102444800 if forever else time.time() + CFG["exhausted_hours"] * 3600,
        hash=roadmap_hash(proj, default), forever=forever)
    save_state(state)


def find_pending(state, projects):
    """First interrupted session still worth resuming (priority order)."""
    for proj in [*projects, SELF]:
        p = state.get(proj, {}).get("pending")
        if not p:
            continue
        if proj != SELF and exhausted(state, proj, "human_blocked", repo_info(proj)[2]):
            log(f"discard pending {proj}/{p['mode']} (project parked: human_blocked)")
            state[proj].pop("pending")
            save_state(state)
            continue
        stale = time.time() - p["ts"] > CFG["resume_max_hours"] * 3600
        too_many = p["resumes"] >= CFG["max_resumes"]
        gone = False
        if p["mode"] == "review":
            _, slug, _ = repo_info(proj)
            gone = p["extra"]["pr"] not in [x["number"] for x in open_prs(slug)]
        if stale or too_many or gone:
            log(f"discard pending {proj}/{p['mode']} (stale={stale} too_many={too_many} gone={gone})")
            state[proj].pop("pending")
            save_state(state)
            continue
        return proj, p
    return None


def streak_project():
    """Project that opened all of the last `streak_limit` PRs, else None."""
    n = CFG.get("streak_limit", 5)
    if not RUNS_F.exists():
        return None
    rows = [json.loads(l) for l in RUNS_F.read_text().splitlines() if l.strip()]
    last = [r["project"] for r in rows if r.get("marker") == "PR_OPENED"][-n:]
    return last[0] if len(last) == n and len(set(last)) == 1 else None


def choose_work(state, args):
    projects = [args.project] if args.project else CFG["projects"]
    hot = None if args.project else streak_project()
    if hot and hot in projects:  # same project opened the last N PRs: move on to the next one
        log(f"streak: last {CFG.get('streak_limit', 5)} PRs all in {hot}; deprioritized for new work")
        work = _choose_work(state, args, [p for p in projects if p != hot], projects)
        if work:
            return work
    return _choose_work(state, args, projects, projects)


def _choose_work(state, args, projects, review_projects):
    for proj in review_projects:  # pass A0: open PRs are always finished first
        if args.mode == "review" or (not args.mode and open_prs(repo_info(proj)[1])):
            work = _review_work(state, args, proj)
            if work:
                return work
    for proj in projects:  # pass A: review open PR, else implement next step
        _, _, default = repo_info(proj)
        if args.mode in (None, "implement") and not exhausted(state, proj, "impl_exhausted", default) \
                and (args.force or not exhausted(state, proj, "human_blocked", default)):
            return proj, "implement", {}
    if args.mode in (None, "roadmap"):  # pass B: author new roadmap items (Opus, see model_by_mode)
        for proj in projects:
            _, _, default = repo_info(proj)
            if not exhausted(state, proj, "roadmap_exhausted", default) \
                    and (args.force or not exhausted(state, proj, "human_blocked", default)):
                return proj, "roadmap", {}
    return None


def _review_work(state, args, proj):
    _, slug, _ = repo_info(proj)
    prs = open_prs(slug)
    if not prs:
        return None
    p = prs[0]
    ps = state.setdefault(proj, {}).setdefault("prs", {})
    it = ps.get(str(p["number"]), 0) + 1
    if it > CFG["max_review_iterations"]:
        sh(["gh", "pr", "edit", str(p["number"]), "-R", slug, "--add-label",
            "autopilot-stuck"], check=False)
        notify(f"{proj} PR #{p['number']} stuck after {it-1} review iterations", urgent=True)
        return None
    return proj, "review", dict(pr=p["number"], branch=p["headRefName"], iteration=it,
                                max_iter=CFG["max_review_iterations"])


_LAST_USAGE = [{}]  # usage read right after the last run: reused by the next loop iteration


def pick_and_run(minutes, args, u_before):
    """Run one unit of work. Return True if a run finished normally."""
    state = load_state()
    projects = [args.project] if args.project else CFG["projects"]
    pend = find_pending(state, projects)
    resume = None
    if pend:
        proj, resume = pend
        mode, extra = resume["mode"], resume["extra"]
    elif args.mode == "selfreview" or (not args.mode and selfreview_due(state)):
        proj, mode, extra = SELF, "selfreview", prepare_selfreview()
    else:
        work = choose_work(state, args)
        if not work:
            log("nothing to do (all projects exhausted)")
            return False
        proj, mode, extra = work
        floor = CFG.get("min_step_minutes", 30)
        if mode in ("implement", "roadmap") and minutes < floor and not args.force:
            # A whole step does not fit: the run would declare INCOMPLETE without doing anything.
            log(f"only {minutes:.0f}m budget (< min_step_minutes {floor}); no new {mode} this window")
            return False
    default = None if mode == "selfreview" else repo_info(proj)[2]
    slug = None if mode == "selfreview" else repo_info(proj)[1]
    if mode == "review" and not resume:  # count the iteration at launch
        state.setdefault(proj, {}).setdefault("prs", {})[str(extra["pr"])] = extra["iteration"]
        save_state(state)

    t0 = time.time()
    marker, reason, sid, meta = run_claude(proj, mode, minutes, extra, resume)
    _LAST_USAGE[0] = record_run(proj, mode, marker, reason, sid, meta, u_before)
    state = load_state()

    if reason:  # interrupted: keep session so the next run resumes it
        state.setdefault(proj, {})["pending"] = dict(
            mode=mode, extra=extra, session_id=sid, reason=reason,
            ts=resume["ts"] if resume else time.time(),
            resumes=resume["resumes"] + 1 if resume else 0)
        save_state(state)
        notify(f"{proj}/{mode} interrupted ({reason}); will resume next run")
        return time.time() - t0 > 30  # instant failure => stop the tick loop

    state.get(proj, {}).pop("pending", None)
    if mode == "selfreview":
        applied = finalize_selfreview(extra) if marker == "REVIEW_DONE" else {}
        state.setdefault(SELF, {})["last_selfreview"] = time.time()
        txt = ", ".join(f"{k}: {a}->{b}" for k, (a, b) in applied.items()) or "no config changes"
        pruned = prune() if marker == "REVIEW_DONE" else "no prune (review not completed)"
        notify(f"weekly self-review done ({txt}); report: {Path(extra['report_file']).name}; {pruned}", urgent=True)
    elif mode == "review":
        key = str(extra["pr"])
        prs = state.setdefault(proj, {}).setdefault("prs", {})
        if marker == "MERGED":
            prs.pop(key, None)
            notify(f"{proj} PR #{extra['pr']} merged")
        elif marker == "BLOCKED":
            sh(["gh", "pr", "edit", key, "-R", slug, "--add-label", "autopilot-stuck"], check=False)
            notify(f"{proj} PR #{extra['pr']} blocked: unfixable Medium+ finding (issue opened)", urgent=True)
    elif marker == "NO_PENDING_STEPS":
        mark_exhausted(state, proj, "impl_exhausted", default)
    elif marker == "NO_NEW_ITEMS":
        mark_exhausted(state, proj, "roadmap_exhausted", default)
    elif marker == "PR_OPENED":
        notify(f"{proj}: PR opened ({mode})")
    elif marker == "BLOCKED":
        if mode in ("implement", "roadmap"):
            # Park the project: a BLOCKED step needs a human, so retrying it each
            # tick only burns budget and spams alerts. Released by a ROADMAP.md
            # edit (new hash) or `--unblock <project>`.
            mark_exhausted(state, proj, "human_blocked", default, forever=True)
            notify(f"{proj}/{mode} BLOCKED: needs a human (see {meta['log']}); parked "
                   f"until ROADMAP.md changes or `--unblock {proj}`", urgent=True)
        else:
            notify(f"{proj}/{mode} BLOCKED: needs a human (see {meta['log']})", urgent=True)
    save_state(state)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--simulate")
    ap.add_argument("--force", action="store_true", help="skip pacing, run one unit")
    ap.add_argument("--prune", action="store_true", help="prune logs/telemetry now (honours --dry-run)")
    ap.add_argument("--project")
    ap.add_argument("--mode", choices=["implement", "review", "roadmap", "selfreview"])
    ap.add_argument("--unblock", metavar="PROJECT", help="clear a human_blocked park set by a BLOCKED run")
    args = ap.parse_args()

    if args.unblock:
        s = load_state()
        e = s.get(args.unblock, {}).pop("human_blocked", None)
        save_state(s)
        log(f"{'cleared' if e else 'no'} human_blocked park for {args.unblock}")
        return
    if args.prune:
        prune(dry=args.dry_run)
        return
    if (AP / "PAUSE").exists() and not args.force:
        log("PAUSE present, skipping")
        return
    try:
        tick(args)
    finally:
        if not (args.dry_run or args.simulate):
            flush_digest()


def tick(args):
    for n in range(CFG["max_runs_per_tick"]):
        try:
            if args.simulate:
                u = simulated(args.simulate)
            else:  # reuse the post-run read: a second fetch seconds later trips HTTP 429
                u = _LAST_USAGE[0] or fetch_usage_retry()
                _LAST_USAGE[0] = {}
        except Exception as e:
            log(f"usage fetch failed: {e}")
            return
        k = learn_k()
        if args.force:
            go, mode, reason, minutes = True, "force", "forced", CFG["max_run_minutes"]
        else:
            go, mode, reason, minutes = decide(u, k)
        if not args.simulate:
            append_usage(dict(u, go=go, mode=mode, reason=(reason or "")[:100]))
        r5 = "none" if u["r5"] is None else f"{u['r5']:.0f}m"
        log(f"u5={u['u5']:.0f}% r5={r5} u7={u['u7']:.0f}% r7={u['r7']:.1f}h -> "
            f"{'GO' if go else 'skip'} [{mode}] {reason}")
        if not go or args.dry_run:
            return
        if not sh(["which", "gh"], check=False):
            log("gh not installed; abort")
            return
        ok = pick_and_run(minutes, args, u)
        if not ok or args.force or args.simulate:
            return  # one unit only when forced/simulated or when nothing/failed


if __name__ == "__main__":
    main()
