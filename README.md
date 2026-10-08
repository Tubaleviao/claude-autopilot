# claude-autopilot

Cron job that spends leftover Claude Code budget (5h window + weekly) on roadmap work.

- `autopilot.py` – tick: usage API → pacing policy → pick project → `claude -p` in a git worktree
- `config.example.json` – project priority, thresholds, model. Copy to `config.json` (gitignored) and edit; `projects_dir` accepts `~`
- `prompts/` – implement / review / roadmap prompts (+ common guardrails)
- Local (gitignored): `config.json`, `reports/`, `logs/`, `worktrees/`, `state.json`, `usage-log.jsonl`, `PAUSE`

## Policy
- **TAIL** (default): run only in last `tail_minutes` of the 5h window (window start stays free for you), if weekly usage isn't ahead of linear pace.
- **SATURATE**: if filling every remaining window barely reaches 100% weekly (learned `k`), run anytime, even opening a new window.
- **Intra-window pace line**: inside a live window the autopilot may only reach `100 * elapsed_share + u5_pace_margin`% (`u5_pace_margin`, default 15), so it drains leftover budget across the whole window instead of one burst — the rest stays free for you. SATURATE is exempt.
- Per project (priority order): open `autopilot/*` PR → review+fix until no Medium+ and CI green → squash-merge; else implement the next pending roadmap step **in full** (a step = a whole roadmap section/phase, all its deliverables and acceptance criteria — never a single checklist line, never a "(partial)" PR). While budget remains after a step's PR the run takes the next step (its own branch and PR); a step it cannot finish ends with `INCOMPLETE` and is resumed next run in the same worktree. All exhausted → author new roadmap items (deferred steps → open GitHub issues → brainstorm).
- **Trust boundary**: only PRs authored by the `gh` user from a same-repo branch are reviewed/merged (fork PRs named `autopilot/*` are ignored). Roadmap mode reads only issues by `@me`. Review treats a PR shipping part of a roadmap step as Medium+ and completes the step before merging.
- **Blocked (needs a human)**: when an implement/roadmap run ends `BLOCKED` — a real blocker such as an architecture decision, missing credentials, hardware or a manual acceptance criterion — the project is **parked** in `state.json` (`human_blocked`), its pending resume is dropped, and the tick moves on to the next project instead of retrying the same step forever. The park is released automatically when `ROADMAP.md` changes (different hash: a human edited the plan), or by hand with `--unblock <project>`. `--force` ignores the park. One alert per block, not one per tick.
- **Step floor**: no new implement/roadmap run starts with less than `min_step_minutes` (30) of budget — a whole step would not fit. Reviews and resumes still run. Branches are named by the driver: `autopilot/<MMDD>-<session6>-<slug>`.
- **Telegram**: stuck/BLOCKED/self-review notify at once; routine events (PR opened/merged, resumes) are batched into one digest every `digest_minutes` (60; 0 = send each immediately).
- **Streak rule**: if the last `streak_limit` (5) opened PRs were all in one project, that project is skipped for new work until another project has opened a PR (open PRs are still reviewed; falls back to it if nothing else has work). Open PRs across all projects are reviewed before new implement work.

## Usage
    python3 autopilot.py --dry-run
    python3 autopilot.py --simulate "u5=60 r5=20 u7=10 r7=96" --dry-run
    python3 autopilot.py --force --project my-project --mode implement
    python3 autopilot.py --unblock project-nihon   # release a BLOCKED park
    touch PAUSE   # disable;  rm PAUSE to resume

Needs `gh` authenticated. Cron line:

    */10 * * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin HOME=$HOME flock -n /path/to/claude-autopilot/lock python3 /path/to/claude-autopilot/autopilot.py >> /path/to/claude-autopilot/logs/tick.log 2>&1

## Pacing (ramp)
`fill = (100-u7) / windows_left / (k*100)` = share of each remaining 5h window that must be burned to still hit 100% weekly.
Allowed tail = `max(tail_minutes, fill*ramp_gain*300)`; `>=300` → SATURATE (any time, opens windows). A week left untouched ramps up from day 1 instead of waiting for the last hours. Within a tick the job runs back-to-back (up to `max_runs_per_tick`), re-reading usage after each run.

The tail only says *when* the autopilot may start; the intra-window pace line says *how much* it may hold. It stops spending once `u5` reaches `100 * elapsed_share + u5_pace_margin`, so a burst can no longer drain the window hours before it expires.

## Resume
Every run gets a known `--session-id`. If a run ends without its status marker (killed at time limit, usage limit hit, crash) — or with `INCOMPLETE` (the step is unfinished) — the session is stored in `state.json` (`pending`) and the next run resumes it (`claude --resume`) in the same worktree without resetting it, before picking new work. Dropped after `max_resumes` (3), `resume_max_hours` (48) or if its PR is gone. Markers are matched loosely (`STATUS: X`, `**X**`, `X (note)` all count), so a decorated marker is no longer mistaken for a missing one.

## Weekly cap (anti front-loading)
Autopilot never pushes weekly usage past `pace + pace_margin` (pace = linear share of the week elapsed, margin 4 pts), re-checked after every run. The tail only decides *when* it may start; this cap decides *how much* it may spend. With no window open it starts one only if clearly behind the line (`fill >= open_window_fill` and `u7 < pace`).

## Models
`model_by_mode` in `config.json`: roadmap authoring and the weekly self-review run on `opus` (alias → latest Opus); everything else uses `model` (sonnet).

## Telemetry + weekly self-review
- `runs.jsonl`: one row per run (mode, model, duration vs budget, cost, turns, marker, interruption, usage before/after). `usage-log.jsonl`: one row per tick incl. the decision and its reason.
- `python3 analyze.py [--days 7]` prints the summary (run efficiency, PR iterations per merge, window utilization/wasted headroom, week trajectory, skip reasons, learned k).
- Every `selfreview_days` (7), once ≥ `selfreview_min_runs` runs exist, the next GO tick runs an Opus self-review (`prompts/selfreview.md`) before other work. Force: `python3 autopilot.py --force --mode selfreview`.
- Guardrails (enforced by the driver after the run): it may change at most 3 whitelisted numeric config keys inside `BOUNDS` (see autopilot.py); edits to autopilot.py / analyze.py / prompts / any other config key are reverted. Prompt/code ideas go to `reports/<date>-selfreview.md` as proposals for you to approve. Backups: `reports/backup-*`.

## Pruning
After a *completed* weekly self-review (`REVIEW_DONE`) the job prunes: per-run `logs/*.log|.err` older than `log_keep_days` (14), `tick.log` lines older than that (trimmed in place, safe with cron's append handle), `usage-log.jsonl` / `runs.jsonl` rows and `reports/backup-*` / `stats-*` older than `data_keep_days` (90). Retention keys are not editable by the self-review. Manual: `python3 autopilot.py --prune [--dry-run]`.
