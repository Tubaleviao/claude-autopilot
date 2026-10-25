#!/usr/bin/env python3
"""Claude autopilot: spend leftover Claude Code budget on roadmap work.

Run from cron every 10 min. Cheap tick (one HTTP call); launches `claude -p`
only when the pacing policy says go. See README.md.

  autopilot.py                      normal tick
  --dry-run                         decide, print, do not launch claude
  --simulate "u5=60 r5=20 u7=10 r7=96"   fake usage (r5 minutes, r7 hours)
  --force --project my-project [--mode implement|review|roadmap]   skip pacing
  --burn                            no pacing: run back-to-back until a hard limit (also config "burn")

Merging is the driver's job: a clean review ends CLEAN, the driver polls CI on the
head commit (no model tokens) and merges, or sends the PR back to review with a
Haiku summary of the failed CI logs.
"""
import argparse, functools, itertools, hashlib, json, re, shutil, statistics, subprocess, time, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
AP = Path(__file__).resolve().parent
if not (AP / "config.json").exists():
    raise SystemExit("config.json missing: copy config.example.json to config.json and edit it")
CFG = json.loads((AP / "config.json").read_text())
PROJECTS_DIR = Path(CFG["projects_dir"]).expanduser()


def code_sig():
    """mtimes of the files loaded once at startup; prompts/ are re-read every run."""
    return tuple((AP / f).stat().st_mtime for f in ("autopilot.py", "config.json"))


START_SIG = code_sig()


def popt(proj, key, default=None):
    """Per-project override from config "project_opts": roadmap path, push_remote, merge, author_roadmap."""
    return CFG.get("project_opts", {}).get(proj, {}).get(key, default)

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


def decide_burn(u):
    """BURN: spend as fast as possible. Only the hard max_u5/max_u7 limits stop it.

    No tail, no pace line, no weekly cap, no window-end clamp: a run cut off by
    the 5h limit is stored as pending and resumed once the window resets.
    """
    if u["u5"] >= CFG["max_u5"]:
        return False, "burn", f"BURN: 5h window full ({u['u5']:.0f}%); waiting for reset", 0
    if u["u7"] >= CFG["max_u7"]:
        return False, "burn", f"BURN: weekly full ({u['u7']:.0f}%); waiting for reset", 0
    return True, "burn", f"BURN u5={u['u5']:.0f}% u7={u['u7']:.0f}%", CFG["max_run_minutes"]


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
    r = subprocess.run(["git", "show", f"origin/{default}:{popt(proj, 'roadmap', 'ROADMAP.md')}"],
                       cwd=repo, capture_output=True)
    return hashlib.sha1(r.stdout).hexdigest()


_ME = []


def gh_login():
    if not _ME:
        _ME.append(sh(["gh", "api", "user", "--jq", ".login"]))
    return _ME[0]


def remote_owner(proj, remote):
    url = sh(["git", "remote", "get-url", remote], PROJECTS_DIR / proj)
    return url.split("github.com")[-1].lstrip(":/").split("/")[0]


def open_prs(proj, include_parked=False):
    _, slug, _ = repo_info(proj)
    out = sh(["gh", "pr", "list", "-R", slug, "--state", "open", "--limit", "50", "--json",
              "number,title,headRefName,headRefOid,labels,isCrossRepository,headRepositoryOwner,author"])
    # Only our own PRs: on a public repo anyone can open a fork PR from a branch named
    # autopilot/*, and review mode would merge it if it looks clean. A project with
    # push_remote works from our fork, so its PRs are cross-repo with us as head owner.
    me = gh_login()
    fork = bool(popt(proj, "push_remote"))
    prs = [p for p in json.loads(out)
           if (p.get("author") or {}).get("login") == me
           and bool(p.get("isCrossRepository")) == fork
           and (not fork or (p.get("headRepositoryOwner") or {}).get("login") == me)]
    if CFG["pr_scope"] == "autopilot":
        prs = [p for p in prs if p["headRefName"].startswith("autopilot/")]
    if include_parked:
        return prs
    st = load_state().get(proj, {})
    parked, waiting = st.get("parked_prs", {}), st.get("waiting_prs", {})
    ci_wait = st.get("ci_wait", {})
    return [p for p in prs if "autopilot-stuck" not in [l["name"] for l in p["labels"]]
            and parked.get(str(p["number"])) != p["headRefOid"]
            and (ci_wait.get(str(p["number"])) or {}).get("sha") != p["headRefOid"]
            and not (waiting.get(str(p["number"])) == p["headRefOid"] and waits_on_dep(proj, p["number"]))]


DEP_LABEL = "autopilot-dependency"


def dependents(proj):
    """Projects whose config says depends_on: proj."""
    return [p for p in CFG["projects"] if popt(p, "depends_on") == proj]


def dep_issues(slug):
    """Open autopilot-dependency issues on slug: work a dependent repo's PR is waiting for."""
    out = sh(["gh", "issue", "list", "-R", slug, "--state", "open", "--label", DEP_LABEL, "--author", "@me",
              "--limit", "50", "--json", "number,title,body,comments"], check=False)
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


def pr_issues(slug, num):
    """Open issues we filed that mention PR #num: earlier review rounds' follow-ups and blockers."""
    out = sh(["gh", "issue", "list", "-R", slug, "--state", "open", "--author", "@me", "--limit", "50",
              "--search", f'"PR #{num}"', "--json", "number,title,body"], check=False)
    try:
        issues = json.loads(out or "[]")
    except ValueError:
        return []
    ref = re.compile(rf"\bPR #{num}\b", re.I)  # search is fuzzy: "PR #15" also hits "PR #8 ... 15"
    return sorted(({"number": i["number"], "title": i["title"]} for i in issues
                   if ref.search(i["title"] + " " + (i.get("body") or ""))), key=lambda i: -i["number"])


def existing_issues_rule(slug, num):
    """Review prompt text: reuse and merge the issues earlier rounds opened instead of adding more."""
    issues = pr_issues(slug, num)
    if not issues:
        return "None yet."
    fu = [i for i in issues if i["title"].lower().startswith("follow-ups from pr")]
    lines = "\n".join(f"  - #{i['number']} {i['title']}" for i in issues)
    keep = (f"Keep #{fu[0]['number']} as THE follow-up issue: add new Low findings to its checklist "
            f"(`gh issue edit {fu[0]['number']} -R {slug} --body ...`, keep existing items, tick ones now fixed) "
            f"instead of opening a new one. " if fu else "")
    return (f"Earlier rounds opened these (newest first):\n{lines}\n{keep}"
            f"Before opening any issue, reuse one of these that covers it. If several cover the same thing, keep "
            f"the newest, copy unique unchecked items from the others into it, and close the others with "
            f"`gh issue close N -R {slug} --reason \"not planned\" --comment \"Duplicate of #<kept>.\"`. "
            f"Never close an issue that still has unique unresolved content.")


def waits_on_dep(proj, num):
    """True while an open dependency issue in depends_on's repo references <slug>#num."""
    dep = popt(proj, "depends_on")
    if not dep:
        return False
    ref = re.compile(re.escape(repo_info(proj)[1]) + rf"#{num}\b")
    return any(ref.search(" ".join([i["title"], i.get("body") or ""]
                                   + [c.get("body") or "" for c in i.get("comments") or []]))
               for i in dep_issues(repo_info(dep)[1]))


def park_pr(state, proj, num, by_sha=False):
    """Stop reviewing a PR. Same-repo PRs get the autopilot-stuck label; READY PRs and PRs
    on an upstream we cannot label are parked by head SHA, so a new commit un-parks them."""
    _, slug, _ = repo_info(proj)
    if by_sha or popt(proj, "push_remote"):
        sha = sh(["gh", "pr", "view", str(num), "-R", slug, "--json", "headRefOid",
                  "--jq", ".headRefOid"], check=False)
        state.setdefault(proj, {}).setdefault("parked_prs", {})[str(num)] = sha
    else:
        sh(["gh", "pr", "edit", str(num), "-R", slug, "--add-label", "autopilot-stuck"], check=False)


def overrides(proj, slug, default, mode):
    """Prompt rules for projects that push to a fork and/or must not merge (config project_opts)."""
    rules, push = [], popt(proj, "push_remote")
    if push:
        owner = remote_owner(proj, push)
        rules += [
            f"- Remote `origin` is upstream {slug}: read-only for you. Base every new branch on "
            f"`origin/{default}` and open every PR against {default} (never stack PRs on other branches). "
            f"Push branches to git remote `{push}` (owner {owner}); wherever a step says `git fetch origin` "
            f"or `origin/<branch>` for a PR branch, use `{push}` instead.",
            f"- Open PRs with `gh pr create -R {slug} --base {default} --head {owner}:<branch> ...`.",
            f"- Never create issues or labels on {slug}: you do not own it. Where a step says to open an "
            f"issue, post that content as a PR comment (`gh pr comment`) instead; skip `Closes #n` lines."]
    if not popt(proj, "merge", True):
        rules.append("- NEVER merge a PR (no `gh pr merge`): upstream maintainers merge.")
        if mode == "implement":
            titles = [f"  - #{p['number']} {p['title']}" for p in open_prs(proj, include_parked=True)]
            if titles:
                rules.append("- These open PRs already implement roadmap steps and await upstream merge: "
                             "treat their steps as done and pick the next pending step.\n" + "\n".join(titles))
    dep = popt(proj, "depends_on")
    if dep:
        dslug = repo_info(dep)[1]
        what = ("If, after everything else is fixed, that dependency is the only thing failing CI (see CI STATUS) "
                "or the local checks, "
                "comment on the PR linking the issue and finish with marker WAITING (not BLOCKED, not FIXED): "
                "the PR is parked until the issue closes. Before finishing, close every issue in EXISTING ISSUES "
                f"that only asks for that dependency work (`gh issue close N -R {slug} --reason \"not planned\" "
                f"--comment \"Duplicate. Tracked in {dslug}#<dep issue>.\"`); for one that also lists other items, "
                "comment that the dependency part moved and leave it open." if mode == "review" else
                "Still finish and open the step's PR; name the dependency issue in the PR body.")
        rules.append(
            f"- {slug} depends on {dslug} (project {dep}). {popt(proj, 'context', '')} "
            f"Checks here run against {dslug}'s default branch, so work only {dslug} can do (e.g. files or "
            f"placeholders that must exist there) cannot be fixed in this repo and is NOT a finding here. Never "
            f"open issues about it in {slug} and never list it in follow-up issues. Instead reuse an open issue "
            f"from `gh issue list -R {dslug} --label {DEP_LABEL} --state open` that covers it (comment "
            f"`Also needed by {slug}#<pr>`), or create ONE: `gh label create {DEP_LABEL} -R {dslug} --color fbca04 "
            f"--description \"Needed by a dependent repo\" 2>/dev/null; gh issue create -R {dslug} --label "
            f"{DEP_LABEL} --title \"Needed by {slug}#<pr>: <what>\" --body ...` listing the exact paths/keys "
            f"needed and how to verify them. {what}")
    if mode == "implement" and dependents(proj):
        issues = [f"  - #{i['number']} {i['title']}" for i in dep_issues(slug)]
        if issues:
            rules.append(
                f"- These open {DEP_LABEL} issues block PRs in dependent repos "
                f"({', '.join(dependents(proj))}). Do them FIRST, before any roadmap step: exactly what each "
                f"issue asks, one PR (several issues may share one) with a `Closes #n` line per issue. Then "
                f"continue with roadmap steps.\n" + "\n".join(issues))
    red = main_fix_due(load_state(), proj) if mode == "implement" else None
    if red:
        hours = (time.time() - red["since"]) / 3600
        rules.append(
            f"- CI on {default} has been red for {hours:.0f} h (failing: {', '.join(red['names'])}), and open PRs "
            f"keep failing on it. Fix it FIRST, before any roadmap step: one branch from origin/{default} and one PR "
            f"titled \"Fix {default} CI: <cause>\" that makes those jobs pass. Then continue with roadmap steps. "
            f"Driver's summary of the failed {default} jobs:\n" + ci_failure_summary(proj, red["jobs"]))
    if not rules:
        return ""
    return "\nPROJECT OVERRIDES (take precedence over the task steps below)\n" + "\n".join(rules) + "\n"


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
    body = None
    if len(rows) >= CFG.get("digest_summary_min_events", 6):  # long digest: Haiku condenses it
        body = helper("digest", (AP / "prompts/digest.md").read_text(), "\n".join(lines))
    if not body:
        if len(lines) > 40:
            lines = lines[:15] + [f"... {len(lines) - 30} more ..."] + lines[-15:]
        body = "\n".join(lines)
    if send_telegram(f"digest ({len(rows)} events)\n" + body[:3800]):
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


# ---------- helper model (Haiku) ----------
HELPERS_F = AP / "helpers.jsonl"


def helper(task, prompt, data="", timeout=180):
    """One-shot, tool-less call on the cheap helper model; `data` goes in on stdin.

    Returns the answer text, or None on any failure (callers fall back to plain Python).
    """
    model = CFG.get("helper_model", "haiku")
    t0, text, cost = time.time(), None, None
    try:
        r = subprocess.run(["claude", "-p", prompt, "--model", model, "--output-format", "json",
                            "--tools", "", "--no-session-persistence"],
                           input=data, capture_output=True, text=True, timeout=timeout, cwd=AP)
        d = json.loads(r.stdout)
        cost = d.get("total_cost_usd")
        if not d.get("is_error"):
            text = (d.get("result") or "").strip() or None
    except Exception as e:
        log(f"helper {task} failed: {e}")
    with HELPERS_F.open("a") as f:
        f.write(json.dumps(dict(ts=int(time.time()), task=task, model=model, ok=text is not None,
                                cost=cost, duration_s=round(time.time() - t0))) + "\n")
    return text


def has_pending_step(proj):
    """Haiku pre-check before a Sonnet implement run. False only when the helper answers NONE."""
    repo, _, default = repo_info(proj)
    sh(["git", "fetch", "origin", "-q"], repo, check=False)
    r = subprocess.run(["git", "show", f"origin/{default}:{popt(proj, 'roadmap', 'ROADMAP.md')}"],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return True  # no roadmap: let the implement run decide
    ans = helper("precheck", (AP / "prompts/precheck.md").read_text(), r.stdout[:400_000])
    last = [l for l in (ans or "").splitlines() if l.strip()]
    verdict = last[-1].strip().strip("*`").upper() if last else ""
    log(f"precheck {proj}: {verdict[:80] or 'no answer'}")
    return not verdict.startswith("NONE")


# ---------- CI gate (driver-side merge) ----------
CI_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def ci_status(proj, num, sha, since):
    """('pass' | 'fail' | 'pending', failed check rows) for PR num's checks on head `sha`."""
    _, slug, _ = repo_info(proj)
    out = sh(["gh", "pr", "view", str(num), "-R", slug, "--json", "headRefOid,state,statusCheckRollup"],
             check=False)
    try:
        d = json.loads(out)
    except ValueError:
        return "pending", []
    if d.get("state") != "OPEN" or d.get("headRefOid") != sha:
        return "gone", []
    fork_ok = not popt(proj, "merge", True)  # upstream CI on a fork PR may await maintainer approval
    pending, failed = False, []
    for c in d.get("statusCheckRollup") or []:
        if c.get("__typename") == "StatusContext":
            st = c.get("state")
            if st in ("PENDING", "EXPECTED"):
                pending = True
            elif st != "SUCCESS":
                failed.append(dict(name=c.get("context"), url=c.get("targetUrl")))
            continue
        if c.get("status") != "COMPLETED":
            pending = True
        elif c.get("conclusion") == "ACTION_REQUIRED":
            pending = pending or not fork_ok
        elif c.get("conclusion") not in CI_OK:
            failed.append(dict(name=c.get("name"), url=c.get("detailsUrl"), conclusion=c.get("conclusion")))
    if failed:
        return "fail", failed
    if pending:
        return "pending", []
    if not d.get("statusCheckRollup"):  # nothing reported (yet): no workflows, or none triggered after a while
        n = sh(["gh", "api", f"repos/{slug}/actions/workflows", "--jq", ".total_count"], check=False)
        if n.strip() not in ("", "0") and time.time() - since < CFG.get("ci_settle_minutes", 5) * 60:
            return "pending", []
    return "pass", []


def job_failure_log(slug, job):
    """Annotations + the log lines leading up to the first ##[error] of one Actions job.

    (`gh run view --log-failed` returns nothing on older gh versions, and a plain log
    tail is mostly post-job cleanup.)
    """
    notes = sh(["gh", "api", f"repos/{slug}/check-runs/{job}/annotations", "--jq",
                '.[] | "\\(.path):\\(.start_line) \\(.annotation_level): \\(.message)"'], check=False)
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", l)
             for l in sh(["gh", "api", f"repos/{slug}/actions/jobs/{job}/logs"], check=False).splitlines()]
    errs = [i for i, l in enumerate(lines) if "##[error]" in l]
    window = lines[max(0, errs[0] - 150):errs[0] + 5] if errs else lines[-150:]
    extra = [lines[i] for i in errs[1:20]]
    return "\n".join(["annotations:", notes, "log:", *window, *extra])


def ci_failure_summary(proj, failed):
    """Failed job logs condensed by Haiku; falls back to the raw log tail."""
    _, slug, _ = repo_info(proj)
    chunks = []
    for c in failed[:4]:
        m = re.search(r"/actions/runs/\d+/job/(\d+)", c.get("url") or "")
        chunks.append(f"=== {c['name']} ({c.get('conclusion', 'failed')}) {c.get('url') or ''}\n"
                      + (job_failure_log(slug, m.group(1)) if m else ""))
    raw = "\n\n".join(chunks)[-60_000:]
    names = ", ".join(c["name"] or "?" for c in failed)
    summary = helper("cisummary", (AP / "prompts/cisummary.md").read_text(), raw) if raw.strip() else None
    return f"Failed checks: {names}\n" + (summary or "\n".join(raw.splitlines()[-60:]))


_MAIN_CI = {}  # proj -> (fetched_at, result): a burn loop calls this every few minutes


def main_ci(proj):
    """Failed jobs ([{name, url}], [] when green) of the latest completed push run of each workflow
    on the default branch; None when gh fails (callers keep what they knew)."""
    hit = _MAIN_CI.get(proj)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    _, slug, default = repo_info(proj)
    try:
        runs = json.loads(sh(["gh", "run", "list", "-R", slug, "--branch", default, "--event", "push",
                              "--status", "completed", "--limit", "30",
                              "--json", "databaseId,workflowName,conclusion"]))
        latest = {}
        for r in runs:  # newest first
            latest.setdefault(r["workflowName"], r)
        failed = []
        for r in latest.values():
            if r["conclusion"] in ("success", "skipped", "neutral", "cancelled"):
                continue
            jobs = json.loads(sh(["gh", "run", "view", str(r["databaseId"]), "-R", slug, "--json", "jobs"]))
            failed += [dict(name=j["name"], url=j.get("url")) for j in jobs.get("jobs", [])
                       if j.get("conclusion") in ("failure", "timed_out")]
    except (RuntimeError, ValueError, KeyError) as e:
        log(f"main CI check {proj} failed: {e}")
        return None
    _MAIN_CI[proj] = (time.time(), failed)
    return failed


def track_main_ci(st, proj):
    """Keep st["main_red"] = {since, jobs} while the default branch's CI is red."""
    failed = main_ci(proj)
    if failed is None:
        return False
    old = st.get("main_red")
    if not failed:
        if old:
            st.pop("main_red")
            log(f"{proj}: {repo_info(proj)[2]} CI green again")
        return bool(old)
    names = sorted({j["name"] for j in failed})
    if old and old["names"] == names:
        return False
    st["main_red"] = dict(since=old["since"] if old else time.time(), names=names, jobs=failed)
    log(f"{proj}: {repo_info(proj)[2]} CI red: {', '.join(names)}")
    return True


def main_fix_due(state, proj):
    """The main_red record once the default branch has been red for main_red_fix_hours, unless an
    open autopilot PR already fixes it; else None."""
    red = state.get(proj, {}).get("main_red")
    if not red or time.time() - red["since"] < CFG.get("main_red_fix_hours", 2) * 3600:
        return None
    default = repo_info(proj)[2]
    if any(p["title"].lower().startswith(f"fix {default} ci") for p in open_prs(proj, include_parked=True)):
        return None
    return red


def gate_row(proj, num, marker, note=""):
    """runs.jsonl row for a driver-side gate outcome (analyze.py counts merges from these)."""
    with RUNS_F.open("a") as f:
        f.write(json.dumps(dict(ts=int(time.time()), project=proj, mode="gate", marker=marker,
                                interrupted=None, pr=num, note=note[:200], model=None, duration_s=0,
                                cost=0, turns=0)) + "\n")


def process_ci_waits(projects):
    """Merge PRs whose clean review is now green on CI; send red/stuck ones back to review."""
    state = load_state()
    changed = False
    for proj in projects:
        st = state.setdefault(proj, {})
        changed |= track_main_ci(st, proj)
        main_bad = set((st.get("main_red") or {}).get("names", []))
        for key, w in list(st.get("ci_wait", {}).items()):
            res, failed = ci_status(proj, key, w["sha"], w["since"])
            _, slug, default = repo_info(proj)
            ctx = None
            if res == "fail" and {c["name"] for c in failed} <= main_bad:
                # main fails the same jobs: a review run would only fix main's breakage inside this PR.
                # Keep waiting; once main is green the PR goes back to review to merge it in.
                if not w.get("main_red"):
                    w["main_red"] = sorted({c["name"] for c in failed})
                    gate_row(proj, key, "WAITING", "main also red: " + ", ".join(w["main_red"]))
                    notify(f"{proj} PR #{key}: CI fails only where {default} also fails; waiting for {default}")
                    changed = True
                continue
            if res == "gone":  # closed, merged by hand, or new commits: nothing to gate
                log(f"ci gate {proj} #{key}: PR closed or head moved; dropping wait")
            elif res == "pending":
                if time.time() - w["since"] < CFG.get("ci_timeout_minutes", 30) * 60:
                    continue
                ctx = (f"CI did not finish within {CFG.get('ci_timeout_minutes', 30)} minutes on {w['sha'][:10]}. "
                       f"Check `gh pr view {key} -R {slug} --json statusCheckRollup`: fix a hanging test/workflow if "
                       f"it is in this repo; if CI cannot finish for reasons outside this repo, finish BLOCKED.")
                gate_row(proj, key, "CI_TIMEOUT")
                notify(f"{proj} PR #{key}: CI still pending after {CFG.get('ci_timeout_minutes', 30)}m; back to review")
            elif res == "fail":
                ctx = ("CI FAILED on the head commit " + w["sha"][:10] + " (summary of the failed job logs, made by "
                       "the driver):\n" + ci_failure_summary(proj, failed))
                if w.get("main_red"):
                    ctx = (f"While this PR waited, {default} itself failed CI ({', '.join(w['main_red'])}); {default} "
                           f"has changed since. First merge origin/{default} into the branch and push so CI re-runs; "
                           f"fix only what still fails after that.\n" + ctx)
                gate_row(proj, key, "CI_FAILED", ", ".join(c["name"] or "?" for c in failed))
                notify(f"{proj} PR #{key}: CI failed; back to review")
            elif not popt(proj, "merge", True):  # green, but upstream merges
                st.get("prs", {}).pop(key, None)
                park_pr(state, proj, int(key), by_sha=True)
                gate_row(proj, key, "READY")
                notify(f"{proj} PR #{key} ready for upstream review (not merged)")
            else:
                r = subprocess.run(["gh", "pr", "merge", key, "-R", slug, "--squash", "--delete-branch",
                                    "--match-head-commit", w["sha"]],
                                   cwd=PROJECTS_DIR, capture_output=True, text=True)
                if r.returncode == 0:
                    st.get("prs", {}).pop(key, None)
                    gate_row(proj, key, "MERGED")
                    notify(f"{proj} PR #{key} merged")
                else:
                    err = (r.stderr or r.stdout).strip()[:500]
                    ctx = (f"CI is green, but the driver's `gh pr merge --squash` failed: {err}\n"
                           f"Usually a conflict with {default}: merge origin/{default} into the branch, resolve, "
                           f"run the tests, push, and finish FIXED.")
                    gate_row(proj, key, "MERGE_FAILED", err)
                    notify(f"{proj} PR #{key}: merge failed ({err[:80]}); back to review")
            st["ci_wait"].pop(key)
            if ctx:
                st.setdefault("ci_context", {})[key] = dict(sha=w["sha"], text=ctx)
            changed = True
    if changed:
        save_state(state)


def ci_context_text(proj, num, sha):
    c = load_state().get(proj, {}).get("ci_context", {}).get(str(num))
    if c and c.get("sha") == sha:
        return c["text"]
    return "No CI result for this head commit yet (the driver checks CI after a CLEAN review)."


# ---------- run claude ----------
VALID = {"implement": {"PR_OPENED", "NO_PENDING_STEPS", "INCOMPLETE", "BLOCKED"},
         # MERGED/READY: legacy, from runs that still merged themselves; no longer offered
         "review": {"CLEAN", "MERGED", "READY", "FIXED", "WAITING", "BLOCKED"},
         "roadmap": {"PR_OPENED", "NO_NEW_ITEMS", "BLOCKED"},
         "selfreview": {"REVIEW_DONE", "BLOCKED"}}

# INCOMPLETE = "the step is not finished, keep this session and resume it". It is
# accepted from the model but never offered back to a resumed run (otherwise a
# resume could stall forever by re-declaring itself incomplete).
RESUME_MARKERS = {m: " | ".join(sorted(v - {"MERGED", "READY"})) for m, v in VALID.items()}


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
You have roughly {minutes} more minutes (deadline {deadline}; check with `date -u`). Finish with one status marker as the LAST line: {markers}."""


def model_for(mode):
    return CFG.get("model_by_mode", {}).get(mode, CFG["model"])


def run_claude(proj, mode, minutes, extra, resume=None):
    """Return (marker, interrupted_reason|None, session_id, meta)."""
    selfrev = mode == "selfreview"
    if selfrev:
        slug = default = None
    else:
        repo, slug, default = repo_info(proj)
    deadline = time.strftime("%H:%M UTC", time.gmtime(time.time() + minutes * 60))
    if resume:
        wt = AP if selfrev else AP / "worktrees" / proj  # keep uncommitted work: no reset
        sid = resume["session_id"]
        prompt = RESUME_PROMPT.format(reason=resume["reason"], minutes=int(minutes), deadline=deadline,
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
                       roadmap=popt(proj, "roadmap", "ROADMAP.md"),
                       overrides=overrides(proj, slug, default, mode),
                       existing_issues=existing_issues_rule(slug, extra["pr"]) if mode == "review" else "",
                       ci_context=ci_context_text(proj, extra["pr"], extra.get("sha")) if mode == "review" else "",
                       branch_prefix=f"autopilot/{time.strftime('%m%d')}-{sid[:6]}",
                       minutes=int(minutes), deadline=deadline, **extra)
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
    rewrite(HELPERS_F, keep_jsonl)
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
            gone = p["extra"]["pr"] not in [x["number"] for x in open_prs(proj)]
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
        if args.mode == "review" or (not args.mode and open_prs(proj)):
            work = _review_work(state, args, proj)
            if work:
                return work
    if not args.mode:  # pass A1: a starved project gets roadmap authoring ahead of others' implement work
        for proj in projects:
            if starved_for_roadmap(state, args, proj):
                log(f"{proj}: no pending steps and no roadmap run in {CFG.get('starved_roadmap_days', 3)}d; "
                    f"authoring roadmap ahead of other projects")
                return proj, "roadmap", {}
    for proj in projects:  # pass A: review open PR, else implement next step
        _, slug, default = repo_info(proj)
        # open dependency issues from a dependent repo, or a long-red default branch, override
        # impl_exhausted: PRs wait on them
        if args.mode in (None, "implement") \
                and (not exhausted(state, proj, "impl_exhausted", default)
                     or (dependents(proj) and dep_issues(slug)) or main_fix_due(state, proj)) \
                and (args.force or not exhausted(state, proj, "human_blocked", default)):
            return proj, "implement", {}
    if args.mode in (None, "roadmap"):  # pass B: author new roadmap items (Opus, see model_by_mode)
        for proj in projects:
            _, _, default = repo_info(proj)
            if popt(proj, "author_roadmap", True) \
                    and not exhausted(state, proj, "roadmap_exhausted", default) \
                    and (args.force or not exhausted(state, proj, "human_blocked", default)):
                return proj, "roadmap", {}
    return None


def starved_for_roadmap(state, args, proj):
    """Pass B only runs when no project has implement work, so a project out of steps never gets new
    ones while another always has some. True when its last precheck/run found no pending step and
    the roadmap is unchanged since (the impl_exhausted record may have expired: nothing re-checks
    it while other projects win pass A), and it had no roadmap run for starved_roadmap_days."""
    _, _, default = repo_info(proj)
    e = state.get(proj, {}).get("impl_exhausted")
    if not e or e.get("hash") != roadmap_hash(proj, default) or not popt(proj, "author_roadmap", True) \
            or exhausted(state, proj, "roadmap_exhausted", default) \
            or (not args.force and exhausted(state, proj, "human_blocked", default)):
        return False
    since = time.time() - CFG.get("starved_roadmap_days", 3) * 86400
    rows = [json.loads(l) for l in RUNS_F.read_text().splitlines() if l.strip()] if RUNS_F.exists() else []
    return not any(r.get("project") == proj and r.get("mode") == "roadmap" and r.get("ts", 0) > since
                   for r in rows)


def _review_work(state, args, proj):
    prs = open_prs(proj)
    if not prs:
        return None
    p = prs[0]
    ps = state.setdefault(proj, {}).setdefault("prs", {})
    open_nums = {str(x["number"]) for x in prs}
    for k in [k for k in ps if k not in open_nums]:  # stale counters of PRs no longer open
        ps.pop(k)
    it = ps.get(str(p["number"]), 0) + 1
    if it > CFG["max_review_iterations"]:
        park_pr(state, proj, p["number"])
        ps.pop(str(p["number"]), None)  # a SHA-parked PR un-parked by new commits starts over
        save_state(state)
        notify(f"{proj} PR #{p['number']} stuck after {it-1} review iterations", urgent=True)
        return None
    return proj, "review", dict(pr=p["number"], branch=p["headRefName"], sha=p["headRefOid"], iteration=it,
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
        floor = CFG.get("min_step_minutes", 30)
        if mode in ("implement", "roadmap") and minutes < floor and not args.force:
            # A short resume answers INCOMPLETE within seconds and burns max_resumes: wait for a real budget.
            log(f"only {minutes:.0f}m budget (< min_step_minutes {floor}); resume of {proj}/{mode} waits")
            return False
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
        if mode == "implement" and CFG.get("precheck", True) and not args.force \
                and popt(proj, "merge", True) and not (dependents(proj) and dep_issues(repo_info(proj)[1])) \
                and not main_fix_due(state, proj) and not has_pending_step(proj):
            # Haiku read the roadmap and found no doable step: skip the Sonnet run that would say NO_PENDING_STEPS.
            mark_exhausted(state, proj, "impl_exhausted", repo_info(proj)[2])
            notify(f"{proj}: no pending roadmap step (Haiku pre-check); implement skipped")
            _LAST_USAGE[0] = u_before
            return True
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
        if marker == "CLEAN":  # driver waits for CI on this head, then merges (process_ci_waits)
            sha = sh(["gh", "pr", "view", key, "-R", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
                     check=False)
            if sha and sha == extra.get("sha"):
                state[proj].setdefault("ci_wait", {})[key] = dict(sha=sha, since=time.time())
                state[proj].get("ci_context", {}).pop(key, None)
            else:  # the run pushed commits despite CLEAN: review the new head again
                log(f"{proj} PR #{key} CLEAN but head moved ({str(extra.get('sha'))[:8]} -> {sha[:8]}); re-review")
        elif marker == "MERGED":
            prs.pop(key, None)
            notify(f"{proj} PR #{extra['pr']} merged")
        elif marker == "READY":  # merge: false project: clean and green, left for upstream to merge
            prs.pop(key, None)
            park_pr(state, proj, extra["pr"], by_sha=True)
            notify(f"{proj} PR #{extra['pr']} ready for upstream review (not merged)")
        elif marker == "WAITING":  # needs work in depends_on's repo: parked until its issue closes
            if waits_on_dep(proj, extra["pr"]):  # no issue filed => keep counting toward the stuck cap
                prs.pop(key, None)
            sha = sh(["gh", "pr", "view", key, "-R", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
                     check=False)
            state[proj].setdefault("waiting_prs", {})[key] = sha
            notify(f"{proj} PR #{key} waiting on {popt(proj, 'depends_on')} ({DEP_LABEL} issue)")
        elif marker == "BLOCKED":
            park_pr(state, proj, extra["pr"])
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
    ap.add_argument("--burn", action="store_true",
                    help="no pacing: run back-to-back until max_u5/max_u7 (same as config \"burn\": true)")
    args = ap.parse_args()
    args.burn = args.burn or bool(CFG.get("burn"))

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
    # BURN never yields the tick: it loops until a limit, nothing-to-do or a failed run;
    # cron (flock) restarts it 10 min later.
    for n in (itertools.count() if args.burn else range(CFG["max_runs_per_tick"])):
        if code_sig() != START_SIG:  # a burn loop lives for hours: let cron start the new code
            log("autopilot.py/config.json changed since start; exiting so the next tick loads it")
            return
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
        elif args.burn:
            go, mode, reason, minutes = decide_burn(u)
        else:
            go, mode, reason, minutes = decide(u, k)
        if not args.simulate:
            append_usage(dict(u, go=go, mode=mode, reason=(reason or "")[:100]))
        if not (args.dry_run or args.simulate):  # merging spends no budget: gate CI on every tick
            try:
                process_ci_waits([args.project] if args.project else CFG["projects"])
            except Exception as e:
                log(f"ci gate failed: {e}")
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
